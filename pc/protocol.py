"""NDJSON line protocol shared with the ESP32 firmware.

One JSON object per line, UTF-8, '\n'-terminated. Every message carries
`t` (type) and `v` (version). Non-JSON lines (logs) and unknown types are
ignored by callers. v2.
"""
import json

from pc.version import PROTO_VERSION

# Kept as a name because every message builder below already spells it this
# way. It is the protocol's version, not the product's -- see pc/version.py.
VERSION = PROTO_VERSION


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
          session_resets_in_s=-1, weekly_resets_in_s=-1, stale=False) -> dict:
    """A usage message.

    The *_resets_in_s fields carry the remaining seconds. The board has no
    wall clock when tethered over USB, so it cannot derive a countdown from
    the absolute resets_at timestamps; it ticks these down locally instead.
    -1 means unknown. The absolute timestamps are kept for readability and
    for any consumer that does know the time.

    Known models are ALSO flattened into sonnet_pct/opus_pct: the board's
    JSON scanner reads scalar keys only, and its per-model peek needs these
    without growing a full array parser.

    `stale` is a declared field, not an afterthought bolted on by a caller:
    this function is the one place the wire contract is defined.
    pc/statusline_source.py, the only producer of usage messages, sets it
    when the payload it read has outlived its own freshness window.

The firmware reads this field: proto.c's "usage" handler calls
    msg_get_bool(json, "stale", ...) and sets USAGE_STATUS_STALE when it is
    true, after the update and models calls that set OK internally.

    An absent key leaves it false on the board, so a daemon older than that
    firmware reads as OK rather than as a permanent warning. Older firmware
    given this field simply ignores it -- it degrades to a green dot on a
    stale reading, which is the behaviour that existed before either side
    knew about the field.
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
        "stale": stale,
        **flat,
    }


def status(state: str, detail: str = "") -> dict:
    return {"t": "status", "v": VERSION, "state": state, "detail": detail}


# --- OTA over the serial link (see pc/ota.py and the OTA block in proto.c) ---


def ota_avail(version, size, sha256, app=None):
    """`app` is the daemon version this release also carries, when it is newer
    than the one running. Additive and optional: firmware that predates it
    ignores the field, which is the whole reason the protocol version does not
    have to move for this."""
    msg = {"t": "ota_avail", "v": VERSION, "version": version,
           "size": int(size), "sha256": sha256}
    if app:
        msg["app"] = app
    return msg


def ota_begin(version):
    """The firmware write is starting now.

    Distinct from consent. The board used to persist "I am installing X" the
    moment it sent ota_flash, but in a pair update this program replaces
    itself first -- and opening the serial port from the new process resets
    the board, which then boots, sees a breadcrumb for a version it is not
    running, and reports "Update failed, previous version restored." before
    the firmware install has even begun. Then the real install finishes with
    the breadcrumb already spent, so the success notice never appears either.
    """
    return {"t": "ota_begin", "v": VERSION, "version": version}


def ota_resume(version):
    """Continuing an install the user already approved, after the daemon
    replaced itself. The board reopens its progress screen rather than sitting
    on an "Install?" prompt for something already under way."""
    return {"t": "ota_resume", "v": VERSION, "version": version}


def ota_none():
    return {"t": "ota_none", "v": VERSION}




def ota_error(why=""):
    return {"t": "ota_error", "v": VERSION, "why": why}
