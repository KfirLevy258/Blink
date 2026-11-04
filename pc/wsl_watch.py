"""Claude Code inside a WSL distribution, watched from Windows.

The shims inside the distribution write into a Windows folder the daemon
owns, %USERPROFILE%\\.blink\\wsl-<guid> (claude_home.wsl's drop_dir), so the
2 s poll reads NTFS and never starts or enters the distribution
(docs/plans/enterprise-spend.md, section 4).

Claude Code's config, the transcripts and the drift check are inside the
distribution, reached through \\\\?\\UNC\\wsl.localhost\\<name>. WslWatch reads
them on a thread of its own, and only while the distribution is in use:
the drop folder written in the last LIVE_S and the distribution running.
Two reasons for the gate, both on a real desk. Opening a path under
\\\\wsl.localhost starts a distribution that is not running, so a reader that
did not ask first would undo every `wsl --shutdown` and keep WSL holding its
memory all day. And the serial loop must never wait on any of it: a stat
over 9P, or the wsl.exe that asks whether the distribution is running
(wsl.running, 5 s timeout), held there, keeps the board's pongs past their
30 s window and puts HOST LOST on the desk.

What the shims write -- the status line payload, the hook state -- is on
NTFS in the drop folder, and the providers read it there directly, with no
gate (statusline_source.payload_path, claude_state).

Nothing here runs at import (tests/test_import_is_inert.py), and nothing
here logs a path or a file's contents: a failed tick is said by the
exception's type alone, once per run of failures.
"""
import fnmatch
import os
import stat
import sys
import threading
import time

from pc import burn_reader, claude_plan, install_statusline, wsl

# The drop folder must have changed this recently for the distribution to
# count as in use: the spec's ten minutes. A status line renders on every
# message and a hook fires on every prompt, so ten quiet minutes is a user
# who has walked away, and their account's figures do not move meanwhile.
LIVE_S = 600

# The least time between two `wsl -l --running` asks. Each is a wsl.exe
# start; the UNC reads ride on the tick that has just asked (see tick()),
# so this is also how often the config and the transcripts are read.
RUNNING_EVERY_S = 15

# How long a burn_source().poll() keeps the transcripts being read. The
# bridge asks a marked page every 15 s while Claude Code is producing and
# every minute when it is not (bridge.BURN_*), so three minutes outlasts the
# slow cadence twice; an account with a five-hour window, whose page is
# never marked, has its transcripts never read at all, as natively
# (IngestionBus.burn).
WANT_TTL_S = 180

# How often the thread wakes. The work is gated by the constants above; a
# tick that is not due does one stat and one listing of the drop folder.
TICK_S = 5

# How often the shims' abandoned temps are swept from the drop folder. On
# NTFS, so not gated: a killed render's temp holds a payload either way.
SWEEP_EVERY_S = 300

# How old a shim's temp file must be before it is taken for abandoned. The
# shims write one and rename it in the same breath -- a printf and an mv --
# so a minute is not a write still in flight; it is one killed between the
# two, by Claude Code timing out a status line render or a terminal closing
# on a hook. Shorter than install_statusline.STALE_TEMP_S because those are
# whole settings.json writes; these are renders, many a minute.
SHIM_TEMP_STALE_S = 60

# What the shims name their temps: statusline.json.$$.tmp, and
# $sid.state.$$.tmp under state/ (tools/blink-statusline.sh,
# tools/blink-hook.sh). install_statusline._sweep_stale_temps matches
# <file>.<hex>.blink-tmp beside one settings file, and none of these.
_SHIM_TEMP = "*.tmp"


