"""Read Claude Code's statusline payload and map it to a board usage message.

This project handles no Anthropic credential. Claude Code owns the credential
and hands us the two numbers it has already computed; we read a file it wrote.
There is nothing here to authenticate with, and nothing to leak.

The payload is PUSHED by a running Claude Code process, so it can be absent or
old. Old is reported as `stale` rather than hidden -- a frozen meter presented
as live is the one failure mode worth designing against.
"""
import json
import math
import os

from pc import protocol
from pc.providers import base

# Who we are on the ingestion bus. The board and the normalizer both key off
# these, so they are named once here rather than spelled at each call site.
PROVIDER_ID = "claude"
SRC_ID = "cli"


def payload_path() -> str:
    """Where the shim writes the payload, resolved now rather than at import.

    This was a module constant, expanduser'd when the module was imported,
    and the readers here took it as a default argument -- all of it
    evaluated before tests/conftest.py redirects HOME. So in a test process
    the no-argument readers named the developer's REAL
    ~/.blink/statusline.json, which on a desk with Blink installed holds live
    usage: the sandbox hole CLAUDE.md describes. ClaudeCliProvider had already
    stopped using the constant for the same reason (2026-10-19);
    gapietro's fork is where the readers' half turned up. The constant is
    gone rather than kept beside this, so nothing can reach the
    import-time value again, and tests/test_sandbox.py fails on any module-scope home lookup under pc/.

    The ONE place the path is spelled in Python: the provider, `blink
    status` and `blink uninstall` all ask here. Only the shell shim
    (tools/blink-statusline.sh) writes it out separately, because it cannot
    import this.
    """
    return os.path.expanduser("~/.blink/statusline.json")


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
#     That is base.window_ended, and it is exact rather than a guess.
#   - usage happens somewhere we cannot see -- claude.ai, the phone app --
#     so an old reading can understate the truth. Nothing detects that, so
#     this bound stays as the backstop. Half an hour is long enough that
#     ordinary pauses are silent, short enough that a genuinely abandoned
#     Claude Code is not presented as live.
STALE_AFTER_S = 1800


# Bounds for a reset stamp. The ceiling is codex_cli's and claude_desktop's
# (2100-01-01; it would notice the unit changing to milliseconds). The floor is
# 0 and not their 2020: a stamp in the past is how a window that has ended is
# recognised (base.window_ended), so a long-past one is evidence and not noise,
# and what is refused here is only what is not a time at all.
RESET_EPOCH_MIN = 0
RESET_EPOCH_MAX = 4_102_444_800


def window_present(rate_limits, key: str) -> bool:
    """Does the payload carry this window at all?

    An object under the key, and nothing else: absent, null, a string or a
    list is no window. The ONE definition. ClaudeCliProvider reports a
    window from it, and tells a reply that came back without one by it
    (pc/session_verdict), and it used to ask with
    `"five_hour" in limits` while _window below treated null as absent, so a
    `"five_hour": null` was a window to one and not to the other (review,
    2026-10-31). An object with no usable percentage IS a window here: that
    is the likeliest shape of a payload change, and not something to
    conclude "this account has no limit" from.
    """
    return isinstance(rate_limits, dict) and isinstance(rate_limits.get(key),
                                                        dict)


def window_usable(rate_limits, key: str) -> bool:
    """Does this window carry a percentage the dial could show?

    Asked of the payload alone, with no clock: what the file said when it
    was written, which is the question a payload's own shape answers.
    Whether that figure still holds now is settle_window's business, below.
    """
    return window_present(rate_limits, key) and _window(rate_limits,
                                                        key)[0] >= 0


