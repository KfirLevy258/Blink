"""The seven-day boundary: learned once, rolled forward, withdrawn on doubt.

A chat-only Claude Desktop user has no local source for this timestamp. Every
store on the machine was searched and it is not there -- see
docs/research/claude-desktop-window-sources.md. What IS true is that the
boundary is stable: two observed resets were exactly 604800 s apart and both
landed on Wednesday 06:00:00Z.

So this does not read anything on a schedule. It remembers one boundary that
some source actually published -- the Claude Code status line, a legacy
Cowork audit file, anything -- and counts forward from it.

This is the only value this project ships that was not directly observed, so
the interesting code is the withdrawal rules rather than the arithmetic.
"""
import json
import math
import os

WEEK_S = 604800.0

# A weekly-percentage drop more than this far from a predicted boundary means
# the anchor is wrong. Generous on purpose: the drop's own resolution is
# poor -- measured brackets of 45 h and 108 h, because plan-usage-history.json
# only samples while the app is open -- so this can refute an anchor but must
# never be trusted to confirm one.
ANCHOR_REFUTE_TOLERANCE_S = 86400.0

# Eight weeks. An anchor nobody has re-observed for two months is describing
# a subscription that may have changed underneath it.
ANCHOR_MAX_UNCORROBORATED_S = 8 * WEEK_S

# The same plausibility window pc/desktop_local_storage.py and
# pc/providers/claude_desktop.py already use for an epoch read off disk: any
# real observation lands between 2020-01-01 and 2100-01-01. A resets_at
# outside this window is corrupt, not merely old, and -- unlike a stale
# reading -- a bad timestamp in the far future never ages out on its own, so
# it must be rejected on the way in rather than trusted to expire.
SAMPLE_EPOCH_MIN = 1_577_836_800
SAMPLE_EPOCH_MAX = 4_102_444_800


def _finite_num(v) -> bool:
    """True for a real, finite timestamp -- never a bool, never NaN/Infinity.

    json.load happily parses the bare literals NaN/Infinity/-Infinity by
    default, and json.dump writes them back out, so a plain isinstance check
    is not enough: it lets a NaN through, and every comparison this module
    makes against a NaN silently does nothing (NaN is smaller than nothing,
    including itself). This anchor is a permanent one-shot memory -- a NaN
    written once would sit there forever -- so every number that enters or
    leaves this module is checked here instead.
    """
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _reject_constant(_token):
    """Passed to json.load so NaN/Infinity/-Infinity are refused at parse
    time, rather than admitted as floats and caught later by _finite_num."""
    raise ValueError("weekly anchor: refusing a non-finite JSON constant")


def anchor_path() -> str:
    return os.path.expanduser("~/.blink/weekly-anchor.json")


def load(path: str):
    """The stored anchor, or None. Absence and corruption are both normal."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh, parse_constant=_reject_constant)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    r, o = doc.get("resets_at"), doc.get("observed_at")
    if not (_finite_num(r) and _finite_num(o)):
        return None
    r = float(r)
    if not (SAMPLE_EPOCH_MIN <= r <= SAMPLE_EPOCH_MAX):
        return None
    return {"resets_at": r, "observed_at": float(o)}


def save(path: str, resets_at, observed_at) -> None:
    """Persist an anchor. A write failure is not worth an exception."""
    if not (_finite_num(resets_at) and _finite_num(observed_at)):
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"resets_at": float(resets_at),
                       "observed_at": float(observed_at)}, fh)
        os.replace(tmp, path)
    except OSError:
        return


def observe(frames, path: str, now_epoch: float) -> None:
    """Learn from any frame that carries an exact weekly reset.

    Deliberately source-agnostic. The status line has one, a legacy Cowork
    audit file has one, and a machine that has ever seen either never needs
    the last-resort seeder at all.
    """
    best = None
    for f in frames or []:
        r = getattr(f, "weekly_resets_at", None)
        if not _finite_num(r):
            continue
        r = float(r)
        if not (SAMPLE_EPOCH_MIN <= r <= SAMPLE_EPOCH_MAX):
            continue
        at = getattr(f, "observed_at", now_epoch)
        if not _finite_num(at):
            continue
        at = float(at)
        if best is None or at > best[1]:
            best = (r, at)
    if best is None:
        return
    current = load(path)
    if current is not None and current["observed_at"] >= best[1]:
        return
    save(path, best[0], best[1])


def project(anchor, now_epoch: float):
    """The next boundary this anchor predicts, or None if it is withdrawn."""
    if not anchor:
        return None
    resets_at = anchor.get("resets_at")
    observed_at = anchor.get("observed_at")
    if not (_finite_num(resets_at) and _finite_num(observed_at)):
        return None
    if now_epoch - observed_at > ANCHOR_MAX_UNCORROBORATED_S:
        return None
    r = float(resets_at)
    if r <= now_epoch:
        steps = int((now_epoch - r) // WEEK_S) + 1
        r = r + steps * WEEK_S
    return r


def refuted_by(anchor, samples, now_epoch: float) -> bool:
    """True when an observed weekly-percentage drop contradicts the anchor.

    Only ever refutes. A drop bounded to a 45-hour window cannot confirm a
    boundary, and treating it as confirmation is how an approximate number
    acquires false authority.
    """
    if not anchor or not isinstance(samples, list):
        return False
    resets_at = anchor.get("resets_at")
    if not _finite_num(resets_at):
        return False
    resets_at = float(resets_at)
    prev = None
    for s in samples:
        if not isinstance(s, dict):
            continue
        u = s.get("u")
        t = s.get("t")
        if not isinstance(u, dict) or not _finite_num(t):
            continue
        sd = u.get("sd")
        if not _finite_num(sd):
            continue
        at = float(t) / 1000.0
        if prev is not None and sd < prev[1]:
            # The window emptied somewhere between the two samples. Fold the
            # anchor's boundary to the one occurrence of it closest to (at
            # or before) `hi`, in one bounded step -- direct arithmetic via
            # Python's float modulo, not a loop that walks a week at a time.
            # A corrupt anchor (a 1970 epoch, a -1e18) previously cost
            # thousands of iterations, or hung outright; this is O(1)
            # regardless of how far `resets_at` is from `hi`.
            lo, hi = prev[0], at
            r = hi - ((hi - resets_at) % WEEK_S)
            if not (lo - ANCHOR_REFUTE_TOLERANCE_S <= r
                    <= hi + ANCHOR_REFUTE_TOLERANCE_S):
                return True
        prev = (at, sd)
    return False
