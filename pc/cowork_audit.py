"""A seven-day boundary out of a legacy Cowork audit file.

Plain JSON, in a directory that holds no chat store -- which is why this is
tried before pc/desktop_idb. It is also rare: on the machine this was written
from, 3 of 218 rate-limit events carried the windows at all, and the current
managed Cowork sessions write no audit file whatsoever. Absence is the normal
answer and is not worth a log line.

Privacy: these files sit inside the owner's Claude session directory and the
surrounding lines are conversation-adjacent. Nothing this module reads --
no line, no fragment of one, no decoded value, no surrounding bytes -- is
ever written, printed, or placed into an exception message, at any log
level. The only observable output is seven_day_reset()'s return value: two
floats, or None.
"""
import datetime
import math
import json
import os
import sys

SAMPLE_EPOCH_MIN = 1_577_836_800
SAMPLE_EPOCH_MAX = 4_102_444_800

# Bounded on purpose. This walks a directory that can hold hundreds of
# sessions, and it runs when a machine has no anchor -- which for most
# machines is every start, forever.
MAX_FILES = 20
TAIL_BYTES = 262144


def sessions_dir() -> str:
    if sys.platform == "darwin":
        return os.path.expanduser(
            "~/Library/Application Support/Claude/local-agent-mode-sessions")
    if os.name == "nt":
        appdata = os.environ.get("APPDATA") or os.path.expanduser(
            "~\\AppData\\Roaming")
        return os.path.join(appdata, "Claude", "local-agent-mode-sessions")
    return os.path.expanduser("~/.config/Claude/local-agent-mode-sessions")


def _zone_seconds(zone):
    """The offset an explicit ISO-8601 zone suffix denotes, in seconds."""
    sign = -1 if zone[0] == "-" else 1
    body = zone[1:].replace(":", "")
    if len(body) == 2:
        hh, mm = body, "00"
    elif len(body) == 4:
        hh, mm = body[:2], body[2:]
    else:
        return None
    # isdigit() alone is true for non-ASCII digits that int() then refuses.
    if not (hh.isascii() and hh.isdigit() and mm.isascii() and mm.isdigit()):
        return None
    h, m = int(hh), int(mm)
    if h > 23 or m > 59:
        return None
    return sign * (h * 3600 + m * 60)


def _split_zone(ts):
    """(body, offset_seconds) for a timestamp that names its zone, else None.

    A trailing Z, or an explicit offset in any of the forms this family of
    producers emits: +00:00, -0730, +03. datetime.isoformat() writes the
    first of those, so a record whose timestamp came from Python rather than
    from a JS Date carries an offset and no Z.

    A timestamp with NO zone at all stays unreadable on purpose. Guessing one
    turns an honest absence into a silent multi-hour error, and the callers
    here would rather have None.
    """
    if ts.endswith("Z"):
        return ts[:-1], 0
    # A zone suffix is at most six characters, and nothing before it may be
    # mistaken for one -- the '-' in the date must never match.
    for i in range(len(ts) - 1, max(len(ts) - 7, 0), -1):
        c = ts[i]
        if c in "+-":
            secs = _zone_seconds(ts[i:])
            if secs is None:
                return None
            return ts[:i], secs
        if not (c.isdigit() or c == ":"):
            return None
    return None


def iso_to_epoch(ts):
    """An ISO-8601 timestamp as epoch seconds, or None.

    Public and meant to stay stable: pc/desktop_idb needs the same parse for
    its own timestamps, and two copies of a date parser is two places to get
    fractional seconds wrong. Never raises -- a malformed timestamp is a
    normal state here, not an error.

    Accepts a trailing Z and an explicit numeric offset alike. The offset
    form matters to pc/desktop_idb: it has no second timestamp to fall back
    to, so a shape this refused would silently cost a machine its only
    reading of the seven-day boundary.
    """
    if not isinstance(ts, str):
        return None
    split = _split_zone(ts)
    if split is None:
        return None
    body, offset = split
    if "." in body:
        head, frac = body.split(".", 1)
        body = head + "." + (frac + "000000")[:6]
        fmt = "%Y-%m-%dT%H:%M:%S.%f"
    else:
        fmt = "%Y-%m-%dT%H:%M:%S"
    try:
        dt = datetime.datetime.strptime(body, fmt)
    except ValueError:
        return None
    # `body` is a wall-clock reading in the zone the suffix named, so the
    # offset comes OFF: 15:58+03:00 is 12:58 UTC, not 18:58.
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp() - offset


