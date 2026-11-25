"""Is a Claude Desktop chat replying right now? One boolean, guessed from disk.

Claude Code tells us when it is working: it runs hooks, and the statusline
hook writes a file we read. A chat in Claude Desktop runs no hooks at all, so
there is nothing it tells us. What it does do is write. While a reply
streams, Desktop's own browser engine writes the conversation into its
claude.ai IndexedDB (IndexedDB/https_claude.ai_0.indexeddb.leveldb) and,
for values too large for the leveldb, into the blob folder beside it
(IndexedDB/https_claude.ai_0.indexeddb.blob). Those files hold the
conversation itself, so this module reads ONLY what os.scandir and os.stat
give: names, sizes and mtimes. It never opens a file in either folder, and
tests/pc/test_desktop_chat_activity.py records every file the poll opens
to hold it to that.

The second signal is Desktop's analytics queue, a value in its Local Storage
whose key contains `antalytics_queue_v1` (the store pc/desktop_local_storage
already reads for the five-hour record, which holds no conversation content).
Each queued event has a name and a time, and only those two are kept: in
memory, for one poll, never logged, stored or sent. The owner chose to read
it (decision 2026-11-24). It may only hold the light ON, never be the only
thing that turns it off: on macOS the queue reaches disk on a 60 s grid,
20-60 s after the event, and in one run it missed 2 of 7 rapid turns
altogether. A "message sent" with no finish after it says a reply is still
coming even while the IndexedDB is quiet; a finish in the queue does not
end a hold the IndexedDB started, because the queue is late and incomplete.

Only a boolean leaves this module (and an availability word for `blink
status`). No name, size, time, key or value from either store is ever
printed, logged, raised or returned. There is no print in this file.

What it costs, measured with throwaway probes on macOS (Claude Desktop
2.19675.0) and Windows 11 ARM64 (Claude Desktop 2.16120.0.0), 2026-10-01/02:

  - It can light while a draft is being typed, before it is sent: Desktop
    saves drafts into the same IndexedDB.
  - It trails the end of a reply by about 20 s on Windows and 50 s on macOS.
    That is QUIET_HOLD_S: the longest gap between writes inside one reply,
    so the light does not flicker off mid-answer.
  - Two chats replying at once count as one: the folder does not say which
    conversation was written.
  - Windows was silent on disk for 89 s inside one 2000-word reply. The
    queue's pending "sent" (PENDING_CAP_S) is what bridges that.
  - Windows writes nothing to the IndexedDB while Desktop is idle, so on
    Windows a write is activity.
  - macOS has idle cache churn: the IndexedDB log grows now and then with
    nobody chatting, and each churn write creates a new blob in one
    sub-folder of the blob folder. A reply's writes do not come with a new
    blob in that sub-folder. So on macOS the module learns which
    sub-folders churn (CHURN_OCCASIONS separate idle occasions, at least
    CHURN_OCCASION_GAP_S apart) and then ignores log growth that comes
    within CO_WRITE_WINDOW_S of a new blob there. Until it has learned one,
    the IndexedDB says nothing on macOS and the queue alone can light it.
    A changed set of sub-folders (Desktop reset its store) forgets what was
    learned. This churn-learning rule was defined in code and has not been
    replayed against the probe data. A blob folder that is missing or
    cannot be listed makes macOS unavailable: without it churn and a reply
    look the same. Windows never reads the blob folder.

No size threshold anywhere: a draft, a churn write and a reply are told
apart by when and where they land, not by how much, so every figure above
holds whatever the write sizes are.

Open cases:

  - Two chats replying at once on macOS (not yet measured on a desk).
  - Desktop with its analytics turned off, where the queue may never fill
    (not yet measured on a desk).
  - Going offline, reconnecting, and a Desktop restart in the middle of a
    reply (not yet measured on a desk).
  - A second Desktop version on each OS (not yet measured on a desk).

Cost per poll: one listing of the IndexedDB leveldb folder and one stat of
its newest log; on macOS a two-level listing of the blob folder; and one
pc.leveldb.scan of Local Storage, whose parse cache means an unchanged
table is not re-read.

Nothing here runs at import. Every path is resolved inside poll(), through
pc/desktop_paths, unless the caller handed one in.
"""
import datetime
import json
import os
import re
import stat
import sys
import time

from pc import desktop_local_storage, desktop_paths, leveldb, user_config

# The key in <blink_home>/config.json. Missing means DEFAULT_ON.
SETTING = "desktop-chat-light"
DEFAULT_ON = True

