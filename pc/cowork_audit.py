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


def iso_to_epoch(ts):
    """An ISO-8601 Z timestamp as epoch seconds, or None.

    Public and meant to stay stable: pc/desktop_idb needs the same parse for
    its own timestamps, and two copies of a date parser is two places to get
    fractional seconds wrong. Never raises -- a malformed timestamp is a
    normal state here, not an error.
    """
    if not isinstance(ts, str) or not ts.endswith("Z"):
        return None
    body = ts[:-1]
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
    return dt.replace(tzinfo=datetime.timezone.utc).timestamp()


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