def _sweep_shim_temps(drop_dir, now):
    """Remove the shims' temp files older than SHIM_TEMP_STALE_S in
    `drop_dir` and its state/ folder.

    Natively ~/.blink is on the machine's own disk, and a leftover there
    costs its size. The drop folder is reached from Linux through the
    automount, where a killed render leaves the temp on NTFS, and nothing
    else on Windows will ever look for it; each is a copy of a status line
    payload, with the session's working directory in it. Never raises: a
    file another render is renaming this instant is that render's, and a
    folder that is not there yet has nothing in it.
    """
    for folder in (drop_dir, os.path.join(drop_dir, "state")):
        try:
            entries = list(os.scandir(folder))
        except OSError:
            continue
        for e in entries:
            if not fnmatch.fnmatchcase(e.name, _SHIM_TEMP):
                continue
            try:
                if (e.is_file(follow_symlinks=False)
                        and now - e.stat(follow_symlinks=False).st_mtime
                        > SHIM_TEMP_STALE_S):
                    os.remove(e.path)
            except OSError:
                pass


def _drop_newest(home):
    """The newest mtime among what the shims write -- statusline.json and
    the files of state/ -- or None when there is none.

    Not the folders' own mtimes. The drop folder holds the install's
    markers too, which the drift repair rewrites, so its mtime would keep a
    desk "in use" on the strength of our own write; and the daemon sweeps
    abandoned slots out of state/, which moves that folder's. All on NTFS:
    one stat and one listing, which costs the distribution nothing."""
    newest = None
    try:
        newest = os.stat(os.path.join(home.drop_dir,
                                      "statusline.json")).st_mtime
    except OSError:
        pass
    try:
        with os.scandir(home.state_dir) as it:
            for e in it:
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISREG(st.st_mode) and (newest is None
                                                 or st.st_mtime > newest):
                    newest = st.st_mtime
    except OSError:
        pass
    return newest