def _finite_num(v) -> bool:
    """True for a real, finite number -- never a bool, never NaN/Infinity.

    json.loads happily parses the bare literals NaN/Infinity/-Infinity by
    default, and a bare isinstance check lets a NaN straight through: every
    comparison against it silently does nothing. Guarded here rather than
    trusted to whatever reads this module's output.
    """
    return (isinstance(v, (int, float)) and not isinstance(v, bool)
            and math.isfinite(v))


def _plausible(v) -> bool:
    return _finite_num(v) and SAMPLE_EPOCH_MIN <= v <= SAMPLE_EPOCH_MAX


def _audit_files(root: str, max_files: int) -> list:
    found = []
    for dirpath, _dirs, names in os.walk(root):
        if "audit.jsonl" not in names:
            continue
        p = os.path.join(dirpath, "audit.jsonl")
        try:
            found.append((os.path.getmtime(p), p))
        except OSError:
            continue
    found.sort(reverse=True)
    return [p for _mtime, p in found[:max_files]]


def _tail_lines(path: str, tail_bytes: int) -> list:
    """The newest lines of one file, opened, read and closed -- never held.

    Reads at most `tail_bytes` from the end so a large audit file costs a
    bounded seek-and-read rather than a full parse, and never keeps a handle
    open on a file another application (Claude Desktop) owns.
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as fh:
            if size > tail_bytes:
                fh.seek(size - tail_bytes)
            raw = fh.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    # Seeking into the middle of a file cuts the first line in half.
    if size > tail_bytes and len(lines) > 1:
        return lines[1:]
    return lines


def _reset_from_event(ev):
    if not isinstance(ev, dict) or ev.get("type") != "rate_limit_event":
        return None
    info = ev.get("rate_limit_info")
    if not isinstance(info, dict):
        return None
    windows = info.get("unifiedWindows")
    if not isinstance(windows, dict):
        return None
    seven = windows.get("seven_day")
    if not isinstance(seven, dict):
        return None
    r = seven.get("resetsAt")
    if not _plausible(r):
        return None
    return float(r)


def seven_day_reset(root=None, max_files=MAX_FILES, tail_bytes=TAIL_BYTES):
    """(resets_at, observed_at) from the newest usable event, or None.

    Never raises. A missing root, an unreadable file, a truncated JSON
    object and a line with no windows are all normal states here, and every
    one of them is folded into the same None -- the caller falls back.
    """
    root = root if root is not None else sessions_dir()
    best = None
    try:
        files = _audit_files(root, max_files)
    except Exception:
        return None
    for path in files:
        for line in _tail_lines(path, tail_bytes):
            # Cheap reject before the JSON parse: almost every line in these
            # files is conversation traffic we have no business decoding.
            if "unifiedWindows" not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            resets_at = _reset_from_event(ev)
            if resets_at is None:
                continue
            at = iso_to_epoch(ev.get("timestamp"))
            if not _plausible(at):
                try:
                    at = os.path.getmtime(path)
                except OSError:
                    continue
                if not _plausible(at):
                    continue
            # >= , not >: several events in one file can share a
            # timestamp (the mtime fallback gives every event in a file the
            # SAME `at`), and the intent is the newest -- i.e. the LAST one
            # seen for a given `at` -- not whichever happened to be read
            # first.
            if best is None or at >= best[1]:
                best = (resets_at, float(at))
    return best
