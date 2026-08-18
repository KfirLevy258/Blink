"""Transport-agnostic bridge logic: react to board messages, poll usage, track
liveness. I/O (serial) is injected so this is unit-testable without hardware."""
import datetime
import time
import urllib.error

from pc import protocol

LIVENESS_WINDOW_S = 30.0

# Rate-limit backoff. The usage endpoint throttles aggressively, and the poll
# cadence alone (60 s) is enough to stay throttled once it starts: every reply
# is a 429, so the board sat red and the budget never recovered. Start well
# above the cadence and double to ten minutes, which is what the firmware's own
# WiFi-mode fetch already does for the same response.
RATE_LIMIT_MIN_S = 120.0
RATE_LIMIT_MAX_S = 600.0


def _http_code(exc):
    """The HTTP status behind an exception, or None if it wasn't one."""
    return getattr(exc, "code", None) if isinstance(exc, urllib.error.HTTPError) else None


def _retry_after_s(exc):
    """Retry-After in seconds, if the server named one we can read.

    Only the delta-seconds form; the HTTP-date form is legal but Anthropic does
    not send it, and guessing at clock skew would be worse than our own ladder.
    """
    hdrs = getattr(exc, "headers", None)
    if not hdrs:
        return None
    try:
        return max(0.0, float(hdrs.get("Retry-After")))
    except (TypeError, ValueError):
        return None


def _local_wall():
    """(unix seconds, local UTC offset in minutes) -- DST-aware."""
    now = datetime.datetime.now().astimezone()
    return int(now.timestamp()), int(now.utcoffset().total_seconds() // 60)


class Bridge:
    def __init__(self, write_msg, fetch_usage, now=time.monotonic, app_ver="0.3.0",
                 wall=_local_wall):
        self._write = write_msg          # callable(dict)
        self._fetch = fetch_usage        # callable() -> usage message dict
        self._now = now
        self._wall = wall                # callable() -> (epoch_s, utc_offset_min)
        self._app_ver = app_ver
        self._last_ping = None
        self._throttled_until = 0.0      # monotonic; 0 = free to fetch
        self._throttle_step = 0.0        # last backoff used, for doubling

    # --- inbound ---
    def on_message(self, msg: dict):
        t = msg.get("t")
        if t == "hello":
            self._write(protocol.welcome("clauge-bridge", self._app_ver))
            self.poll_once()             # push current data immediately
        elif t == "ping":
            self._last_ping = self._now()
            # Free: never fetch here. The usage endpoint is aggressively
            # rate-limited and this fires every 10 s.
            self._write(protocol.pong())
        # unknown types ignored

    def board_alive(self) -> bool:
        return (self._last_ping is not None
                and self._now() - self._last_ping <= LIVENESS_WINDOW_S)

    # --- outbound ---
    def poll_once(self):
        # Time rides along with every push so the board's clock re-anchors
        # at the same cadence as the data.
        epoch, off = self._wall()
        self._write(protocol.time_msg(epoch, off))

        # Held off after a 429. Still SAY so on every poll: going quiet would
        # leave whatever the board last heard on screen, and on a fresh
        # connect that is a green dot over numbers we never fetched. This gate
        # also covers the hello handler, which pushes immediately on every
        # reconnect -- a flashing session fired one off-cadence call per boot.
        if self._now() < self._throttled_until:
            self._write(protocol.status("rate_limited", "rate limited, holding off"))
            return

        try:
            usage = self._fetch()
        except Exception as e:
            if _http_code(e) == 429:
                self._throttle(_retry_after_s(e))
                # Amber, not red: a 429 says "ask later", not "the numbers are
                # wrong". The board maps rate_limited to its own STALE state
                # and keeps showing the last good figures.
                self._write(protocol.status("rate_limited", str(e)[:80]))
            else:
                self._clear_throttle()
                self._write(protocol.status("error", str(e)[:80]))
            return

        self._clear_throttle()
        self._write(usage)

    def _throttle(self, retry_after):
        """Arm the next hold-off: the server's number if it gave one, else our
        own ladder from RATE_LIMIT_MIN_S, doubling to the ceiling."""
        if retry_after is not None:
            wait = min(retry_after, RATE_LIMIT_MAX_S)
        elif self._throttle_step:
            wait = min(self._throttle_step * 2, RATE_LIMIT_MAX_S)
        else:
            wait = RATE_LIMIT_MIN_S
        self._throttle_step = wait
        self._throttled_until = self._now() + wait

    def _clear_throttle(self):
        self._throttled_until = 0.0
        self._throttle_step = 0.0
