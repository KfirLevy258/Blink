"""Keeping ~/.blink/bridge.log readable, and keeping it small.

The log had no timestamps, no size limit and nothing that ever removed a
line. One customer's was 2 MB after a week; every machine that has run the
daemon since it shipped has been growing one without bound. It is also the
file `docs/README-full.md` asks a customer to send when something is wrong,
so it is a support artifact, not scratch output.

Three separate faults were producing that, and they need three answers:

  1. NOTHING WAS EVER REMOVED. rotate_if_full() below.

  2. MOST OF IT WAS HEARTBEAT. Profiling 3000 consecutive lines of a real
     log: 626 `-> pong`, 619 raw `{"t":"ping"}` and 581 parsed `<- ping` --
     about 60% of the file, for a 10-second keepalive that says nothing
     except that a cable nobody unplugged is still plugged in. The bridge
     now counts those instead of printing them, and says so once in a while.

  3. TWO WRITERS SHARED ONE FILE DESCRIPTOR. The board's console was echoed
     with a raw `sys.stderr.buffer.write(data)` while every other line went
     through `print(..., file=sys.stderr)`. Text-mode and binary writes to
     the same fd interleave mid-line, and the real log is full of the
     result:

         [u[bridge] <- {'t': 'ping', 'v': 2, 'up_ms': ...
         [proto] ho[bridge] <- {'t': 'pref', ...

     which breaks grep on exactly the file support greps. ConsoleEcho below
     assembles whole lines and hands them to the same print path, so there
     is one writer again.

Nothing here imports anything outside the standard library. The Journal
counts bytes and looks at no payload. ConsoleEcho does look at what it
forwards, on purpose and only to cut it down: see console_line.
"""
import contextlib
import os
import threading
import time

# Rotate at 4 MB and keep four older generations: 20 MB on a customer's disk,
# ever, no matter how long the daemon runs.
#
# The numbers are measured rather than chosen. On a development machine under
# continuous use -- the worst case there is, because the reading genuinely
# changes almost every poll and almost nothing gets suppressed -- the log runs
# at 24 KB/hour, or about 4 MB a week. 20 MB is therefore around five weeks
# there and considerably longer on a machine that is merely in use.
#
# An earlier version of this said 2 MB and three generations, on an estimate
# that the usage frame would rarely change. It changes most minutes on a
# machine somebody is working at, which put the real retention at a fortnight.
# Disk is the cheapest thing in this trade: the daemon's own download is 25 MB,
# so a 20 MB ceiling on its log is not a number anyone will notice, and a
# ceiling is the whole point -- a date rule cannot promise one.
CAP_BYTES = 4 * 1024 * 1024
KEEP = 4


def _stamp(when=None) -> str:
    """Local time, because the reader is a person comparing it with what they
    saw on their own clock. Sortable and unambiguous; the year is there
    because these files now live long enough to span one."""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(when))


# How far back the repeat detector looks for a repeating block, and how long
# a run may go unreported. The window has to cover a Python traceback, which
# is the shape that matters -- a crash-looping login service writes the same
# five-to-ten lines every ThrottleInterval, forever.
REPEAT_WINDOW = 16
TALLY_EVERY_S = 300.0