def _window(rate_limits: dict, key: str):
    """(used_percentage, resets_at_epoch|None) for one window.

    Claude Code documents each window as individually optional -- a session
    can report five_hour with no seven_day, or neither. A window that is
    entirely absent returns pct=-1.0, the same "unknown" sentinel already
    used below for *_resets_in_s, rather than 0.0: 0.0 reads as a confident
    "0% used", which is a stronger claim than "we don't have this number" and
    is exactly the frozen-meter failure mode this module exists to avoid.
    """
    if not window_present(rate_limits, key):
        return -1.0, None
    w = rate_limits[key]
    try:
        pct = float(w["used_percentage"])
    except (KeyError, TypeError, ValueError, OverflowError):
        # OverflowError: an int of 309 digits or more, which json reads and
        # float() refuses. It raised out of every poll and the source was
        # benched with backoff (the re-review of 2026-11-01, finding 12).
        # Absent, null, or not a number -- all three are "we don't have it",
        # and all three used to land on 0.0 here, which is the confident zero
        # the paragraph above says this function exists to avoid. A window
        # object present but missing its percentage is the likeliest shape of
        # a payload change, so it is the case worth getting right.
        pct = -1.0
    # Finite and 0..1000, as the Desktop and Codex readers already require
    # (1000 and not 100: overage carries a window past its limit). json reads
    # 1e999 as infinity, and an infinite percentage reached the merge, where
    # inf - inf is NaN and max() of nothing raised, so one corrupt number in
    # statusline.json took the whole poll -- the Codex page too -- down.
    # NaN fails both comparisons, so it lands here as well.
    if not (0 <= pct <= 1000):
        pct = -1.0
    resets = w.get("resets_at")
    # A real reset stamp only, bounded like the Codex and Desktop readers'.
    # json reads 1e999 as infinity and NaN as NaN, and two
    # infinite stamps made the merge's same-window test false even for a
    # frame against itself, which emptied max() and took every provider's
    # usage down with a perfectly good percentage beside it. A bool is an int
    # to isinstance and not a time. Unknown, like an absent stamp.
    if (isinstance(resets, bool) or not isinstance(resets, (int, float))
            or not (RESET_EPOCH_MIN <= resets <= RESET_EPOCH_MAX)):
        resets = None
    return pct, resets


# _secs_until moved to protocol.secs_until when a second provider needed the
# same countdown. The reasoning that used to live here -- why -1 rather than 0,
# both for missing input and for an already-past reset -- moved with it.
_secs_until = protocol.secs_until


# What happens to a window once its reset has passed is decided in
# pc/providers/base (window_ended, settle_window), along with the reasons.
# It was written here first and moved on 2026-10-01, when a Codex rollout
# turned out to hold a pre-reset figure in exactly the same way -- the
# case DevOceanIT brought from their own desk.


def _amount(v):
    """A finite, non-negative number from the payload, else None.

    bool is refused by name because it is an int: `"used_usd": true` is a
    malformed field, not one dollar. A negative or infinite amount would
    reach the panel as "$-4" or "$inf", and "not reported" is the honest
    reading of either.
    """
    # type() rather than isinstance(): bool passes isinstance(v, int).
    usable = type(v) in (int, float) and math.isfinite(v) and v >= 0
    return float(v) if usable else None


