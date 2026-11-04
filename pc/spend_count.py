"""The running count of spend between /usage runs.

/usage saves an exact anchor; between runs the only live signal is the
status line's per-session `cost.total_cost_usd`, a cumulative figure that
each Claude Code process renders on its own. This turns those samples into
counted cents.

Rules, each from a way the raw number misleads:

* A session's first sighting adds nothing. A resumed session shows its whole
  past cost on the first render, and that is not new spend.
* Only a rise over the session's high-water mark counts. Two processes on one
  session id show different totals for the same session; a lower figure from
  the lagging one must not be counted again when the other catches up.
* A rise is stamped with the previous sighting of its session. `since(anchor)`
  counts only rises whose previous sighting is at or after the anchor: a rise
  between a sighting before the anchor and one after it may have happened
  partly before /usage measured, so it is dropped rather than counted twice.
* `at` is the render's own time (the payload's mtime), so reading the same
  render again moves nothing.

* A cost below zero is no cost. Claude Code does not write one; a damaged
  payload can, and a negative "high-water mark" would count the next real
  figure twice over.

* A residual, kept rather than fixed (U10 in the final review of
  2026-11-04). Two processes on one session id -- `claude --continue` in
  a second terminal -- render different totals, and any sighting at or
  after the last one moves the session's stamp. If the leading process's
  render just before a /usage is lost (another session wrote
  statusline.json over it before the poll read it), the lagging one's
  figure, equal to the high-water mark, stamps the session after the
  anchor, and the leader's next rise is counted in full although part of
  it was spent before /usage measured: 30 cents counted for 20. Only a
  sighting below the mark could be told apart, and the lost render makes
  the lagging one's equal to it, so no rule on the figures alone fixes
  it; the status line carries no process id. It needs two processes on
  one session, a lost render and a /usage together, and the next /usage
  makes the figure exact again.

What the file keeps, and why it stays small. Until the final review of
2026-11-04 every rise of the month was kept one by one, so the file grew
all month and was rewritten on nearly every render. Now:

* Rises from the last five hours (the burn view's twenty quarter-hours,
  measured back from the newest render seen) are kept one by one, because
  `buckets()` needs each one's quarter-hour.
* Older rises are folded into one carried entry: their total, the total of
  those stamped at or after the fold's boundary, the earliest previous
  sighting among them and the newest render. The boundary is the newest
  anchor `since()` has been asked about -- claude_cli._spend_frame asks
  with the /usage time or, with none this month, the period start.
* `since(anchor)` is exact for the anchors the page can ask: the period
  start and any anchor at or before the earliest folded sighting (every
  folded rise counts), the boundary itself (the carried total at or after
  it), and an anchor newer than the newest folded render (none counts,
  since a rise is stamped no later than its render). An anchor between
  the boundary and the newest folded render -- a /usage first seen more
  than five hours after it ran, while rises were being counted -- counts
  no folded rise: dropped, never counted twice, the same direction as the
  straddling rule above. One between the earliest folded sighting and
  the boundary (an anchor older than one already asked) counts the
  carried total at the boundary, a figure no higher than the truth.
  Neither arises from Claude Code's config, whose /usage time only moves
  forward within a login and a month, and bind() empties everything when
  either changes.
* Each session's highest estimate, so a resumed session's past cost is
  never counted as new. These are kept until the month turns or the login
  changes, and they are what the file's size follows: about 70 bytes a
  session, so 2 KB a day at thirty sessions a day and 66 KB by the 31st
  (tests/pc/test_spend_count.py's busy month, U13b in the final review of
  2026-11-04, which found the README's "a few kilobytes" untrue). Not
  pruned: a session dropped while its terminal stood idle would take its
  next render for a first sighting and count nothing from it.
* A save writes a rise on a session already in the file at most once a
  minute (SAVE_EVERY_S). A rise not yet written is counted again after a
  restart, from the saved high-water mark and stamped with the saved
  sighting before it -- but only when that session renders again. One that
  had closed never does, so until the final review of 2026-11-04 (U12) a
  restart lost the last minute of a closed session's rises ($10.70 on the
  page fell to $10.20). An ordinary exit now writes whatever the pacing
  held back (_flush_at_exit; the daemon's SIGTERM is an ordinary exit).
  What a crash or a kill -9 loses is still at most the last minute of rises
  on sessions that do not render again. A session's first sighting and an
  emptied count (bind()) are written at once. A restart that missed the
  first sighting would take the session's next render for its first and
  count nothing from it, and one that missed the emptying would carry the
  last login's count into this one (tests/pc/test_spend_page.py's login
  round trip caught that in the first cut of the pacing).

Integer cents throughout. State is per session UUID (a per-session total is
all the spec allows) and is never printed or logged.
"""
import atexit
import json
import math
import os
import time
import weakref

