"""Does this Claude account have a five-hour window? Decided on evidence.

Verified Ultra accounts lost the five-hour limit on 2026-10-30. Such an
account must not have its session dial filled by a borrowed reading (a
Claude Desktop sample two days old drew "1%, 37 hours old" over live data),
and its board shows the five-hour burn view instead (docs/plans/burn-5h.md).
Pro and Max must keep their dial, at a reset and after it.

THE RULE: the account is marked as having no window only on positive
evidence that it has none, and the mark goes on positive evidence that it
has one. Quiet data proves nothing. The rule this replaced inferred "no
window" from a weekly-only status line whose window had not been seen for
five hours and ten minutes, and quiet is exactly what a Pro or Max desk is
after a reset: a laptop asleep across one, a 40-day-old status line read at
start, a fresh `blink status`, two terminals painting different blocks --
each read as Ultra (review, 2026-10-31, findings 2, 3, 5 and 10).

Evidence of NO window, measured on this desk's verified Ultra account on
2026-10-31, read-only, and argued against Pro and Max:

  - a Claude Desktop usage-history sample with NO `fh` key whose `sd`
    rose strictly since the sample just before it, the two one poll apart
    (DESKTOP_RISE_MAX_GAP_S), and that earlier sample with no `fh` key
    either. The newest sample after the change had `sd` and no `fh`, and
    the samples before it (10-28, 10-29) had `fh` 0 and 1; that alone was
    proof until the owner's decision of 2026-11-01, since a Pro or Max
    account idle past its window may write the same. A key that is present
    and null is not this evidence: absent is the claim.
    (pc/providers/claude_desktop, _no_window_rise)
  - Claude Code replying, over more than five hours, without a five-hour
    window ever being rendered: replies with no five_hour, none more than
    an hour after the one before, the first and the latest more than a
    window apart, and no five_hour seen in the status line in between.
    One reply or two is not proof -- a request streaming across a reset,
    or a local clock running fast, does that on Pro and Max (the
    re-review of 2026-11-01). (pc/providers/claude_cli,
    _reply_without_window)
  - on an account whose Claude Code config (.claude.json) names the Ultra
    plan, ONE such reply: Pro and Max never take this path, since their
    plan is not Ultra (owner's request, 2026-11-01; pc/claude_plan).

Evidence of a window, any of which clears the mark:

  - a status line with a five-hour window;
  - a Claude Desktop sample whose `fh` key holds a number, 0 included;
  - a Claude Desktop Local Storage frame with a session reading or a reset
    stamp.

Each piece of evidence is dated -- the moment the figure it rests on was
taken, or for a status line the moment it was rendered -- and the newest
wins, a window on a tie. A provider puts it on its
frame as `window_evidence`; this module only weighs it.

THE VERDICT IS PERSISTED (~/.blink/session-window.json), so a restarted
daemon and `blink status` say what the running daemon says (finding 5): with
nothing on disk a fresh process has no evidence, and an Ultra desk would
show its session dial until it had replied for five more hours. Written atomically, and only
when the verdict changes, which on a desk is once and then never. Read once
per process, on first use, from a path resolved then: tests/conftest.py's
sandbox moves HOME before that, and tests/test_sandbox.py fails on a home
lookup at import.
"""
import json
import math
import os

KIND_STATUSLINE_WINDOW = "statusline_window"
# The two no-window kinds were renamed when their rules were (the re-review
# of 2026-11-01): "statusline_reply" was one reply, then two, and
# "desktop_no_fh" one sample with no `fh`, and a Pro or Max desk could earn
# either. A verdict saved under an old name -- the owner's own desk had
# {"evidence": "desktop_no_fh"} on disk, and builds of 592efa7 wrote
# statusline_reply -- is no longer a kind, so load() reads it as no verdict
# and the account is decided again, by the rules below. Never reuse a name
# for a different rule: the name on disk is what says which rule made it.
KIND_STATUSLINE_SUSTAINED = "statusline_sustained"
# One weekly-only reply on an account whose Claude Code config names the
# Ultra plan (pc/claude_plan; owner's request, 2026-11-01). A name of its
# own, not statusline_sustained's: the rules differ, and the name on disk
# is what says which rule made the verdict.
KIND_STATUSLINE_ULTRA = "statusline_ultra"
KIND_DESKTOP_FH = "desktop_fh"
KIND_DESKTOP_RISE = "desktop_rise"
KIND_LOCAL_STORAGE = "local_storage"

