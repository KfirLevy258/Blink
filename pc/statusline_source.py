"""Read Claude Code's statusline payload and map it to a board usage message.

This project handles no Anthropic credential. Claude Code owns the credential
and hands us the two numbers it has already computed; we read a file it wrote.
There is nothing here to authenticate with, and nothing to leak.

The payload is PUSHED by a running Claude Code process, so it can be absent or
old. Old is reported as `stale` rather than hidden -- a frozen meter presented
as live is the one failure mode worth designing against.
"""
import json
import os
import time

from pc import protocol

PAYLOAD_PATH = os.path.expanduser("~/.clauge/statusline.json")
# How old the payload may get before we stop vouching for it.
#
# This was 120 s, and 120 s was wrong in kind, not just in value. Claude Code
# writes this file only when it RENDERS its status line, which happens when
# the user is doing something. So the age of the file measures how long the
# person has been idle -- not how wrong the numbers are. On a real desk it
# made the panel flap amber/green every few minutes: observed alternating
# STALE -> usage -> STALE -> usage while its owner simply paused to read.
#
# The numbers do not rot with wall-clock time. They go wrong two ways, and
# both are checked below:
#   - the window they describe resets, after which the reading is definitely
#     wrong (usage went back to near zero and this file still says 47%).
#     That is `_window_has_reset`, and it is exact rather than a guess.
#   - usage happens somewhere we cannot see -- claude.ai, the phone app --
#     so an old reading can understate the truth. Nothing detects that, so
#     this bound stays as the backstop. Half an hour is long enough that
#     ordinary pauses are silent, short enough that a genuinely abandoned
#     Claude Code is not presented as live.
STALE_AFTER_S = 1800


def _window(rate_limits: dict, key: str):
    """(used_percentage, resets_at_epoch|None) for one window.

    Claude Code documents each window as individually optional -- a session
    can report five_hour with no seven_day, or neither. A window that is
    entirely absent returns pct=-1.0, the same "unknown" sentinel already
    used below for *_resets_in_s, rather than 0.0: 0.0 reads as a confident
    "0% used", which is a stronger claim than "we don't have this number" and
    is exactly the frozen-meter failure mode this module exists to avoid.
    """
    w = rate_limits.get(key)
    if not isinstance(w, dict):
        return -1.0, None
    try:
        pct = float(w.get("used_percentage", 0.0))
    except (TypeError, ValueError):
        pct = 0.0
    resets = w.get("resets_at")
    return pct, resets if isinstance(resets, (int, float)) else None


def _secs_until(resets_at, now_epoch: float) -> int:
    """Seconds until `resets_at`. -1 when unknown.

    -1 rather than 0 for missing input: 0 renders as "resets now", which is a
    confident lie. -1 lets the display say "--".
    """
    if resets_at is None:
        return -1
    return max(0, int(resets_at - now_epoch))


def _window_has_reset(resets_at, now_epoch: float) -> bool:
    """True once the window this reading describes has rolled over.

    At that moment the percentage is not merely old, it is wrong: usage went
    back to near zero and the file still says whatever it said. Age cannot
    detect this -- a reading taken one minute before a reset is stale the
    moment the reset lands -- so it is checked directly.
    """
    return resets_at is not None and now_epoch >= resets_at


def _rolled_over(pct: float, resets_at, now_epoch: float):
    """Carry a window across its own reset instead of disowning the reading.

    A window that has just reset is at 0%. That is not an estimate -- it is
    what resetting means -- so there is a better answer available than "this
    number may be wrong", which is what marking the message stale says.

    It matters because the payload is only rewritten when Claude Code renders
    its status line. Between a reset at 03:00 and its owner sitting down at
    09:00, the old behaviour left the panel amber for six hours, announcing a
    problem that did not exist. Observed 2026-08-22: a five-hour window rolled
    over, the board flagged the whole message stale, and the weekly figure --
    which was perfectly good -- was dragged down with it.

    The reset time goes back to unknown rather than being guessed forward: the
    next five-hour window does not start until the next message, so there is
    no honest number to put there until Claude Code tells us one.

    Only ever called on a payload that is otherwise fresh. On an old one the
    same reasoning inverts -- a three-day-old file has a long-past resets_at
    and any amount of usage may have happened since, so 0% would be the lie.
    """
    if pct < 0 or not _window_has_reset(resets_at, now_epoch):
        return pct, resets_at
    return 0.0, None


def map_statusline(payload: dict, now_epoch: float, mtime_epoch: float) -> dict:
    """Convert a statusline payload into a 'usage' protocol message."""
    rate_limits = payload.get("rate_limits") or {}
    session_pct, session_resets = _window(rate_limits, "five_hour")
    weekly_pct, weekly_resets = _window(rate_limits, "seven_day")

    # Staleness is age, and only age. A window resetting used to set this too,
    # which conflated two different facts: "we cannot vouch for this reading"
    # and "this reading has been superseded by a zero we can compute". The
    # second has a real answer, so it gets one -- see _rolled_over().
    stale = (now_epoch - mtime_epoch) > STALE_AFTER_S
    if not stale:
        session_pct, session_resets = _rolled_over(session_pct, session_resets,
                                                   now_epoch)
        weekly_pct, weekly_resets = _rolled_over(weekly_pct, weekly_resets,
                                                 now_epoch)

    # No per-model rows: the statusline payload has no per-model breakdown.
    return protocol.usage(
        session_pct, "", weekly_pct, "", [],
        session_resets_in_s=_secs_until(session_resets, now_epoch),
        weekly_resets_in_s=_secs_until(weekly_resets, now_epoch),
        stale=stale,
    )


def read_payload(path: str = PAYLOAD_PATH):
    """(payload, mtime) or (None, None) when absent/unreadable/malformed."""
    try:
        mtime = os.path.getmtime(path)
        with open(path) as f:
            return json.load(f), mtime
    except (OSError, ValueError):
        return None, None


def make_fetch(path: str = PAYLOAD_PATH):
    """Zero-arg callable for Bridge(fetch_usage=...). None when no data yet."""
    def fetch():
        payload, mtime = read_payload(path)
        if payload is None:
            return None
        return map_statusline(payload, time.time(), mtime)
    return fetch
