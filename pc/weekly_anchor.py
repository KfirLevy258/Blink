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


def anchor_path() -> str:
    return os.path.expanduser("~/.blink/weekly-anchor.json")


def load(path: str):
    """The stored anchor, or None. Absence and corruption are both normal."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    r, o = doc.get("resets_at"), doc.get("observed_at")
    for v in (r, o):
        if not isinstance(v, (int, float)) or isinstance(v, bool):
            return None
    return {"resets_at": float(r), "observed_at": float(o)}


def save(path: str, resets_at: float, observed_at: float) -> None:
    """Persist an anchor. A write failure is not worth an exception."""
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
        if not isinstance(r, (int, float)) or isinstance(r, bool):
            continue
        at = getattr(f, "observed_at", now_epoch)
        if best is None or at > best[1]:
            best = (float(r), float(at))
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
    if now_epoch - anchor["observed_at"] > ANCHOR_MAX_UNCORROBORATED_S:
        return None
    r = anchor["resets_at"]
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
    prev = None
    for s in samples:
        if not isinstance(s, dict):
            continue
        u = s.get("u")
        t = s.get("t")
        if not isinstance(u, dict) or not isinstance(t, (int, float)):
            continue
        sd = u.get("sd")
        if not isinstance(sd, (int, float)) or isinstance(sd, bool):
            continue
        at = float(t) / 1000.0
        if prev is not None and sd < prev[1]:
            # The window emptied somewhere between the two samples.
            lo, hi = prev[0], at
            r = anchor["resets_at"]
            if r > hi:
                steps = int((r - hi) // WEEK_S) + 1
                r -= steps * WEEK_S
            while r + WEEK_S <= hi:
                r += WEEK_S
            if not (lo - ANCHOR_REFUTE_TOLERANCE_S <= r
                    <= hi + ANCHOR_REFUTE_TOLERANCE_S):
                return True
        prev = (at, sd)
    return False