class Journal:
    """A text stream that timestamps every line and collapses repeats.

    Wraps sys.stderr rather than replacing the 31 print() calls that write to
    it, and rides on the supervisor's own redirect: launchd and the Windows
    task both point the daemon's stderr at bridge.log, so writing through
    here is writing to that file.

    WHY IT COLLAPSES BLOCKS AND NOT LINES. The case worth handling is a
    daemon that cannot start: launchd restarts it every ThrottleInterval and
    it writes the same traceback each time, about 8,600 times a day. Adjacent
    identical lines never occur in that -- consecutive lines of a traceback
    differ; it is the BLOCK that repeats -- so a syslog-style "last message
    repeated" would not fire once. Without this, such a loop fills all five
    rotation generations in about four days and evicts the history from
    before the fault, which is the only part anyone needs.

    The block is written TWICE before anything is suppressed, so a reader
    sees the shape before they see the count.

    print() reaches write() more than once for one line -- the text, then the
    newline -- so lines are assembled here and judged whole. A partial line
    is held rather than written, because a line cannot be un-written once it
    has gone out, and flush() emits whatever is pending.

    THREADS. The status-page watch (pc/status_page.py) made the daemon two
    threads printing through one Journal, and a Journal without a lock broke
    three ways under it, all reproduced: one thread's text glued to the other
    thread's newline, empty stamped lines, and IndexError raised out of
    print() from the block detector's index -- which on the serial loop
    escapes main() and ends the daemon. So every entry point takes one lock,
    and the partial line is kept PER THREAD: the lock alone cannot stop
    thread B's whole line landing between thread A's text and A's newline,
    because print() makes those two separate write() calls.
    """

    def __init__(self, stream, clock=None, window=REPEAT_WINDOW,
                 tally_every_s=TALLY_EVERY_S, now=None):
        import time as _time
        self._stream = stream
        self._clock = clock or _stamp
        self._now = now or _time.monotonic
        self._window = window
        self._tally_every = tally_every_s
        self._lock = threading.RLock()
        self._partials = {}         # thread id -> a line not yet terminated
        self._recent = []           # lines emitted, most recent last
        self._block = None          # the repeating block, once detected
        self._pos = 0               # how far into it the current pass is
        self._reps = 0              # complete passes suppressed so far
        self._tally_at = 0.0

    # -- the raw path: stamps and writes, never consulted by the detector --
    def _emit(self, text):
        self._stream.write(self._clock() + " " + text + "\n")
        # PYTHONUNBUFFERED is set in the plist and the unit, but the Windows
        # service opens its own file with buffering=1 and this wrapper sits
        # between print() and that -- so flush here rather than trust which
        # of the two is in play. The log lagging minutes behind the daemon
        # reads as a hang; the comment on the plist says so.
        self._stream.flush()

    def _tally(self):
        """Report and clear the suppressed run, if there is one."""
        if not self._reps:
            return
        n, self._reps = self._reps, 0
        if len(self._block) == 1:
            self._emit(f"[log] last message repeated {n} times")
        else:
            self._emit(f"[log] last {len(self._block)} lines"
                       f" repeated {n} times")
        self._tally_at = self._now()

    def _period(self):
        """Shortest L for which the last 2L lines are the same block twice.

        Shortest, not longest: "A A A A" has period 1, and reporting it as 2
        would halve the count and read as though pairs were the unit.
        """
        r = self._recent
        for L in range(1, self._window + 1):
            if 2 * L > len(r):
                break
            if r[-L:] == r[-2 * L:-L]:
                return L
        return 0

    def _offer(self, text):
        """One complete line, from the caller."""
        if self._block is not None:
            if text == self._block[self._pos]:
                self._pos += 1
                if self._pos == len(self._block):
                    self._pos = 0
                    self._reps += 1
                    # A run that never ends must not leave the log silent --
                    # that reads as a daemon that died, which is the opposite
                    # of what is happening. Report and keep counting.
                    if self._now() - self._tally_at >= self._tally_every:
                        self._tally()
                return
            # The pattern broke. Say what was skipped, then carry on.
            self._tally()
            self._block = None
            self._pos = 0
            self._recent = []

        self._emit(text)
        self._recent.append(text)
        if len(self._recent) > 2 * self._window:
            del self._recent[:-2 * self._window]
        L = self._period()
        if L:
            self._block = list(self._recent[-L:])
            self._pos = 0
            self._reps = 0
            self._tally_at = self._now()

    def write(self, s) -> int:
        if not s:
            return 0
        me = threading.get_ident()
        with self._lock:
            partial = self._partials.pop(me, "") + s
            while True:
                i = partial.find("\n")
                if i < 0:
                    break
                line = partial[:i].rstrip("\r")
                partial = partial[i + 1:]
                self._offer(line)
            # A writer that never sends a newline must not grow this without
            # bound. Nothing does; the cap is here because a buffer fed from
            # somebody else's print() is the wrong place to assume it.
            if len(partial) > 8192:
                self._offer(partial)
                partial = ""
            if partial:
                self._partials[me] = partial
        return len(s)

    def flush(self):
        """Anything this thread holds goes out now.

        Called after every print() by the daemon's own flushing, and at
        shutdown -- so a partial line, or a suppressed run at the moment the
        process ends, is still reported rather than lost. Only the calling
        thread's partial line: another thread's is a line it is still in the
        middle of printing, and emitting it here would cut it in two.
        """
        # With a timeout: at interpreter shutdown the status thread (a daemon
        # thread) is frozen wherever it was, possibly holding this lock, and
        # the last flush waiting on it would hang the exit forever. A second
        # is long enough for any live writer and loses at most a tally.
        if not self._lock.acquire(timeout=1.0):
            return
        try:
            partial = self._partials.pop(threading.get_ident(), "")
            if partial:
                self._offer(partial)
            self._tally()
            self._stream.flush()
        finally:
            self._lock.release()

    def hold(self):
        """The lock every write takes, for a caller that must not be
        written under -- rotate_if_full, between its copy and its truncate."""
        return self._lock

    def isatty(self) -> bool:
        return False

    def fileno(self) -> int:
        """The wrapped stream's descriptor.

        rotate_if_full() needs it: it will not truncate a file until it has
        established that the file really is where our stderr goes, and after
        this wrapper is installed `sys.stderr` IS this object. Without this
        the check answers "not our file" for every machine and the rotation
        silently never runs -- a feature that is present, tested, and dead.
        """
        return self._stream.fileno()

    # The daemon's `bye` path and anything else that reaches for raw bytes
    # gets the real stream's buffer, unstamped. Nothing does today -- the
    # console echo used to and no longer does -- but a wrapper that raises
    # AttributeError on .buffer would turn a stray byte write into a crash
    # in a login service, which is a poor trade for a missing timestamp.
    @property
    def buffer(self):
        return getattr(self._stream, "buffer", None)