class WslWatch:
    """A WSL home's config view and burn buckets, for the serial loop to
    read from memory.

    Modelled on status_page.StatusWatch. Two threads touch it: the serial
    loop calls view() and burn_source().poll(), which only read memory
    under the lock; tick(), on start()'s thread, does every read and holds
    the lock only to swap results in. Nothing slow -- wsl.exe, a stat over
    9P, a print -- ever runs under it.

    It keeps a claude_plan.ConfigView, never the parsed document: the
    config holds every project's history, and parse() drops it before
    returning. `running` is wsl.running (name -> bool), `clock` wall time.
    `wsl_run`, `drift` and `burn_reader` are for the tests: the drift
    watchdog is install_statusline.wsl_drift_watchdog(home, wsl_run), the
    reader a BurnReader of home.transcripts.
    """

    def __init__(self, home, running=wsl.running, clock=time.time,
                 wsl_run=None, drift=None, burn_reader=None):
        self._home = home
        self._running = running
        self._clock = clock
        self._drift = (drift if drift is not None
                       else install_statusline.wsl_drift_watchdog(home,
                                                                  wsl_run))
        self._reader = (burn_reader if burn_reader is not None
                        else _burn_reader_for(home))
        self._lock = threading.Lock()
        # Read by the serial loop, under the lock.
        self._view = None
        self._burn = None
        self._live = False
        # Written by the serial loop (burn_source), under the lock.
        self._wanted_at = None
        # The tick's own; only its thread touches these.
        self._ran_at = None
        self._ran = False
        self._sig = None
        self._swept_at = None
        self._said = None

    # -- the serial loop's side: memory only -------------------------------

    def view(self):
        """The last ConfigView read, or None before the first one. Kept
        while the distribution is stopped: the account does not change
        while it is not in use."""
        with self._lock:
            return self._view

    def live(self):
        """Whether the last tick found the distribution in use."""
        with self._lock:
            return self._live

    def burn_snapshot(self):
        """(q, tokens) as of the last tick, or None. Does not count as an
        ask (burn_source)."""
        with self._lock:
            return _copy(self._burn)

    def burn_source(self):
        """The claude page's burn reader for the bus: poll() answers from
        burn_snapshot() and asks for the transcripts to be read."""
        return _BurnSource(self)

    def _asked(self):
        with self._lock:
            self._wanted_at = self._clock()
            return _copy(self._burn)

    # -- the thread's side -------------------------------------------------

    def tick(self):
        """One round: sweep the drop folder when due, decide whether the
        distribution is in use, and read it if so. Blocks for all of it;
        see start()."""
        now = self._clock()
        if self._swept_at is None or not (
                0 <= now - self._swept_at < SWEEP_EVERY_S):
            self._swept_at = now
            _sweep_shim_temps(self._home.drop_dir, now)
        newest = _drop_newest(self._home)
        fresh = newest is not None and now - newest <= LIVE_S
        asked_now = False
        if not fresh:
            # Forgotten, so a desk coming back is asked at once.
            self._ran, self._ran_at = False, None
        elif (self._ran_at is None or not
                (0 <= now - self._ran_at < RUNNING_EVERY_S)):
            # Stamped before the ask, so a check that raises is not
            # retried on every tick.
            self._ran_at = now
            self._ran = False
            self._ran = bool(self._running(self._home.distro_name))
            asked_now = True
        live = fresh and self._ran
        with self._lock:
            self._live = live
            wanted_at = self._wanted_at
        wanted = wanted_at is not None and 0 <= now - wanted_at <= WANT_TTL_S
        view, notes = None, []
        # Into the distribution only on the tick that has just been told it
        # is running. A True up to RUNNING_EVERY_S old is not enough: a
        # `wsl --shutdown` inside that window, then a UNC read, would start
        # the distribution again behind the user's back.
        if live and asked_now:
            view = self._look()
            if wanted:
                got = self._reader.poll(now)
            else:
                got = self._reader.held(now)
            msg = self._drift.tick()
            if msg:
                notes.append(f"[watchdog] {msg}")
        else:
            got = self._reader.held(now)
        with self._lock:
            if view is not None:
                self._view = view
            self._burn = got
        for line in notes:
            print(line, file=sys.stderr)

    def _look(self):
        """A new ConfigView if the config changed, else None. Resolved
        here, by claude_plan.config_path's rule, on every look: which file
        wins is never stored (claude_plan.config_path)."""
        try:
            path = claude_plan.config_path(self._home)
            st = os.stat(path)
        except FileNotFoundError:
            self._sig = None
            return claude_plan.EMPTY_VIEW
        except (OSError, ValueError):
            return None                 # not knowing is not "no seat"
        sig = (path, stat.S_IFMT(st.st_mode)) + claude_plan.signature(st)
        if sig == self._sig:
            return None
        got = claude_plan.parse(path, st)
        # As PlanReader.look: a read worth trying again is not cached
        # against the stamp, and the last good view stands meanwhile.
        self._sig = None if got is None else sig
        return got

    def _tick_once(self):
        try:
            self.tick()
            self._said = None
        except Exception as e:
            # By type alone: an OSError's text names the UNC path, which
            # is a Linux user name and a folder inside their distribution.
            if self._said != type(e).__name__:
                self._said = type(e).__name__
                print(f"[wsl] check failed: {self._said}", file=sys.stderr)

    def start(self):
        """Run tick() every TICK_S on a daemon thread, for the process's
        life."""
        t = threading.Thread(target=self._run, name="blink-wsl",
                             daemon=True)
        t.start()
        return t

    def _run(self):
        while True:
            self._tick_once()
            time.sleep(TICK_S)


class _BurnSource:
    """WslWatch's face for IngestionBus(burn={"claude": ...}). Not a
    BurnReader, so the bus asks poll(now) without a budget: the reading is
    the watch's thread's, and costs the serial loop's budget nothing. No
    prune(): the watch prunes on every tick, cable or none."""

    def __init__(self, watch):
        self._watch = watch

    def poll(self, now, budget=None):
        return self._watch._asked()


def _burn_reader_for(home):
    return burn_reader.BurnReader(root=home.transcripts)


def _copy(got):
    if got is None:
        return None
    q, tokens = got
    return q, list(tokens)
