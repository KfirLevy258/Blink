"""Claude Desktop's five-hour usage record, and only that.

The reasoning about this source lives here rather than in the provider, the
way pc/statusline_source backs pc/providers/claude_cli. The provider is then
a thin adapter and there is one place to look when the app changes shape.

Two facts about the real record, both of which produce a confidently wrong
panel rather than an error if they are missed:

  - `utilization` is a FRACTION. 0.06 is six percent. Read as a percentage it
    draws an empty ring on a window that is filling.

  - `resetsAt` is in SECONDS, and it is frequently in the PAST. The record is
    only rewritten when the app does something, so between a window rolling
    and the next turn the stored percentage describes a window that no longer
    exists. The provider, not this module, decides what to publish then.

This store holds no conversation content -- its other keys are onboarding
flags, experiment toggles, activation checklists and an analytics queue. That
is why it is the store this project reads. Anything that changes it to read a
different one has to re-establish that property first.
"""
import json
import os
import sys

from pc import leveldb

# The logical key is obfuscated and carries the organization uuid. We match
# on the stable middle rather than reconstructing the whole thing, because
# the origin prefix and the uuid are both things we would get wrong.
USAGE_KEY_MARKER = b"ochre_heron_tide"

# Plausible bounds for a record timestamp: 2020-01-01 to 2100-01-01. Same
# guard, and the same reason, as pc/providers/claude_desktop -- one bad
# timestamp in the far future is never stale and beats every real reading,
# forever.
SAMPLE_EPOCH_MIN = 1_577_836_800
SAMPLE_EPOCH_MAX = 4_102_444_800


def store_path() -> str:
    """Where Claude Desktop keeps its Local Storage on this platform.

    Returned whether or not it exists: absence is a normal state and is
    handled by the reader, not by pretending we do not know where to look.
    """
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/Local Storage/leveldb")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(
            "~\\AppData\\Roaming")
        return os.path.join(appdata, "Claude", "Local Storage", "leveldb")
    return os.path.expanduser("~/.config/Claude/Local Storage/leveldb")


def decode_value(raw: bytes):
    """Chromium's Local Storage value framing: one prefix byte, then text."""
    if not raw:
        return None
    try:
        if raw[0] == 0:
            return raw[1:].decode("utf-16-le")
        return raw[1:].decode("utf-8")
    except (UnicodeDecodeError, IndexError):
        return None


def _valid(rec) -> bool:
    if not isinstance(rec, dict):
        return False
    at = rec.get("observedAt")
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        return False
    if not (SAMPLE_EPOCH_MIN <= at <= SAMPLE_EPOCH_MAX):
        return False
    return isinstance(rec.get("utilization"), (int, float))


def usage_records(dir_path: str) -> list:
    """Every decodable usage record surviving in the store.

    Goes through `leveldb.scan_all`, not `leveldb.scan`: scan() collapses to
    one final value per key, so when the same key exists in both an old
    .ldb table and a newer .log (or the other way around), one of the two
    copies is already gone before this module ever sees it. scan_all keeps
    every surviving copy -- including more than one put for the same key
    across different files -- so the observedAt tie-break in `newest_record`
    below always has every copy to compare, not whichever one a file-order
    heuristic happened to keep.

    scan_all still honours a real deletion of the key: a tombstone discards
    the copies that came before it, so a sign-out, an org switch, or a
    cleared store does not resurrect a stale reading left behind in an old
    table.
    """
    out = []
    for _key, raw in leveldb.scan_all(
            dir_path, lambda k: USAGE_KEY_MARKER in k):
        text = decode_value(raw)
        if text is None:
            continue
        try:
            rec = json.loads(text)
        except (TypeError, ValueError):
            continue
        if _valid(rec):
            out.append(rec)
    return out


def newest_record(dir_path: str):
    """The record with the greatest observedAt, or None.

    By its own timestamp rather than by file order: pc.leveldb.scan_all
    hands back every surviving copy precisely so a caller can do this, and
    pc.leveldb.scan's docstring points here as the reason that one exists.
    """
    best = None
    for rec in usage_records(dir_path):
        if best is None or rec["observedAt"] > best["observedAt"]:
            best = rec
    return best
