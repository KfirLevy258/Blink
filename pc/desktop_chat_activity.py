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
light on: it never switches the light on by itself, and it never switches
it off. Only an IndexedDB write switches it on, so every lit stretch starts
at one. A "message sent" with no finish after it then keeps an already-lit
light on while the IndexedDB is quiet (Windows went 89 s without a write
inside one reply), up to PENDING_CAP_S.

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
