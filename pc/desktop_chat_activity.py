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
already reads for the five-hour record; it is not free of conversation
content, since Desktop keeps composer drafts in it too). Of each queued
event this module uses only the name and the time, and its own copy of
those lasts one poll. The read is not that narrow underneath, though:
pc/leveldb's parse cache (_parse_cache, up to 16 files and 4 MB) holds the
whole parsed Local Storage files in the daemon's memory for the life of the
process, account and conversation ids and drafts included. Nothing of it is
written out, logged or sent. The owner chose to read
it (decision 2026-11-24), on the understanding that it may only HOLD the
light on: it never switches the light on by itself, and a finish in it never
cuts short a hold the IndexedDB started. Only an IndexedDB write switches
it on, so every lit stretch starts at one. A "message sent" with no finish
after it then keeps an already-lit light on while the IndexedDB is quiet
(Windows went 89 s without a write inside one reply), up to PENDING_CAP_S.
A hold kept up only by such a pending "sent" (the IndexedDB's own hold has
lapsed) ends when its finish arrives or at PENDING_CAP_S.

The reason for both halves is that the queue is late and incomplete: on
macOS it reaches disk on a 60 s grid, 20-60 s after the event, and in one
run it missed 2 of 7 rapid turns altogether. A finish in it therefore does
not end a hold the IndexedDB started. And a "sent" whose finish was lost
would, if the queue could light the board on its own, light an idle desk
for up to PENDING_CAP_S, for instance at daemon start, when the queue still
holds the last send of the day. What the rule costs: if the IndexedDB hold
runs out before the queue's "sent" reaches disk, the light goes off and
that "sent" does not bring it back. A gap (the setting off, a folder that
cannot be read, an error) also ends the light, and the queue cannot resume
it afterwards; the next IndexedDB write can.

Only a boolean leaves this module (and an availability word for `blink
status`). No name, size, time, key or value from either store is ever
printed, logged, raised or returned. There is no print in this file.

One thing is written to disk, and only by the daemon's watcher (built with
publish=True in pc/ingest.default_providers): READING_FILE in the Blink
home, holding exactly four values, the availability word, the yes/no of
"a chat is answering", the time the daemon wrote them and the daemon's own
process id. It exists because `blink status` is another process, and a
watcher of its own, looked through once, cannot tell a quiet chat from a
busy one: its first poll is only a baseline, and on macOS churn is learned
over minutes. The daemon's watcher has that history, so status reads its
verdict (write_reading, read_reading). Nothing observed in Desktop's
folders, no name, size, time, count or queue event, is written there or
anywhere else, logged or sent; the learned churn, the baseline and the
queue's events stay in memory. The process id is the daemon's, from
os.getpid(), not anything Desktop wrote.