# What a line of the board's console may say in bridge.log.
#
# ConsoleEcho used to forward each printk line verbatim, and the firmware
# prints values: the usage it applied (proto.c:627, "[usage] session 41%
# (11723s) weekly 9% ..."), every version on offer and running (ota.c:374,
# ui_settings.c:830, main.c:126, main.c:233), the app and protocol of the
# host (proto.c:727), the edition (bootclip.c:68), heap figures
# (mem_stats.c:131), touch coordinates (ui_swipe.c:190 and :313), the
# backlight level (backlight.c:39) -- and until 2026-10-28 the stored WiFi
# network's name, verbatim (cfg_store.c:491, which now says only "present").
# All of it landed in the file a customer emails us, against the CLAUDE.md
# rule that no value off the serial buffer is logged; the SSID was fixed at
# the firmware end, which only protects boards that have taken the update. Dropping the echo would also drop the only firmware-side evidence
# of an OTA, a fault reboot or a failed store, so the owner chose
# (2026-10-30) to keep the tag and a fixed set of messages and nothing else.
#
# Each entry is the constant text a printk in firmware/src starts with, up
# to its first formatted value: the whole message when it has none, and the
# words before the value when it does, so "[ota] downloading %s (%u bytes)"
# logs as "[ota] downloading". The values are cut, never carried.
# tests/pc/test_console_allowlist.py fails if an entry stops being the
# prefix of a real printk, so the list cannot drift away from the firmware
# silently. Matched longest first, because several share a beginning.
#
# Left out on purpose, and logged as the bare tag: messages whose constant
# prefix says nothing without its value ("[ota] latest", "[ota] image %u",
# "[mem] lvgl", "[swipe] dx", "[slide] dir", "[bl]", "[cfg] mode=" -- that
# last one is the SSID and token line, so even its stem stays out, because a
# board on older firmware still prints the network's name after it).
#
# The cost: "[mem] lvgl <where>: used=... peak=..." (mem_stats.c:131) no
# longer carries its numbers into bridge.log. Size the LVGL pool from the
# board's own console on a bench (a serial monitor, with the daemon
# stopped), not from a customer's log.
#
# And every line that is not one of our printks is now "(board line)": the
# lines firmware/prj.conf calls out as the evidence before a fault --
# LVGL's pool-exhaustion warnings ("No memory", "Allocating layer buffer
# failed"), Zephyr's "--- N messages dropped ---", and ">>> ZEPHYR FATAL
# ERROR" with its register dump. Their text is fixed, but it lives in Zephyr
# and LVGL, not in this repository, so no test here could fail when an
# upgrade rewords it the way test_console_allowlist fails for ours; and the
# LOG_* lines arrive behind a timestamp and, on a colour console, an ANSI
# prefix, so a stem would have to be matched past text the board chose.
# Left out for those reasons (review of 2026-10-30, N1). The placeholder
# still marks that the board said something at that moment, and a fault is
# still named by our own "[fatal] reason" as it happens and "[fatal] this
# boot follows" on the next boot.
CONSOLE_MESSAGES = tuple(sorted((
    # [ota] -- firmware/src/ota.c, ui_settings.c, main.c
    "[ota] slot1 write failed",                                # ota.c:175
    "[ota] bad redirect target",                               # ota.c:344
    "[ota] too many redirects for",                            # ota.c:352
    "[ota] downloading",                                       # ota.c:412
    "[ota] transfer ended",                                    # ota.c:436
    "[ota] truncated",                                         # ota.c:466
    "[ota] sha256 mismatch",                                   # ota.c:480
    "[ota] install not started: the offer changed under the prompt",
    "[ota] install not started: the offer changed under the confirm",
    "[ota] offer shown",                              # ui_settings.c:830
    "[ota] offer withdrawn: a different version is on offer",  # :1311
    "[ota] offer withdrawn: state is no longer available",     # :1326
    "[ota] install confirm withdrawn: the offer it named is no longer"
    " available",                                     # ui_settings.c:1346
    "[ota] test boot of",                                      # main.c:126
    "[ota] image confirmed",                                   # main.c:160
    "[ota] not healthy within 90 s -- rebooting to revert",    # main.c:164
    # [proto] -- firmware/src/proto.c
    "[proto] host says bye",                                   # :437
    "[proto] host time synced",                                # :642
    "[proto] host connected",                                  # :727
    "[proto] daemon offers",                                   # :825
    "[proto] ota: writing",                                    # :839
    "[proto] daemon resuming install of",                      # :859
    "[proto] ota failed",                                      # :889
    "[proto] host status",                                     # :896
    "[proto] burn line refused",                               # :958
    "[proto] rx ring overflow, dropped",                       # :1000
    "[proto] host went away",                                  # :1078
    # [fatal] -- firmware/src/fatal.c. The three "reason" lines differ only
    # after their numbers, so they share one stem.
    "[fatal] reason",                                          # :68 :78 :82
    "[fatal] selftest: writing through NULL at",               # :98
    "[fatal] this boot follows",                               # :117
    "[fatal] SELFTEST BUILD: will fault at",                   # :122
    # [usage] -- firmware/src/main.c, proto.c
    "[usage] panel: DBI bus not ready, MADCTL uncorrected",    # main.c:97
    "[usage] panel: MADCTL write failed",                      # main.c:103
    "[usage] no app after 60 s; dozing until one speaks",      # main.c:542
    "[usage] firmware boot OK",                                # main.c:568
    "[usage] FATAL: mbedTLS allocator hookup failed",          # main.c:584
    "[usage] display not ready",                               # main.c:589
    "[usage] cleared a stored refresh token: no WiFi in this build",
    "[usage] mode: USB bridge (no host yet)",                  # main.c:663
    "[usage] mode: USB bridge",                                # main.c:639
    "[usage] session",                                         # proto.c:627
    # [cfg] -- firmware/src/cfg_store.c, proto.c
    "[cfg] migrated 1.3.4 record",                             # :218
    "[cfg] migrated pre-lock record",                          # :268
    "[cfg] migrated pre-edition record",                       # :301
    "[cfg] migrated pre-main_src record",                      # :329
    "[cfg] migrated pre-OTA record",                           # :355
    "[cfg] persist failed",                                    # :383
    "[cfg] storage open failed",                               # :412
    "[cfg] theme and focus taken from the older slot",         # :480
    "[cfg] unknown edition",                                   # proto.c:676
    "[cfg] edition already stamped as",                        # proto.c:693
    "[cfg] edition set to",                                    # proto.c:698
    "[cfg] could not store the edition",                       # proto.c:701
    # [theme] -- firmware/src/ui_theme.c, proto.c
    "[theme] preview timed out; back to the stored theme",     # :114
    "[theme] could not store the theme",                       # :155
    "[theme] preview cancelled",                               # :214
    "[theme] saved",                                           # :248
    "[theme] line refused",                                    # proto.c:769
    # [boot] -- firmware/src/ui_boot.c, logo.c, bootclip.c
    "[boot] logo: frame failed to decode",                     # ui_boot.c:203
    "[boot] logo: cannot map partition",                       # logo.c:38
    "[boot] logo: company",                                    # logo.c:46
    "[boot] logo: none",                                       # logo.c:53
    "[boot] unknown edition",                                  # bootclip.c:64
    "[boot] edition",                                          # bootclip.c:68
    # the rest
    "[sleep] dozing",                                          # ui_sleep.c:183
    "[sleep] waking",                                          # ui_sleep.c:201
    "[focus] could not store the choice",                # ui_settings.c:2717
    "[slide] VSCRDEF refused; transitions will not scroll",    # ui_slide.c:132
    "[touch] INJECT BUILD: the PC can drive this panel -- bench only, never"
    " ship this",                                         # touch_inject.c:490
    "[time] manual time",                                      # net_time.c:67
), key=len, reverse=True))

