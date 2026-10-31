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
2026-10-31, read-only:

  - a Claude Desktop usage-history sample with a usable `sd` and NO `fh`
    key. The newest sample after the change had exactly that, and the
    samples before it (10-28, 10-29) had `fh` 0 and 1. A key that is
    present and null is not this evidence: absent is the claim.
    (pc/providers/claude_desktop)
  - a Claude Code reply that came back without a five-hour window. On Pro
    and Max every API response carries the five-hour headers, so a reply
    brings five_hour back into the status line; a reply after which it is
    still absent is proof. (pc/providers/claude_cli)

Evidence of a window, any of which clears the mark:

  - a status line with a five-hour window;
  - a Claude Desktop sample whose `fh` key holds a number, 0 included;
  - a Claude Desktop Local Storage frame with a session reading or a reset
    stamp.

Each piece of evidence is dated -- the moment the figure it rests on was
taken -- and the newest wins, a window on a tie. A provider puts it on its
frame as `window_evidence`; this module only weighs it.

THE VERDICT IS PERSISTED (~/.blink/session-window.json), so a restarted
daemon and `blink status` say what the running daemon says (finding 5): with
nothing on disk a fresh process has no evidence, and an Ultra desk would
show its session dial until the next reply. Written atomically, and only
when the verdict changes, which on a desk is once and then never. Read once
per process, on first use, from a path resolved then: tests/conftest.py's
sandbox moves HOME before that, and tests/test_sandbox.py fails on a home
lookup at import.
"""
import json
import math
import os

KIND_STATUSLINE_WINDOW = "statusline_window"
KIND_STATUSLINE_REPLY = "statusline_reply"
KIND_DESKTOP_FH = "desktop_fh"
KIND_DESKTOP_NO_FH = "desktop_no_fh"
KIND_LOCAL_STORAGE = "local_storage"
KINDS = (KIND_STATUSLINE_WINDOW, KIND_STATUSLINE_REPLY, KIND_DESKTOP_FH,
         KIND_DESKTOP_NO_FH, KIND_LOCAL_STORAGE)

STATE_WINDOW = "window"
STATE_NO_WINDOW = "no_window"

# What `blink status` says the verdict rests on, one line each. Sentence
# case, like everything else a customer reads.
DESCRIBE = {
    KIND_STATUSLINE_WINDOW: "Claude Code reported a five-hour window",
    KIND_STATUSLINE_REPLY: "a Claude Code reply came back without one",
    KIND_DESKTOP_FH: "Claude Desktop reported a five-hour figure",
    KIND_DESKTOP_NO_FH: "Claude Desktop reports weekly usage only",
    KIND_LOCAL_STORAGE: "Claude Desktop holds an open five-hour window",
}


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
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    state, kind, at = doc.get("state"), doc.get("evidence"), doc.get("at")
    if state not in (STATE_WINDOW, STATE_NO_WINDOW):
        return None
    if isinstance(at, bool) or not isinstance(at, (int, float)):
        return None
    ev = (state == STATE_WINDOW, kind, float(at))
    return ev if _valid(ev) else None


def save(path, ev) -> None:
    """Persist a verdict: temp file and os.replace, so a reader never sees
    half of one. A write that fails costs the next restart its memory and
    nothing else, so it is not worth an exception."""
    has_window, kind, at = ev
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"state": STATE_WINDOW if has_window
                       else STATE_NO_WINDOW,
                       "evidence": kind, "at": at}, f)
        os.replace(tmp, path)
    except OSError:
        return


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

    def path(self):
        return self._path if self._path is not None else verdict_path()

    def current(self):
        """(has_window, kind, at), or None while there is no evidence."""
        if not self._loaded:
            self._loaded = True
            self._current = load(self.path())
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
            changed = cur is None or ev[0] != cur[0]
            self._current = cur = ev
            if changed and self._persist:
                save(self.path(), ev)
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
