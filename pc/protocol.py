"""NDJSON line protocol shared with the ESP32 firmware.

One JSON object per line, UTF-8, '\n'-terminated. Every message carries
`t` (type) and `v` (version). Non-JSON lines (logs) and unknown types are
ignored by callers. v1.
"""
import json

VERSION = 1


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


def usage(session_pct, session_resets_at, weekly_pct, weekly_resets_at, models) -> dict:
    return {
        "t": "usage", "v": VERSION,
        "session_pct": session_pct, "session_resets_at": session_resets_at,
        "weekly_pct": weekly_pct, "weekly_resets_at": weekly_resets_at,
        "models": models,
    }


def status(state: str, detail: str = "") -> dict:
    return {"t": "status", "v": VERSION, "state": state, "detail": detail}