# How long the light stays on after the last IndexedDB write that counted:
# the longest quiet gap measured inside one reply on each OS (see above).
QUIET_HOLD_S = {"win32": 20.0, "darwin": 50.0}

# How long a queued "sent" with no finish after it keeps the light on. Ten
# minutes is well past the 89 s Windows silence and any reply measured, and
# short enough that a lost finish (the queue missed 2 of 7 turns) does not
# leave the light on for the rest of the day.
PENDING_CAP_S = 600.0

# A "sent" stamped further ahead of this computer's clock than this is not
# believed: the queue's times are Desktop's, the poll's are ours, and one
# far-future stamp would otherwise hold the light on until it came due.
FUTURE_SLACK_S = 60.0

# macOS churn learning: a sub-folder of the blob folder is churn once it has
# gained a blob on this many separate idle occasions, each at least
# CHURN_OCCASION_GAP_S after the last one counted, so one burst of writes
# counts once.
CHURN_OCCASIONS = 3
CHURN_OCCASION_GAP_S = 60.0
# Log growth within this many seconds of a new blob in a churn sub-folder is
# that churn write, not a reply. Wider than one 2 s poll, so a blob that
# lands one poll before the log still pairs.
CO_WRITE_WINDOW_S = 3.0

AVAILABLE = "available"
UNAVAILABLE = "unavailable"
OFF = "off"

SENT = "claudeai.message.sent"
FINISH = frozenset({"claudeai.message.perceived_ttlt",
                    "claudeai.message.stopped",
                    "claudeai.message.streaming_abandoned"})

QUEUE_KEY_MARKER = b"antalytics_queue_v1"

_LOG_NAME = re.compile(r"^(\d+)\.log$")
_SUPPORTED = ("darwin", "win32")


def config_state(blink_home):
    """True or False for the owner's choice, None when config.json cannot be
    read. The shape cli._CONFIG wants (status_page.enabled gives the reason
    None is not "off"). A missing key is DEFAULT_ON."""
    try:
        value = user_config.load(blink_home).get(SETTING)
    except (user_config.Unreadable, OSError):
        return None
    if DEFAULT_ON:
        return value is not False
    return value is True


def enabled(blink_home) -> bool:
    """The choice as a plain bool for the poll. An unreadable config.json is
    DEFAULT_ON: one failed read must not flip the light to something the
    owner did not choose, and the default is what they have not changed."""
    try:
        state = config_state(blink_home)
    except Exception:
        return DEFAULT_ON
    return DEFAULT_ON if state is None else state


def _epoch(value):
    """Seconds since the epoch from a queue timestamp, or None.

    A number below 1e11 is seconds, anything larger milliseconds (1e11 s is
    the year 5138; 1e11 ms is 1973). A string is ISO 8601, a trailing Z
    accepted, a naive one read as UTC. Booleans are refused: bool is an int
    in Python, and True would be 1970. Outside 2020-2100 is refused, the same
    bounds desktop_local_storage puts on its own records.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        t = float(value)
        if t != t:                  # NaN
            return None
        if t >= 1e11:
            t /= 1000.0
    elif isinstance(value, str):
        text = value.strip()
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.datetime.fromisoformat(text)
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        t = dt.timestamp()
    else:
        return None
    if not (desktop_local_storage.SAMPLE_EPOCH_MIN <= t
            <= desktop_local_storage.SAMPLE_EPOCH_MAX):
        return None
    return t


def _event_list(doc):
    """The list of events in one queue value: the value itself when it is a
    list, else the first list-valued field of an object."""
    if isinstance(doc, list):
        return doc
    if isinstance(doc, dict):
        for v in doc.values():
            if isinstance(v, list):
                return v
    return []


def _newest_log(idb_dir):
    """(file number, size, mtime_ns) of the newest *.log that is a regular
    file, or None when there is none. Raises OSError for a folder that is
    missing or refused. Listed and stat'ed, never opened."""
    # Only a regular file is a log. A leveldb never makes a folder (or a
    # symlink, or a socket) called 000003.log, so one is a layout we do not
    # recognise, and counting it would have `blink status` say "available"
    # for a store this module cannot read. A non-regular entry is skipped,
    # not picked: a real log beside a higher-numbered folder of that name is
    # still the log, and a folder holding only such entries is no log.
    #
    # A leveldb deletes the old log right after it starts the next one, which
    # is exactly when a reply is writing. A log listed and gone before the
    # stat is that rotation, not an unreadable folder: list again (the new
    # log is there) instead of reporting the folder unavailable, which would
    # also throw the baseline away. Three tries, then the error stands.
    for attempt in range(3):
        candidates = []
        with os.scandir(idb_dir) as it:
            for entry in it:
                m = _LOG_NAME.match(entry.name)
                if not m:
                    continue
                try:
                    if not entry.is_file(follow_symlinks=False):
                        continue
                except OSError:
                    continue
                candidates.append((int(m.group(1)), entry.path))
        if not candidates:
            return None
        candidates.sort(reverse=True)
        vanished = False
        for n, path in candidates:
            try:
                st = os.stat(path, follow_symlinks=False)
            except FileNotFoundError:
                # The newest went between the listing and the stat: that is
                # rotation, and the next listing has its successor.
                vanished = True
                break
            # The listing's answer can be stale by the stat: a file swapped
            # for a folder in between is still not a log.
            if stat.S_ISREG(st.st_mode):
                return n, st.st_size, st.st_mtime_ns
        if not vanished:
            return None
        if attempt == 2:
            raise FileNotFoundError(2, "log rotated away three times")
    return None


