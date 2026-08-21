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
STALE_AFTER_S = 120


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


def map_statusline(payload: dict, now_epoch: float, mtime_epoch: float) -> dict:
    """Convert a statusline payload into a 'usage' protocol message."""
    rate_limits = payload.get("rate_limits") or {}
    session_pct, session_resets = _window(rate_limits, "five_hour")
    weekly_pct, weekly_resets = _window(rate_limits, "seven_day")

    # No per-model rows: the statusline payload has no per-model breakdown.
    return protocol.usage(
        session_pct, "", weekly_pct, "", [],
        session_resets_in_s=_secs_until(session_resets, now_epoch),
        weekly_resets_in_s=_secs_until(weekly_resets, now_epoch),
        stale=(now_epoch - mtime_epoch) > STALE_AFTER_S,
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