# As a package import, like every other module under pc/. A bare
# `import protocol` worked only where pc/ itself was on sys.path (its own
# test put it there), and `from pc import spend_count` -- the bus's import,
# and the daemon's -- raised ModuleNotFoundError.
from pc import protocol, theme

_BUCKETS = 20
# Rises newer than this, measured back from the newest render, are kept one
# by one; the burn view's twenty quarter-hours are exactly this long.
_RECENT_S = _BUCKETS * 900
# The shortest gap between two writes of the file, on the monotonic clock.
SAVE_EVERY_S = 60
# 2: the carried entry and the newest anchor asked about. A version-1 file
# (every rise one by one, from before 2026-11-04) is read as empty, as any
# file this cannot read is: it held an estimate, which the next /usage
# makes exact.
_VERSION = 2


# The largest figure the counter believes, in cents: ten million dollars.
# Every bound below is this one. No Claude Code session costs that much, and
# without a bound a status-line total of 10**307 passed observe(), was saved
# as a 309-digit cent figure a minute later, and every start after it died in
# _load, where math.isfinite raises OverflowError on an int that large (the
# final review of 2026-11-04, U1). 1.8e306 dollars did not get as far:
# times 100 it rounds to infinity, and round() raised out of observe().
# A carried total sums many sessions' rises, so it is allowed a thousand
# times more; that is still exact as a float and as a cent figure on the
# page.
MAX_CENTS = 10 ** 9
_MAX_FOLDED_CENTS = MAX_CENTS * 1000
# A time is a render's mtime: the epoch up to 2100-01-01, the ceiling
# statusline_source puts on a reset stamp. Anything past it is not a time.
_MAX_TIME = 4_102_444_800


def _num(x) -> bool:
    """A finite int or float that is not a bool.

    The try: math.isfinite converts to float, and an int of 309 digits or
    more raises OverflowError there instead of answering False."""
    if isinstance(x, bool) or not isinstance(x, (int, float)):
        return False
    try:
        return math.isfinite(x)
    except OverflowError:
        return False


def _time(x) -> bool:
    return _num(x) and 0 <= x <= _MAX_TIME


def _cents(x, ceiling=MAX_CENTS) -> bool:
    return _num(x) and 0 <= x <= ceiling


# Every counter that has written its file, for the exit flush (U12 in the
# final review of 2026-11-04). A WeakSet, so a counter dropped by its owner
# (a test's, a bus replaced) is not kept alive by this, and registered with
# atexit on the first one rather than at import: importing a module of ours
# does nothing (CLAUDE.md).
_WRITTEN = weakref.WeakSet()
_HOOKED = []


def _flush_at_exit():
    """Write what the pacing held back, at an ordinary exit.

    The daemon's SIGTERM is an ordinary exit (claude_usage_bridge's
    exit_on_sigterm), so this runs when launchd or systemd stops it. Each
    counter writes to the file it last wrote and nowhere else: by exit the
    test sandbox has handed HOME back, and resolving the default path again
    then would name the real ~/.blink. A failure is ignored; there is no
    later save to hand it to."""
    for counter in list(_WRITTEN):
        try:
            counter._flush()
        except Exception:
            pass


def _default_path() -> str:
    # Resolved when called: a module constant would read the real home at
    # import, before the test sandbox runs.
    return os.path.join(os.path.expanduser("~"), ".blink", "spend-count.json")


def _parse(d):
    """(fp, period, sessions, adds, fold, anchor) from a loaded file, or
    None for one not to believe. Every figure must be finite, at or above
    zero and within its bound: json reads Infinity, NaN and ints of any
    length, and a high-water mark below zero would count the next real
    figure on top of it (-100000 then $1.00 counted $1001)."""
    if not isinstance(d, dict) or d.get("v") != _VERSION:
        return None
    fp, period = d.get("fp"), d.get("period")
    if fp is not None and not isinstance(fp, str):
        return None
    if period is not None and not _time(period):
        return None
    if not isinstance(d["sessions"], dict) or not isinstance(d["adds"], list):
        return None
    sessions = {}
    for sid, v in d["sessions"].items():
        if not isinstance(v, list) or len(v) != 2:
            return None
        hw, seen = v
        if isinstance(hw, bool) or not isinstance(hw, int) \
                or not _cents(hw) or not _time(seen):
            return None
        sessions[sid] = [hw, seen]
    adds = []
    for a in d["adds"]:
        if not isinstance(a, list) or len(a) != 3:
            return None
        prev, at, cents = a
        if not (_time(prev) and _time(at) and _cents(cents)):
            return None
        adds.append([prev, at, int(cents)])
    fold = d.get("fold")
    if fold is not None:
        if not isinstance(fold, list) or len(fold) != 5:
            return None
        b, after, total, first, last = fold
        if not (_time(b) and _time(first) and _time(last)
                and _cents(after, _MAX_FOLDED_CENTS)
                and _cents(total, _MAX_FOLDED_CENTS)):
            return None
        fold = [b, int(after), int(total), first, last]
    anchor = d.get("anchor")
    if anchor is not None and not _time(anchor):
        return None
    return fp, period, sessions, adds, fold, anchor


