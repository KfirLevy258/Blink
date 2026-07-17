"""NDJSON line protocol shared with the ESP32 firmware.

One JSON object per line, UTF-8, '\n'-terminated. Every message carries
`t` (type) and `v` (version). Non-JSON lines (logs) and unknown types are
ignored by callers. v2.
"""
import json

VERSION = 2


def encode(msg: dict) -> bytes:
    """Serialize a message dict to a single NDJSON line (bytes)."""
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def decode(line: str):
    """Parse one line. Returns a dict, or None for non-protocol/garbage lines."""
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) else None


class LineReader:
    """Buffers incoming bytes and yields decoded protocol messages."""

    def __init__(self):
        self._buf = b""

    def feed(self, chunk: bytes):
        self._buf += chunk
        out = []
        while b"\n" in self._buf:
            raw, self._buf = self._buf.split(b"\n", 1)
            try:
                msg = decode(raw.decode("utf-8", "replace"))
            except Exception:
                msg = None
            if msg is not None:
                out.append(msg)
        return out


def welcome(app: str, app_ver: str) -> dict:
    return {"t": "welcome", "v": VERSION, "app": app, "app_ver": app_ver}


def pong() -> dict:
    """Answer to the board's ping.

    Liveness has to run both ways. Usage is only pushed every 300 s, so with
    no answer to the board's 10 s ping the board cannot distinguish a host
    that is merely between polls from one that has died -- and would sit
    there showing a green dot over numbers that stopped updating.
    """
    return {"t": "pong", "v": VERSION}


def time_msg(epoch: int, utc_offset_min: int) -> dict:
    """Wall clock for the board.

    Sent on hello and alongside every usage push: the board has no RTC, so it
    anchors this epoch to its own uptime and re-anchors on each message,
    bounding drift to one push interval. utc_offset_min is the PC's local
    offset (DST included) -- the board does epoch + offset and renders HH:MM.
    """
    return {"t": "time", "v": VERSION, "epoch": int(epoch),
            "utc_offset_min": int(utc_offset_min)}


def usage(session_pct, session_resets_at, weekly_pct, weekly_resets_at, models,
          session_resets_in_s=-1, weekly_resets_in_s=-1) -> dict:
    """A usage message.

    The *_resets_in_s fields carry the remaining seconds. The board has no
    wall clock when tethered over USB, so it cannot derive a countdown from
    the absolute resets_at timestamps; it ticks these down locally instead.
    -1 means unknown. The absolute timestamps are kept for readability and
    for any consumer that does know the time.

    Known models are ALSO flattened into sonnet_pct/opus_pct: the board's
    JSON scanner reads scalar keys only, and its per-model peek needs these
    without growing a full array parser.
    """
    flat = {}
    for m in models or []:
        name = m.get("name")
        if name in ("fable", "sonnet", "opus") and "weekly_pct" in m:
            flat[f"{name}_pct"] = float(m["weekly_pct"])
    return {
        "t": "usage", "v": VERSION,
        "session_pct": session_pct, "session_resets_at": session_resets_at,
        "session_resets_in_s": session_resets_in_s,
        "weekly_pct": weekly_pct, "weekly_resets_at": weekly_resets_at,
        "weekly_resets_in_s": weekly_resets_in_s,
        "models": models,
        **flat,
    }


def status(state: str, detail: str = "") -> dict:
    return {"t": "status", "v": VERSION, "state": state, "detail": detail}