A reading is believed only while the daemon that wrote it is the one
running. Until 2026-11-25 the file was taken on its time stamp alone, so
after a restart `blink status` could print the old daemon's "busy" (a
reviewer's finding): the new watcher writes nothing on its first poll, and
the old stamp stayed fresh for READING_MAX_AGE_S. So a publishing watcher's
first poll removes whatever reading is there (clear_reading) and still
writes nothing of its own, and until its first write status prints no
busy/idle line at all. And read_reading refuses a reading whose writer is
gone: on macOS and Linux it asks the kernel whether the pid is alive
(os.kill(pid, 0), which delivers nothing there). On Windows os.kill is
TerminateProcess and would kill the process it was asking about, so there
it asks through OpenProcess and GetExitCodeProcess instead
(_windows_process_is_gone), which only look; and when that check cannot
tell, the reading is not believed at all. So a dead daemon's "busy" is
refused at once on Windows too, and not after READING_MAX_AGE_S as it was
until 2026-11-25. The Windows check has run only against a fake kernel32
in the suite; on Windows itself it is (not yet measured on a desk).

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
    queue's pending "sent" (PENDING_CAP_S) is what bridges that, once the
    IndexedDB has lit the light.
  - Windows writes nothing to the IndexedDB while Desktop is idle, so on
    Windows a write is activity.
  - macOS has idle cache churn: the IndexedDB log grows now and then with
    nobody chatting, and each churn write creates a new blob in one
    sub-folder of the blob folder. A reply's writes do not come with a new
    blob in that sub-folder. So on macOS the module learns which
    sub-folders churn (CHURN_OCCASIONS separate idle occasions, at least
    CHURN_OCCASION_GAP_S apart) and then ignores log growth that comes
    within CO_WRITE_WINDOW_S of a new blob there. Until it has learned one,
    the IndexedDB says nothing on macOS, and since the queue cannot light
    it alone, the chat light does not come on at all on macOS until then.
  - An idle occasion is not counted when the blob is seen. A new blob seen
    while the light is off and no send is pending is only a candidate,
    confirmed IDLE_CONFIRM_S later, and dropped if in that window the
    queue shows a pending send, or any "sent" or finish stamped within
    IDLE_CONFIRM_S before the candidate or after it, or the IndexedDB lights
    the light again. That is because the queue's "sent" for a reply reaches
    disk 20-60 s after the reply's first blob: a reviewer's replay of the
    rule that counted at once marked a reply's own sub-folder as churn
    after three replies, and the fourth reply, and every one after it,
    never lit. What remains: a reply whose "sent" the queue never records
    at all (it missed 2 of 7 rapid turns in one run) can still be counted
    as idle, and three such, 60 s apart, in the same sub-folder would still
    teach that sub-folder wrongly.
  - A changed set of sub-folders (Desktop reset its store) forgets what was
    learned, including a change made while the folders could not be read
    or the setting was off. This churn-learning rule was defined in code
    and has not been replayed against the probe data. A blob folder that
    is missing or cannot be listed, or holds a sub-folder (or a folder
    inside one) that is refused, makes macOS unavailable: without it churn
    and a reply look the same. Windows never reads the blob folder.
  - On macOS the blob folder is listed on every poll, about every 2 s, as
    well as the IndexedDB folder (the figures are under "Cost per poll"
    below); on a large, long-used store that cost is (not yet measured on
    a desk).

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
its newest log; on macOS a names-only listing of the blob folder three
os.scandir levels deep (the blob folder, each sub-folder, each folder inside
one); and one pc.leveldb.scan of Local Storage, whose parse cache means an
unchanged table is not re-read. On 2026-11-25 on a Mac (macOS 27.2) that
names-only listing of a real Desktop blob folder holding 89 entries took
about 1.4 ms, mean of 500 runs: at one poll every 2 s, under a tenth of a
percent of one core. Windows never lists it. The cost on a large,
long-used store is (not yet measured on a desk).