def _spend_frame(rate_limits: dict, now_epoch: float, mtime_epoch: float):
    """The dollar cap Claude Code put on the status line, as a frame.

    None when `rate_limits.spend_limit` is absent or unreadable. That object
    is how one kind of Claude Enterprise seat reports its
    allowance: one behind a Claude apps gateway that sets a dollar cap for
    this developer (code.claude.com/docs/en/statusline, "spend limit
    fields"). A desk on Pro or Max never has it, and nor does an Enterprise
    login straight to claude.ai, so for every desk this project ships to
    today this returns None and the payload is read exactly as it was.

    Only `used_percentage` is required. Claude Code sends it, with
    `resets_at`, on every response; `used_usd`, `limit_usd` and `period`
    come from a second request that runs minutes behind and is skipped
    altogether under CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC. A frame
    without the dollars is therefore ordinary and is still drawn.

    If the payload has the five-hour and seven-day windows as well, they
    are ignored. The gateway checks the dollar cap on each request, so the
    cap is what refuses work when it runs out; drawing both on one page
    would put two different allowances under one heading.

    When the period's reset has passed, the share goes through
    base.settle_window exactly as a window's percentage does, and the
    dollars spent are made to agree with whatever it decides: zero beside
    a zero, unknown beside an unknown. The cap keeps its value. Reaching
    the 1st of the month empties what was spent, not what may be spent.

    Never logs anything from the object. A spend figure is somebody's
    budget, and the rule for serial buffers applies to it in spirit.
    """
    obj = rate_limits.get("spend_limit")
    if not isinstance(obj, dict):
        return None
    pct = _amount(obj.get("used_percentage"))
    if pct is None:
        # Not a percentage, or a negative or NaN one. Without it there is
        # nothing to put on a gauge, and an object we cannot read is not
        # evidence of an allowance -- so the payload is read the old way.
        return None
    resets = obj.get("resets_at")
    if isinstance(resets, bool) or not isinstance(resets, (int, float)):
        resets = None
    period = obj.get("period")
    if period not in base.SPEND_PERIODS:
        period = ""
    frame = base.NormalizedUsageFrame(
        provider=PROVIDER_ID, src=SRC_ID, observed_at=mtime_epoch,
        plan=base.PLAN_SPEND, spend_period=period,
        spend_limit_usd=_amount(obj.get("limit_usd")),
        stale=(now_epoch - mtime_epoch) > STALE_AFTER_S)
    settled = base.settle_window(pct, resets, now_epoch, frame.stale)
    share = settled.pct
    frame.session_pct = share
    frame.session_resets_at = settled.resets_at
    frame.session_rolled_at = settled.ended_at
    spent = _amount(obj.get("used_usd"))
    if frame.session_rolled_at is not None:
        # The period this was spent in is over. settle_window has said either
        # 0.0 (a fresh reading: definitely empty) or UNKNOWN (an old one),
        # and the dollar figure claims exactly as much.
        spent = None if share < 0 else 0.0
    frame.spend_usd = spent
    return frame


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
    # The only door to the spend page, and it opens only when Claude Code
    # actually sent the object. See _spend_frame.
    spend = _spend_frame(rate_limits, now_epoch, mtime_epoch)
    if spend is not None:
        return spend
    session_pct, session_resets = _window(rate_limits, "five_hour")
    weekly_pct, weekly_resets = _window(rate_limits, "seven_day")

    # Staleness is age, and only age. A window resetting used to set this too,
    # which conflated two different facts: "we cannot vouch for this reading"
    # and "this reading has been superseded by a zero we can compute". The
    # second has a real answer, so it gets one -- see base.settle_window().
    stale = (now_epoch - mtime_epoch) > STALE_AFTER_S
    # Unconditional, and `stale` is an INPUT to it rather than a gate on it:
    # whether a window has ended is a question about the clock, not about the
    # file's age, and only the percentage it hands back depends on how old
    # the reading is. See base.settle_window().
    session = base.settle_window(session_pct, session_resets, now_epoch,
                                 stale)
    weekly = base.settle_window(weekly_pct, weekly_resets, now_epoch, stale)

    return base.NormalizedUsageFrame(
        provider=PROVIDER_ID, src=SRC_ID, observed_at=mtime_epoch,
        session_rolled_at=session.ended_at, weekly_rolled_at=weekly.ended_at,
        session_pct=session.pct, session_resets_at=session.resets_at,
        weekly_pct=weekly.pct, weekly_resets_at=weekly.resets_at,
        stale=stale,
    )


def map_statusline(payload: dict, now_epoch: float, mtime_epoch: float) -> dict:
    """Convert a statusline payload into a 'usage' protocol message."""
    # No per-model rows: the statusline payload has no per-model breakdown.
    return protocol.frame_to_usage(
        map_statusline_frame(payload, now_epoch, mtime_epoch), now_epoch)


def read_payload(path):
    """(payload, mtime) or (None, None) when absent/unreadable/malformed.

    The path has no default. A default was where the import-time path hid
    (see payload_path), so every caller names the file it means.
    """
    try:
        mtime = os.path.getmtime(path)
        # UTF-8 by name. Without it Windows opens the file in the machine's
        # ANSI code page, and on a Hebrew Windows 10 (cp1255) the transcript
        # path Claude Code writes into this payload raised UnicodeDecodeError
        # -- a ValueError, swallowed below as "no data" -- so a board on that
        # machine never received a figure while the file sat there fresh and
        # complete (2026-08-29).
        with open(path, encoding="utf-8") as f:
            return json.load(f), mtime
    except (OSError, ValueError):
        return None, None
