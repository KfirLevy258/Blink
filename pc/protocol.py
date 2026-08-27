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

# The firmware's inbound line buffer, mirrored here on purpose.
#
# proto.c:367-371 does not truncate an over-long line, it DROPS it: on
# overflow it resets line_len to 0 and the whole message is gone. There is no
# error, no partial parse and nothing on the panel to show for it -- the board
# simply stops updating while the daemon reports success. Every field added to
# a message spends this budget, so encode_checked() below refuses to put a
# line on the wire that the board could not receive.
MAX_LINE_BYTES = 512

# The one variable-length field on the usage message. "Opus 5 (1M context)" is
# 19 characters; a future model name is not bounded by anything we control, so
# it is bounded here instead rather than being allowed to push the line over
# the limit above.
MODEL_MAX_CHARS = 24

# Percentages and context fullness we do not have. Same sentinel as
# pc/providers/base.UNKNOWN and as the firmware's msg_get_double default, so a
# value crosses every layer without being re-encoded.
UNKNOWN = -1.0


def encode(msg: dict) -> bytes:
    """Serialize a message dict to a single NDJSON line (bytes)."""
    return (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")


def encode_checked(msg: dict):
    """encode(), but None when the result could not be received.

    Returns (bytes, None) on success and (None, reason) when the line exceeds
    what the firmware will accept. Callers are expected to log the reason and
    skip the message rather than write it: a line the board drops is strictly
    worse than one never sent, because only the second is visible from here.
    """
    raw = encode(msg)
    if len(raw) > MAX_LINE_BYTES:
        return None, (f"{msg.get('t', '?')} message is {len(raw)} bytes, over "
                      f"the board's {MAX_LINE_BYTES} byte line limit")
    return raw, None


def secs_until(resets_at, now_epoch: float) -> int:
    """Seconds until `resets_at`. -1 when unknown or already past.

    -1 rather than 0 for missing input: 0 renders as "resets now", which is a
    confident lie. -1 lets the display say "--".

    And -1 rather than 0 for a reset that has ALREADY passed, which is not a
    presentation choice. usage_view.c treats a countdown of exactly 0 as "this
    window just rolled over" and zeroes the percentage with it -- correct for
    the firmware's own countdown reaching zero, and wrong for a reading we are
    merely relaying, because usage may have happened from claude.ai or the
    phone since. Sending 0 hands the board a decision the parser declined to
    make, and it wipes the last-known numbers.

    Lives here rather than in a provider because it is a property of the wire
    format -- the board has no clock over USB, so the countdown is computed on
    this side for every provider, not just the first one.
    """
    if resets_at is None or resets_at <= now_epoch:
        return -1
    return int(resets_at - now_epoch)


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
          session_resets_in_s=-1, weekly_resets_in_s=-1, stale=False,
          provider="claude", src="cli", state="", n_sess=0, n_run=0, n_wait=0, n_stuck=0,
          n_agents=0, p2="", p2_session_pct=UNKNOWN,
          p2_weekly_pct=UNKNOWN, p2_session_resets_in_s=-1,
          p2_weekly_resets_in_s=-1) -> dict:
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
    # `models` itself never went on the wire usefully: the firmware reads the
    # flattened scalar keys below and never the array, and since the status
    # line became the only source it has always been empty. Dropping it buys
    # back thirteen bytes of a budget that the second provider's fields just
    # made tight.
    flat = {}
    for m in models or []:
        name = m.get("name")
        if name in ("fable", "sonnet", "opus") and "weekly_pct" in m:
            flat[f"{name}_pct"] = float(m["weekly_pct"])

    # The multi-provider fields, added WITHOUT moving PROTO_VERSION.
    #
    # pc/version.py states the rule these follow: protocol changes are
    # additive, and the version is a floor that refuses, not a format
    # selector. A board running older firmware ignores every key below --
    # msg_get_* simply never asks for them -- and goes on rendering the two
    # dials it already knows about. Moving the version instead would have
    # meant every deployed unit stops being offered updates, over the very
    # link the update travels on, which is not a mistake that can be
    # corrected remotely.
    #
    # Unknown fields are OMITTED rather than sent as sentinels. That is the
    # MAX_LINE_BYTES budget talking: an over-long line is dropped whole by the
    # board (proto.c:367-371), so every key that carries no information is
    # spending a budget a future field will need. An absent key already means
    # "unknown" on both sides.
    extra = {"provider": provider, "src": src}
    if state:
        extra["state"] = state

    # Session and agent counts. Zeros are omitted like everything else here --
    # an absent count reads as zero on both sides, and on the common machine
    # (one session, no agents) that keeps this whole block down to about
    # twenty bytes instead of sixty.
    for key, val in (("n_sess", n_sess), ("n_run", n_run), ("n_wait", n_wait),
                     ("n_stuck", n_stuck), ("n_agents", n_agents)):
        if val:
            extra[key] = int(val)

    # A second provider, drawn as the inner ring on both gauges. Sent only
    # when there IS one, which on a single-provider machine is never -- so
    # the common line pays nothing for the capability.
    if p2:
        extra["p2"] = p2[:MODEL_MAX_CHARS]
        if p2_session_pct is not None and p2_session_pct >= 0:
            extra["p2_session_pct"] = p2_session_pct
        if p2_weekly_pct is not None and p2_weekly_pct >= 0:
            extra["p2_weekly_pct"] = p2_weekly_pct
        # Short names on purpose. These are the last two fields that fit: the
        # fully-loaded line is close enough to MAX_LINE_BYTES that spelling
        # them "p2_session_resets_in_s" would cost more than they carry, and
        # nothing but proto.c ever reads them.
        if p2_session_resets_in_s is not None and p2_session_resets_in_s >= 0:
            extra["p2_s_in_s"] = int(p2_session_resets_in_s)
        if p2_weekly_resets_in_s is not None and p2_weekly_resets_in_s >= 0:
            extra["p2_w_in_s"] = int(p2_weekly_resets_in_s)

    return {
        "t": "usage", "v": VERSION,
        "session_pct": session_pct, "session_resets_at": session_resets_at,
        "session_resets_in_s": session_resets_in_s,
        "weekly_pct": weekly_pct, "weekly_resets_at": weekly_resets_at,
        "weekly_resets_in_s": weekly_resets_in_s,
        "stale": stale,
        **flat,
        **extra,
    }