# "[ota] update %s: wanted %s, running %s (from %s)" (main.c:233) is the line
# that says whether an update landed or was reverted, and its first value is
# one of two words the firmware picks itself, never one it was sent. Those
# two renderings are kept whole; everything after them is cut like any other
# value.
CONSOLE_RENDERED = ("[ota] update landed", "[ota] update REVERTED")

# Every tag a printk in firmware/src opens with. A line opening with any
# other bracketed word -- or with none: the bootloader's and Zephyr's own
# banners, the touch tracer's #TT rows -- is a fixed placeholder, because
# its first word is the board's text too.
CONSOLE_TAGS = frozenset({
    "bl", "boot", "cfg", "fatal", "focus", "mem", "ota", "proto", "sleep",
    "slide", "swipe", "theme", "time", "touch", "usage",
})

BOARD_LINE = "(board line)"


def console_line(line: str) -> str:
    """What bridge.log may say for one line of the board's console.

    A known message logs as its constant text, a known tag on anything else
    as the bare tag, and everything else as BOARD_LINE. Nothing the board
    formatted into the line survives.
    """
    text = line.strip()
    for known in CONSOLE_RENDERED + CONSOLE_MESSAGES:
        if text.startswith(known):
            return known
    if text.startswith("[") and "]" in text:
        tag = text[1:text.index("]")]
        if tag in CONSOLE_TAGS:
            return f"[{tag}]"
    return BOARD_LINE