STATE_WINDOW = "window"
STATE_NO_WINDOW = "no_window"

# What `blink status` says the verdict rests on, one line each. Sentence
# case, like everything else a customer reads.
DESCRIBE = {
    KIND_STATUSLINE_WINDOW: "Claude Code reported a five-hour window",
    KIND_STATUSLINE_SUSTAINED: ("Claude Code replied for over five hours"
                                " without one"),
    KIND_STATUSLINE_ULTRA: ("Claude Code reports an Ultra plan, and its"
                            " replies come back without one"),
    KIND_DESKTOP_FH: "Claude Desktop reported a five-hour figure",
    KIND_DESKTOP_RISE: ("Claude Desktop's weekly usage rose with no"
                        " five-hour figure before or after"),
    KIND_LOCAL_STORAGE: "Claude Desktop holds an open five-hour window",
}
# Every kind evidence can carry. Taken from DESCRIBE rather than restated
# beside it: a kind written into one list and not the other had its evidence,
# and a verdict saved on it, dropped as invalid without a word (the
# /code-review max of 2026-11-01). A tuple, not a frozenset: _valid asks
# `ev[1] in KINDS` of whatever a frame carries, and a frozenset raises
# TypeError on an unhashable value where a tuple just says no.
KINDS = tuple(DESCRIBE)

# How long after a failed write the verdict is written again. See
# SessionVerdict.observe.
SAVE_RETRY_S = 60


def verdict_path() -> str:
    """Resolved when called, never at import (see the module docstring)."""
    return os.path.join(os.path.expanduser("~"), ".blink",
                        "session-window.json")


def evidence(has_window, kind, at):
    """The tuple a frame carries as window_evidence."""
    return (bool(has_window), kind, float(at))


def _valid(ev):
    return (isinstance(ev, tuple) and len(ev) == 3
            and isinstance(ev[0], bool) and ev[1] in KINDS
            and isinstance(ev[2], float) and math.isfinite(ev[2]))