Nothing here runs at import. Every path is resolved inside poll(), through
pc/desktop_paths, unless the caller handed one in; the reading's path is
joined inside write_reading/read_reading from the blink_home the caller
passes.
"""
import datetime
import json
import os
import re
import stat
import sys
import time

from pc import desktop_local_storage, desktop_paths, leveldb, user_config
# For its pid check, the one this codebase already trusts with a hook's pid
# (_process_is_gone, and _PID_LIVENESS_AVAILABLE, which keeps os.kill off
# Windows). Importing it does nothing: it resolves no path at import.
from pc.providers import claude_state

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
# counts once. An occasion's time is when its candidate blob was seen, not
# when it was confirmed. With one candidate per sub-folder and the
# IDLE_CONFIRM_S wait below, two counted occasions in one sub-folder are
# already at least IDLE_CONFIRM_S apart, so the gap no longer bites on its
# own; it stays as a floor should the confirmation wait ever be shortened.
CHURN_OCCASIONS = 3
CHURN_OCCASION_GAP_S = 60.0
# How long a new blob seen with the light off and no send pending waits
# before it counts as an idle occasion. Longer than the 60 s the queue can
# lag on macOS (its 60 s disk grid, 20-60 s after the event), plus slack,
# so the "sent" of the reply that wrote the blob has reached disk and can
# drop the candidate before it is counted.
IDLE_CONFIRM_S = 90.0
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

# The daemon's verdict for `blink status`, in <blink_home>. The model is
# pc/link_status (link.json): an atomic write of a tiny JSON with the time of
# writing, and a read that calls anything too old "the app is not reporting".
READING_FILE = "desktop_chat.json"
# The four keys the file holds, and nothing else (see the module docstring).
# "pid" is the writing daemon's own process id. A reading without one (the
# three-key shape this file had before 2026-11-25) is not believed: nothing
# would say whether its writer is still running.
READING_KEYS = frozenset({"availability", "busy", "t", "pid"})
# The range a pid must be in to be asked about, claude_state's: zero and
# negatives name process groups to os.kill, not a process.
_PID_MAX = claude_state._PID_MAX
# The daemon polls about every 2 s. Writing on every poll would be some
# 43,000 writes a day for a value that changes a few times an hour, so the
# watcher writes when (availability, busy) changes and otherwise every
# HEARTBEAT_S, which is only there to show status the daemon is still alive.
HEARTBEAT_S = 30.0
# Four heartbeats: a serial loop that stalls for a pass or two (a slow burn
# read, a board reconnect) does not blank the line, while a daemon that has
# died stops being quoted within two minutes. A change is written at once,
# so this bounds only how long a dead daemon's last word is believed, not
# how late a live one's busy/idle is.
#
# Since 2026-11-25 it is the last guard rather than the only one: a restart
# clears the old reading on the new watcher's first poll, and a reading
# whose writer's pid is gone is refused at once, on macOS and Linux through
# os.kill(pid, 0) and on Windows through OpenProcess/GetExitCodeProcess
# (_writer_gone; on Windows itself (not yet measured on a desk)). What it
# still bounds alone is a dead daemon whose pid now belongs to another
# process, or one that exited with code 259 on Windows: both read alive. It
# was not lowered for those: at two or three heartbeats (60-90 s) a stall
# of a pass or two on top of a heartbeat would blank a live daemon's line,
# to shorten a window that needs a reused pid to open at all.
READING_MAX_AGE_S = 120.0
# A stamp ahead of the reader's clock by more than this is refused. The
# writer and the reader are on one computer and one clock, so only a clock
# step can do it; the next heartbeat corrects it.
READING_FUTURE_SLACK_S = 5.0
# A real reading is under 100 bytes; read no more than this, so a damaged or
# replaced file cannot make status load something large.
_READING_MAX_BYTES = 4096
_WORDS = frozenset({AVAILABLE, UNAVAILABLE, OFF})

_LOG_NAME = re.compile(r"^(\d+)\.log$")
_SUPPORTED = ("darwin", "win32")

# An entry Chromium deleted between our listing of its folder and our look
# inside it. NotADirectoryError is the same race one step on: the folder was
# removed and a file of that name made in its place.
_VANISHED = (FileNotFoundError, NotADirectoryError)


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


def write_reading(blink_home, availability, busy, now, pid=None) -> bool:
    """Write the daemon's verdict to <blink_home>/READING_FILE, atomically:
    a temporary file in the same folder, then os.replace, so status never
    reads half a file. True when it was written. Never raises and never
    prints: the chat light is best effort, and a full disk or a refused
    folder must not cost the poll that called this, nor put a path or an
    error into the daemon's log.

    `pid` is the writer's process id, os.getpid() when None (the daemon's
    case; a test names another to stand for a daemon that has gone).

    The Blink home is not created here. The daemon makes it at install; a
    missing one means there is no installed app for status to report on."""
    tmp = None
    try:
        # The values first, so a bad one fails before any file is named.
        doc = {"availability": str(availability), "busy": bool(busy),
               "t": float(now),
               "pid": int(os.getpid() if pid is None else pid)}
        path = os.path.join(blink_home, READING_FILE)
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(doc, f)
        os.replace(tmp, path)
        return True
    except Exception:
        if tmp is not None:
            try:
                os.unlink(tmp)
            except Exception:
                pass
        return False


def clear_reading(blink_home) -> bool:
    """Remove <blink_home>/READING_FILE. True when there is none afterwards
    (it was removed, or was never there). Never raises and never prints.

    Called by a publishing watcher on its first poll, the baseline, which
    has no verdict of its own to write: whatever the file holds then was
    written by an earlier daemon, and status must not quote that daemon's
    "busy" as this one's (see the module docstring).

    On the first poll and not when the watcher is built (considered
    2026-11-25): pc/ingest.IngestionBus defaults to publish_chat=True, so a
    removal at construction would run for any bus built anywhere, a script's
    or a future caller's, beside a daemon that is running and wiping its
    word; and claude_usage_bridge.build_bus can build an interim bus and a
    WSL one in one process. Between construction and the first poll the
    old reading is refused anyway when its writer is dead (_writer_gone,
    now on Windows too), which is the restart this was about."""
    try:
        os.unlink(os.path.join(blink_home, READING_FILE))
        return True
    except FileNotFoundError:
        return True
    except Exception:
        return False


# The Windows answer to "is the daemon that wrote this still running?". On
# POSIX that is os.kill(pid, 0); on Windows CPython's os.kill is
# TerminateProcess for every signal but the two console events, so the same
# call would kill the daemon it asks about. Until 2026-11-25 Windows was
# therefore not asked at all and a dead daemon's "busy" was believed for up
# to READING_MAX_AGE_S, including the stretch between a restart and the new
# watcher's first poll (a reviewer's finding). These calls only look:
# OpenProcess with the narrowest access right there is, which grants no way
# to touch the process, then GetExitCodeProcess, then CloseHandle.
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_ERROR_ACCESS_DENIED = 5
_ERROR_INVALID_PARAMETER = 87
_STILL_ACTIVE = 259


class _LivenessUnknown(Exception):
    """The Windows check could not tell. It carries no text: there is
    nothing in it worth printing, and a pid is not for the log either."""


def _load_kernel32():
    """(kernel32, get_last_error) for _windows_process_is_gone, loaded on
    every call and never at import: ctypes.WinDLL and ctypes.get_last_error
    exist only on Windows, and importing this module must do nothing on any
    platform. A separate function so a test can hand in a fake kernel32 on
    macOS and Linux, where the suite runs.

    Its own WinDLL with use_last_error=True, not ctypes.windll.kernel32: the
    shared one is loaded without it, so get_last_error() would read a stale
    value, and the error code is the whole answer when OpenProcess fails
    (pc/win_driver reads 1223 the same way for the same reason). Its own
    instance also means the argtypes set here cannot change what another
    module's calls through ctypes.windll see. restype is set on every call:
    a HANDLE is pointer-sized, and the default int restype would truncate a
    64-bit handle."""
    import ctypes
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle_t = ctypes.c_void_p
    dword = ctypes.c_ulong          # 32 bits on Windows, which is LLP64
    bool_t = ctypes.c_int
    kernel32.OpenProcess.argtypes = (dword, bool_t, dword)
    kernel32.OpenProcess.restype = handle_t
    kernel32.GetExitCodeProcess.argtypes = (handle_t, ctypes.POINTER(dword))
    kernel32.GetExitCodeProcess.restype = bool_t
    kernel32.CloseHandle.argtypes = (handle_t,)
    kernel32.CloseHandle.restype = bool_t
    return kernel32, ctypes.get_last_error


def _windows_process_is_gone(pid):
    """True when Windows says `pid` names no running process, False when it
    names one, and _LivenessUnknown when it cannot be told.

    No handle with ERROR_INVALID_PARAMETER is no such process: gone. No
    handle with ERROR_ACCESS_DENIED is a process we may not query (another
    account's, or a protected one): it exists, so alive, the same answer
    claude_state gives a POSIX PermissionError. With a handle, an exit code
    other than STILL_ACTIVE is a process that has exited and whose handle
    somebody still holds open, which Windows keeps openable: gone. The
    handle is closed whatever happens after it was opened.

    "Cannot tell" (any other error code, a failed GetExitCodeProcess, no
    ctypes, no kernel32) is raised rather than returned as gone or alive.
    Returned as alive, it would put back exactly the age-only rule this
    replaces; returned as gone, it would be a false answer in a function
    whose name a later caller will take at its word. Raised, read_reading
    turns it into None, its rule for a liveness check that raises: status
    then prints no busy/idle line, which is less than it could say, never
    more than it can back.

    What is still wrong in the safe direction: a pid Windows has handed to
    another process reads alive, and a daemon that exited with code 259
    reads alive; both leave the reading to READING_MAX_AGE_S. The whole
    check has run only under a fake kernel32 so far; on Windows itself it
    is (not yet measured on a desk)."""
    import ctypes
    kernel32, get_last_error = _load_kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION,
                                  False, pid)
    if not handle:
        err = get_last_error()
        if err == _ERROR_INVALID_PARAMETER:
            return True
        if err == _ERROR_ACCESS_DENIED:
            return False
        raise _LivenessUnknown()
    try:
        code = ctypes.c_ulong(0)
        if not kernel32.GetExitCodeProcess(handle, ctypes.pointer(code)):
            raise _LivenessUnknown()
        return code.value != _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _writer_gone(pid):
    """True when the operating system says no running process has `pid`.
    May raise when it cannot tell, and read_reading takes a raise as "do
    not believe the reading".

    On POSIX through claude_state._process_is_gone (os.kill(pid, 0), which
    delivers no signal; a process of another account reads alive). Anywhere
    else through _windows_process_is_gone, never os.kill: on Windows that is
    TerminateProcess and would kill the process asked about. A platform that
    is neither has no kernel32 to load, so the check raises there and no
    reading is believed, rather than one judged on its age alone.

    Every wrong answer here points the safe way. A pid the kernel has since
    handed to another process reads alive, which is the age bound again. A
    status run in another pid namespace than the daemon (a container with
    ~/.blink mounted into it) reads a live daemon as gone, and that costs
    the busy/idle line, never a word status cannot back."""
    if not claude_state._PID_LIVENESS_AVAILABLE:
        return _windows_process_is_gone(pid)
    return claude_state._process_is_gone(pid)


def read_reading(blink_home, now=None, max_age_s=READING_MAX_AGE_S,
                 writer_gone=None):
    """{"availability", "busy", "t"} as the daemon last wrote them, or None
    when there is nothing status can believe: no file, an unreadable or
    malformed one, any shape but the four keys (an availability word this
    module does not know, a busy that is not a bool, a time that is not a
    number, a pid that is not a whole number from 1 to _PID_MAX), a stamp
    more than READING_FUTURE_SLACK_S in the future, one older than
    `max_age_s` (the app is not running, or not reporting), or one whose
    writer is gone. `writer_gone(pid)` answers that last, _writer_gone when
    None, and a writer_gone that raises (the Windows check could not tell)
    is a reading not believed. Never raises."""
    try:
        if writer_gone is None:
            writer_gone = _writer_gone
        if now is None:
            now = time.time()
        now = float(now)
        with open(os.path.join(blink_home, READING_FILE),
                  encoding="utf-8") as f:
            text = f.read(_READING_MAX_BYTES + 1)
        if len(text) > _READING_MAX_BYTES:
            return None
        rec = json.loads(text)
        if not isinstance(rec, dict) or set(rec) != READING_KEYS:
            return None
        availability, busy, t = rec["availability"], rec["busy"], rec["t"]
        if not isinstance(availability, str) or availability not in _WORDS:
            return None
        if not isinstance(busy, bool):
            return None
        # bool is an int in Python: True is not a time.
        if isinstance(t, bool) or not isinstance(t, (int, float)):
            return None
        t = float(t)
        if t != t:                  # NaN
            return None
        age = now - t
        if age < -READING_FUTURE_SLACK_S or age > max_age_s:
            return None
        pid = rec["pid"]
        # bool is an int in Python: True is not a process.
        if isinstance(pid, bool) or not isinstance(pid, int):
            return None
        if not (1 <= pid <= _PID_MAX):
            return None
        # Last, after every cheap check: one syscall per status run.
        if writer_gone(pid):
            return None
        return {"availability": availability, "busy": busy, "t": t}
    except Exception:
        return None


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
                except _VANISHED:
                    # Rotated away between the listing and the look.
                    continue
                # Any other OSError is a log we could not look at, and it
                # propagates (the caller reports unavailable): skipping it
                # would let an older log, or none, stand for a folder that
                # was not read, the shape _blob_names had until 2026-11-25.
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
    blob folder, or any sub-folder or folder inside one, cannot be read.
    Chromium lays blobs out as <db>/<nn>/<blob>; files one level down are
    named "nn/blob", files directly in <db> by their own name. Names only,
    never opened.

    Until 2026-11-25 only the top folder could make this None: a refused
    sub-folder, or a refused folder inside one, was swallowed and read as
    empty (a reviewer's finding that day). Two things followed. `blink
    status` said "available" for a layout this module had not read. And on
    the poll the folder became readable again, every name in it was missing
    from the last listing, so each was a new blob: a candidate, a light, or
    a churn write paired with a reply's log growth. None makes _poll report
    unavailable and forget the baseline, so the listing after the gap is
    only recorded and nothing already in it reads as new.

    A vanish is not a refusal. Chromium deletes blobs and their folders as
    it goes, so an entry listed and gone by the time we look inside is the
    store working, not a store we cannot read, and calling it unavailable
    would drop the light in the middle of a reply. A vanished sub-folder is
    left out of the result (it is gone, not empty); a vanished folder inside
    one adds no names."""
    try:
        with os.scandir(blob_dir) as it:
            subs = []
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        subs.append((e.name, e.path))
                except _VANISHED:
                    continue
    except OSError:
        return None
    out = {}
    for name, path in subs:
        names = set()
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        is_dir = e.is_dir(follow_symlinks=False)
                    except _VANISHED:
                        continue
                    except OSError:
                        return None
                    if not is_dir:
                        names.add(e.name)
                        continue
                    try:
                        with os.scandir(e.path) as inner:
                            for f in inner:
                                names.add(e.name + "/" + f.name)
                    except _VANISHED:
                        # Deleted after the sub-folder was listed: no names,
                        # and the rest of the sub-folder still counts.
                        continue
                    except OSError:
                        return None
        except _VANISHED:
            # The whole sub-folder went after the top listing: leave it out
            # rather than report it empty.
            continue
        except OSError:
            return None
        out[name] = frozenset(names)
    return out


class DesktopChatActivity:
    """One watcher, polled by the daemon. Everything it learns and observes
    is held in memory. With `publish` True (the daemon's watcher, and only
    it) and a `blink_home`, poll() also writes its verdict to READING_FILE
    for `blink status`: the availability word, busy, the time and its own
    pid, nothing else; and its first poll removes the reading an earlier
    daemon left. The default writes and removes nothing, which is what
    status's own one-look watcher and every other caller want."""

    def __init__(self, idb_dir=None, blob_dir=None, ls_dir=None,
                 platform=None, blink_home=None, publish=False):
        self._publish_on = bool(publish)
        self._polls = 0
        # Whether an earlier daemon's reading is known to be gone: removed
        # by clear_reading, or replaced by this watcher's own write.
        self._cleared = False
        # (availability, busy) and the time of the last write attempt.
        self._published = None
        self._published_at = None
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
        self._candidates = {}       # sub -> when its candidate blob was seen
        # The sub-folder names learning was built on. Kept apart from
        # _blobs because _forget_disk drops _blobs at every gap, and a
        # store Desktop reset during a gap (folder unreadable, setting off)
        # must still forget what was learned about the old one.
        self._learned_subs = None
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
        gone) records afresh instead of calling the gap's writes a reply.

        Candidates go too: we cannot know whether a send or a reply came
        and went inside the gap, so a candidate seen before it proves
        nothing about idleness. _learned_subs stays, so a changed set of
        sub-folders is still noticed when the listing comes back."""
        self._log = None
        self._blobs = None
        self._blob_new_at = {}
        self._candidates = {}
        self._last_activity = None

    def _forget_learning(self):
        self._churn = set()
        self._occasions = {}
        self._candidates = {}

    # -- the queue ----------------------------------------------------------

    def _queue_events(self, ls_dir):
        """[(event name, epoch seconds)] for SENT and FINISH events in the
        analytics queue. Any failure is []. This module keeps only the
        returned list, and only for the poll that asked; pc/leveldb's parse
        cache, beneath it, keeps the whole parsed Local Storage files
        (ids and drafts included) in memory for the life of the process,
        never written out, logged or sent."""
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

    def _pending(self, events, now):
        """True when the latest "sent" in `events` (this poll's queue, far
        future stamps already dropped by _poll) has no finish after it and
        is at most PENDING_CAP_S old."""
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
        self._polls += 1
        try:
            busy = self._poll(float(now))
        except Exception:
            self._busy = False
            # Published as unavailable, not "available, idle": a poll that
            # broke has not looked, and status must not print a word for it.
            # What matters most is that a "busy" written before cannot stand.
            self._publish(UNAVAILABLE, False, now)
            return False
        self._publish(self._availability or UNAVAILABLE, busy, now)
        return busy

    def _publish(self, availability, busy, now):
        """Write the reading for `blink status` when publishing, the pair
        changed or HEARTBEAT_S has passed. Never raises and never changes
        what poll() returns.

        No verdict on a watcher's first poll. That poll is only a baseline,
        so its False is not "idle". But the file is not left alone either:
        what it holds then was written by an earlier daemon, and until
        2026-11-25 it stayed believable for READING_MAX_AGE_S after a
        restart, so status could print the old daemon's "busy" (a
        reviewer's finding). The first poll removes it (clear_reading), and
        status prints no busy/idle line until this watcher's first write,
        one poll later, about 2 s after the daemon starts. A removal that
        fails is tried again on each poll until a removal or a write of our
        own has worked; meanwhile the old reading is still refused once its
        writer is gone (read_reading's pid check: os.kill(pid, 0) on macOS
        and Linux, OpenProcess/GetExitCodeProcess on Windows) and otherwise
        ages out.

        That removal is why only the long-running daemon may publish. A
        one-shot caller that built a publishing watcher and polled it once
        would delete the running daemon's reading and blank status's line
        for up to HEARTBEAT_S. pc/ingest.default_providers publishes only
        for the daemon's bus; `blink status --wire` passes publish_chat
        False, and status's own watcher is built with the default.

        The attempt is recorded whether or not the write worked, so a Blink
        home that refuses writes costs one failed open per heartbeat, not
        one every 2 s (plus the one failed unlink a poll above, a single
        syscall); whatever the file said before ages out of read_reading
        within READING_MAX_AGE_S. A missing Blink home is no refusal: there
        is nothing to remove, and the removal counts as done."""
        try:
            if not self._publish_on or self._blink_home is None:
                return
            if not self._cleared:
                self._cleared = clear_reading(self._blink_home)
            if self._polls < 2:
                return
            try:
                t = float(now)
                if t != t:          # NaN
                    raise ValueError
            except Exception:
                t = time.time()
            pair = (availability, bool(busy))
            last = self._published_at
            # A clock that went backwards (last ahead of t) writes at once,
            # or the reading would carry a future stamp until it caught up.
            if (pair == self._published and last is not None
                    and 0.0 <= t - last < HEARTBEAT_S):
                return
            self._published = pair
            self._published_at = t
            if write_reading(self._blink_home, availability, busy, t):
                self._cleared = True
        except Exception:
            pass

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
            # Every readable listing is compared with the set learning was
            # built on, not only the one after an unbroken poll: _blobs is
            # dropped at every gap, and comparing against it alone let a
            # store reset while the folder was unreadable (or the setting
            # off) keep its old churn, which then ignored a real reply.
            subs = frozenset(blobs)
            if self._learned_subs is None:
                self._learned_subs = subs
            elif subs != self._learned_subs:
                self._forget_learning()
                self._learned_subs = subs
                # The renamed or added folders are a store Desktop reset, not
                # blobs that arrived: compared with the old listing every
                # name in them was "new", and each folder was made a
                # candidate (found by a test, 2026-11-25), starting it at one
                # idle occasion of three. Record this listing as the
                # baseline instead, as after any other gap.
                self._blobs = None
                self._blob_new_at = {}

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

        # The queue is read once per poll, and both the pending check and
        # the learning's "a chat was here" check come from this one list. A
        # stamp far ahead of our clock is dropped, whichever kind it is: a
        # far-future "sent" must not hide an earlier real one (max() would
        # pick it), a far-future finish must not cancel every send, and
        # either would otherwise drop every candidate until it came due.
        events = [(n, t) for n, t in self._queue_events(ls_dir)
                  if t - now <= FUTURE_SLACK_S]
        pending = self._pending(events, now)
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

        if s1_active:
            self._last_activity = now
        s1_hold = (self._last_activity is not None
                   and now - self._last_activity <= hold)

        if platform == "darwin":
            self._learn(new_subs, events, pending, held_before, now)

        # The queue only holds: a pending "sent" counts while the light is
        # already on (the previous poll said busy), and only an IndexedDB
        # write can switch it on, so the first True of every lit stretch
        # comes from s1_hold. The queue is 20-60 s late on macOS and missed
        # 2 of 7 rapid turns, so a stale "sent" whose finish was lost would
        # otherwise light an idle desk for up to PENDING_CAP_S, for one at
        # daemon start. _busy is reset to False by every gap (setting off,
        # unavailable, an exception), so the queue cannot resume a light
        # after one either.
        busy = bool(s1_hold or (pending and self._busy))
        self._availability = AVAILABLE
        self._busy = busy
        return busy

    def _learn(self, new_subs, events, pending, held_before, now):
        """macOS churn learning, one poll's worth.

        A new blob is a candidate first and an idle occasion only once
        IDLE_CONFIRM_S has passed with no sign of a chat, because the
        queue's "sent" for the reply that wrote it reaches disk 20-60 s
        later; counting at once taught a reply's own sub-folder as churn
        after three replies, and the light never came on again for it.

        A candidate is dropped on any poll where: a send is pending; the
        queue holds any "sent" or finish stamped at or after (candidate
        time - IDLE_CONFIRM_S), which is that chat arriving late; or the
        IndexedDB counted activity after the candidate was seen. The last is
        "after", not "at": after learning, a blob in a sub-folder that is
        not churn lights the light by itself, and dropping the candidate
        for the light its own write switched on would mean a second churn
        sub-folder could never be learned. A reply goes on writing to the
        log on the polls after, so its candidate still goes."""
        for sub, seen in list(self._candidates.items()):
            chat = (pending
                    or any(t >= seen - IDLE_CONFIRM_S for _n, t in events)
                    or (self._last_activity is not None
                        and self._last_activity > seen))
            if chat:
                del self._candidates[sub]
            elif now - seen >= IDLE_CONFIRM_S:
                del self._candidates[sub]
                self._count_occasion(sub, seen)
        # New candidates come after the confirmations, so a blob that lands
        # on the poll that confirms the last one starts the next.
        if held_before or pending:
            return
        for sub in new_subs:
            if sub in self._churn or sub in self._candidates:
                continue
            if any(t >= now - IDLE_CONFIRM_S for _n, t in events):
                continue
            self._candidates[sub] = now

    def _count_occasion(self, sub, seen):
        """Count one confirmed idle occasion in `sub`, timed at `seen`, the
        candidate's time: the confirmation comes a fixed delay later and
        says nothing about when the churn wrote."""
        count, last = self._occasions.get(sub, (0, None))
        if last is None or seen - last >= CHURN_OCCASION_GAP_S:
            count += 1
            self._occasions[sub] = (count, seen)
            if count >= CHURN_OCCASIONS:
                self._churn.add(sub)

    def _blob_changes(self, current, now):
        """The sub-folders that gained a blob since the last poll, given this
        poll's _blob_names listing (never None: _poll has already turned an
        unlistable blob folder into UNAVAILABLE, because without a listing
        the macOS rule cannot tell churn from a reply). The first listing
        after a baseline is forgotten is only recorded, so it gains none.
        A changed set of sub-folders is _poll's to notice, against
        _learned_subs, which survives the gaps that _blobs does not."""
        prev = self._blobs
        self._blobs = current
        if prev is None:
            return set()
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