class ConsoleEcho:
    """The board's own console output, one whole line at a time.

    Fed the same bytes the protocol reader is fed. Emits complete lines
    only, so a read that lands mid-line does not produce half of one in the
    log while another writer is mid-line too.

    Protocol JSON is dropped. Every one of those lines is also parsed by
    protocol.LineReader and logged by the bridge in the form it acted on, so
    echoing the raw text as well wrote everything twice -- and the heartbeat,
    which is most of the traffic, is written a third time as the pong going
    back. What is left is what the echo was actually for: the board's printk
    output, its [proto] and [ota] and [usage] lines, which exist nowhere
    else -- and those are cut to console_line() before they are returned,
    because the firmware formats values into them.
    """

    # Deliberately a shape test on the first character and not a parse. This
    # is not deciding what a message means -- the reader does that -- only
    # whether the bridge is about to log the same bytes twice.
    def __init__(self, limit=4096):
        self._buf = bytearray()
        # A board that emits a very long line without a newline must not grow
        # this without bound. Nothing does today; the cap is here because a
        # buffer fed from a wire is the wrong place to assume that.
        self._limit = limit

    def feed(self, data):
        """Returns the complete console lines in `data`, as text."""
        self._buf.extend(data)
        lines = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            raw = bytes(self._buf[:nl])
            del self._buf[:nl + 1]
            line = raw.decode("utf-8", "replace").rstrip("\r")
            if not line.strip():
                continue
            if line.lstrip().startswith("{"):
                continue            # protocol JSON: logged parsed instead
            lines.append(console_line(line))
        if len(self._buf) > self._limit:
            del self._buf[:-self._limit]
        return lines


