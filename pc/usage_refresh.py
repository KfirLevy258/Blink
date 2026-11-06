"""Run the person's own `claude -p /usage`, judge the answer, remember it.

Measured 2026-11-04 on a logged-in Claude Code: `claude -p /usage` is a
local slash command. The JSON it prints has num_turns 0, total_cost_usd 0
and an empty modelUsage, and it rewrites `cachedUsageUtilization` in
~/.claude.json, which is what claude_plan reads. Nothing reaches the model,
so nothing is billed against the owner's quota. A logged-out Claude Code also
answers with success (2026-11-05), so a run is only trusted when the cache
moves; that check belongs to the daemon thread, not here.

Why each flag is there:
  --no-session-persistence   no transcript is written for the run.
  --strict-mcp-config + an empty --mcp-config   no MCP server of the
                             person's is started for a command that needs none.
  --settings {"disableAllHooks":true}   none of their hooks fire either.
  never --bare               it drops the subscription login and the
                             command then has nothing to ask.
The run happens in <blink_home>/refresh so the folder-trust and project
files of wherever the daemon started do not matter, and BLINK_REFRESH=1
lets the person's own hooks and statusline tell the run from their work.

The guard is the judge: any sign the model was used (a turn, a cent, a
model in modelUsage) turns the feature off for good, whatever the exit code.

Nothing above RefreshWatch logs. The answer holds the person's figures and
must never reach a log line; RefreshWatch logs the event name only, in the
exact words below. Importing does nothing.
"""

import dataclasses
import enum
import glob
import json
import math
import os
import stat
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields

from . import (claude_home, claude_plan, protocol, theme, user_config, wsl)
from . import idle as idle_probe

SETTING = "usage-refresh"

FRESH_SOURCE_S = 15 * 60
REFRESH_AFTER_S = 25 * 60
ASK_EVERY_S = 24 * 3600
ASK_AT_MOST = 3
IDLE_LIMIT_S = 30 * 60
RUN_TIMEOUT_S = 60
BACKOFF_AFTER = 3
BACKOFF_S = 3600
GIVE_UP_S = 24 * 3600
# And at least this many failed runs in the streak: a day of wall time alone
# was two failures -- one on Friday evening, none over a weekend with nobody
# at the desk, one on Monday morning before the network was up -- and the
# refresh turned itself off with "/login" advice that fitted neither
# (/code-review max of feat/usage-refresh, V12). Six runs span at least
# 3 h 50 m: three at the REFRESH_AFTER_S spacing, then the hourly backoff.
GIVE_UP_AFTER = 6
TICK_S = 60

STATE_FILE = "usage-refresh.json"


class Verdict(enum.Enum):
    OK = "ok"
    COST = "cost"
    FAILED = "failed"
    TIMEOUT = "timeout"


# Where the other installers put `claude`, tried after PATH. The daemon runs
# under launchd (/usr/bin:/bin:/usr/sbin:/sbin), systemd or Task Scheduler,
# whose PATH holds none of them, so `blink install` found Homebrew's or
# npm's claude in a terminal, asked, got a yes, and the daemon then never
# found it: "usage refresh skipped" once, and `blink status` said "on"
# forever (final review of feat/usage-refresh, Important 2). A module
# constant with no home in it, so tests can point it at an empty folder.
SYSTEM_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin")


def _version_key(path):
    """Sort key of an nvm folder name, v20.11.1 -> (20, 11, 1); a name that
    is not a version sorts first."""
    name = os.path.basename(path).lstrip("v")
    try:
        return tuple(int(x) for x in name.split("."))
    except ValueError:
        return (-1,)


def _on_path(name):
    """`name` in an absolute PATH folder that is not the current one, or
    None. shutil.which is not used: on Windows it looks in the current
    folder before PATH, so `blink config usage-refresh on` run inside a
    project holding a claude.cmd recorded that file, and the daemon then
    ran it every 25 minutes with the person's environment -- a repo's
    wrapper, or a hostile clone's (/code-review max of feat/usage-refresh,
    V18). A relative entry ("." or "bin") names the current folder by
    another route, so only absolute ones are searched, and the current
    folder is skipped even when PATH lists it by its full name. Windows
    tries PATHEXT's extensions, as CreateProcess's callers do."""
    try:
        here = os.path.normcase(os.path.realpath(os.getcwd()))
    except OSError:
        here = None
    win = sys.platform == "win32"
    if win:
        exts = [e for e in os.environ.get(
            "PATHEXT", ".COM;.EXE;.BAT;.CMD").split(";") if e]
        names = [name + e for e in exts]
    else:
        names = [name]
    for d in os.environ.get("PATH", "").split(os.pathsep):
        d = d.strip('"') if win else d
        if not d or not os.path.isabs(d):
            continue
        try:
            if here is not None and os.path.normcase(
                    os.path.realpath(d)) == here:
                continue
        except (OSError, ValueError):
            continue
        for n in names:
            p = os.path.join(d, n)
            if os.path.isfile(p) and (win or os.access(p, os.X_OK)):
                return p
    return None