def frame_to_usage(frame, now_epoch: float, secondary=None) -> dict:
    """Turn a NormalizedUsageFrame into the usage message for the board.

    The single crossing point from provider-space to wire-space. Providers
    never build protocol messages themselves -- if they did, a second provider
    would be free to invent its own field names and the firmware would need to
    learn each one. Everything upstream produces frames; this function is the
    only thing that decides what a frame looks like on the wire.
    """
    return usage(
        frame.session_pct, frame.session_resets_at,
        frame.weekly_pct, frame.weekly_resets_at,
        [],
        session_resets_in_s=secs_until(frame.session_resets_at, now_epoch),
        weekly_resets_in_s=secs_until(frame.weekly_resets_at, now_epoch),
        stale=frame.stale,
        provider=frame.provider, src=frame.src,
        state=frame.state,
        n_sess=frame.n_sessions(), n_run=frame.n_run, n_wait=frame.n_wait,
        n_stuck=frame.n_stuck, n_agents=frame.n_agents,
        p2=(secondary.provider if secondary else ""),
        p2_session_pct=(secondary.session_pct if secondary else UNKNOWN),
        p2_weekly_pct=(secondary.weekly_pct if secondary else UNKNOWN),
        p2_session_resets_in_s=(secs_until(secondary.session_resets_at,
                                           now_epoch) if secondary else -1),
        p2_weekly_resets_in_s=(secs_until(secondary.weekly_resets_at,
                                          now_epoch) if secondary else -1),
    )


EDITIONS = ("claude", "codex")


def edition(name: str) -> dict:
    """Tell the board which edition it is -- which boot clip to play.

    A factory step, sent once after programming, not a user setting. The board
    persists it and applies it on the next boot, because what it selects is a
    boot animation.

    Additive like everything else on this wire: firmware that predates it
    ignores an unknown message type, so sending one to an older board is a
    no-op rather than an error. `clauge provision` reports what the board
    actually says, which is the only way to tell those two apart.
    """
    if name not in EDITIONS:
        raise ValueError(f"unknown edition {name!r}; expected one of "
                         + ", ".join(EDITIONS))
    return {"t": "edition", "v": VERSION, "edition": name}


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