class Heartbeat:
    """Counts the keepalive instead of printing it.

    The board pings every 10 seconds and the bridge pongs back. Logging both
    ends produced 25,000 lines a day that carry one bit between them -- the
    link is up -- which the summary line states directly, and which the next
    real message states anyway.

    Kept rather than deleted outright because the absence of a heartbeat is
    diagnostic: a support reader needs to be able to tell "the cable went
    quiet at 14:02" from "the daemon stopped logging".
    """

    def __init__(self, every_s=600.0, now=None):
        self._every = every_s
        self._now = now or time.monotonic
        self._n = 0
        self._next = self._now() + every_s

    def beat(self) -> None:
        self._n += 1

    def due(self):
        """A line to log, or None. Call as often as you like.

        Silent when nothing has beaten since the last summary: a quiet link
        should leave a gap in the log, not a line every ten minutes saying
        there is nothing to say.
        """
        now = self._now()
        if now < self._next:
            return None
        self._next = now + self._every
        if not self._n:
            return None
        n, self._n = self._n, 0
        return f"[bridge] link ok: {n} keepalives in the last {int(self._every)}s"


class Repeats:
    """Log a message when it says something new; count it when it does not.

    The usage frame goes out once a minute whether or not anything moved, and
    once the heartbeat stopped being logged it was most of what was left:
    about 270 bytes a minute, 0.5 MB a day, which is forty days of history in
    the 20 MB the rotation allows. An idle desk was spending all of that
    saying "still 8%".

    Comparison is on the fields that carry meaning, NOT on the whole message.
    Three of its fields -- the two countdowns and the age -- change on every
    single frame by construction, so any "is this the same message" test that
    included them would answer no every time and suppress nothing. That is
    the trap this class exists to avoid, and it is why the fields are named
    by the caller rather than inferred here.

    A change is always logged immediately. The suppression only ever applies
    to a frame that repeats what the line above it already said.
    """

    def __init__(self, fields, label="messages", every_s=600.0, now=None):
        self._fields = tuple(fields)
        self._label = label
        self._every = every_s
        self._now = now or time.monotonic
        self._last = None
        self._n = 0
        self._next = self._now() + every_s

    def _key(self, m):
        return tuple(m.get(f) for f in self._fields)

    def worth_logging(self, m) -> bool:
        key = self._key(m)
        if key != self._last:
            self._last = key
            return True
        self._n += 1
        return False

    def due(self):
        """A line for the frames that were held back, or None."""
        now = self._now()
        if now < self._next:
            return None
        self._next = now + self._every
        if not self._n:
            return None
        n, self._n = self._n, 0
        return f"[bridge] {n} unchanged {self._label} not logged"


class RepeatsBy:
    """Repeats, kept separately for each value of one field.

    For `svc`, which the heartbeat re-states once a minute per page shown. A
    single Repeats over both pages never suppresses anything, because claude
    and codex alternate and each line differs from the one before it -- and
    logged in full those restatements made bridge.log seven to nine times
    larger on every desk, with the feature on or off (an idle one-page desk:
    151 lines and 8 KB a day became 1007 lines and 69 KB). One Repeats per
    provider logs each page's changes and counts the rest.
    """

    def __init__(self, by, fields, label="messages", every_s=600.0, now=None):
        self._by = by
        self._make = lambda: Repeats(fields, label, every_s, now)
        self._each = {}
        self._label = label
        self._every = every_s
        self._now = now or time.monotonic
        self._next = self._now() + every_s

    def worth_logging(self, m) -> bool:
        key = m.get(self._by)
        if key not in self._each:
            self._each[key] = self._make()
        return self._each[key].worth_logging(m)

    def due(self):
        """One line for everything held back, across every key, or None."""
        now = self._now()
        if now < self._next:
            return None
        self._next = now + self._every
        n = 0
        for r in self._each.values():
            n, r._n = n + r._n, 0
        if not n:
            return None
        return f"[bridge] {n} unchanged {self._label} not logged"