def find_claude(home):
    """The claude executable, or None: PATH first (_on_path: never the
    current folder), then where the official installer (~/.local/bin), the
    older local install (~/.claude/local), Homebrew (/opt/homebrew/bin,
    /usr/local/bin), npm with a user prefix (~/.npm-global/bin) and the
    newest nvm Node put it. Desktop's private copy is never used. `home` is
    the user's home, passed in, never looked up here
    (tests/test_sandbox.py)."""
    found = _on_path("claude")
    if found:
        return found
    win = sys.platform == "win32"
    name = "claude.exe" if win else "claude"
    dirs = [os.path.join(home, ".local", "bin"),
            os.path.join(home, ".claude", "local")]
    if not win:
        dirs += list(SYSTEM_BIN_DIRS)
        dirs.append(os.path.join(home, ".npm-global", "bin"))
        nvm = glob.glob(os.path.join(home, ".nvm", "versions", "node", "*"))
        dirs += [os.path.join(d, "bin")
                 for d in sorted(nvm, key=_version_key, reverse=True)[:1]]
    for d in dirs:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def recorded_claude(blink_home):
    """The path the CLI last found claude at (remember_claude), or None."""
    exe = _read(blink_home).get("exe")
    return exe if isinstance(exe, str) and os.path.isabs(exe) else None


def remember_claude(blink_home, exe):
    """Keep `exe`, found by the CLI with the person's own PATH, for the
    daemon, whose PATH is the service manager's. The path as found, not its
    target: ~/.local/bin/claude is a link to a versioned file that each
    Claude Code update replaces. Never logged. Raises what the write
    raises; nothing is written when it is already the one kept."""
    exe = os.path.abspath(exe)
    doc = _read(blink_home)
    if doc.get("exe") == exe:
        return
    doc["exe"] = exe
    theme._write_json(_path(blink_home), doc, tries=1)


def locate_claude(blink_home, home):
    """What the daemon runs: the recorded path while it is still there,
    else find_claude. A Claude Code that moved is found by the fallbacks
    until the next `blink install`, `update` or `config usage-refresh on`
    records it again."""
    exe = recorded_claude(blink_home)
    if exe and os.path.isfile(exe):
        return exe
    return find_claude(home)


# The first Claude Code whose `claude -p /usage` answers with no turn and no
# cost. Measured logged out in the throwaway Ubuntu VM on 2026-11-06, by
# bisecting the published releases: 2.1.100 says "Unknown skill: usage",
# 2.1.113 "/usage isn't available in this environment", and 2.1.119 shows
# the figures but counts two turns. All at zero cost and zero tokens, and
# each read by judge() as COST, which turned the refresh off "for using the
# model" on a desk that had spent nothing. judge() stays strict; the refresh
# is simply not run, asked or offered below this version. 2.1.120, .122,
# .126, .150, .200, .250, .300 and .318 all answered with 0 turns, $0.
USAGE_SINCE = (2, 1, 120)
VERSION_TIMEOUT_S = 30

# exe -> ((mtime_ns, size), version or None). An update replaces the file,
# so a desk that updates Claude Code is let in at the next look.
_versions = {}


def parse_version(text):
    """(major, minor, patch) from `claude --version` output such as
    "2.1.318 (Claude Code)", or None when it is not a version. Every piece
    must be a number: mapping an odd one to 0 once reported a good install
    as too old (see cli.claude_version)."""
    head = text.split()[0] if text and text.split() else ""
    pieces = head.split(".")[:3]
    if not pieces or not all(x.isdigit() for x in pieces):
        return None
    parts = [int(x) for x in pieces]
    while len(parts) < 3:
        parts.append(0)
    return tuple(parts)


def version_of(exe, runner=wsl._bounded_run):
    """The version of the Claude Code at `exe`, or None when it cannot be
    told. Asked once per file: `--version` is a process, and the daemon may
    look every minute. Never raises."""
    try:
        st = os.stat(exe)
        key = (st.st_mtime_ns, st.st_size)
    except OSError:
        return None
    hit = _versions.get(exe)
    if hit is not None and hit[0] == key:
        return hit[1]
    extra = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
    try:
        r = runner([exe, "--version"], timeout=VERSION_TIMEOUT_S,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, **extra)
        v = parse_version((r.stdout or b"").decode(errors="replace"))
    except Exception:
        v = None
    _versions[exe] = (key, v)
    return v


def version_too_old(v):
    """For a version already in hand (cli.claude_version's tuple). One that
    could not be told is not "too old": judge() still guards that run."""
    return v is not None and tuple(v) < USAGE_SINCE


def too_old(exe, runner=wsl._bounded_run):
    """True when `exe` is a Claude Code that predates USAGE_SINCE."""
    return version_too_old(version_of(exe, runner))


