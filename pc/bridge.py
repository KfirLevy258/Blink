"""Transport-agnostic bridge logic: react to board messages, poll usage, track
liveness. I/O (serial) is injected so this is unit-testable without hardware."""
import datetime
import hashlib
import sys
import time
import urllib.error

from pc import ota as ota_mod
from pc import protocol
from pc.version import PROTO_VERSION, RELEASE_VERSION

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
    def __init__(self, write_msg, fetch_usage, now=time.monotonic,
                 app_ver=RELEASE_VERSION,
                 wall=_local_wall, fetch_manifest=None, fetch_firmware=None,
                 flash_image=None, self_update=None, pending=None,
                 fetch_signed_manifest=None):
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
        # What the board says it is. Set from hello; None until it speaks.
        self._board_proto = None
        self._board_fw = None
        self._announced_ahead = False
        # Pair updates. self_update replaces this program and does not return
        # when it works; pending remembers the consent across that restart;
        # _fetch_signed is the only manifest source allowed to decide that a
        # binary should be installed (see _app_available).
        self._self_update = self_update
        self._pending = pending
        if fetch_signed_manifest is None:
            from pc import update as _u
            fetch_signed_manifest = _u.fetch_signed_manifest
        self._fetch_signed = fetch_signed_manifest
        self._app_update = None          # (version, artifact) from last query

    # --- inbound ---
    def on_message(self, msg: dict):
        t = msg.get("t")
        if t == "hello":
            self._note_board(msg)
            self._write(protocol.welcome("clauge-bridge", self._app_ver))
            self.poll_once()             # push current data immediately
            self._resume_pending()
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

    def _note_board(self, hello):
        """Record what the board is, and notice when it outranks us.

        Both sides have always stamped "v" on every message and neither side
        has ever read one. That is fine while the protocol only grows -- new
        fields are ignored by whoever does not know them -- but it leaves no
        way to refuse the one case that is genuinely unsafe: firmware that
        speaks a protocol this daemon does not, being driven by this daemon
        through a firmware update.
        """
        try:
            self._board_proto = int(hello.get("v"))
        except (TypeError, ValueError):
            self._board_proto = None
        self._board_fw = hello.get("fw")
        if self._board_ahead() and not self._announced_ahead:
            print(f"[bridge] the board speaks protocol {self._board_proto} and"
                  f" this app speaks {PROTO_VERSION} -- update the app on this"
                  " computer; firmware updates are held until you do",
                  file=sys.stderr)
            self._announced_ahead = True

    def _board_ahead(self) -> bool:
        return self._board_proto is not None and self._board_proto > PROTO_VERSION

    def _ota_reset(self):
        self._manifest = None

    def _on_ota_query(self, cur):
        # Refuse to drive a board we may not understand. This daemon writes
        # slot0 in place, with no test boot behind it, so "probably fine" is
        # not a good enough basis for the one operation that can leave a
        # customer holding a device that does not start.
        if self._board_ahead():
            print(f"[bridge] ota: board speaks protocol {self._board_proto},"
                  f" this app speaks {PROTO_VERSION} -- not offering an update",
                  file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_none())
            return
        m = self._fetch_manifest()
        # A release may declare the protocol it needs to be installed over.
        # Absent (every release so far) means no floor.
        floor = ((m or {}).get("fw") or {}).get("proto_min")
        if isinstance(floor, int) and floor > PROTO_VERSION:
            print(f"[bridge] ota: {m.get('version')} needs protocol {floor} and"
                  f" this app speaks {PROTO_VERSION} -- update the app first",
                  file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_none())
            return
        if not m or not ota_mod.is_newer(m.get("version", ""), cur):
            have = m.get("version", "?") if m else "unreachable"
            print(f"[bridge] ota: board has {cur}, release has {have}"
                  " -- nothing to do", file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_none())
            return
        self._manifest = m
        # Does this release also carry a newer version of THIS program? If so
        # the board says so on the confirmation screen, because the customer is
        # about to approve two installs with one tap and should know it.
        self._app_update = self._app_available(m)
        app = self._app_update[0] if self._app_update else None
        print(f"[bridge] ota: offering {m['version']} ({m['size']} bytes)"
              + (f", app {app}" if app else ""), file=sys.stderr)
        self._write(protocol.ota_avail(m["version"], m["size"], m["sha256"],
                                       app=app))

    def _app_available(self, manifest):
        """(version, artifact) if this release has a newer daemon for us.

        Deliberately ignores the manifest it is handed. That one came from
        ota.fetch_manifest(), which does NOT check the signature -- and does
        not need to for firmware, because MCUboot will refuse an image that
        was not signed with the release key no matter what a manifest claims.

        A daemon binary has no such backstop. It is about to be run as a login
        service on the customer's machine, and the only thing standing between
        it and an attacker who can answer for the release URL is the manifest
        signature. Taking the version, size and sha256 from an unverified
        manifest would have meant the board-initiated update -- the one a
        customer actually taps -- skipping the check that the whole signing
        arrangement exists to perform, while the daily background check kept
        it. Fetch a signed one instead.
        """
        if self._self_update is None:
            return None
        try:
            from pc import update
            return update.available(self._fetch_signed())
        except Exception:
            return None

    def _resume_pending(self):
        """Finish an install the user approved before we replaced ourselves.

        Re-runs the ordinary query and flash path rather than trusting anything
        recorded on disk: the version is a note about consent, not about what
        is safe to install, so the protocol floor, the size and the hash are
        all checked again from the live manifest.
        """
        if not self._pending:
            return
        version = self._pending.take()
        if not version:
            return
        print(f"[bridge] ota: resuming the approved install of {version}",
              file=sys.stderr)
        self._on_ota_query(self._board_fw or "0.0.0")
        if not self._manifest or self._manifest.get("version") != version:
            print("[bridge] ota: the release moved on; not resuming",
                  file=sys.stderr)
            self._ota_reset()
            return
        # Put the board back on its progress screen. It has been sitting on an
        # "Install?" prompt for something it already agreed to.
        self._write(protocol.ota_resume(version))
        self._on_ota_flash()

    def _on_ota_flash(self):
        """The board approved. Fetch the image and hand it to the flasher.

        When the release also carries a newer daemon, that goes FIRST and this
        process is replaced -- the new daemon is the half that knows how to
        drive the new firmware, and installing them the other way round would
        leave the newest firmware being driven by the oldest app.
        """
        if self._manifest and self._app_update and self._self_update:
            version, artifact = self._app_update
            self._app_update = None
            fw_version = self._manifest["version"]
            print(f"[bridge] ota: updating this app to {version} first",
                  file=sys.stderr)
            if self._pending:
                self._pending.set(fw_version)
            if self._self_update(version, artifact):
                return          # unreachable in practice: we exit into the new
                                # binary, which picks the firmware back up
            # It failed and we are still here. The old app can still install
            # the firmware -- the protocol floor was already checked -- so do
            # that rather than leaving the customer with nothing.
            if self._pending:
                self._pending.take()
            print("[bridge] ota: app update failed; installing the firmware"
                  " with the current app", file=sys.stderr)
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
        # And check the hash, which for a while nobody did.
        #
        # The manifest has always carried sha256 and the board has always been
        # sent it, but over USB the board never sees the bytes -- the daemon
        # runs esptool -- so the verification that pc/ota.py's docstring claimed
        # was happening on the board could not have been. Length agreed with the
        # manifest and that was the whole check.
        #
        # It matters more here than it would over the WiFi path: slot0 is
        # written in place, so a bad image is not caught by a test boot and
        # rolled back, it just does not boot.
        digest = hashlib.sha256(blob).hexdigest()
        want = str(self._manifest["sha256"]).strip().lower()
        if digest != want:
            print(f"[bridge] ota: sha256 {digest} != manifest {want}",
                  file=sys.stderr)
            self._ota_reset()
            self._write(protocol.ota_error("sha256 mismatch"))
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
        # No second message for staleness any more. The usage message carries
        # `stale` and the firmware reads it (proto.c, via msg_get_bool), so the
        # board colours its own dot from the reading it was just given.
        #
        # What was here sent status "rate_limited" whenever the payload was
        # stale, purely because that string already mapped to amber -- which
        # left a stale reading and a real rate limit indistinguishable on the
        # panel, and put the wrong words in the log.

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