def _is_the_same_file(path, stream) -> bool:
    """Is `stream` actually writing to `path`?

    The daemon does not open its own log on macOS -- launchd does, via
    StandardErrorPath -- so before truncating a file we have to establish
    that it is the file our own stderr goes to. On Linux the unit sets no
    StandardOutput at all and the output goes to the journal, which rotates
    itself and must not be touched from here; this returns False there and
    rotation becomes a no-op, which is the correct behaviour rather than a
    missing feature.
    """
    try:
        fd = stream.fileno()
    except (AttributeError, OSError, ValueError):
        return False
    try:
        a = os.fstat(fd)
        b = os.stat(path)
    except OSError:
        return False
    return (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino)


def rotate_if_full(path, stream, cap=CAP_BYTES, keep=KEEP) -> bool:
    """Copy the log aside and truncate it in place. Returns whether it ran.

    Copy-and-truncate, NOT rename-and-reopen, and the reason is that we do
    not own the file descriptor. launchd opened bridge.log before the daemon
    existed and holds that fd for the life of the job: rename the file and
    the daemon goes on writing to the same unlinked inode, so the new
    bridge.log stays empty forever and the old one is invisible but still
    growing. That failure is silent and total, which is why this is the one
    approach here.

    Truncating under a live writer is only safe because the fd is O_APPEND,
    which makes every write seek to end-of-file first: after the truncation
    the next line lands at offset 0 rather than at the old offset with two
    megabytes of NUL in front of it. Measured, not assumed -- `lsof +fg` on
    the running daemon reports FILE-FLAG `R,W,AP` on both fd 1 and fd 2, and
    the Windows path opens its own file with mode "a".

    The window between the copy and the truncation can lose a line written
    inside it. That is inherent to the approach (logrotate's copytruncate has
    the same note) and it is the right trade here: the alternative loses
    every line from the rotation onwards.
    """
    try:
        if os.path.getsize(path) < cap:
            return False
    except OSError:
        return False
    if not _is_the_same_file(path, stream):
        return False

    try:
        # Under the Journal's lock when the stream is one: a line printed
        # between the copy and the truncate is in neither file, and with the
        # status-page thread printing too, "inherent" was no longer rare.
        # Lines from outside the Journal (none today) keep the old window.
        #
        # Everything that can fail comes BEFORE the truncate, and every
        # failure leaves the generations in a state the next attempt can
        # finish without losing more than a clean rotation would.
        hold = getattr(stream, "hold", None)
        with (hold() if hold is not None else contextlib.nullcontext()):
            # The copy goes to a side name first, so a copy that dies half
            # way (a full disk) never leaves a short file sitting in .1 for
            # the next attempt to shift up as if it were a generation.
            staged = f"{path}.part"
            _copy(path, staged)
            _shift(path, keep)
            os.replace(staged, f"{path}.1")
            os.truncate(path, 0)
    except OSError:
        # A log that cannot be rotated must never take the daemon down with
        # it. The next tick tries again; in the meantime the file grows,
        # which is what it did for the whole of this product's life so far.
        return False
    return True


def _shift(path, keep):
    """Move bridge.log.1 .. .N each up one, making .1 free.

    Only the unbroken run that starts at .1 moves, highest first, so every
    destination is vacant -- except the last when the run reaches `keep`,
    where the oldest generation is the one that falls off, and os.replace
    overwrites it. The oldest used to be deleted up front, on every attempt,
    and that cost a generation per failure: a rename refused part way (a
    Windows antivirus or a log viewer holding .2 for a moment) was retried one
    serial-loop pass later, deleted .4 again, and by then .4 was the old .3.
    Measured with keep=4 and one refused rename: only generations 1 and 2
    survived, where a clean rotation keeps 1 to 3. Found in review,
    2026-10-31.

    Moving only the run from .1 also makes the retry exact: a first attempt
    that got as far as vacating .3 and .1 leaves a gap, which the run stops
    at, so the retry moves what is still below the gap and leaves what is
    above it. A generation someone deleted by hand is treated the same.
    """
    run = 0
    while run < keep and os.path.exists(f"{path}.{run + 1}"):
        run += 1
    for i in range(min(run, keep - 1), 0, -1):
        os.replace(f"{path}.{i}", f"{path}.{i + 1}")


def _copy(src, dst):
    """shutil.copyfile without importing shutil into the frozen bundle for
    one call. Chunked, because the file is megabytes by definition here."""
    with open(src, "rb") as r, open(dst, "wb") as w:
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            w.write(chunk)