class SpendCounter:
    def __init__(self, path=None, persist=True, clock=time.monotonic):
        self._path = path
        self._persist = persist
        self._clock = clock
        self._saved_at = None  # self._clock() at the last write
        self._failed_at = None  # self._clock() at the last refused write
        self._written_to = None  # the file the last write went to
        self._fp = None
        self._period = None
        self._sessions = {}   # sid -> [high_water_cents, last_seen]
        self._adds = []       # [prev_seen, at, cents], the last _RECENT_S
        # [boundary, cents_from_boundary, cents, first_prev, last_at], or
        # None: every rise folded out of _adds (see the module docstring).
        self._fold = None
        self._anchor = None   # the newest anchor since() was asked about
        self._latest = None   # the newest render seen
        self._dirty = False
        # Written at the next save whatever the pacing: see the docstring.
        self._urgent = False
        if persist:
            self._load()

    # -- state -----------------------------------------------------------
    def _file(self) -> str:
        return self._path or _default_path()

    def _clear(self):
        self._sessions = {}
        self._adds = []
        self._fold = None
        self._anchor = None
        self._latest = None
        self._dirty = self._urgent = True

    def _load(self):
        """The saved state, or nothing: any file this cannot read whole and
        believe is read as empty, as the module docstring promises. It held
        an estimate, which the next /usage makes exact; a daemon that will
        not start because of it (a raise here comes out of SpendCounter(),
        and so out of build_bus(), at every start) shows nothing at all."""
        try:
            with open(self._file(), "r", encoding="utf-8") as f:
                d = json.load(f)
            got = _parse(d)
        except (OSError, ValueError, KeyError, TypeError, OverflowError,
                RecursionError):
            # RecursionError: json's answer to an array nested thousands
            # deep, as in session_verdict.load. OverflowError: a number
            # too large for a float, met by any arithmetic on it.
            return
        if got is None:
            return
        fp, period, sessions, adds, fold, anchor = got
        self._fp, self._period = fp, period
        self._sessions, self._adds = sessions, adds
        self._fold, self._anchor = fold, anchor
        stamps = [a[1] for a in adds] + [v[1] for v in sessions.values()]
        if fold is not None:
            stamps.append(fold[4])
        self._latest = max(stamps, default=None)

    def bind(self, account_fp, period_start) -> None:
        if self._fp is None and self._period is None:
            # Nothing stored or loaded: adopt without discarding what was
            # observed before the first look.
            self._fp, self._period = account_fp, period_start
            self._dirty = self._urgent = True
            return
        if account_fp != self._fp or period_start != self._period:
            self._fp, self._period = account_fp, period_start
            self._clear()

    # -- counting --------------------------------------------------------
    def observe(self, session_id, cost_usd, at) -> bool:
        # The counter owns the guard: a NaN, infinity, string or bool from
        # a damaged payload must not touch the state.
        # And a cost past MAX_CENTS or a time that is not one: see there.
        if (not _num(cost_usd) or not _time(at) or cost_usd < 0
                or cost_usd > MAX_CENTS / 100):
            return False
        cents = int(round(cost_usd * 100))
        s = self._sessions.get(session_id)
        if s is None:
            self._sessions[session_id] = [cents, at]
            if self._latest is None or at > self._latest:
                self._latest = at
            self._dirty = self._urgent = True
            return True
        hw, last = s
        changed = False
        if cents > hw:
            self._adds.append([last, at, cents - hw])
            s[0] = cents
            changed = True
        if at > last:
            s[1] = at
            changed = True
        if changed:
            if self._latest is None or at > self._latest:
                self._latest = at
            self._compact()
            self._dirty = True
        return changed

    def _compact(self):
        """Fold every rise older than _RECENT_S into the carried entry."""
        cutoff = self._latest - _RECENT_S
        old = [a for a in self._adds if a[1] < cutoff]
        if not old:
            return
        self._adds = [a for a in self._adds if a[1] >= cutoff]
        fold = self._fold
        candidates = [x for x in (self._anchor, self._period,
                                  None if fold is None else fold[0])
                      if x is not None]
        boundary = (max(candidates) if candidates
                    else min(prev for prev, _at, _c in old))
        if fold is None:
            fold = [boundary, 0, 0, math.inf, -math.inf]
        elif boundary != fold[0]:
            # What was carried from the old boundary, re-read at the new
            # one: since() answers that question, so it decides here too.
            fold = [boundary, self._folded_since(boundary), fold[2],
                    fold[3], fold[4]]
        for prev, at, c in old:
            if prev >= boundary:
                fold[1] += c
            fold[2] += c
            fold[3] = min(fold[3], prev)
            fold[4] = max(fold[4], at)
        self._fold = fold

    def _folded_since(self, anchor_at) -> int:
        """The carried cents an anchor counts; see the module docstring."""
        if self._fold is None:
            return 0
        boundary, from_boundary, total, first, last = self._fold
        if anchor_at <= first:
            return total
        if anchor_at <= boundary:
            return from_boundary
        return 0

    def since(self, anchor_at) -> int:
        if self._anchor is None or anchor_at > self._anchor:
            # Not dirty: the anchor is written with the next rise, and
            # until then nothing has been folded against it.
            self._anchor = anchor_at
        return (self._folded_since(anchor_at)
                + sum(c for prev, _at, c in self._adds if prev >= anchor_at))

    def buckets(self, now) -> list:
        out = [0] * _BUCKETS
        q = protocol.burn_quarter(now)
        for _prev, at, c in self._adds:
            i = _BUCKETS - 1 - (q - protocol.burn_quarter(at)) // 900
            if 0 <= i < _BUCKETS:
                out[int(i)] += c
        return out

    # -- persistence -----------------------------------------------------
    def save(self) -> None:
        """Write the file if anything changed: a rise at most once a
        minute (SAVE_EVERY_S), a new session or an emptied count at once,
        and the first save of a process at once.

        After a refused write, nothing is tried again for SAVE_EVERY_S,
        urgent or not. This runs on every poll, every two seconds, and a
        folder that refuses the file refused it each time: a temp created,
        the whole document dumped into it and the temp removed, forever
        (the final review of 2026-11-04, U8). The count stays in memory
        meanwhile; what a restart in that minute loses is what any unsaved
        rise loses (the module docstring)."""
        if not self._persist or not self._dirty:
            return
        tick = self._clock()
        if (self._failed_at is not None and tick >= self._failed_at
                and tick - self._failed_at < SAVE_EVERY_S):
            return
        if (not self._urgent and self._saved_at is not None
                and tick >= self._saved_at
                and tick - self._saved_at < SAVE_EVERY_S):
            return
        self._write(self._file(), tick)

    def _flush(self):
        """The exit flush (_flush_at_exit): whatever is unwritten, to the
        file the last write went to, pacing and back-off aside."""
        if self._persist and self._dirty and self._written_to is not None:
            self._write(self._written_to, self._clock())

    def _write(self, target, tick):
        doc = {"v": _VERSION, "fp": self._fp, "period": self._period,
               "sessions": self._sessions, "adds": self._adds,
               "fold": self._fold, "anchor": self._anchor}
        try:
            # theme._write_json and not tempfile.mkstemp. On Python 3.11,
            # the interpreter the Windows build ships, mkstemp takes a
            # PermissionError for "that name is taken" and tries the next,
            # up to os.TMP_MAX, about two billion on Windows; a ~/.blink
            # that refuses new files (antivirus, a folder policy) held the
            # serial thread inside this save, pongs stopped and the board
            # went to HOST LOST (the final review of 2026-11-04, U7; the
            # same fault theme._write_json documents from review S4). One
            # os.replace attempt, as session_verdict.save makes, for the
            # same reason: Windows' nine retry sleeps of 0.3 s would hold
            # the serial thread 2.7 s. Its temps are named the way
            # install_statusline.temp_siblings finds them, so a killed
            # write is swept by the next one.
            theme._write_json(target, doc, tries=1)
        except OSError:
            # Kept in memory and tried again after the back-off (save).
            self._failed_at = tick
            return
        self._dirty = self._urgent = False
        self._saved_at = tick
        self._failed_at = None
        self._written_to = target
        _WRITTEN.add(self)
        if not _HOOKED:
            _HOOKED.append(True)
            atexit.register(_flush_at_exit)
