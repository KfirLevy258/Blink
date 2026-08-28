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
from pc.providers import base

# Who we are on the ingestion bus. The board and the normalizer both key off
# these, so they are named once here rather than spelled at each call site.
PROVIDER_ID = "claude"
SRC_ID = "cli"

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
        pct = float(w["used_percentage"])
    except (KeyError, TypeError, ValueError):
        # Absent, null, or not a number -- all three are "we don't have it",
        # and all three used to land on 0.0 here, which is the confident zero
        # the paragraph above says this function exists to avoid. A window
        # object present but missing its percentage is the likeliest shape of
        # a payload change, so it is the case worth getting right.
        pct = -1.0
    resets = w.get("resets_at")
    return pct, resets if isinstance(resets, (int, float)) else None


# _secs_until moved to protocol.secs_until when a second provider needed the
# same countdown. The reasoning that used to live here -- why -1 rather than 0,
# both for missing input and for an already-past reset -- moved with it.
_secs_until = protocol.secs_until


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
        return pct, resets_at, None
    # The third value is the epoch the window EMPTIED, which is exactly the
    # resets_at we are discarding. It used to be thrown away, and that was the
    # hole: this frame's own observed_at is the payload's mtime, which can be
    # long before the reset, so downstream had no way to tell that a NEWER
    # reading from another source was nonetheless taken before the window
    # rolled. pc/normalizer needs this to refuse that reading.
    return 0.0, None, resets_at


def map_statusline_frame(payload: dict, now_epoch: float,
                         mtime_epoch: float):
    """Convert a statusline payload into a NormalizedUsageFrame.

    The real body of this module. map_statusline() below is the same thing
    rendered straight to a protocol message, kept because it is what the
    existing tests pin and what a single-provider daemon needed.
    """
    rate_limits = payload.get("rate_limits")
    # An object, or nothing. The file is written by a shell shim from
    # whatever Claude Code sent; a string or a list here reached .get() and
    # took this source off the bus for the rest of the process.
    if not isinstance(rate_limits, dict):
        rate_limits = {}
    session_pct, session_resets = _window(rate_limits, "five_hour")
    weekly_pct, weekly_resets = _window(rate_limits, "seven_day")

    # Staleness is age, and only age. A window resetting used to set this too,
    # which conflated two different facts: "we cannot vouch for this reading"
    # and "this reading has been superseded by a zero we can compute". The
    # second has a real answer, so it gets one -- see _rolled_over().
    stale = (now_epoch - mtime_epoch) > STALE_AFTER_S
    session_rolled = weekly_rolled = None
    if not stale:
        session_pct, session_resets, session_rolled = _rolled_over(
            session_pct, session_resets, now_epoch)
        weekly_pct, weekly_resets, weekly_rolled = _rolled_over(
            weekly_pct, weekly_resets, now_epoch)

    return base.NormalizedUsageFrame(
        provider=PROVIDER_ID, src=SRC_ID, observed_at=mtime_epoch,
        session_rolled_at=session_rolled, weekly_rolled_at=weekly_rolled,
        session_pct=session_pct, session_resets_at=session_resets,
        weekly_pct=weekly_pct, weekly_resets_at=weekly_resets,
        stale=stale,
    )


def map_statusline(payload: dict, now_epoch: float, mtime_epoch: float) -> dict:
    """Convert a statusline payload into a 'usage' protocol message."""
    # No per-model rows: the statusline payload has no per-model breakdown.
    return protocol.frame_to_usage(
        map_statusline_frame(payload, now_epoch, mtime_epoch), now_epoch)


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
