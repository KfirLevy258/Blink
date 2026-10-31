"""Transport-agnostic bridge logic: react to board messages, poll usage, track
liveness. I/O (serial) is injected so this is unit-testable without hardware."""
import datetime
import hashlib
import queue
import sys
import threading
import time

from pc import ota as ota_mod
from pc import protocol
from pc import status_page
from pc.version import PROTO_VERSION, RELEASE_VERSION

LIVENESS_WINDOW_S = 30.0

# How long after a greeting no theme line is sent: one fast tick
# (claude_usage_bridge.FAST_POLL_INTERVAL_S). The reply to a hello is already
# the largest thing the daemon sends without pausing (tests/pc/
# test_hello_burst.py), and the board's theme_now answers that same welcome,
# so a save decided inside the burst would be decided on a report not yet
# heard. Addendum 2 of the theme contract: theme lines go out on the next
# fast tick instead.
THEME_QUIET_S = 2.0

# How long nothing else is written after a theme SAVE. The board answers it
# with a flash erase and write on its main thread (ui_theme.c, THEME_OP_SAVE
# -> cfg_set_theme -> persist), and an ESP32 flash erase masks the console
# UART's interrupt: for its length only the 128-byte FIFO, about 11 ms of
# line at 115200 baud, keeps what the daemon sends. A usage line written
# right behind a save arrived with its middle missing -- page 2 dropped out,
# the pips went wrong -- in 99 of 99 simulated cases with an erase of 30 ms
# or more (firmware review, 2026-10-31). cfg_store.c puts the whole erase and
# write at "tens of milliseconds", so 0.5 s is several times that, and it is
# short beside the 2 s fast tick, the 60 s heartbeat and the 30 s the board is
# given before it is called lost. The board's theme_now why=saved, which it
# sends only after the store has finished, ends the hold early. Only a save
# holds: preview and cancel write no flash.
THEME_SAVE_HOLD_S = 0.5

# The burn line's cadence (docs/plans/burn-5h.md): after the quiet window
# that follows a greeting -- THEME_QUIET_S, for the same reason, since it is
# not part of the hello burst -- then whenever q or b changes, never sooner
# than BURN_MIN_GAP_S after the last one, and at least every BURN_HEARTBEAT_S
# so a board that lost a line, or rebooted without a hello reaching us, has
# it back within a minute. The board keeps the view up for 180 s after the
# last line, three heartbeats.
BURN_MIN_GAP_S = 15.0
BURN_HEARTBEAT_S = 60.0


def _spawn(fn):
    threading.Thread(target=fn, name="blink-theme-open", daemon=True).start()

# The 429 backoff that lived here is gone with the endpoint it was for.
#
# fetch_usage now reads a file Claude Code wrote (pc/statusline_source.py); it
# has no server to be throttled by, and read_payload() turns every failure into
# None rather than an exception. The ladder, the Retry-After parsing, the
# _throttled_until gate and their tests all described a branch that could not
# execute -- which is worse than absent, because it read as "rate limits are
# handled here" to anyone deciding what to do next.


