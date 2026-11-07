"""The running count of spend between /usage runs.

/usage saves an exact anchor; between runs the only live signal is the
status line's per-session `cost.total_cost_usd`, a cumulative figure that
each Claude Code process renders on its own. This turns those samples into
counted cents.

Rules, each from a way the raw number misleads:

* A session's first sighting adds nothing. A resumed session shows its whole
  past cost on the first render, and that is not new spend.
* Only a rise over the session's high-water mark counts, and only a rise
  moves the mark's stamp. Two processes on one session id -- `claude
  --continue` in a second terminal -- show different totals; a lower
  figure from one of them is never counted, and an equal one does not
  restamp the mark.
* A rise is stamped with the sighting that set the mark it is counted
  against. `since(anchor)` counts only rises stamped at or after the
  anchor: a rise from a mark set before the anchor may have happened
  partly before /usage measured, so it is dropped rather than counted
  twice. For a /usage on a WSL desk the anchor is moved SKEW_S later
  (since_usage), because its time and a render's come from clocks that can
  disagree. A clock step is met in observe(): a render dated in the future
  is taken as made now, and a rise dated before its own mark counts toward
  the month only.
* `at` is the render's own time (the payload's mtime), so reading the same
  render again moves nothing.

* A cost below zero is no cost. Claude Code does not write one; a damaged
  payload can, and a negative "high-water mark" would count the next real
  figure twice over.

Why one mark, and not one per ledger. Each Claude Code process keeps its
own cost ledger, and /login starts a process's again from zero under the
same session id (2.1.318, not yet measured on a desk), so a sign-out and sign-in to
the same seat -- same id, same fingerprint -- counts nothing until the
session passes its old total: $14.34 shown for $16.84 in U9 of the final
review of 2026-11-04. Rounds 2 and 3 of that review tried a mark per
ledger (K1, R1) and each cut counted too much across a /usage, because
the status line carries no process id: a figure landing on another
ledger's mark took that mark's stamp past the /usage while the ledger it
stood for, its renders lost, had already moved (55 cents counted for
about 15; an equal 0.00 from a new process restamping another's mark, 40
for 10). The cut that stopped that stamped every rise with the oldest mark
under it, and one idle `--continue` terminal then held a session's count
since the /usage at zero for the rest of the month. With one mark a lower
ledger is never counted, so none of that can happen; what it costs is
undercount, which the next /usage corrects:
  - a lower ledger's own rises: a /login's until the session passes its
    old total (U9), a second terminal's for as long as it stays below;
  - the first rise after a /usage, and, since an equal figure does not
    restamp, a rise after a render that showed no change.
tests/pc/test_spend_count.py drives random lives -- up to four processes,
/logins, exits, resumes (two may start from one saved total), 30% of
renders lost, a /usage between every two steps. At 3,000 lives (900,000
(life, /usage) pairs): this rule counted too much 0 times and covered
21.3% of the spend after a /usage; the per-ledger cut of round 2 counted
too much 35,178 times; the single mark as it was before round 3, which
an equal or lower figure restamped, 139 times (U10: a lagging process's
figure, equal to the mark, stamped it after a /usage and the leader's
next rise was counted whole, 30 cents for 20) and covered 21.9%.

What it cannot rule out, kept: a process whose renders are lost while it
climbs past the mark before a /usage, and that is next seen above the
mark after one set after the /usage, is counted from that mark although
part of its climb came first. At 10,000 lives (3,000,000 pairs) it came
up 3 times, the worst 1724 cents counted for 1708 and, in relative terms,
89 for 71. The status line carries no process id, so only a rule that
counts nothing after a /usage while two processes run could rule it out.

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
* Each session's mark, so a resumed session's past cost is never counted
  as new: its highest cost estimate and the time of the render that first
  showed that amount -- the session's first render, or the one where it
  last rose. A render showing the same amount again does not move it (an
  equal figure may be another process's). These are kept until the month
  turns or the login
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
  held back (flush_all: at exit, in the daemon's SIGTERM handler, and before
  a self-update's restart).
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
import sys
import time

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
# How far after a /usage a rise's previous sighting must be for the rise to
# count toward it (since). The two times come from different clocks: a
# render's is the status-line file's mtime, and on a WSL desk that file is
# written to the Windows side and dated by the Windows clock, while the
# /usage time is Date.now() inside the distribution, and the two can
# disagree. A render made before the /usage but dated after it has its rise
# counted whole although part of it is in the /usage figure. How far they
# disagree has not been measured: no checklist or test compares Windows'
# clock with the distribution's yet (until the review of the WSL work,
# 2026-11-05, this comment pointed at a Plan B checklist item that does not
# exist). Two minutes until it is. That errs in the direction the count is
# allowed to err only while the distribution's clock is within two minutes
# of Windows': what it costs then is the rises whose previous sighting
# falls in the two minutes after a /usage, left to the next one, and the
# page says it is an estimate while there are any (rose_after). Past two
# minutes of disagreement a render made before the /usage can pass the
# margin and its rise is counted whole -- an overcount, until the next
# /usage -- and a distribution clock far enough ahead dates every /usage
# into the future, where claude_cli refuses it (SPEND_FUTURE_S). Round 5
# of the 2026-11-04 review took the margin out for claiming "exact" over
# those rises; round 6 put it back with that fixed. Applied by
# since_usage, which claude_cli asks with a /usage time on a WSL home
# only: a native desk dates both by one clock (final review of round 7,
# F1). observe() also uses it as the tolerance before a render's date is
# called a fast clock's.
SKEW_S = 120
# 2: the carried entry and the newest anchor asked about. A version-1 file
# (every rise one by one, from before 2026-11-04) is read as empty, as any
# file this cannot read is: it held an estimate, which the next /usage
# makes exact.
_VERSION = 2
# A session is [mark, seen]. Round 2 of the 2026-11-04 review's builds,
# never released, wrote several pairs, one a cost ledger; such a file is
# read as empty, like any other this cannot believe (round 5, ruling D).


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


# Every counter that has written its file and not been closed, for the exit
# flush (U12 in the final review of 2026-11-04). Held strongly. It was a
# WeakSet, so that a counter dropped by its owner would not be kept alive,
# and that emptied it at the one moment it is read: the daemon's counter is
# reachable only from claude_usage_bridge.main()'s frame, and CPython frees
# that frame -- clearing the SIGTERM's SystemExit and its traceback --
# before atexit runs, so the flush found nothing and the held-back rise was
# lost (the review of the WSL work, 2026-11-05, with the daemon's own
# objects). An owner that drops a counter calls close(). Registered with
# atexit on the first write rather than at import: importing a module of
# ours does nothing (CLAUDE.md).
_LIVE = set()
_HOOKED = []


def flush_all():
    """Write what the pacing held back, for every live counter.

    Called at an ordinary exit (atexit), from the daemon's SIGTERM handler
    before its SystemExit unwinds anything (claude_usage_bridge), and by
    update.restart_from_daemon before its os._exit, which skips atexit
    altogether. Each counter writes to the file it last wrote and nowhere
    else: by exit the test sandbox has handed HOME back, and resolving the
    default path again then would name the real ~/.blink. A failure is
    ignored; there is no later save to hand it to."""
    for counter in list(_LIVE):
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
        # False while the daemon's bus is the interim one: the file is
        # loaded, and nothing is written over it until the record settles
        # (ingest.IngestionBus.allow_writes).
        self.writable = True
        self._clock = clock
        self._saved_at = None  # self._clock() at the last write
        self._failed_at = None  # self._clock() at the last refused write
        self._written_to = None  # the file the last write went to
        self._fp = None
        self._period = None
        # sid -> [highest cost estimate in cents, the time of the render
        # that first showed it: the session's first, or its last rise]
        self._sessions = {}
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
        # Possibly past now, from a file saved under a fast clock; the
        # first observe() that is told the time puts it back (see there).
        self._latest = max(stamps, default=None)

    def bind(self, account_fp, period_start) -> None:
        if self._fp is None and self._period is None:
            # Nothing stored or loaded: adopt without discarding what was
            # observed before the first look.
            self._fp, self._period = account_fp, period_start
            self._dirty = self._urgent = True
            return
        if account_fp != self._fp or period_start != self._period:
            # Emptied first, named after. The exit flush (_flush) can save
            # whatever state an exit leaves half done, and this order
            # leaves either the old login's name, which the next start
            # empties again, or an emptied count -- never the old count
            # under the new name (K4, round 2 of the 2026-11-04 review).
            self._clear()
            self._fp, self._period = account_fp, period_start

    def bound(self) -> bool:
        """Bound to a seat billed by usage, now or in the file it loaded:
        bind() is only called for one."""
        return self._fp is not None

    def forget_sessions(self) -> None:
        """Drop every session's high-water mark, keeping what was counted.

        For a login switch (claude_cli's _note_login): Claude Code keeps a
        session's id across /login and starts its cost from zero, so a mark
        from before the switch would swallow every rise after it. Each
        session's next sighting is then a first one and adds nothing.
        Written at once, like an emptied count: a restart that reloaded
        the old marks would undercount again."""
        if self._sessions:
            self._sessions = {}
            self._dirty = self._urgent = True

    # -- counting --------------------------------------------------------
    def observe(self, session_id, cost_usd, at, now=None) -> bool:
        """Count one render's figure; True when the state changed.

        `now`, the poll's wall clock when the caller has one, is what
        keeps a clock step from bending the count (the review of the WSL
        work, 2026-11-05). A render dated past now + SKEW_S was made by a
        clock that has since been put back, and is taken as made now:
        unclamped, one render dated two days ahead became the newest render
        seen, every later rise was folded at once and the burn view stayed
        empty until real time caught up. A rise whose render is dated
        before the mark it rose from (the clock went back between the two)
        cannot be placed against a /usage on the clock that now reads: it
        is stamped with the period start, so it counts toward the month and
        toward no /usage. Stamped with its mark, as any other rise is, it
        was counted on top of a /usage that already held it (a clock 30
        minutes fast, put right: since() = 30 cents, truth 0).

        Ordered for the exit flush (_flush): the daemon's SIGTERM raises
        SystemExit between any two bytecodes of the main thread, the one
        that polls, and the flush saves whatever state that leaves. Each
        step here leaves one that counts no more than the truth: the
        session's mark is replaced whole, in one store, before the rise
        it covers is appended, so an exit between the two loses that one
        rise rather than saving it against the old mark. Saved the other
        way round, the restart counted it again from there (K4, round 2
        of the 2026-11-04 review). The first fix skipped the flush while
        an update was in flight, which lost every other unsaved rise with
        it (R3) and, its flag cleared only on success, every flush after
        an ordinary exception (R2)."""
        # The counter owns the guard: a NaN, infinity, string or bool from
        # a damaged payload must not touch the state.
        # And a cost past MAX_CENTS or a time that is not one: see there.
        if (not _num(cost_usd) or not _time(at) or cost_usd < 0
                or cost_usd > MAX_CENTS / 100):
            return False
        cents = int(round(cost_usd * 100))
        if now is not None and _num(now):
            if at > now + SKEW_S:
                at = now
            if self._latest is not None and self._latest > now + SKEW_S:
                self._latest = now
        mark = self._sessions.get(session_id)
        if mark is None:
            self._sessions[session_id] = [cents, at]
            if self._latest is None or at > self._latest:
                self._latest = at
            self._dirty = self._urgent = True
            return True
        hw, stamp = mark
        if cents <= hw:
            # Lower: another ledger, a resume or a /login (the module
            # docstring). Equal: no news, and possibly another ledger's
            # figure, so not a sighting of this mark's ledger either.
            return False
        if at < stamp:
            # Dated before its own mark: see the docstring above.
            stamp = self._period if self._period is not None else 0
        # The new mark first, in one store, then the rise it covers: see
        # the docstring above.
        self._sessions[session_id] = [cents, at]
        self._adds.append([stamp, at, cents - hw])
        if self._latest is None or at > self._latest:
            self._latest = at
        self._compact()
        self._dirty = True
        return True

    def _compact(self):
        """Fold every rise older than _RECENT_S into the carried entry."""
        cutoff = self._latest - _RECENT_S
        old = [a for a in self._adds if a[1] < cutoff]
        if not old:
            return
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
        else:
            fold = list(fold)
        for prev, at, c in old:
            if prev >= boundary:
                fold[1] += c
            fold[2] += c
            fold[3] = min(fold[3], prev)
            fold[4] = max(fold[4], at)
        # Built aside and stored last, the rises taken out before the fold
        # that carries them goes in: an exit between the two stores (the
        # exit flush, observe's docstring) loses them rather than saving
        # them twice.
        self._adds = [a for a in self._adds if a[1] >= cutoff]
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

    def since(self, anchor_at, now=None) -> int:
        """The cents counted after the /usage made at `anchor_at`.

        `now`, the poll's wall clock: at a new anchor, a rise stamped later
        than it (past SKEW_S, observe's own allowance) was recorded under a
        clock since put back, before this /usage, which already holds it. It is dropped, and a mark stamped
        so is moved to now, so the next rise is placed on the clock that
        reads. Counted, the clock 30 minutes fast and put right showed
        $10.60 for $10.30 for the rest of the month (verify/v8/c21, item 13
        of the /code-review max of 277b81a..4003b0b)."""
        if self._anchor is None or anchor_at > self._anchor:
            # Not dirty: the anchor is written with the next rise, and
            # until then nothing has been folded against it.
            self._anchor = anchor_at
            if now is not None and _num(now):
                self._drop_ahead_of(now + SKEW_S, now)
        return (self._folded_since(anchor_at)
                + sum(c for prev, _at, c in self._adds if prev >= anchor_at))

    def _drop_ahead_of(self, bound, now):
        kept = [a for a in self._adds if a[0] <= bound]
        if len(kept) != len(self._adds):
            self._adds = kept
            self._dirty = True
        if self._fold is not None and self._fold[3] > bound:
            self._fold = None           # every rise in it is ahead of now
            self._dirty = True
        for sid, (hw, stamp) in list(self._sessions.items()):
            if stamp > bound:
                self._sessions[sid] = [hw, now]
                self._dirty = True
        if self._latest is not None and self._latest > bound:
            self._latest = now

    def rose_after(self, t) -> bool:
        """Whether any rise was seen in a render after `t`, counted toward
        an anchor or not: a page that says "exact" with one, dropped by
        the straddling rule or SKEW_S, would be claiming what it did not
        count (round 6 of the 2026-11-04 review, item 6)."""
        if self._fold is not None and self._fold[4] > t:
            return True
        return any(at > t for _prev, at, _c in self._adds)

    def since_usage(self, fetched_at, now=None) -> int:
        """since() for a /usage made at `fetched_at`: counted from SKEW_S
        after it, for the clocks the two times come from. The period start
        needs no such margin -- bind() empties the count there, so a rise
        dated into the month that was made before it is counted once, in
        the month it is dated to."""
        return self.since(fetched_at + SKEW_S, now)

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
        if not self._persist or not self.writable or not self._dirty:
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

    def close(self) -> None:
        """Write what is held back and leave the exit flush's registry.
        For an owner done with this counter; one that never closes it is
        flushed at exit."""
        try:
            self._flush()
        finally:
            _LIVE.discard(self)

    def _flush(self):
        """The exit flush (flush_all): whatever is unwritten, to the
        file the last write went to, pacing and back-off aside. An exit
        can land in the middle of observe() or bind(); both are ordered so
        that what it leaves is safe to save (see observe)."""
        if (self._persist and self.writable and self._dirty
                and self._written_to is not None):
            self._write(self._written_to, self._clock())

    def _write(self, target, tick):
        doc = {"v": _VERSION, "fp": self._fp, "period": self._period,
               "sessions": self._sessions,
               "adds": self._adds,
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
        except OSError as e:
            # Kept in memory and tried again after the back-off (save). Said
            # once per streak, by type: a ~/.blink that refuses writes lost
            # the count at every restart with nothing in the log (review of
            # PR #8, 2026-11-06).
            if self._failed_at is None:
                print(f"[spend] count not saved ({type(e).__name__})",
                      file=sys.stderr)
            self._failed_at = tick
            return
        self._dirty = self._urgent = False
        self._saved_at = tick
        self._failed_at = None
        self._written_to = target
        _LIVE.add(self)
        if not _HOOKED:
            _HOOKED.append(True)
            atexit.register(flush_all)
