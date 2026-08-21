"""Transport-agnostic bridge logic: react to board messages, poll usage, track
liveness. I/O (serial) is injected so this is unit-testable without hardware."""
import datetime
import sys
import time
import urllib.error

from pc import ota as ota_mod
from pc import protocol

LIVENESS_WINDOW_S = 30.0

# Rate-limit backoff. The usage endpoint throttles aggressively, and the poll
# cadence alone (60 s) is enough to stay throttled once it starts: every reply
# is a 429, so the board sat red and the budget never recovered. Start well
# above the cadence and double to ten minutes, which is what the firmware's own
# WiFi-mode fetch already does for the same response.
RATE_LIMIT_MIN_S = 120.0
RATE_LIMIT_MAX_S = 600.0
# Upper bound on a server-supplied Retry-After, so one absurd header cannot
# park the display on stale numbers for a day.
RATE_LIMIT_CEILING_S = 3600.0


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
                 wall=_local_wall, fetch_manifest=None, fetch_firmware=None,
                 flash_image=None):
        self._write = write_msg          # callable(dict)
        self._fetch = fetch_usage        # callable() -> usage message dict
        self._now = now
        self._wall = wall                # callable() -> (epoch_s, utc_offset_min)
        self._app_ver = app_ver
        self._last_ping = None
        self._throttled_until = 0.0      # monotonic; 0 = free to fetch
        self._throttle_step = 0.0        # last backoff used, for doubling
        # OTA. Injected so the transfer is testable without GitHub.
        self._fetch_manifest = fetch_manifest or ota_mod.fetch_manifest
        self._fetch_firmware = fetch_firmware or ota_mod.fetch_firmware
        self._manifest = None            # release offered to the board
        self._flash = flash_image        # callable(blob, version); owns the port

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
        elif t == "ota_query":
            self._on_ota_query(msg.get("cur", ""))
        elif t == "ota_flash":
            self._on_ota_flash()
        # unknown types ignored

    # --- OTA, for a board with no network of its own -------------------
    #
    # The board asks and approves; we fetch and write. The image is NOT sent
    # over this protocol -- an earlier revision did that in base64 chunks and
    # managed 213 B/s, and MCUboot still had to swap slot1 afterwards. esptool
    # against slot0 does the same job in about 75 s with no swap, and it is
    # the same command this project has always flashed with by hand.

    def _ota_reset(self):
        self._manifest = None

    def _on_ota_query(self, cur):
        m = self._fetch_manifest()
        if not m or not ota_mod.is_newer(m.get("version", ""), cur):
            have = m.get("version", "?") if m else "unreachable"
            print(f"[bridge] ota: board has {cur}, release has {have}"
                  " -- nothing to do", file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_none())
            return
        self._manifest = m
        print(f"[bridge] ota: offering {m['version']} ({m['size']} bytes)",
              file=sys.stderr)
        self._write(protocol.ota_avail(m["version"], m["size"], m["sha256"]))

    def _on_ota_flash(self):
        """The board approved. Fetch the image and hand it to the flasher."""
        if not self._manifest:
            self._write(protocol.ota_error("nothing staged"))
            return
        if self._flash is None:
            self._write(protocol.ota_error("no flasher configured"))
            return
        try:
            blob = self._fetch_firmware()
        except Exception as e:
            print(f"[bridge] ota: download failed: {e}", file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_error("download failed"))
            return
        # Refuse an image that already disagrees with its own manifest rather
        # than writing it to slot0, where there is no auto-revert to catch it.
        if len(blob) != self._manifest["size"]:
            print(f"[bridge] ota: asset is {len(blob)} bytes, manifest says"
                  f" {self._manifest['size']}", file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_error("size mismatch"))
            return
        version = self._manifest["version"]
        self._ota_reset()
        print(f"[bridge] ota: flashing {version} ({len(blob)} bytes)",
              file=sys.stderr)
        self._flash(blob, version)

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

        if usage is None:
            return          # No statusline payload yet -- board keeps its last values.

        self._clear_throttle()
        self._write(usage)

    def _throttle(self, retry_after):
        """Arm the next hold-off: our own ladder from RATE_LIMIT_MIN_S doubling
        to RATE_LIMIT_MAX_S, which a server-supplied Retry-After may EXTEND but
        never shorten.

        Retry-After names the earliest moment a retry is allowed; it is not a
        recommended interval. This endpoint sends `Retry-After: 0` (observed
        2026-08-18), and taking that literally disabled the backoff entirely --
        the daemon went straight back to knocking every 60 s, which is what got
        it throttled in the first place.
        """
        if self._throttle_step:
            wait = min(self._throttle_step * 2, RATE_LIMIT_MAX_S)
        else:
            wait = RATE_LIMIT_MIN_S
        if retry_after is not None:
            wait = max(wait, min(retry_after, RATE_LIMIT_CEILING_S))
        self._throttle_step = wait
        self._throttled_until = self._now() + wait
        print("[bridge] rate limited; holding off %.0fs (Retry-After: %s)"
              % (wait, "absent" if retry_after is None else "%.0fs" % retry_after),
              file=sys.stderr)

    def _clear_throttle(self):
        self._throttled_until = 0.0
        self._throttle_step = 0.0