def command(exe):
    return [exe, "-p", "/usage", "--output-format", "json",
            "--no-session-persistence", "--strict-mcp-config",
            "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}']


def _positive(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0


def judge(returncode, stdout):
    try:
        doc = json.loads(stdout)
        if not isinstance(doc, dict):
            return Verdict.FAILED
        if (_positive(doc.get("num_turns"))
                or _positive(doc.get("total_cost_usd"))
                or doc.get("modelUsage")):
            return Verdict.COST
        if (returncode == 0 and doc.get("type") == "result"
                and doc.get("is_error") is False
                and doc.get("num_turns") == 0
                and doc.get("total_cost_usd") == 0):
            return Verdict.OK
    except Exception:
        pass
    return Verdict.FAILED


def run_once(exe, blink_home, runner=wsl._bounded_run, home=None):
    """One run. `home` is the claude_home record BLINK reads the cache
    from: when CLAUDE_CONFIG_DIR chose its folder, the run is given the
    same folder, and when it did not, the run is not given one either. The
    daemon runs under launchd or systemd without the person's shell rc, so
    someone who exports CLAUDE_CONFIG_DIR there had `/usage` run against
    the default folder (no login, or another one), the recorded folder's
    cache never moved, and a day later the refresh turned itself off with
    the wrong advice (final review, Important 5)."""
    try:
        cwd = os.path.join(blink_home, "refresh")
        os.makedirs(cwd, mode=0o700, exist_ok=True)
        # Read at call time so a test can pose as Windows.
        extra = {"creationflags": 0x08000000} if sys.platform == "win32" else {}
        env = dict(os.environ, BLINK_REFRESH="1")
        if home is not None:
            if home.config_dir_set:
                env["CLAUDE_CONFIG_DIR"] = home.config_dir
            else:
                env.pop("CLAUDE_CONFIG_DIR", None)
        r = runner(command(exe), timeout=RUN_TIMEOUT_S, cwd=cwd, env=env,
                   stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                   stderr=subprocess.DEVNULL, **extra)
    except subprocess.TimeoutExpired:
        return Verdict.TIMEOUT
    except OSError:
        return Verdict.FAILED
    return judge(r.returncode, r.stdout or b"")


@dataclass
class State:
    last_run_at: float = 0.0
    fails: int = 0
    failing_since: "float | None" = None
    off_reason: str = ""
    # When `blink config usage-refresh on` last cleared the above
    # (clear_off): a watch that sees a newer one than it holds takes the
    # file's fresh start over its own memory (RefreshWatch._floor).
    cleared_at: float = 0.0
    # The daemon looked for claude and found it nowhere; `blink status`
    # says so (final review, Important 2).
    missing: bool = False
    # The last good run made after a real idle reading: when it started,
    # and the fetch time of the cache it made (None when the cache could
    # not be read after it). Kept here, not only in the watch's memory, so
    # a daemon restarted after the run still dates that cache as activity
    # (RefreshWatch.activity_for; item 8 of the review of 277b81a..4003b0b,
    # where a restart two minutes after a run let the board doze with the
    # person at it).
    proven_start: "float | None" = None
    proven_fetched: "float | None" = None


def _path(blink_home):
    return os.path.join(blink_home, STATE_FILE)


def _read(blink_home):
    """The state file as a dict, {} for anything else. RecursionError is
    json's answer to an array nested thousands deep, as in
    session_verdict.load: uncaught, one corrupt file made `blink status`
    and `blink config usage-refresh on` die with a traceback and every
    daemon tick fail (/code-review max of feat/usage-refresh, V15)."""
    try:
        with open(_path(blink_home), encoding="utf-8") as f:
            doc = json.load(f)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError, RecursionError):
        return {}


def _finite(v):
    """A finite int or float that is not a bool. json reads NaN and
    Infinity: a NaN last_run_at made due() False for good, and an infinite
    one crashed `blink status` in int()."""
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def load_state(blink_home):
    run = _read(blink_home).get("run")
    if not isinstance(run, dict):
        return State()
    try:
        kw = {f.name: run[f.name] for f in fields(State) if f.name in run}
        s = State(**kw)
        if (not isinstance(s.fails, int) or isinstance(s.fails, bool)
                or not _finite(s.last_run_at)
                or not isinstance(s.off_reason, str)
                or not _finite(s.cleared_at)
                or not isinstance(s.missing, bool)
                or not all(v is None or _finite(v) for v in (
                    s.failing_since, s.proven_start, s.proven_fetched))):
            return State()
        return s
    except (TypeError, ValueError):
        return State()


def save_state(blink_home, state):
    doc = _read(blink_home)
    doc["run"] = asdict(state)
    theme._write_json(_path(blink_home), doc, tries=1)


def record(state, verdict, now):
    s = dataclasses.replace(state, last_run_at=now, missing=False)
    if verdict is Verdict.OK:
        s.fails = 0
        s.failing_since = None
    elif verdict is Verdict.COST:
        s.off_reason = "cost"
    else:
        s.fails += 1
        if s.failing_since is None:
            s.failing_since = now
        if (now - s.failing_since >= GIVE_UP_S
                and s.fails >= GIVE_UP_AFTER):
            s.off_reason = "failing"
    return s


def inconclusive(state, now):
    """A run that exited well but whose cache could not be read afterwards
    (a torn read while Claude Code rewrote the file, a lock): neither OK
    nor a failure. Only the time is kept, so the next try is a spacing
    away and the streak stands as it was."""
    return dataclasses.replace(state, last_run_at=now, missing=False)


def spacing(state):
    return BACKOFF_S if state.fails >= BACKOFF_AFTER else REFRESH_AFTER_S


# How far before the run's start a cache's fetch time may sit and still be
# the run's own: the cache's clock is Claude Code's and its fetch can stamp
# a moment before ours did, but not a minute before.
MOVED_SLACK_S = 5


def _seen_at(now, other_at):
    """`other_at` as a time it can be: None for one dated past now (by more
    than MOVED_SLACK_S), which a clock since put back stamped. A render five
    hours ahead counted as activity now for five hours, somebody at a
    Linux desk all night and a fresh source holding every refresh off (the
    /code-review max of 277b81a..4003b0b, verify/v6/repro_c27.py)."""
    if other_at is None or other_at > now + MOVED_SLACK_S:
        return None
    return min(other_at, now)


def at_computer(now, idle_s, other_at):
    """Whether somebody is at the computer, as far as it can be known.

    An idle reading decides when the OS gives one (macOS, Windows): input
    inside IDLE_LIMIT_S. When it cannot say (`idle_s` None: Linux, where
    no stdlib source exists), the answer is yes only if another Claude
    source -- the status line, Claude Desktop, any frame but BLINK's own
    /usage -- showed activity inside the same IDLE_LIMIT_S. Until
    2026-11-05 None counted as in use, and a Linux desk left on refreshed
    all night, with nobody there to read it and a Claude Code login
    spending its calls on nothing."""
    if idle_s is not None:
        return idle_s < IDLE_LIMIT_S
    other_at = _seen_at(now, other_at)
    return other_at is not None and now - other_at < IDLE_LIMIT_S


def due(now, *, enabled, native, board_alive, idle_s, cache_at, other_at,
        state):
    """Whether to ask /usage now: the fallback rule, and nothing else.

    BLINK's own sources first: any other Claude reading younger than
    FRESH_SOURCE_S (the status line in a terminal, Claude Desktop's Code
    tab, chat or Cowork) means no run. No exception for a spend seat: while
    Claude Code is used, the running count already adds what each session
    costs. `idle_s` None is an OS that cannot say (Linux): then only another
    source's activity inside IDLE_LIMIT_S says someone is here (at_computer).
    `cache_at` None is no cache of this login's yet, which is due."""
    other_at = _seen_at(now, other_at)
    return bool(
        enabled and native and board_alive
        and state.off_reason == ""
        and at_computer(now, idle_s, other_at)
        and (other_at is None or now - other_at >= FRESH_SOURCE_S)
        and (cache_at is None or now - cache_at >= REFRESH_AFTER_S)
        and now - state.last_run_at >= spacing(state))


# --- asking on the board -----------------------------------------------------
#
# Someone updated automatically never sees `blink install`, where the
# question is asked in the terminal, and the refresh never runs without a
# yes. So a board that lists the "ask" cap is sent {"t":"ask","id":"refresh"}
# and puts up "Keep figures fresh?" with Turn on / No thanks
# (firmware/src/ask_prompt.h). An ask counts toward ASK_AT_MOST only when
# the person saw it: on an answer, "later" (the box closed itself after a
# minute) included. A box the board withdrew (host lost, a doze) or an ask
# it dropped because it could not show it within ten minutes answers
# nothing and counts nothing.
#
# Only an answer starts the day before the next ask. An ask with no answer
# was not seen, and the next may go out ASK_RETRY_S after it, still only
# while someone is at the computer. The board answers within eleven minutes
# or not at all (it holds an ask ten minutes for the glass,
# ASK_PROMPT_PENDING_MS, and shows it one, ASK_PROMPT_OPEN_MS), so an hour
# never sends a second ask over one still on the glass. Until
# the /code-review max of feat/usage-refresh (V21) a send started the day
# too: a board dozing for want of Claude activity drops the ask after ten
# minutes, and the daemon then waited a day, every day, so the people the
# board ask is for -- in Desktop or a browser, no Claude Code terminal --
# were never asked at all.
# Counted on send, as first built, all three could be spent on a dozing
# board for exactly the person the board ask is for -- someone in Desktop
# with no Claude Code terminal, whose board dozes for want of hook activity
# -- who would never have seen the box (final review, Important 4; owner's
# ruling: "at most 3 times" means shown 3 times).

ANSWERS = protocol.ASK_ANSWERS

ASK_RETRY_S = 3600


@dataclass
class AskState:
    times: int = 0
    # When the board last answered: the day before the next ask runs from
    # here.
    last_at: float = 0.0
    declined: bool = False
    # When the last ask went out, answered or not.
    sent_at: float = 0.0


def load_ask(blink_home):
    """The "ask" half of STATE_FILE; a fresh AskState for anything that is
    not one, as load_state does for "run"."""
    ask = _read(blink_home).get("ask")
    if not isinstance(ask, dict):
        return AskState()
    try:
        kw = {f.name: ask[f.name] for f in fields(AskState) if f.name in ask}
        a = AskState(**kw)
        if (not isinstance(a.times, int) or isinstance(a.times, bool)
                or a.times < 0
                or not _finite(a.last_at) or not _finite(a.sent_at)
                or not isinstance(a.declined, bool)):
            return AskState()
        return a
    except (TypeError, ValueError):
        return AskState()


def save_ask(blink_home, ask):
    """Replace "ask" and nothing else: "run" is RefreshWatch's, written from
    its own thread, and save_state likewise keeps this half."""
    doc = _read(blink_home)
    doc["ask"] = asdict(ask)
    theme._write_json(_path(blink_home), doc, tries=1)


def should_ask(now, *, setting_absent, native, claude_found, work_login,
               board_caps, idle_s, ask_state, other_at=None):
    """Whether the board may ask "Keep figures fresh?" now.

    Only while the person has not chosen (the key absent from config.json),
    for a native Claude home (never WSL in this version), with Claude Code
    there to run, never on a work login (asked only in the terminal, with No
    as the default), only on a board that lists "ask", only while someone is
    at the computer (at_computer: `idle_s` None is an OS that cannot say,
    and then only another Claude source's activity inside IDLE_LIMIT_S
    counts), at most ASK_AT_MOST answered times, a day after the last answer,
    an hour after an ask that went unanswered, and never after a no."""
    return bool(
        setting_absent and native and claude_found and not work_login
        and "ask" in board_caps
        and at_computer(now, idle_s, other_at)
        and ask_state.times < ASK_AT_MOST
        and not ask_state.declined
        and now - ask_state.last_at >= ASK_EVERY_S
        and now - ask_state.sent_at >= ASK_RETRY_S)


class BoardAsk:
    """The daemon's half of the board's question, for the Bridge.

    `due(caps)` reads config files and starts the idle probe, so the Bridge
    calls it off the serial thread (its run_async); `asked()` and
    `answer(a)` write two small files and are called from the serial
    thread. One lock keeps their reads and writes of the state apart. Built once for the life of the
    process and handed to each connection's Bridge, so the copy held here
    is a floor under the file, as RefreshWatch keeps one: save_ask is a
    single try, and a write that failed must not put the box up again at
    the next heartbeat.

    `home()` is the claude_home record, `view(now)` the config
    (config_view). Nothing here logs the answer, which is the person's; a
    failure is said by its type alone, once per streak (_say). Swallowed
    silently until the review of PR #8, a fault here left the box never
    shown, or a "Turn on" never saved, with nothing in the log."""

    def __init__(self, blink_home, home, view, now=time.time, idle=None,
                 other_at=None):
        self._blink_home = blink_home
        self._home = home
        self._view = view
        self._now = now
        self._idle = idle if idle is not None else idle_probe.seconds_idle
        self._other_at = other_at if other_at is not None else (lambda: None)
        self._lock = threading.Lock()
        self._mem = None
        self._said = None

    def _say(self, what, e):
        name = type(e).__name__
        if self._said != (what, name):
            self._said = (what, name)
            print(f"usage refresh {what} ({name})", file=sys.stderr)

    def _ok(self, what):
        # A call that worked ends that streak, so the same fault coming back
        # weeks later is said again (review of PR #8: it was once per
        # process, the silence this was written to end).
        if self._said is not None and self._said[0] == what:
            self._said = None

    def _state(self):
        read = load_ask(self._blink_home)
        mem = self._mem
        if mem is None:
            return read
        return AskState(max(mem.times, read.times),
                        max(mem.last_at, read.last_at),
                        mem.declined or read.declined,
                        max(mem.sent_at, read.sent_at))

    def _keep(self, state):
        self._mem = state
        try:
            save_ask(self._blink_home, state)
        except Exception:
            pass

    def due(self, board_caps):
        """Never raises: a question is never worth the thread."""
        try:
            due = self._due(frozenset(board_caps))
        except Exception as e:
            self._say("ask failed", e)
            return False
        self._ok("ask failed")
        return due

    def _due(self, caps):
        now = self._now()
        try:
            absent = chosen(self._blink_home) is None
        except (user_config.Unreadable, OSError):
            return False                # a file that cannot be read: not now
        # The lock for the state alone, not for the look: the serial thread
        # takes it to count an ask or save an answer, and must not wait out
        # a config parse or the idle probe to do so.
        with self._lock:
            state = self._state()
        ask = dict(setting_absent=absent, board_caps=caps, ask_state=state)
        # The cheap answers first; the config parse and the idle probe (a
        # process on macOS) only once everything else already says yes.
        if not should_ask(now, native=True, claude_found=True,
                          work_login=False, idle_s=0.0, **ask):
            return False
        h = self._home()
        if h is None or h.kind != claude_home.NATIVE:
            # Before anything stats a UNC path; None is a record that
            # cannot be read (claude_home.known).
            return False
        view = self._view(now)
        exe = locate_claude(self._blink_home, h.user_dir())
        ask.update(native=True,
                   # One too old for /usage is not offered it (USAGE_SINCE).
                   claude_found=exe is not None and not too_old(exe),
                   # A config that could not be read is taken for a work
                   # login, which the board never asks: a torn read gave
                   # "consumer" until V2 of the /code-review max, and an
                   # Enterprise seat could be asked.
                   work_login=view is None or is_work_login(view))
        if not should_ask(now, idle_s=0.0, **ask):
            return False
        return should_ask(now, idle_s=self._idle(),
                          other_at=self._other_at(), **ask)

    def asked(self):
        """An ask went out just now: the next waits ASK_RETRY_S unless an
        answer starts the day. Not counted: the board may never show it
        (answer() counts)."""
        with self._lock:
            st = self._state()
            self._keep(dataclasses.replace(st, sent_at=self._now()))

    def answer(self, a):
        """The board's answer, which means the person saw the box: one of
        the ASK_AT_MOST. "on" saves yes, "no" saves no and never asks again,
        "later" saves nothing. Applied only while the setting is still
        absent: a box answered after `blink install` or `blink config` made
        the choice must not overturn it. Never raises."""
        if a not in ANSWERS:
            return
        try:
            with self._lock:
                try:
                    absent = chosen(self._blink_home) is None
                except Exception:
                    absent = False      # unreadable: change nothing
                st = self._state()
                self._keep(dataclasses.replace(
                    st, times=st.times + 1, last_at=self._now(),
                    declined=st.declined or (absent and a == "no")))
                if not absent or a == "later":
                    return
                try:
                    user_config.put(self._blink_home, SETTING, a == "on")
                except Exception as e:
                    # Not saved: an "on" is asked again tomorrow, a "no" is
                    # held by `declined` above.
                    self._say("answer not saved", e)
                else:
                    self._ok("answer not saved")
        except Exception as e:
            self._say("answer not saved", e)


def is_work_login(view):
    """Whether the config names a login an employer pays for: a usage-billed
    seat, or an Enterprise or Team organization. Install asks those people
    with No as the default, since the quota is not theirs alone."""
    return bool(view.spend_seat or view.org_kind in ("enterprise", "team"))


def enabled(blink_home):
    """True or False for the person's choice; None when the file cannot be
    read, which is not "off" (status_page.enabled gives the reason)."""
    try:
        return user_config.load(blink_home).get(SETTING) is True
    except (user_config.Unreadable, OSError):
        return None


def chosen(blink_home):
    """True or False once the person has answered, None while the key is
    absent. Raises what user_config.load raises for a file it cannot read."""
    v = user_config.load(blink_home).get(SETTING)
    return v if isinstance(v, bool) else None


def clear_off(blink_home, now=None, missing=False):
    """Forget why the refresh turned itself off, so that turning it on again
    is a fresh start: no off_reason, no failure count, and `cleared_at` set
    to now. Written every time, even with nothing on disk to correct: the
    running watch may hold a reason in memory that its write never got to
    the file, and `cleared_at` is how it learns to drop it (RefreshWatch.
    _floor). The last run's time stays, so the spacing still holds.
    "Wasn't found" goes too: the command has just recorded where claude is
    (cli._remember_claude), and `blink status` kept saying it was missing
    until a run happened -- unless `missing`, claude still not found: then
    it stays, or `blink status` says all is well over a desk with no
    Claude Code (the /code-review max of 277b81a..4003b0b, cut list)."""
    state = load_state(blink_home)
    state.off_reason = ""
    state.fails = 0
    state.failing_since = None
    state.missing = bool(missing and state.missing)
    state.cleared_at = time.time() if now is None else now
    save_state(blink_home, state)


def _cache_at(view):
    """When this login's /usage cache was fetched, or None: the cache's own
    fetchedAtMs, whatever it holds. A cache with both windows null and no
    spend (a Team or Enterprise seat that reports nothing, a free login)
    still moves when a run refreshes it; read through the windows or the
    anchor alone, it never did, every good run was judged a failure, and a
    day later the refresh turned itself off with "run claude and /login"
    (final review, Important 3). The anchor and windows are a fallback for
    a view built without the field."""
    if view.cache_fetched_at is not None:
        return view.cache_fetched_at
    stamps = [x.fetched_at for x in (view.anchor, view.windows)
              if x is not None]
    return max(stamps) if stamps else None


class ConfigLook:
    """Claude Code's config for this module, one per thread that asks:
    look(now) is the view of this read, or None when this read failed --
    a write caught half done, a Windows lock, a file over
    claude_plan.MAX_BYTES or not a regular file. None is "not known", never
    "consumer, no cache": a fresh PlanReader per look gave EMPTY_VIEW for
    a failed read, so the board could ask an Enterprise seat, and a run
    straight before a torn read was judged FAILED (/code-review max of
    feat/usage-refresh, V2/V3). Not the last good view either: before a
    run that view's cache time is old, and after one it is the cache from
    before the run, which is the misjudgement this exists to stop. A look
    that fails is asked again at the next tick or heartbeat.

    Parsed at every look, not through ConfigFile's spacing: that parses at
    most once a FIRST_ANSWER_RETRY_S (5 s), so the look straight after a
    run that took under 5 s would be handed the view from before it. A
    parse a minute on this thread is nothing (11 ms for a generated 5.6 MB
    config, claude_plan.ConfigFile). Not the bus's reader: that one is
    used from the serial loop, and a reader is not thread-safe."""

    def __init__(self, home):
        self._home = home

    def __call__(self, now_epoch):
        h = self._home()
        if h is None or h.kind != claude_home.NATIVE:
            # Asked again a moment after the tick's own look: None fell
            # through to config_path(None), which is current(), and read
            # this computer's config on a WSL desk; a WSL home's is a UNC
            # path, never stat'ed here (the scoped review of
            # feat/spend-wsl-refresh, item 3).
            return None
        try:
            path = claude_plan.config_path(h)
            st = os.stat(path)
        except FileNotFoundError:
            return claude_plan.EMPTY_VIEW       # no config: known, empty
        except (OSError, ValueError):
            return None
        if not stat.S_ISREG(st.st_mode) or st.st_size > claude_plan.MAX_BYTES:
            return None
        return claude_plan.parse(path, st)


def config_view(home):
    """`view` for RefreshWatch and BoardAsk: a ConfigLook of `home`'s
    config. Each caller builds its own."""
    return ConfigLook(home)


class RefreshWatch:
    """Asks the person's own Claude Code for /usage, on a thread of its own,
    when nothing fresher exists and someone is at the computer (due).

    Never on the serial loop: a run may take the whole RUN_TIMEOUT_S, and
    the board calls the host lost after 30 s without a pong. Everything it
    is handed is a callable read from this thread: `enabled()` the setting,
    `board_alive()` a timestamp the Bridge keeps, `view(now)` the config
    (config_view), `other_at()` the newest other Claude reading, `home()`
    the claude_home record (a WSL one is never run).

    Log lines are exactly: usage refresh ran / skipped / failed (exit N) /
    failed (timeout) / off (cost) / off (failing), and "failed (<type>)"
    for an exception, by type alone. Never the answer, a figure or any of
    stdout: the answer holds the person's usage."""

    def __init__(self, blink_home, home, enabled, board_alive, view,
                 other_at, now=time.time, idle=None, runner=None):
        self._blink_home = blink_home
        self._home = home
        self._enabled = enabled
        self._board_alive = board_alive
        self._view = view
        self._other_at = other_at
        self._now = now
        # Resolved here, not as default arguments, so a test that replaces
        # the module function is followed.
        self._idle = idle if idle is not None else idle_probe.seconds_idle
        self._runner = runner if runner is not None else wsl._bounded_run
        self._said = None          # the exception type said for this streak
        # The state as this watch last left it: the floor under what the
        # file says. The throttle and the cost guard must not rest on one
        # write: save_state is a single try, and a failed write (a full
        # disk, a lock, a folder made read-only) left the file's old
        # last_run_at, so Claude Code ran at every tick, and after a cost
        # verdict nothing held it off (review of Task 4, round 1).
        self._mem = None
        # (start, fetched) of the last good run made after a real idle
        # reading, for activity_for: this process's, or the one the state
        # file kept from before a restart.
        st = load_state(blink_home)
        self._proven = (None if st.proven_start is None
                        else (st.proven_start, st.proven_fetched))

    def tick_once(self):
        """One decision and at most one run. Never raises: a refresh is
        never worth the thread, and the thread is never worth the daemon."""
        try:
            self._tick()
        except Exception as e:
            self._say_failure(type(e).__name__)

    def _say_failure(self, name):
        # Once per streak, by type alone, as WslWatch._tick_once: the text of
        # an exception from a run can carry what the run printed.
        if self._said != name:
            self._said = name
            print(f"usage refresh failed ({name})", file=sys.stderr)

    def _tick(self):
        now = self._now()
        on = self._enabled()
        if on is None:
            # config.json could not be read (a lock, a write caught half
            # done): not knowing is not "off". Nothing runs and nothing held
            # here is touched. Read as off until V11 of the /code-review
            # max, one unreadable tick wiped the streak held below, and with
            # it a cost guard whose writes had both failed.
            return
        if on is not True:
            # Off in config.json, which is where the person turns it back
            # on (the cost guard writes it off first). From here the file
            # decides whether it was turned off for a reason; only the
            # throttle stays held. The failure streak goes too: kept, a
            # day of failures behind a "failing" shutdown made the first
            # failure after `blink config usage-refresh on` turn it off
            # again at once, since failing_since was already a day old
            # (final review, Important 1, reproduced).
            if self._mem is not None:
                self._mem = State(self._mem.last_run_at,
                                  cleared_at=self._mem.cleared_at)
            return
        h = self._home()
        if h is None or h.kind != claude_home.NATIVE:
            # Before the config is read: a WSL home's is a UNC path, and
            # nothing here may stat one. None: the record cannot be read
            # (claude_home.known), and a guess is not run against.
            return
        state = self._floor(load_state(self._blink_home))
        if state.last_run_at > now:
            # The clock went back past the last run. Counted as run just
            # now, as StatusWatch does, so the next is a spacing from here
            # rather than whenever the clock catches up with the old reading.
            state = dataclasses.replace(state, last_run_at=now)
            self._keep(state)
        view = self._view(now)
        if view is None:
            # The config could not be read: when the cache was fetched is
            # not known, and not knowing is not "no cache yet, due".
            return
        ask = dict(enabled=True, native=True,
                   board_alive=bool(self._board_alive()),
                   cache_at=_cache_at(view),
                   other_at=self._other_at(), state=state)
        # The idle probe starts a process on macOS, so it is asked only once
        # everything else already says yes.
        if not due(now, idle_s=0.0, **ask):
            return
        idle_s = self._idle()
        if not due(now, idle_s=idle_s, **ask):
            return
        exe = locate_claude(self._blink_home, h.user_dir())
        if exe is None:
            if not state.missing:
                # Kept in the state so `blink status` can say "on, but
                # Claude Code wasn't found", and said once per streak.
                unsaved = self._keep(dataclasses.replace(state, missing=True))
                print("usage refresh skipped", file=sys.stderr)
                if unsaved:
                    self._say_failure(unsaved)
            return
        if too_old(exe):
            # Not run: its answer would read as COST and turn the refresh off
            # for a cost nobody paid (USAGE_SINCE). Said once; `blink status`
            # names it, and an updated Claude Code is run at the next tick.
            if self._said != "too old":
                self._said = "too old"
                print("usage refresh skipped (Claude Code too old for"
                      " /usage)", file=sys.stderr)
            return
        # Presence is proven only by a reading: the Linux rule (another
        # source's activity) lets a run happen, but is not a person at the
        # desk now.
        self._ask(exe, h, state, now, proven=idle_s is not None)

    def activity_for(self, fetched_at):
        """When to date the saved /usage as activity: the start of the run
        that wrote it, if this watch ran it after a real idle reading
        under IDLE_LIMIT_S and `fetched_at` is that run's fetch; else None.

        A /usage the daemon ran counts as somebody at the desk only when
        the OS had just said so. Counted never (42c58ad), a desk used
        only in a browser -- the feature's main audience, no other Claude
        source to say so -- had its board doze with the person there; always,
        a Linux desk left on refreshed itself awake all night. A cache
        written by anything else has another fetch time, and counts none.
        Read from the serial loop: one tuple, replaced whole."""
        p = self._proven
        if p is None:
            return None
        started, fetched = p
        if fetched is not None:
            # The cache that run made, by its own fetch time.
            return started if fetched_at == fetched else None
        if started - MOVED_SLACK_S <= fetched_at <= started + RUN_TIMEOUT_S:
            return started
        return None

    def _floor(self, read):
        """`read` (the file) under this watch's own copy: a reload never
        moves last_run_at back or clears an off_reason held here -- unless
        the person cleared it since (a newer `cleared_at`, written by
        `blink config usage-refresh on`): then the file's fresh start wins
        over the streak and the reason held here. Without that, turning it
        on within one tick of a shutdown kept it off until a restart while
        `blink status` said "on" (Task 6 note). Only a newer stamp counts:
        a file that cannot be read loads as all zeros, and must not lift a
        cost guard held here."""
        mem = self._mem
        if mem is None:
            return read
        last = max(mem.last_run_at, read.last_run_at)
        if read.cleared_at > mem.cleared_at:
            return dataclasses.replace(read, last_run_at=last)
        base = mem if mem.last_run_at >= read.last_run_at else read
        proven = max((mem, read), key=lambda x: x.proven_start or 0.0)
        return State(last, base.fails, base.failing_since,
                     mem.off_reason or read.off_reason,
                     cleared_at=mem.cleared_at, missing=base.missing,
                     proven_start=proven.proven_start,
                     proven_fetched=proven.proven_fetched)

    def _keep(self, state):
        """Hold `state` here, then write it. The type of a failed write is
        returned for the caller to say; the copy held here is the floor
        either way."""
        self._mem = state
        try:
            save_state(self._blink_home, state)
        except Exception as e:
            return type(e).__name__
        return None

    def _ask(self, exe, home, state, started, proven=False):
        codes, raised = [], []

        def runner(argv, **kw):
            try:
                r = self._runner(argv, **kw)
            except subprocess.TimeoutExpired:
                raise
            except Exception as e:
                raised.append(type(e).__name__)
                raise
            codes.append(r.returncode)
            return r

        try:
            verdict = run_once(exe, self._blink_home, runner=runner,
                               home=home)
        except Exception as e:
            # run_once maps a timeout and an OSError; anything else from the
            # runner is a failed run all the same, counted and spaced.
            verdict = Verdict.FAILED
            raised.append(type(e).__name__)
        unread = False
        moved = None
        # Why an exit-0 run failed, in fixed words: both used to read
        # "failed (exit 0)", and a logged-out Claude Code (no cache) could
        # not be told from output that changed shape (review of PR #8).
        why = None if verdict is Verdict.OK else "unexpected output"
        if verdict is Verdict.OK:
            # A logged-out Claude Code answers -p /usage with a zero-cost
            # success too, and writes no cache (measured 2026-11-05). Only a
            # cache fetched by this run makes it a refresh. A config that
            # could not be read now says neither: the run is inconclusive,
            # not a step toward the day of failures (V3 of the /code-review
            # max: a torn read straight after a good run counted one).
            try:
                view = self._view(self._now())
            except Exception:
                view = None
            if view is None:
                unread = True
            else:
                moved = _cache_at(view)
                if moved is None or moved < started - MOVED_SLACK_S:
                    verdict = Verdict.FAILED
                    why = "no cache written"
        # `blink config usage-refresh on` may have landed while claude ran
        # (up to RUN_TIMEOUT_S): its fresh start wins over the streak this
        # run began with, and only the run's time is added to it. Recorded
        # over the old state until V13 of the /code-review max, which put
        # the streak back, so the next failure could turn it off again
        # right after the person turned it on. A cost verdict still turns
        # it off: the guard is never lifted by a clear.
        fresh = load_state(self._blink_home)
        if fresh.cleared_at > state.cleared_at:
            state = dataclasses.replace(fresh, missing=False)
            if verdict is not Verdict.COST:
                unread = True
        new = (inconclusive(state, started) if unread
               else record(state, verdict, started))
        if proven and verdict is Verdict.OK:
            self._proven = (started, moved)
            new = dataclasses.replace(new, proven_start=started,
                                      proven_fetched=moved)
        self._mem = new
        if new.off_reason and not state.off_reason:
            # The setting first, each write in its own try: either one
            # holds it off, and this watch's copy holds it until the
            # setting is seen off. So `blink status` and `blink config`
            # say off as well.
            put_failed = None
            try:
                user_config.put(self._blink_home, SETTING, False)
            except Exception as e:
                # The state file still holds it off; said, so a config.json
                # that still reads "on" has a reason in the log.
                put_failed = type(e).__name__
            unsaved = self._keep(new)
            print(f"usage refresh off ({new.off_reason})", file=sys.stderr)
            self._said = None
            for name in (put_failed, unsaved):
                if name:
                    self._say_failure(name)
            return
        # A failed write is said after the run's own line, by type.
        unsaved = self._keep(new)
        if raised or (verdict is Verdict.FAILED and not codes):
            # No exit code: the runner raised (run_once maps an OSError to
            # FAILED, and so does the except above), or the run folder could
            # not be made, which is an OSError too.
            self._say_failure(raised[-1] if raised else "OSError")
            return
        elif verdict is Verdict.OK:
            print("usage refresh ran", file=sys.stderr)
        elif verdict is Verdict.TIMEOUT:
            print("usage refresh failed (timeout)", file=sys.stderr)
        elif codes[-1] == 0 and why:
            print(f"usage refresh failed ({why})", file=sys.stderr)
        else:
            print(f"usage refresh failed (exit {codes[-1]})",
                  file=sys.stderr)
        self._said = None
        if unsaved:
            self._say_failure(unsaved)

    def start(self):
        """Run tick_once() every TICK_S on a daemon thread, for the
        process's life."""
        t = threading.Thread(target=self._run, name="blink-refresh",
                             daemon=True)
        t.start()
        return t

    def _run(self):
        while True:
            self.tick_once()
            time.sleep(TICK_S)