def _blob_names(blob_dir):
    """{top-level sub-folder: frozenset of relative names}, or None when the
    blob folder cannot be listed. Chromium lays blobs out as
    <db>/<nn>/<blob>; files one level down are named "nn/blob", files
    directly in <db> by their own name. Names only, never opened."""
    try:
        with os.scandir(blob_dir) as it:
            subs = [(e.name, e.path) for e in it
                    if e.is_dir(follow_symlinks=False)]
    except OSError:
        return None
    out = {}
    for name, path in subs:
        names = set()
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            with os.scandir(e.path) as inner:
                                for f in inner:
                                    names.add(e.name + "/" + f.name)
                        else:
                            names.add(e.name)
                    except OSError:
                        # Gone or refused between the listing and the look
                        # inside: the rest of the sub-folder still counts.
                        continue
        except OSError:
            pass
        out[name] = frozenset(names)
    return out


class DesktopChatActivity:
    """One watcher, polled by the daemon. Holds only in-memory state."""

    def __init__(self, idb_dir=None, blob_dir=None, ls_dir=None,
                 platform=None, blink_home=None):
        self._idb_dir = idb_dir
        self._blob_dir = blob_dir
        self._ls_dir = ls_dir
        self._platform = platform
        self._blink_home = blink_home
        self._availability = None
        self._busy = False
        self._log = None            # (number, size, mtime_ns) last poll
        self._blobs = None          # {sub: frozenset(names)} last poll
        self._blob_new_at = {}      # sub -> when it last gained a blob
        self._churn = set()         # learned churn sub-folders (macOS)
        self._occasions = {}        # sub -> (count, last counted time)
        self._last_activity = None

    # -- paths, resolved at each poll ---------------------------------------

    def _paths(self):
        # One resolve for the three folders: on Windows each is a listing of
        # several folders, and three separate ones could straddle a change.
        with desktop_paths.one_answer():
            idb = self._idb_dir or desktop_paths.indexeddb_dir()
            blob = self._blob_dir or desktop_paths.indexeddb_blob_dir()
            ls = self._ls_dir or desktop_paths.local_storage_dir()
        return idb, blob, ls

    def _forget_disk(self):
        """Drop the baseline, so the poll after a gap (setting off, folder
        gone) records afresh instead of calling the gap's writes a reply."""
        self._log = None
        self._blobs = None
        self._blob_new_at = {}
        self._last_activity = None

    def _forget_learning(self):
        self._churn = set()
        self._occasions = {}

    # -- the queue ----------------------------------------------------------

    def _queue_events(self, ls_dir):
        """[(event name, epoch seconds)] for SENT and FINISH events in the
        analytics queue. Any failure is [], and nothing read is kept past the
        poll that asked."""
        try:
            if not os.path.isdir(ls_dir):
                return []
            out = []
            for _key, raw in leveldb.scan(
                    ls_dir, lambda k: QUEUE_KEY_MARKER in k):
                text = desktop_local_storage.decode_value(raw)
                if text is None:
                    continue
                try:
                    doc = json.loads(text)
                except (TypeError, ValueError, RecursionError):
                    continue
                for ev in _event_list(doc):
                    if not isinstance(ev, dict):
                        continue
                    name = ev.get("eventName")
                    if not isinstance(name, str):
                        continue
                    if name != SENT and name not in FINISH:
                        continue
                    t = _epoch(ev.get("eventTimestamp"))
                    if t is not None:
                        out.append((name, t))
            return out
        except Exception:
            return []

    def _pending(self, ls_dir, now):
        # A stamp far ahead of our clock is dropped, whichever kind it is: a
        # far-future "sent" must not hide an earlier real one (max() would
        # pick it), and a far-future finish must not cancel every send.
        events = [(n, t) for n, t in self._queue_events(ls_dir)
                  if t - now <= FUTURE_SLACK_S]
        sent = [t for n, t in events if n == SENT]
        if not sent:
            return False
        last_sent = max(sent)
        last_finish = max((t for n, t in events if n in FINISH),
                          default=float("-inf"))
        if last_sent <= last_finish:
            return False
        age = now - last_sent
        return -FUTURE_SLACK_S <= age <= PENDING_CAP_S

    # -- the poll -----------------------------------------------------------

    def poll(self, now) -> bool:
        """Busy or not at `now` (epoch seconds). Never raises."""
        try:
            return self._poll(float(now))
        except Exception:
            self._busy = False
            return False

    def _poll(self, now):
        if self._blink_home is not None and not enabled(self._blink_home):
            self._availability = OFF
            self._busy = False
            self._forget_disk()
            return False
        platform = self._platform or sys.platform
        if platform not in _SUPPORTED:
            return self._unavailable()
        idb_dir, blob_dir, ls_dir = self._paths()
        try:
            log = _newest_log(idb_dir)
        except OSError:
            return self._unavailable()
        if log is None:
            return self._unavailable()

        # The blob listing comes before anything is recorded from this poll,
        # so a blob folder that cannot be listed leaves no half-updated
        # baseline behind: _unavailable() then forgets all of it at once.
        blobs = None
        if platform == "darwin":
            blobs = _blob_names(blob_dir)
            if blobs is None:
                # On macOS the IndexedDB log churns with nobody chatting, and
                # only a new blob beside the growth tells that churn from a
                # reply. Without a blob listing the rule can never learn
                # churn, so the IndexedDB half of the signal is blind, and
                # saying "available" would claim a light this module cannot
                # give.
                return self._unavailable()

        first = self._log is None
        grew = False
        if not first:
            prev = self._log
            if log[0] > prev[0]:
                grew = True             # rotated: a new log was started
            elif log[0] == prev[0] and log[1:] != prev[1:]:
                grew = True
        self._log = log

        new_subs = set()
        if platform == "darwin":
            new_subs = self._blob_changes(blobs, now)

        pending = self._pending(ls_dir, now)
        hold = QUIET_HOLD_S[platform]
        held_before = (self._last_activity is not None
                       and now - self._last_activity <= hold)

        if first:
            s1_active = False
        elif platform == "win32":
            s1_active = grew
        elif self._churn:
            paired = any(
                now - self._blob_new_at[s] <= CO_WRITE_WINDOW_S
                for s in self._churn if s in self._blob_new_at)
            s1_active = ((grew and not paired)
                         or any(s not in self._churn for s in new_subs))
        else:
            s1_active = False

        if platform == "darwin" and not held_before and not pending:
            for sub in new_subs:
                count, last = self._occasions.get(sub, (0, None))
                if last is None or now - last >= CHURN_OCCASION_GAP_S:
                    count += 1
                    self._occasions[sub] = (count, now)
                    if count >= CHURN_OCCASIONS:
                        self._churn.add(sub)

        if s1_active:
            self._last_activity = now
        s1_hold = (self._last_activity is not None
                   and now - self._last_activity <= hold)
        busy = bool(s1_hold or pending)
        self._availability = AVAILABLE
        self._busy = busy
        return busy

    def _blob_changes(self, current, now):
        """The sub-folders that gained a blob since the last poll, given this
        poll's _blob_names listing (never None: _poll has already turned an
        unlistable blob folder into UNAVAILABLE, because without a listing
        the macOS rule cannot tell churn from a reply). The first listing
        after a baseline is forgotten is only recorded, so it gains none."""
        prev = self._blobs
        self._blobs = current
        if prev is None:
            return set()
        if set(current) != set(prev):
            self._forget_learning()
        new = set()
        for sub, names in current.items():
            if names - prev.get(sub, frozenset()):
                new.add(sub)
                self._blob_new_at[sub] = now
        return new

    def _unavailable(self):
        self._availability = UNAVAILABLE
        self._busy = False
        self._forget_disk()
        return False

    def availability(self) -> str:
        """AVAILABLE, UNAVAILABLE or OFF, from the last poll; one poll at the
        wall clock first when there has been none."""
        if self._availability is None:
            self.poll(time.time())
        return self._availability or UNAVAILABLE
