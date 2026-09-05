"""The seven-day boundary out of Claude Desktop's IndexedDB. Last resort.

This is the ONLY source in this project that reads a store holding the
customer's conversations, and it carries three rules none of the others need:

  - Nothing from the buffer is ever logged, printed, or placed in an
    exception message. The two floats returned here are the only things that
    leave. README.md tells customers that ~/.blink/bridge.log holds nothing
    secret, and that promise is load-bearing.
  - It runs at most once per process, and only when the cheaper seeders have
    failed. pc/ingest enforces that; this module must stay cheap enough that
    the enforcement is a convenience rather than a necessity.
  - It navigates structurally. A conversation that MENTIONS resetsAt must
    never become a reading, which is why pc/v8_clone exists.

A record carries THREE resetsAt: an outer one beside rateLimitType, then one
per window. Anything positional pairs them wrongly the day a window is
absent, so the walk below looks the field up by name through the nesting and
only ever reads `unifiedWindows.seven_day.resetsAt`.

Only Cowork sessions carry these records at all -- plain chats do not. See
docs/research/claude-desktop-window-sources.md, which explains how that was
established and how it was first got wrong. Provenance is decided by the
IndexedDB KEY and by nothing else: not the value, not the filesystem around
it.
"""
import math
import os
import sys

from pc import cowork_audit, leveldb, v8_clone

SAMPLE_EPOCH_MIN = 1_577_836_800
SAMPLE_EPOCH_MAX = 4_102_444_800

# Blink externalises any IndexedDB value over 64 KB to a sibling .blob file,
# leaving this prefix and a reference behind. The live records run around
# 45 KB, so a longer-than-usual session crosses that line. We do not read blob
# files: a value carrying this prefix is skipped, and the threshold itself is
# Blink's business, not ours -- nothing here compares a length against it.
BLOB_WRAPPED_PREFIX = b"\xff\x11\x01"

# Deeper than any shape these records take, and shallow enough that a
# pathological document cannot cost real time. pc/v8_clone has already
# refused anything past its own MAX_DEPTH before we get here.
MAX_DEPTH = 16

# Keys are UTF-16 on a real machine and ASCII in the fixtures. Match both
# rather than depending on which.
_KEY_MARKERS = (b"cowork", b"c\x00o\x00w\x00o\x00r\x00k")


def store_path() -> str:
    leaf = os.path.join("IndexedDB", "https_claude.ai_0.indexeddb.leveldb")
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/" + leaf)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(
            "~\\AppData\\Roaming")
        return os.path.join(appdata, "Claude", leaf)
    return os.path.expanduser("~/.config/Claude/" + leaf)


def _wanted_key(key: bytes) -> bool:
    """Cowork sessions only, decided by the key alone.

    Every other value in this store is a conversation. Widening this is not
    a tuning knob -- it is the difference between reading two records and
    decoding the customer's chats.
    """
    return any(m in key for m in _KEY_MARKERS)


def _finite_num(v) -> bool:
    """True for a real, finite number -- never a bool, never NaN/Infinity.

    A NaN passes a bare isinstance check and then loses every comparison
    silently, including the plausibility window below. This project has been
    bitten by that three times, and this value is written into a permanent
    one-shot anchor, so it is checked here rather than downstream.
    """
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _plausible(v) -> bool:
    return _finite_num(v) and SAMPLE_EPOCH_MIN <= v <= SAMPLE_EPOCH_MAX


def _seven_day_of(node: dict):
    """resetsAt from THIS dict's own unifiedWindows.seven_day, or None.

    Never the outer resetsAt beside rateLimitType, and never the five-hour
    window's. Reached by name at every step, so a document that merely
    mentions the field cannot produce a number.
    """
    windows = node.get("unifiedWindows")
    if not isinstance(windows, dict):
        return None
    seven = windows.get("seven_day")
    if not isinstance(seven, dict):
        return None
    r = seven.get("resetsAt")
    if not _plausible(r):
        return None
    return float(r)


def _walk(node, created, depth, out) -> None:
    """Collect (resets_at, created_at) pairs, each window with ITS record's
    timestamp.

    `created` is the nearest enclosing created_at, carried down the tree. One
    value can hold several events, and taking the first window found in a
    document together with the first created_at found in it would pair a
    boundary with a timestamp belonging to something else -- observed_at is
    the field the anchor's staleness withdrawal runs on, so that pairing has
    to be right.
    """
    if depth > MAX_DEPTH:
        return
    if isinstance(node, dict):
        at = cowork_audit.iso_to_epoch(node.get("created_at"))
        if _plausible(at):
            created = float(at)
        resets_at = _seven_day_of(node)
        if resets_at is not None and created is not None:
            out.append((resets_at, created))
        children = node.values()
    elif isinstance(node, list):
        children = node
    else:
        return
    for child in children:
        _walk(child, created, depth + 1, out)


def seven_day_reset(dir_path=None):
    """(resets_at, observed_at) from the newest usable record, or None.

    None means absent, never zero, and absence is the normal answer: most
    machines have never run a Cowork session at all. Never raises.
    """
    path = dir_path if dir_path is not None else store_path()
    try:
        rows = leveldb.scan(path, _wanted_key)
    except Exception:
        return None
    best = None
    for _key, raw in rows:
        try:
            if bytes(raw[:len(BLOB_WRAPPED_PREFIX)]) == BLOB_WRAPPED_PREFIX:
                # The bytes are in a sibling blob file we do not read, so
                # this record cannot be judged -- and being oversized, it is
                # the LONG session, i.e. the one most likely to be current.
                # Answering from a readable sibling would publish a week-old
                # boundary as today's. No answer beats a stale answer.
                return None
            found = []
            _walk(v8_clone.parse(raw), None, 0, found)
            for resets_at, at in found:
                if best is None or at > best[1]:
                    best = (resets_at, at)
        except Exception:
            # Broad and silent, for the same reason as pc/v8_clone.parse: an
            # exception escaping here could carry buffer bytes into a log.
            continue
    return best