def _local_wall():
    """(unix seconds, local UTC offset in minutes) -- DST-aware."""
    now = datetime.datetime.now().astimezone()
    return int(now.timestamp()), int(now.utcoffset().total_seconds() // 60)


def _version_for_log(fw) -> str:
    """A board's firmware version as a log line may say it: rebuilt from the
    integers protocol._fw_tuple parsed, or "an unreadable version". Never the
    string as it came off the wire -- see the ota "board has" line."""
    parts = protocol._fw_tuple(fw)
    # Bounded as well as parsed: int() takes any run of digits, so without a
    # ceiling "1.2.<four thousand digits>" would still carry four thousand
    # characters of whatever the board chose into the log.
    if parts is None or not all(0 <= p <= 9999 for p in parts):
        return "an unreadable version"
    return "%d.%d.%d" % parts


def _proto_for_log(proto) -> str:
    """The board's protocol number as a log line may say it.

    It is int(msg["v"]), so it cannot carry text, but it is still a value
    off the wire with no length: review of this branch (2026-10-30) put a
    ping with a 300-digit v through and got all 300 digits in bridge.log.
    Bounded the way _version_for_log is. Only ever printed for a board ahead
    of us, so anything out of range is still truthfully "a newer protocol".
    """
    if isinstance(proto, int) and 0 <= proto <= 9999:
        return f"protocol {proto}"
    return "a newer protocol"


class Bridge:
    def __init__(self, write_msg, fetch_usage, now=time.monotonic,
                 set_preferred=None,
                 app_ver=RELEASE_VERSION,
                 wall=_local_wall, fetch_manifest=None, fetch_firmware=None,
                 flash_image=None, self_update=None, pending=None,
                 fetch_signed_manifest=None, report_failure=None,
                 status_watch=None, board_caps_hint=None,
                 theme_sync=None, open_builder=None, run_async=_spawn,
                 edit_answers=None, builder_ended=None, builder_open=None,
                 sleep=time.sleep):
        self._write = write_msg          # callable(dict)
        self._fetch = fetch_usage        # callable() -> usage message dict
        # The board owns the primary-provider preference; this applies it.
        # None on a daemon wired without a bus (the tests do this).
        self._set_preferred = set_preferred
        self._now = now
        self._last_query_at = None       # see offer_if_newer
        self._wall = wall                # callable() -> (epoch_s, utc_offset_min)
        self._app_ver = app_ver
        self._last_ping = None
        # OTA. Injected so the transfer is testable without GitHub.
        self._fetch_manifest = fetch_manifest or ota_mod.fetch_manifest
        self._fetch_firmware = fetch_firmware or ota_mod.fetch_firmware
        self._manifest = None            # release offered to the board
        self._flash = flash_image        # callable(blob, version); owns the port
        # What the board says it is. Set from hello; None until it speaks.
        self._board_proto = None
        self._board_fw = None
        # Which board this is, for the theme's "seen" record (theme contract
        # Addendum 4), and only as the wire said it: a hello, or a pref,
        # which names the board from firmware 1.4.0 on and answers every
        # welcome (Addendum 5). Never board.json's id. A hot-plugged board's
        # hello usually reaches nobody -- it is sent about a second after
        # power-up, and the daemon polls for ports every 3 s -- so board.json
        # can name the previous board for a whole connection, and a theme
        # adopted on its say-so was the wrong board's (review of fp/fix-pc,
        # S1, reproduced). None until the wire says.
        self._board_id = None
        self._announced_ahead = False
        # Pair updates. self_update replaces this program and does not return
        # when it works; pending remembers the consent across that restart;
        # _fetch_signed is the only manifest source allowed to decide that a
        # binary should be installed (see _app_available).
        self._self_update = self_update
        self._pending = pending
        # A flash that failed on the previous connection, to be delivered once
        # the board is back. It cannot be told at the time: the port is closed
        # for esptool and the board is mid-reset.
        self._report_failure = report_failure
        self._said_no_source = False
        if fetch_signed_manifest is None:
            from pc import update as _u
            fetch_signed_manifest = _u.fetch_signed_manifest
        self._fetch_signed = fetch_signed_manifest
        self._app_update = None          # (version, artifact) from last query
        self._last_session = None        # (label, n) last sent; see poll_once
        # The status-page watch (pc/status_page.StatusWatch), or None on a
        # Bridge wired without one -- every test fake -- which then sends no
        # svc message at all, exactly as a daemon from before it did.
        self._svc = status_watch
        # What the board can show (protocol.board_caps): None until its
        # hello or its pref has said, then whatever the last of those
        # listed. Every board that has caps puts them on both, so a pref
        # that lists none is a board without them -- not one that forgot.
        self._board_caps = None
        # What board.json says the last board listed. Only a stand-in until
        # this one speaks: a daemon that restarts under a running board gets
        # no hello, and greet()'s first poll runs before the pref that
        # answers its welcome has arrived.
        self._board_caps_hint = (frozenset(board_caps_hint)
                                 if isinstance(board_caps_hint, (list, tuple))
                                 else None)
        self._svc_pair = None            # (provider, p2) of the last usage
        self._svc_pages = ()             # those with a status page, in order
        self._last_svc = {}              # provider -> (level, what) last sent
        # The last usage message actually PUT ON THE WIRE, minus the fields
        # that move on their own (protocol.VOLATILE_USAGE_KEYS). This is what
        # poll_if_changed decides against: "sent", not "read", because the
        # capping on the way out is part of what the board received.
        self._last_pushed = None
        # Themes (pc/theme.py). theme_sync owns the files and what the
        # board is believed to hold; open_builder starts the builder and a
        # browser on it, returning whether one opened, and is rate-limited
        # itself (theme_server.Launcher). Both None on a Bridge wired without
        # them -- every older test -- which then sends no theme line at all
        # and answers theme_edit with ok 0.
        self._theme = theme_sync
        self._open_builder = open_builder
        # callable() -> True once when the builder open_builder opened has
        # ended (Launcher.ended); pump() then tells the board (Addendum 6).
        self._builder_ended = builder_ended
        # callable() -> True/False/None: is that builder open right now
        # (Launcher.editing)? Said in every welcome greet() writes, so a
        # board that rebooted or was away while it opened or ended holds or
        # frees "Edit on computer" to match. None (not wired) leaves the
        # field out, and the board as it is.
        self._builder_open = builder_open
        self._edit_said_at = None        # last edit_state; see _restate_edit
        self._last_ping_wall = None      # see _edit_after_silence
        # The open runs on run_async's thread: starting a browser can take a
        # second or more, and this object lives on the serial thread. The
        # answer comes back through the queue and pump() writes it, so only
        # the serial thread ever writes to the port. The daemon hands in one
        # queue for its whole life, so an open that returns after a
        # reconnect is answered on the new connection.
        self._run_async = run_async
        self._edit_answers = (edit_answers if edit_answers is not None
                              else queue.SimpleQueue())
        self._theme_quiet_until = None
        self._sleep = sleep
        self._store_until = None         # see THEME_SAVE_HOLD_S
        # The burn line (see BURN_MIN_GAP_S). What was last put on the wire,
        # as (p, q, b), and when by self._now; None for nothing since the
        # last greeting or since the line stopped.
        self._burn_sent = None
        self._burn_sent_at = None
        self._burn_quiet_until = None
        self._burn_failed = False

    def greet(self):
        """Introduce ourselves and push what we have, immediately.

        Normally this answers the board's boot `hello`. It is also called
        directly when the daemon found a board that was ALREADY running and
        chose not to reset it (claude_usage_bridge.probe_is_our_board): there
        is no hello in that case, and without this the first usage message
        waits for the 60 s poll -- which is itself gated on board_alive(),
        false until the first ping, so the real wait is a minute of blank
        panel after every service restart. Measured doing exactly that before
        this existed.
        """
        editing = None
        if self._builder_open is not None:
            try:
                editing = self._builder_open()
            except Exception as e:
                # A welcome must go out whatever this says; without the
                # field the board keeps the slot as it is.
                print(f"[bridge] builder state unknown: {e}",
                      file=sys.stderr)
        self._write(protocol.welcome("blink-bridge", self._app_ver,
                                     editing=editing))
        self._store_until = None         # a new conversation, a new board
        if self._theme is not None:
            # A greeting starts a new conversation: the board answers it with
            # theme_now why=boot, and nothing about themes is said until a
            # fast tick after that (THEME_QUIET_S).
            self._theme.hello()
            self._theme_quiet_until = self._now() + THEME_QUIET_S
        # The burn line waits out the same quiet window, BEFORE the poll
        # below: it is not part of the hello burst, which is measured
        # against the board's receive ring (tests/pc/test_hello_burst.py),
        # and the pref that answers this welcome may still be on its way
        # with the caps that decide whether the line goes at all. A board
        # that has just booted holds no burn line, so whatever went to the
        # last connection is forgotten and the first tick after the window
        # sends one, unchanged or not.
        self._burn_sent = None
        self._burn_sent_at = None
        self._burn_quiet_until = self._now() + THEME_QUIET_S
        if self._report_failure:
            self._write(protocol.ota_error(self._report_failure))
            self._report_failure = None
        # A board that just booted holds no session message, and poll_once
        # only sends on change -- so on a reconnect the tracker is what makes
        # it silent. Clear it BEFORE the poll, so the push happens inside it,
        # the same shape as offer_if_newer on every connect rather than once
        # per daemon lifetime.
        self._last_session = None
        # The status level needs no reset here: poll_once re-states it on
        # every heartbeat, and this is one (see protocol.svc's contract).
        self.poll_once()                 # push current data immediately
        # Only once the board has been heard. On the no-reset connect path
        # greet() runs before any message has reached on_message, so
        # _board_proto is still None -- and _board_ahead() reads None as
        # "not ahead", which let a resumed flash skip the one guard that
        # keeps this daemon from writing slot0 on a board it does not
        # understand, while _on_ota_query latched _board_fw to the fallback
        # "0.0.0". The next message the board sends (its pref answers our
        # welcome; a ping follows within 10 s) arms the guard, and
        # on_message resumes then.
        if self._board_proto is not None:
            self._resume_pending()

    # --- inbound ---
    def on_message(self, msg: dict):
        t = msg.get("t")
        # Learn the board's protocol version from ANY message, not just hello.
        #
        # _board_ahead() gates the one operation that can leave a customer
        # holding a device that does not start, and it used to depend entirely
        # on _note_board, reachable only from the hello branch. hello is sent
        # once, at boot (proto.c send_hello) -- so as soon as the daemon
        # learned to connect to an already-running board WITHOUT resetting it,
        # there was no hello, _board_proto stayed None, and the guard degraded
        # silently to "allow". Found by review the same night that path landed.
        #
        # Every message the board sends carries its own "v", so the guard no
        # longer needs a reboot to arm. Firmware version comes from ota_query's
        # "cur" below, which is the only other thing _note_board supplied.
        if self._board_proto is None:
            try:
                self._board_proto = int(msg["v"])
            except (KeyError, TypeError, ValueError, OverflowError):
                # OverflowError because json reads 1e400 as infinity and
                # int(inf) raises it; uncaught, it left the read loop and
                # ended the daemon.
                pass
            else:
                self._announce_if_ahead()
                # The board has spoken, so the guard is armed: pick up an
                # approved install that greet() had to leave waiting. Not on
                # hello -- greet() below does it there, after the welcome,
                # so the board is greeted before it is asked to resume.
                if t != "hello":
                    self._resume_pending()
        if t in ("hello", "pref"):
            self._board_caps = protocol.board_caps(msg)
        if t == "pref":
            # The board's id, from firmware that puts it on pref (Addendum
            # 5). One without it says nothing either way: older firmware
            # named itself in the hello only, and that id stands.
            bid = self._wire_id(msg)
            if bid is not None:
                self._board_id = bid
            # Which provider the user picked on the board's settings screen.
            # It arrives with every hello as well as on change, so a daemon
            # that restarts picks the choice back up without the user
            # touching anything.
            want = msg.get("provider")
            if self._set_preferred and isinstance(want, str):
                if self._set_preferred(want):
                    print(f"[bridge] main source: {want}", file=sys.stderr)
            return
        if t == "hello":
            self._note_board(msg)
            self.greet()
            self.offer_if_newer(msg.get("fw"))
        elif t == "ping":
            self._edit_after_silence()
            self._last_ping = self._now()
            # Free: never fetch here. The usage endpoint is aggressively
            # rate-limited and this fires every 10 s.
            self._write(protocol.pong())
        elif t == "ota_query":
            # The board's own word on what it runs ALWAYS wins. _on_ota_query
            # only fills an empty _board_fw, which is right for the guesses
            # that also reach it -- board.json's version via offer_if_newer,
            # and the "0.0.0" fallback -- and wrong for this: on a reconnect
            # without a reset, board.json's 1.3.4 was latched first, and a
            # board that had just updated itself to 1.4.0 said so and was
            # ignored until the next reconnect.
            cur = msg.get("cur", "")
            if protocol._fw_tuple(cur) is not None:
                self._board_fw = cur
            self._on_ota_query(cur)
        elif t == "ota_flash":
            self._on_ota_flash()
        elif t == "theme_now":
            # Recorded, never answered here: whatever it makes owed goes out
            # on the next tick (see THEME_QUIET_S).
            if msg.get("why") == "saved":
                self._store_until = None     # the store has finished
            if self._theme is not None:
                self._theme.board_now(msg, board_id=self._board_id)
        elif t == "theme_edit":
            self._on_theme_edit()
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
        except (TypeError, ValueError, OverflowError):    # 1e400 is inf
            self._board_proto = None
        self._board_fw = hello.get("fw")
        # A hello's word is final, an absent id included: a board that
        # cannot read its own id is not one to compare with a record.
        self._board_id = self._wire_id(hello)
        self._announce_if_ahead()

    @staticmethod
    def _wire_id(msg):
        bid = msg.get("board_id")
        # The firmware sends "unknown" when hwinfo has no id to give.
        if not isinstance(bid, str) or not bid or bid == "unknown":
            return None
        return bid

    def _announce_if_ahead(self):
        if self._board_ahead() and not self._announced_ahead:
            print(f"[bridge] the board speaks"
                  f" {_proto_for_log(self._board_proto)} and"
                  f" this app speaks {PROTO_VERSION} -- update the app on this"
                  " computer; firmware updates are held until you do",
                  file=sys.stderr)
            self._announced_ahead = True

    def _board_ahead(self) -> bool:
        return self._board_proto is not None and self._board_proto > PROTO_VERSION

    def _ota_reset(self):
        self._manifest = None

    def offer_if_newer(self, fw):
        """Check the feed for a board running `fw`, unasked.

        A board asks for itself once per boot and when its update row is
        tapped, and that is all it ever asked. So a board that stayed
        plugged in through three app updates never heard about the firmware
        that shipped with them (desk board on 1.0.0 with 1.1.1 published,
        2026-08-29), and a tap that landed while the app was restarting for
        one of those updates went unanswered forever. Now every connect --
        the board's hello, or a reconnect to a board already running --
        is a check, and the reply either shows "Install x.y.z" or "Up to
        date", clearing a stale "Checking..." either way. Skipped when the
        board itself asked within the last minute, so its boot-time query
        is not answered twice.
        """
        if not fw or self._fetch_manifest is None:
            return
        if self._last_query_at is not None and \
                self._now() - self._last_query_at < 60:
            return
        self._on_ota_query(fw)

    def _on_ota_query(self, cur):
        self._last_query_at = self._now()
        # The board's firmware version, from the board, on every query. The
        # other half of what _note_board used to be the only source of -- and
        # _resume_pending falls back to "0.0.0" without it, which would compare
        # a real release against a fabricated version.
        if cur and not self._board_fw:
            self._board_fw = cur
        # Refuse to drive a board we may not understand. This daemon writes
        # slot0 in place, with no test boot behind it, so "probably fine" is
        # not a good enough basis for the one operation that can leave a
        # customer holding a device that does not start.
        if self._board_ahead():
            print(f"[bridge] ota: board speaks"
                  f" {_proto_for_log(self._board_proto)},"
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
            # Costs one signed fetch, and buys the board the answer to a
            # question it was otherwise left to guess at. This is the state a
            # working desk sits in, so it is the one that decides whether the
            # update row tells the truth.
            self._app_update = self._app_available(m)
            app = self._app_update[0] if self._app_update else None
            # The board's version as the three numbers this daemon parsed out
            # of it, never the string itself: `cur` is the raw ota_query
            # field (or board.json's copy of an earlier one), and CLAUDE.md
            # keeps serial-buffer values out of the log. Re-formatting the
            # parsed integers keeps the one thing support needs from this
            # line -- which firmware the desk runs -- and nothing a board
            # could put there that is not a version.
            print(f"[bridge] ota: board has {_version_for_log(cur)},"
                  f" release has {have}"
                  " -- nothing to do" + (f", app {app}" if app else ""),
                  file=sys.stderr)
            self._manifest = None
            self._write(protocol.ota_none(app=app))
            return
        self._manifest = m
        # Does this release also carry a newer version of THIS program? If so
        # the board says so on the confirmation screen, because the customer is
        # about to approve two installs with one tap and should know it.
        self._app_update = self._app_available(m)
        why = self._app_pairing(m)
        if why:
            # Not offered rather than offered half. A board a release ahead of
            # its app is a mismatch nothing on the panel can fix, and this is
            # the one moment where declining costs only a retry.
            print(f"[bridge] ota: not offering {m.get('version')} -- {why}",
                  file=sys.stderr)
            app = self._app_update[0] if self._app_update else None
            self._ota_reset()
            self._write(protocol.ota_none(app=app))
            return
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

    def _app_pairing(self, m):
        """Why this app cannot pair with release `m`, or "" when it can.

        The board can end up a release AHEAD of the app on this computer, and
        then the half that is behind is the half the panel cannot reach: the
        board's update row says the app is old and no tap on it can help.
        1.3.1 made the board tell the truth about that; this is what stops it
        happening by accident.

        Firmware and the daemon ship from one tag and are meant to install
        together -- _on_ota_flash does the app first, precisely so the newest
        firmware is never driven by the oldest app. The pair splits when the
        app half cannot be worked out at all: fetch_signed_manifest returns
        None on a network blip or a signature that does not verify, and
        _app_available answers None either way, which is indistinguishable
        from "this release has no newer app". Offering the firmware anyway
        turns a transient failure into a permanent mismatch.

        A platform with no published daemon build is a different thing and
        gets a different answer -- there the pair CANNOT be kept, by design,
        and refusing firmware forever would strand those machines. They are
        offered it, with a line in the log saying why.
        """
        version = m.get("version", "")
        if not ota_mod.is_newer(version, RELEASE_VERSION):
            return ""           # this app is level with the release or ahead
        if self._self_update is None:
            return ""           # not running as a daemon that can replace itself
        try:
            from pc import update
            if update.platform_key() is None:
                print(f"[bridge] ota: {version} publishes no app for this"
                      " platform; the board will run ahead of the app",
                      file=sys.stderr)
                return ""
        except Exception:
            pass
        if self._app_update is not None:
            return ""           # both halves in hand: _on_ota_flash pairs them
        return (f"release {version} is newer than this app ({RELEASE_VERSION})"
                " but its signed manifest could not be read")

    def _resume_pending(self):
        """Finish an install the user approved before we replaced ourselves.

        Re-runs the ordinary query and flash path rather than trusting anything
        recorded on disk: the version is a note about consent, not about what
        is safe to install, so the protocol floor, the size and the hash are
        all checked again from the live manifest.
        """
        if not self._pending:
            return
        from pc import update
        version, tries = self._pending.read()
        if not version:
            return
        print(f"[bridge] ota: resuming the approved install of {version}",
              file=sys.stderr)
        self._on_ota_query(self._board_fw or "0.0.0")
        if not self._manifest:
            #
            # NOTHING WAS ATTEMPTED, so the consent is not spent.
            #
            # This branch is reached when the query could not produce a
            # manifest at all -- the feed did not answer, or the signature did
            # not verify. It says nothing about the image the user approved,
            # and it is the likeliest moment in the whole update for a
            # transient to land: the daemon has just replaced itself and is
            # asking the network a question within seconds of starting.
            #
            # Spending the approval here is what left a customer's board on
            # 1.2.5 with the app on 1.3.2 (2026-09-10), the daemon answering
            # `ota_none`, and nothing on the panel to say an update had been
            # agreed to and dropped. The board will not re-offer inside the
            # same boot either, so the two faults compound into a board that
            # simply never updates again.
            #
            # RESUME_TRIES bounds it: retry on the next connect, and when the
            # tries are gone say so out loud rather than looping in silence.
            attempt = tries + 1
            if attempt >= update.RESUME_TRIES:
                self._pending.clear()
                print(f"[bridge] ota: could not resume {version} after"
                      f" {attempt} attempts; giving up", file=sys.stderr)
                self._write(protocol.ota_error(
                    "could not read the release feed"))
            else:
                self._pending.bump()
                print(f"[bridge] ota: no manifest yet; {version} stays"
                      f" approved (attempt {attempt} of"
                      f" {update.RESUME_TRIES})", file=sys.stderr)
            self._ota_reset()
            return
        if self._manifest.get("version") != version:
            # A different fault: the feed answered, and what it is serving is
            # not what the user agreed to. Retrying cannot change that, so the
            # approval goes rather than riding along onto an image nobody
            # consented to.
            self._pending.clear()
            print("[bridge] ota: the release moved on; not resuming",
                  file=sys.stderr)
            self._ota_reset()
            return
        # From here an install is genuinely attempted, which is what the
        # approval was for. Spend it BEFORE the flash, so a failure that gets
        # as far as writing does not come back on every reconnect.
        self._pending.clear()
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
        # Tell the board to remember what it is about to become, now that
        # nothing else can come between this and esptool taking it away.
        self._write(protocol.ota_begin(version))
        self._flash(blob, version)

    # --- themes ---

    def _board_can(self, cap):
        caps = self._board_caps
        if caps is None:
            caps = self._board_caps_hint
        return caps is not None and cap in caps

    def _takes_theme(self):
        return self._board_can(protocol.BOARD_CAP_THEME)

    def _send_theme(self):
        """The preview, cancel or save the files say is owed. Called on both
        ticks; the fast one is what makes a preview follow the builder.

        True when a save was written, which the board stores to flash: the
        caller must then write nothing more this tick (THEME_SAVE_HOLD_S).
        """
        if self._theme is None:
            return False
        if (self._theme_quiet_until is not None
                and self._now() < self._theme_quiet_until):
            return False
        try:
            lines = self._theme.outgoing(deliver=self._takes_theme())
        except Exception as e:
            # A theme is never worth the serial loop. Only the reason is
            # printed; theme.ThemeError's words never hold file contents.
            print(f"[bridge] theme not sent: {e}", file=sys.stderr)
            return False
        stored = False
        for m in lines:
            self._write(m)
            stored = stored or m.get("op") == "save"
        if stored:
            self._store_until = self._now() + THEME_SAVE_HOLD_S
        return stored

    def _store_wait(self):
        """Seconds left of the hold after a save: never more than
        THEME_SAVE_HOLD_S, whatever the clock does."""
        if self._store_until is None:
            return 0.0
        return max(0.0, min(THEME_SAVE_HOLD_S, self._store_until - self._now()))

    def _on_theme_edit(self):
        """Settings > Theme > "Edit on computer" on the board.

        Every tap is answered, because the board counts down 5 s to its own
        "Run blink theme" fallback each time. The Launcher decides whether a
        tap opens a new tab; its rate limit outlives a reconnect, which one
        kept here would not.
        """
        opener = self._open_builder
        answers = self._edit_answers
        if not self._takes_theme():
            # A board that did not say it takes themes would be sent none
            # (_send_theme), so a builder opened for it could save nothing
            # the panel ever shows. ok 0 puts "Run blink theme" on the board
            # instead of "Opened on your computer" (Addendum 3). No firmware
            # without the cap has the button; this is for one that does and
            # lost its hello, until its next pref says otherwise.
            answers.put(False)
            self.pump()
            return

        def work():
            ok = False
            if opener is not None:
                try:
                    ok = bool(opener())
                except Exception as e:
                    print(f"[bridge] theme builder did not open: {e}",
                          file=sys.stderr)
            if not ok:
                print("[bridge] theme builder: no browser to open here; the"
                      " board says to run blink theme", file=sys.stderr)
            answers.put(ok)
        self._run_async(work)
        self.pump()

    def pump(self):
        """Write any theme_edit answers that are ready. Called by the daemon
        on every pass of its serial loop, so an answer goes out within one
        read timeout of the browser opening -- well inside the board's 5 s."""
        while True:
            try:
                ok = self._edit_answers.get_nowait()
            except queue.Empty:
                break
            self._write(protocol.theme_edit_reply(ok))
        # After any answer, so a board that asked again as the old builder
        # ended hears "ok" for the new one before "done" for the old: its
        # done is ignored while an ask is in flight, never the other way.
        # The board's cap first, so a done for a board not yet heard from
        # (a fresh connection before its hello) waits for it rather than
        # being used up on nobody (review r2, NICE 3); and only to firmware
        # that says it handles the line.
        ended = self._builder_ended
        if (ended is not None
                and self._board_can(protocol.BOARD_CAP_EDIT_DONE)
                and ended()):
            self._write(protocol.theme_edit_done())
            # One line, for windows-check 10c, which times the board's slot
            # coming back against it. Says that it was sent, never anything
            # read off the wire.
            print("[theme] the builder ended; told the board",
                  file=sys.stderr)
        self._restate_edit()

    def _edit_after_silence(self):
        """On every ping, before it is counted: if the board was silent long
        enough to have declared HOST LOST, it has let go of "Edit on
        computer", so the next pump() says at once whether the builder is
        still open (_restate_edit).

        The silence is measured in wall time, not in self._now. That is
        time.monotonic, which on macOS (mach_absolute_time) and Linux stops
        while the computer sleeps -- 93.7 h of it against 193.6 h since
        boot, measured on the desk Mac (review of fp/edit-held r2,
        SHOULD-1; status_page.py moved to wall time for the same reason).
        Across the owner's lunch the monotonic gap was the few awake seconds
        either side, nothing was said until the next minute came round, and
        for up to 60 s the slot read "Edit on computer" over the open
        builder. Wall time sees that and a serial loop that stalled while
        the computer stayed awake alike. A wall clock that steps back counts
        as a silence too: it costs one 37-byte line, never a missed one."""
        wall = self._wall()[0]
        last, self._last_ping_wall = self._last_ping_wall, wall
        if last is None:
            return                       # the first ping: greet() just said
        gap = wall - last
        if gap > LIVENESS_WINDOW_S or gap < 0:
            self._edit_said_at = None

    def _restate_edit(self):
        """protocol.edit_state, every EDIT_STATE_EVERY_S and at once when the
        board is heard again after a silence (_edit_after_silence), so a
        board that freed its hold on HOST LOST holds again when the same
        connection wakes, and a hold over a builder that has gone lasts one
        restatement at most. From pump(), after the answers and the done, so
        a done is never contradicted by a state written before it. It can
        say a builder is open just before that builder's ok reaches the
        board -- launch() records the builder before work() queues the
        answer -- which is harmless: the board holds on either line.

        Never waits: Launcher.editing() answers None while launch() holds its
        lock, and this tries again on the next pass. Only to a board that is
        alive and lists BOARD_CAP_EDIT_DONE.

        Not behind a theme save: fp/daemon-fixes holds every write for
        THEME_SAVE_HOLD_S after one, because the board's flash erase masks
        its UART, and exposes how long is left as self._store_wait(). Where
        that exists this waits it out like the rest and is said on a later
        pass; on a branch without it the lookup finds nothing and costs
        nothing."""
        state = self._builder_open
        if (state is None or not self.board_alive()
                or not self._board_can(protocol.BOARD_CAP_EDIT_DONE)):
            return
        store_wait = getattr(self, "_store_wait", None)
        if store_wait is not None and store_wait() > 0:
            return
        now = self._now()
        if (self._edit_said_at is not None
                and now - self._edit_said_at < protocol.EDIT_STATE_EVERY_S):
            return
        try:
            editing = state()
        except Exception as e:
            print(f"[bridge] builder state unknown: {e}", file=sys.stderr)
            self._edit_said_at = now     # once a minute, not every pass
            return
        if editing is None:
            return                       # a launch in progress; next pass
        self._write(protocol.edit_state(editing))
        self._edit_said_at = now

    def board_alive(self) -> bool:
        return (self._last_ping is not None
                and self._now() - self._last_ping <= LIVENESS_WINDOW_S)

    # --- outbound ---
    #
    # Two cadences, answering two different questions.
    #
    # poll_once is the heartbeat: everything, unconditionally, once every
    # POLL_INTERVAL_S. It is also the only thing that sends `time`, which is
    # what the board re-anchors its clock from and has no reason to happen
    # more often than that.
    #
    # poll_if_changed is the fast tick, and it exists because session state --
    # RUNNING, WAITING, the per-session pips -- is a *now* signal. Claude
    # Code's hook rewrites its state file within a second or two of an event
    # (1.8 s on the live install, measured), and the panel was up to a minute
    # behind it: by the time "a session is waiting on you" reached the glass,
    # the owner had already looked at the screen and turned away.
    #
    # What this does NOT do is make the dials fast, and nothing here should be
    # read as claiming it does. The percentages come from Claude Desktop's own
    # cache, which that app refreshes every 5-15 minutes, so the arcs move no
    # sooner than they ever did. This is about state, not numbers.
    def poll_once(self):
        # A heartbeat is not skipped for a save in flight, as the fast tick
        # is: it waits out what is left of the hold, once, at most
        # THEME_SAVE_HOLD_S. The theme goes LAST in a tick, so that the one
        # tick that writes a save writes nothing behind it.
        wait = self._store_wait()
        if wait > 0:
            self._sleep(wait)
        self._store_until = None
        self._heartbeat()
        self._send_theme()

    def _heartbeat(self):
        # Time rides along with every heartbeat so the board's clock re-anchors
        # at the same cadence as the data.
        epoch, off = self._wall()
        self._write(protocol.time_msg(epoch, off))

        # The status level is re-stated with every heartbeat, and FIRST, so
        # that nothing below can skip it. svc goes out on change, and the
        # board drops a non-ok level it has not heard for 150 s (see
        # protocol.svc) -- so this is both the repair for a line that was
        # lost and the renewal that keeps a real incident up. It used to
        # ride on the usage path, and a provider the bus had quarantined
        # (usage None) left the last incident standing for as long as the
        # quarantine lasted. Pages are the last ones usage named.
        self._last_svc = {}
        self._send_svc_if_changed()

        try:
            usage = self._read_usage()
        except Exception as e:
            # Not expected: statusline_source swallows its own read errors and
            # returns None. Anything that reaches here is a bug worth putting
            # on the panel rather than only in a log nobody opens.
            self._write(protocol.status("error", str(e)[:80]))
            return

        if usage is None:
            # No statusline payload yet. The board keeps its last values and
            # its own dot, which is right -- but the daemon used to say nothing
            # at all, so a machine where Claude Code has simply never rendered
            # looked identical in the log to one where everything works.
            if not self._said_no_source:
                print("[bridge] no usage data yet -- open Claude Code once so"
                      " it renders its status line", file=sys.stderr)
                self._said_no_source = True
            return
        self._said_no_source = False

        # Unconditional. The heartbeat is what re-states everything to a board
        # that may have missed a message, and what keeps a panel that has been
        # sitting on the same numbers all afternoon provably connected.
        self._send_usage(usage)

    def poll_if_changed(self):
        """Send only when something a reader could act on has moved.

        Called every FAST_POLL_INTERVAL_S, on the same thread that reads the
        board's serial messages -- so whatever looking costs is latency this
        loop pays, not background work.

        Looking is no longer only JSON. The bus also walks Claude Desktop's
        LevelDB stores, and re-parsing those on every poll measured 0.47 s
        against a 769 KB store on the reference machine: a fifth of a core
        continuously, and a serial read loop stalled for a quarter of every
        interval, growing with the store. pc/leveldb now caches each parsed
        file against its (size, mtime), which puts an unchanged store back to
        0.0004 s; a poll costs a stat per file plus a fresh parse of whatever
        Chromium actually rewrote -- ordinarily just the small .log.

        Writing was never free either: the fully-loaded usage line runs to 454
        of the 512 bytes the firmware will accept (tests/pc/test_protocol.py
        holds the figure), and putting one on the wire
        every two seconds when nothing changed is thirty times the traffic
        carrying no news.

        Deliberately quieter than the heartbeat about failure. A fetch that
        raises, or a source that has not appeared yet, will be in the same
        state two seconds from now, so complaining here would turn one message
        a minute into thirty; poll_once already reports both at a cadence a
        person can actually read.
        """
        # Inside the hold after a save, nothing is written: the next tick, two
        # seconds on, finds the same news still owed.
        if self._store_wait() > 0:
            return
        # First, and before any return below: a preview is somebody watching
        # the board while they drag a colour, and it must not wait on usage.
        # A save ends the tick instead (THEME_SAVE_HOLD_S): usage is still
        # owed, since _last_pushed has not moved, and goes on the next one.
        if self._send_theme():
            return
        try:
            usage = self._read_usage()
        except Exception:
            usage = None
        if usage is None:
            # Nothing to say about usage, but the status pages still have
            # their own news, and want() still has to be renewed.
            self._send_svc_if_changed()
            return
        if protocol.meaningful_usage(usage) != self._last_pushed:
            self._send_usage(usage)
            return
        # The usage line is not the whole message the panel draws from, and
        # the project name is the part it cannot see. `label` rides its own
        # `session` message -- deliberately, because the usage line has six
        # bytes of headroom and a name is not six bytes -- so when a session
        # in project A ends and one in project B starts running, `state`
        # stays "running", every count stays where it was, and the usage dict
        # is byte-identical. The fast tick found nothing to send and the panel
        # went on naming project A until the next heartbeat, up to a minute
        # later. Naming the wrong project is a wrong statement, not a vague
        # one, and this tick exists precisely so that session moves reach the
        # board in two seconds rather than sixty.
        #
        # Only the name goes out in that case. Re-sending an identical usage
        # line to carry it would spend the budget this comparison exists to
        # protect.
        self._send_session_if_changed()
        # And the status level, which changes on the watch's own thread and
        # would otherwise wait for the next usage change to be noticed.
        self._send_svc_if_changed()
        # And the burn view, whose numbers move while the usage line holds
        # still: an Ultra desk's session dial says "--%" all day.
        self._send_burn_if_due()

    def _read_usage(self):
        """The usage message as it would go out right now, or None if there is
        nothing to report yet.

        Raises whatever the fetch raises: the two callers disagree about how
        loud to be, so neither the logging nor the status message belongs here.
        """
        usage = self._fetch()
        if usage is None:
            return None
        # Percentages above 100 are real -- extra usage puts them there -- but
        # firmware older than protocol.FW_ACCEPTS_OVERAGE turns them into 0 on
        # the panel rather than clamping. Hold them at 100 for those boards.
        return protocol.cap_overage_for_fw(usage, self._board_fw)

    def _send_usage(self, usage):
        """Put a usage message on the wire and remember what was in it."""
        self._write(usage)
        # Recorded here, once, so that no path can push without moving the
        # baseline -- a push that forgot to would leave the fast tick
        # re-sending the same message every two seconds forever.
        self._last_pushed = protocol.meaningful_usage(usage)

        self._send_session_if_changed()

        # Which pages the board has is read off the line it was just sent:
        # `provider` is the outer ring and `p2`, when present, the inner one
        # and the second page. That is the only place the daemon knows it,
        # and it is what keeps a Claude-only desk from ever asking
        # status.openai.com anything.
        #
        # When that pair changes -- the second page drops out or comes back,
        # or the owner swaps which provider is main -- everything sent so far
        # is forgotten and each page is told again. The board does not keep a
        # page's status while the page is gone, so "it was told an hour ago"
        # is not something it still knows.
        pair = (usage.get("provider"), usage.get("p2") or None)
        if pair != self._svc_pair:
            self._svc_pair = pair
            self._last_svc = {}
            # A provider with no status page (OpenCode) gets no message,
            # rather than an "ok" that would claim somebody had checked.
            self._svc_pages = tuple(p for p in status_page.PAGES
                                    if p in pair)
        self._send_svc_if_changed()
        # After the usage line it belongs beside: both come from the same
        # poll, and the board draws the view only while that line says the
        # session is unknown.
        self._send_burn_if_due()

        # No second message for staleness any more. The usage message carries
        # `stale` and the firmware reads it (proto.c, via msg_get_bool), so the
        # board colours its own dot from the reading it was just given.
        #
        # What was here sent status "rate_limited" whenever the payload was
        # stale, purely because that string already mapped to amber -- which
        # left a stale reading and a real rate limit indistinguishable on the
        # panel, and put the wrong words in the log.

    def _send_session_if_changed(self):
        """Put the project name on the wire, on change only.

        Its own message because the usage line has six bytes of headroom and
        a project name is not six bytes. Split out of _send_usage so the fast
        tick can send a name WITHOUT re-sending an unchanged usage line --
        see poll_if_changed, where a project change is otherwise invisible.

        Read off the fetch callable rather than passed through it: the Bridge
        is handed a zero-arg callable returning a finished usage dict and
        never sees the frame the name lives on. A fetch without the accessor
        -- every test fake, and the single-source fetch -- simply sends
        nothing, which is what an older daemon did anyway.
        """
        session_pair = getattr(self._fetch, "session_pair", None)
        if session_pair is None:
            return
        pair = tuple(session_pair())
        if pair != self._last_session:
            self._write(protocol.session(*pair))
            self._last_session = pair

    def _send_burn_if_due(self):
        """Put the burn line on the wire when its cadence says so.

        Read off the fetch callable, like session_pair: the bus's fetch has
        `burn`, every test fake and older fetch has not and sends nothing.
        Asked only for a board that lists BOARD_CAP_BURN, and not inside the
        quiet window or the minimum gap, so the transcripts behind it are
        read only when a line could actually go out.

        None from the fetch means "do not send": the claude page has a
        session window again, or the reader has nothing honest yet. The
        line then simply stops -- there is no "off" message; the board
        restores the gauge from the usage line itself -- and what was sent
        is forgotten, so the next line goes out as soon as the gap allows.
        """
        burn = getattr(self._fetch, "burn", None)
        if burn is None:
            return
        if not self._board_can(protocol.BOARD_CAP_BURN):
            # A board that says it can again (its next pref) holds nothing
            # from before, so it is sent the line as if new.
            self._burn_sent = None
            return
        now = self._now()
        if self._burn_quiet_until is not None and now < self._burn_quiet_until:
            return
        if (self._burn_sent_at is not None
                and now - self._burn_sent_at < BURN_MIN_GAP_S):
            return
        try:
            msg = burn()
        except Exception as e:
            # Never worth the serial loop. The type only: the text of an
            # exception from the reader can be a transcript's path.
            if not self._burn_failed:
                self._burn_failed = True
                print(f"[bridge] burn not sent: {type(e).__name__}",
                      file=sys.stderr)
            return
        self._burn_failed = False
        if msg is None:
            self._burn_sent = None
            return
        key = (msg.get("p"), msg.get("q"), tuple(msg.get("b") or ()))
        if (key == self._burn_sent
                and now - self._burn_sent_at < BURN_HEARTBEAT_S):
            return
        self._write(msg)
        self._burn_sent, self._burn_sent_at = key, now

    def _send_svc_if_changed(self):
        """Put each page's status-page level on the wire, on change only.

        Tells the watch which pages to fetch, then sends what it has for each.
        The watch is never asked to fetch here -- this runs on the serial
        loop, and the network belongs to the watch's own thread.
        """
        if self._svc is None:
            return
        # A board that has not listed "svc" drops it unread. Nothing is sent
        # to it, and want() is not renewed either, so no vendor is asked on
        # behalf of a board that could not show the answer.
        if not self._board_can(protocol.BOARD_CAP_SVC):
            return
        # Renewed on every call: a want() runs out (status_page.WANT_TTL_S),
        # and this is the call that stops being made when the board does.
        self._svc.want(self._svc_pages)
        for p in self._svc_pages:
            cur = tuple(self._svc.shown(p))
            if self._last_svc.get(p) != cur:
                self._write(protocol.svc(p, *cur))
                self._last_svc[p] = cur