def load(path):
    """The persisted verdict as an evidence tuple, or None. Absence and
    corruption are both normal: a fresh install, a half-restored backup."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
    except (OSError, ValueError, RecursionError):
        # RecursionError: json's decoder raises it on an array nested a few
        # thousand deep, and `blink status` died on one with a traceback
        # (the /code-review max of 2026-11-01, finding 12).
        return None
    if not isinstance(doc, dict):
        return None
    state, kind, at = doc.get("state"), doc.get("evidence"), doc.get("at")
    if state not in (STATE_WINDOW, STATE_NO_WINDOW):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    try:
        at = float(at)
    except OverflowError:       # an int of 309 digits or more
        return None
    ev = (state == STATE_WINDOW, kind, at)
    return ev if _valid(ev) else None


def save(path, ev) -> bool:
    """Persist a verdict; True when it is on disk.

    Through pc/theme's _write_json: a unique temp name, and the temp removed
    when the write fails. This used a fixed `.tmp` name, so a refusal left
    the temp beside it (the /code-review max of 2026-11-01, finding 7).

    ONE attempt at the os.replace, not _write_json's ten on Windows' WinError
    5 and 32 with 0.3 s between them. observe() runs on the bridge's serial
    thread, and on a Windows desk whose session-window.json carried the
    read-only attribute every retry slept 2.7 s there, once a minute, with
    pongs and usage lines waiting behind it (the re-review of 2026-11-01,
    finding 14). A failure is not worth an exception or a wait: it costs the
    next restart its memory and nothing else, and observe() tries again a
    SAVE_RETRY_S later.
    """
    has_window, kind, at = ev
    from pc import theme        # here: the daemon's tick does not need it
    try:
        theme._write_json(path, {"state": STATE_WINDOW if has_window
                                 else STATE_NO_WINDOW,
                                 "evidence": kind, "at": at}, tries=1)
    except OSError:
        return False
    return True


def newest(frames):
    """The newest evidence any of `frames` carries, or None. A window wins a
    tie: the mark costs a Pro or Max desk its dial, so it waits for proof
    that is newer than the proof against it."""
    best = None
    for f in frames:
        ev = getattr(f, "window_evidence", None)
        if not _valid(ev):
            continue
        if best is None or (ev[2], ev[0]) > (best[2], best[0]):
            best = ev
    return best


class SessionVerdict:
    """The account's verdict for one process: what is on disk, overruled by
    anything newer the providers report."""

    def __init__(self, path=None, persist=True):
        # None: verdict_path(), asked at first use, not here.
        self._path = path
        self._persist = persist
        self._current = None
        self._loaded = False
        # The state on disk as this process last knew it, (has_window,
        # kind), or None for no file; and when a write last failed (None:
        # it has not). The kind as well as the state: an Ultra desk marked
        # first on its plan file (KIND_STATUSLINE_ULTRA) and then by the
        # sustained chain is the same state on a different proof, and
        # `blink status`, which reads the file, names the proof (the
        # review of PR #6, 2026-11-01).
        self._on_disk = None
        self._failed_at = None

    def path(self):
        return self._path if self._path is not None else verdict_path()

    def current(self):
        """(has_window, kind, at), or None while there is no evidence."""
        if not self._loaded:
            self._loaded = True
            self._current = load(self.path())
            self._on_disk = (None if self._current is None
                             else self._current[:2])
        return self._current

    def observe(self, frames, now_epoch):
        """Weigh what `frames` say against the verdict; return True when the
        account is to be treated as having no window."""
        cur = self.current()
        if cur is not None and cur[2] > now_epoch:
            # Dated ahead of the clock -- a clock since wound back, a file
            # from another machine -- it would outrank every piece of fresh
            # evidence for as long as the clock took to catch up. It keeps
            # its say until any evidence at all arrives, and no longer.
            cur = self._current = (cur[0], cur[1], 0.0)
        ev = newest(frames)
        if ev is not None and (cur is None
                               or (ev[2], ev[0]) > (cur[2], cur[0])):
            self._current = cur = ev
        # Written when the verdict or the kind of its proof differs from
        # what is on disk, which on a desk is once or twice and then never;
        # a newer proof of the same kind is not written. Not only on the
        # poll where it changes:
        # a write that failed then is tried again, at most once a
        # SAVE_RETRY_S, until it lands -- or at once if the clock has gone
        # back past the failure, the arm ingest._cooldown_over and
        # BurnReader.poll have: gated on `now >= failed + 60` alone, an NTP
        # step back of an hour held the retry off for that hour, and a
        # restart in it read the superseded verdict (the re-review of
        # 2026-11-01, finding 15).
        if (self._persist and cur is not None and cur[:2] != self._on_disk
                and (self._failed_at is None
                     or not 0 <= now_epoch - self._failed_at < SAVE_RETRY_S)):
            if save(self.path(), cur):
                self._on_disk = cur[:2]
                self._failed_at = None
            else:
                self._failed_at = now_epoch
        return cur is not None and not cur[0]

    def mark(self, frames, now_epoch):
        """Observe `frames`' claude evidence, then mark the claude frames.
        The mark only ever turns on here, so a frame a scenario marked
        itself (pc/providers/scripted) keeps its mark."""
        claude = [f for f in frames if f.provider == "claude"]
        if self.observe(claude, now_epoch):
            for f in claude:
                f.no_session_window = True
        return frames
