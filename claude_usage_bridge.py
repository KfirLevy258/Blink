#!/usr/bin/env python3
"""Serial daemon: bridge Claude usage to an ESP32 over USB-C.

Run inside the Zephyr venv (has pyserial) or `pip install pyserial`:
    python3 claude_usage_bridge.py --port /dev/cu.usbmodemXXXX
"""
import argparse
import os
import sys
import time

import serial  # pyserial
from serial.tools import list_ports

from pc import ota as ota_mod
from pc import ingest, install_statusline, protocol, statusline_source, update
from pc.version import RELEASE_VERSION
from pc.bridge import Bridge

POLL_INTERVAL_S = 60
PING_GRACE_LOG_S = 30
# How often to ask whether a newer version of THIS program exists.
#
# The first check waits for a board rather than firing at startup: a daemon
# that crashes on launch would otherwise hammer the release feed once every ten
# seconds forever, and a machine with no board attached has nothing to update
# for anyway.
UPDATE_FIRST_CHECK_S = 60
UPDATE_INTERVAL_S = 24 * 3600


# The USB-serial bridge chips these boards actually ship with, by VID:PID.
#
# Identify by ID, not by name: on macOS the CYD's CH340 reports manufacturer
# None and a device node of /dev/cu.usbserial-14140 -- neither "usbmodem" nor
# "espressif", the two things the old heuristic looked for. So autodetect never
# once matched the hardware this product runs on. It went unnoticed because
# tools/dev.sh always passes --port explicitly; nothing but a customer plugging
# in a board would have hit it.
KNOWN_USB_SERIAL = {
    (0x1A86, 0x7523),   # CH340/CH341 -- the common CYD
    (0x1A86, 0x7522),
    (0x1A86, 0x5523),
    (0x10C4, 0xEA60),   # CP2102/CP2104 -- the other CYD variant
    (0x0403, 0x6001),   # FT232R
}
ESPRESSIF_VID = 0x303A  # native USB-serial on -S2/-S3 parts


def autodetect_port():
    for p in list_ports.comports():
        if (p.vid, p.pid) in KNOWN_USB_SERIAL or p.vid == ESPRESSIF_VID:
            return p.device
    # Name heuristics kept as a fallback for a variant carrying a chip that is
    # not in the table yet. Bluetooth ports have no VID and no matching name,
    # so they cannot be picked up by either pass.
    for p in list_ports.comports():
        if "usbmodem" in p.device or (p.manufacturer or "").lower().startswith("espressif"):
            return p.device
    return None


# How long to wait for a device to identify itself before resetting it.
#
# The board answers `welcome` immediately (with ota_query, and usually pref),
# so this only has to cover the round trip and one poll interval.
PROBE_S = 1.5


def hold_single_instance(home, on_wait=None, poll_s=5.0):
    """Block until this process is the only daemon, then keep the lock.

    Two daemons on one board is not a hypothetical. On macOS a /dev/cu.* node
    is NOT exclusive -- two processes open it happily, and both then write
    usage messages and answer the board's pings on the same wire. Observed
    doing exactly that on 2026-08-28: a second daemon connected to a board the
    first was already driving, with no error from either. The board sees
    interleaved traffic and the user sees a panel that flickers between two
    sources with nothing in either log to explain it.

    Linux and Windows do make the port exclusive, so there the second process
    fails to open it and the message in the reconnect loop covers the case.
    This is for the platform where the OS will not say no.

    Waiting rather than exiting, for the same reason wait_for_port waits: the
    service is registered with KeepAlive/Restart=always, so exiting turns a
    duplicate launch into a process start and a log line every ten seconds for
    as long as both exist.

    Returns the open file object, which must stay referenced -- closing it
    releases the lock. POSIX only; on Windows the exclusive port is the lock.
    """
    try:
        import fcntl
    except ImportError:
        return None                      # Windows: the port itself is exclusive

    path = os.path.join(home, "bridge.lock")
    try:
        os.makedirs(home, exist_ok=True)
        fh = open(path, "w")
    except OSError:
        # An unwritable home is not a reason to refuse to run; it only means
        # this protection is unavailable.
        return None

    said = False
    while True:
        try:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            if said:
                print("[bridge] the other daemon exited; taking over",
                      file=sys.stderr)
            return fh
        except OSError:
            if not said:
                print("[bridge] another Clauge daemon is already running and"
                      " has the board; waiting for it to exit. Two of them on"
                      " one cable would interleave on the wire.",
                      file=sys.stderr)
                said = True
            if on_wait:
                on_wait()
            time.sleep(poll_s)


def probe_is_our_board(ser):
    """Ask the thing on this port whether it is a Clauge board, without a reset.

    Why this exists: the reset below is not free, and it is not aimed at a
    board we have identified -- it is aimed at whatever matched a VID:PID.
    1a86:7523 is the CH340, which is in Arduino clones, USB-serial adapters and
    a great deal of other hobbyist hardware. On a desk with any of it plugged
    in, the daemon could open a stranger's device and pulse its reset line.

    A `welcome` is the safe question. It is 60 bytes of JSON that a device
    which is not ours will ignore (it is text, and it toggles no control line),
    and one that IS ours answers within milliseconds. Answer -> skip the reset
    entirely; no answer -> fall through to the reset, which a mute or wedged
    board genuinely needs.

    A pleasant side effect: our own board stops being rebooted every time the
    daemon restarts, which it was, on every login and every service restart.
    """
    try:
        ser.reset_input_buffer()
        ser.write(protocol.encode(protocol.welcome("clauge-bridge",
                                                   RELEASE_VERSION)))
        deadline = time.time() + PROBE_S
        reader = protocol.LineReader()
        while time.time() < deadline:
            chunk = ser.read(256)
            if not chunk:
                continue
            for msg in reader.feed(chunk):
                # Any well-formed message of ours will do. The board sends
                # ota_query on welcome and pings on its own schedule; which one
                # arrives first is timing, and identity is the only question
                # being asked here.
                if isinstance(msg, dict) and msg.get("t"):
                    return True
    except Exception:
        # A probe that fails is not evidence of anything; fall through to the
        # reset and let the usual path decide.
        return False
    return False


def wait_for_port(explicit=None, poll_s=3.0, on_wait=None):
    """Block until there is a board to talk to, then return its device path.

    `on_wait` runs once per poll while we wait. Host-side upkeep that has
    nothing to do with the cable belongs here: a machine sitting with its
    board unplugged is exactly where this function spends its time, and until
    the callback existed the daemon did nothing at all in that state -- so a
    statusLine hook wiped while the board was out stayed wiped until somebody
    plugged it back in.

    Waiting rather than exiting is what keeps the installed service honest
    about "plug it in and it works". install.sh registers this daemon with
    launchd (KeepAlive) or systemd (Restart=always), which bring it back after
    every exit -- so exiting when no board is attached turns "the cable is
    unplugged" into a process launch and a log line every 10 seconds, all day,
    growing bridge.log without bound. One sleeping process is the cheaper
    answer, and the user never had to do anything for it.

    Re-detecting on each call also survives the board returning on a different
    device node, which a port resolved once at startup would not.
    """
    announced = False
    while True:
        port = explicit or autodetect_port()
        # os.path.exists() only means anything on POSIX, where a serial port
        # IS a filesystem node. Windows ports are DOS device names -- COM3 --
        # and os.stat on one fails, so this test was false for every Windows
        # machine with a board plugged in and the daemon there waited forever
        # for hardware it had already found.
        if port and (sys.platform == "win32" or os.path.exists(port)):
            if announced:
                print(f"[bridge] board found at {port}", file=sys.stderr)
            return port
        if not announced:
            # Once, not per attempt: this is the steady state on a machine
            # whose board is simply unplugged, and it is not an error.
            print(f"[bridge] waiting for the board ({explicit or 'USB'})...",
                  file=sys.stderr)
            announced = True
        if on_wait is not None:
            try:
                on_wait()
            except Exception as e:
                # Upkeep must never strand the wait. Failing here would leave
                # a plugged-in board undetected, which is a far worse outcome
                # than whatever the callback was trying to do.
                print(f"[bridge] upkeep failed while waiting: {e}",
                      file=sys.stderr)
        time.sleep(poll_s)


def _self_update_tick(target):
    """Check the signed feed and, if the release says so, replace ourselves.

    `auto` in the manifest is a switch we hold, not the customer: it ships
    false, and turning it on is a decision made per release. Without a remote
    off switch, a bad build would keep installing itself on every machine that
    checked, and nothing here could stop it.
    """
    home = os.path.dirname(os.path.dirname(target))   # ~/.clauge
    manifest = update.fetch_signed_manifest()
    found = update.available(manifest)
    if not found:
        return
    version, artifact = found
    if not ((manifest.get("daemon") or {}).get("auto")):
        print(f"[update] {version} is available; run `clauge update` to install"
              " it", file=sys.stderr)
        return
    if not update.auto_update_allowed(home):
        print(f"[update] {version} is available; automatic updates are turned"
              " off on this machine", file=sys.stderr)
        return
    print(f"[update] installing {version}", file=sys.stderr)
    try:
        blob = update.download(update.platform_key(), artifact)
    except Exception as e:
        print(f"[update] download failed: {e}", file=sys.stderr)
        return
    ok, message = update.apply(blob, target, version)
    print(f"[update] {message}", file=sys.stderr)
    if ok:
        update.restart_from_daemon(target)


def main(argv=None):
    """argv is passed explicitly by the `clauge run` subcommand.

    Without it this parsed sys.argv[1:], which inside the packaged binary is
    ["run"] -- the subcommand name itself. argparse rejected it, the process
    exited immediately, and the login service restarted it every ten seconds
    forever. It never ran once.
    """
    ap = argparse.ArgumentParser(prog="clauge run")
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args(argv)

    # Before anything else: if a previous update died between renaming the old
    # binary aside and moving the new one in, the login service is pointing at
    # a path that does not exist -- and would go on doing so at every boot,
    # silently. This is the one moment that can notice.
    from pc.cli import (_self_path, clauge_home as _clauge_home,
                        installed_bin, settings_path, shim_path)
    self_bin = installed_bin()
    clauge_home = _clauge_home()
    update.recover(self_bin)

    # Outside the reconnect loop, unlike next_poll. A board that comes and goes
    # -- a nudged cable, a laptop waking -- would otherwise re-arm the 60 s
    # first check on every reconnect, turning a daily update check into one per
    # reconnection.
    next_update = time.monotonic() + UPDATE_FIRST_CHECK_S
    report_failure = None       # a flash failure waiting for the board to return

    # Outside the reconnect loop for the same reason next_update is: its
    # interval and its give-up counter describe this machine, not this cable.
    #
    # statusLine is a single slot in a file shared with the user and with
    # Claude Code's own updates, and anything that rewrites settings.json can
    # drop our command silently. The symptom is not an error -- it is a panel
    # that stops updating while this program reports success, and the desktop
    # cache hides it further by going on feeding numbers for hours. See
    # install_statusline.drift_check for the one rule that matters: a missing
    # marker means the user uninstalled, and that is never overridden.
    watchdog = install_statusline.DriftWatchdog(settings_path(), shim_path())

    # Before the port is touched. The lock lives for the life of the process --
    # the file object is bound here so it is not garbage collected, which would
    # release it.
    _instance_lock = hold_single_instance(   # noqa: F841 -- held, not used
        clauge_home, on_wait=lambda: watchdog.tick())

    # Record the pid so uninstall can stop US specifically. Ending the login
    # service is not the same as ending this program, and killing by image name
    # is how the uninstaller once killed itself; a pid is unambiguous.
    #
    # Written NEXT TO THE BINARY rather than under ~/.clauge, because those are
    # not always the same place. A login service runs in the user's own
    # environment, not in whatever environment registered it -- so under the CI
    # harness, which redirects HOME to a temporary directory, the daemon
    # resolved ~ to the real profile and left its pid somewhere uninstall was
    # never going to look. Deriving it from sys.executable ties it to the
    # directory that actually has to be deleted, which is the thing the pid is
    # for.
    pid_file = os.path.join(os.path.dirname(_self_path()), "bridge.pid")
    try:
        with open(pid_file, "w") as f:
            f.write(str(os.getpid()))
    except OSError as e:
        print(f"[bridge] could not record the pid: {e}", file=sys.stderr)

    # Host-side upkeep that runs whether or not a board is attached. Passed
    # into wait_for_port below so an unplugged machine still repairs a wiped
    # hook instead of sitting idle until the cable comes back.
    def _upkeep():
        drifted = watchdog.tick()
        if drifted:
            print(f"[watchdog] {drifted}", file=sys.stderr)

    # Every provider, every source, behind one callable.
    #
    # Claude Code owns the credential and computes these numbers; we read
    # files it and the desktop app have already written. Nothing here
    # authenticates to Anthropic, and the daemon deliberately does not know
    # which providers exist -- pc/ingest owns that, so onboarding a second
    # tool never reaches this loop.
    bus = ingest.IngestionBus()
    fetch = bus.poll

    port = wait_for_port(args.port, on_wait=_upkeep)
    last_err = None

    while True:  # reconnect loop
        try:
            # Open WITHOUT asserting DTR/RTS, then deliberately reset the board.
            #
            # On the CYD, DTR drives GPIO0 and RTS drives EN through the CH340's
            # auto-reset circuit. Opening a tty on macOS momentarily toggles both,
            # so an open landing at the wrong moment (e.g. right after esptool's
            # own reset, while the board is still coming up) can latch GPIO0 low
            # and boot it into ROM download mode -- where it sits mute forever and
            # looks, very convincingly, like dead firmware.
            #
            # Configure-then-open keeps both lines de-asserted, and the explicit
            # RTS pulse below then reboots the board with GPIO0 held HIGH, so it
            # always comes up in run mode. It also means we reliably catch the
            # board's boot-time `hello` and can push usage immediately, instead of
            # waiting up to 60 s for the next poll.
            ser = serial.Serial()
            ser.port = port
            ser.baudrate = args.baud
            ser.timeout = 0.2
            ser.dtr = False
            ser.rts = False
            ser.open()
            # Ask before pulling the reset line. See probe_is_our_board: the
            # VID:PID that got us here belongs to a chip used by a great deal
            # of hardware that is not ours, and a reset is not a question, it
            # is an action taken on someone's device.
            already_running = probe_is_our_board(ser)
            if already_running:
                print(f"[bridge] {port} answered; not resetting it",
                      file=sys.stderr)
            else:
                ser.dtr = False      # GPIO0 HIGH -> boot the app, not the ROM loader
                ser.rts = True       # EN LOW  -> hold in reset
                time.sleep(0.15)
                ser.rts = False      # EN HIGH -> release; board boots
                time.sleep(0.3)
                ser.reset_input_buffer()
        except Exception as e:
            # Deduplicated: a board left unplugged, or a port the user lacks
            # permission to open, would otherwise write this same line to
            # bridge.log every three seconds for as long as the service runs.
            # Name the two failures a person can actually act on. Everything
            # else stays a bare message, but "busy" and "permission" were both
            # being reported as an unexplained open failure and then repeated
            # forever, which is how "the panel does nothing" became a support
            # question instead of a one-line fix.
            text = str(e).lower()
            if "busy" in text or "resource temporarily unavailable" in text:
                err = (f"{port} is busy -- something else has the board open"
                       " (a serial monitor, esptool, the Arduino IDE, or a"
                       " second copy of this daemon). Close it and this"
                       " reconnects on its own")
            elif "permission" in text or "access is denied" in text:
                err = (f"cannot open {port}: permission denied."
                       " On Linux the port is usually group `dialout` --"
                       " `sudo usermod -aG dialout $USER`, then log out and"
                       " back in")
            else:
                err = f"open {port} failed: {e}"
            if err != last_err:
                print(f"[bridge] {err}", file=sys.stderr)
                last_err = err
            time.sleep(3)
            port = wait_for_port(args.port, on_wait=_upkeep)
            continue
        # Cleared on success so a later, genuine failure is reported again
        # rather than silenced by having happened once before.
        last_err = None
        print(f"[bridge] connected on {port}", file=sys.stderr)
        reader = protocol.LineReader()

        def send(m):
            # ota_data is not logged: an image is ~5000 chunks and each line
            # carries 344 characters of base64, which would bury every other
            # message in the log. Bridge prints its own progress every 200.
            if m.get("t") != "ota_data":
                print(f"[bridge] -> {m}", file=sys.stderr)
            # encode_CHECKED. This is the only writer, and it used to call
            # plain encode() -- so protocol.encode_checked, written precisely
            # to guard the board's 512-byte cliff and documented as the thing
            # callers use, had no production caller at all and only tests
            # exercised it.
            #
            # It matters because the board does not truncate an over-long
            # line, it DROPS it whole (proto.c) with no error on either side:
            # the panel silently stops updating while this log keeps printing
            # the message as sent. A fully loaded two-provider frame already
            # measures 484 of the 512 bytes.
            raw, why = protocol.encode_checked(m)
            if raw is None:
                print(f"[bridge] NOT SENT: {why}", file=sys.stderr)
                return
            ser.write(raw)

        # The board approved an update. esptool needs the port to itself, so
        # close it, write slot0, and let the outer reconnect loop pick the
        # board back up -- esptool resets it into the new image on the way out.
        class Reflashed(Exception):
            """Carries the reason when the write failed.

            The port is closed by the time flash() returns and the board has
            just been reset, so it cannot be told on this connection. The
            reason rides out to the reconnect below and is delivered once the
            board is back -- the first moment it can be shown at all.
            """

            def __init__(self, why=None):
                super().__init__(why or "")
                self.why = why

        def flash_image(blob, version):
            # Let the board paint its warning first. esptool resets it into
            # the ROM loader, after which the panel is dead until the new
            # image boots -- so the "the screen goes dark for about 2 minutes"
            # frame has to be on screen BEFORE we take the port away, or the
            # blackout arrives with no explanation.
            time.sleep(4)
            print(f"[bridge] ota: closing port to flash {version}",
                  file=sys.stderr)
            try:
                ser.close()
            except Exception:
                pass
            ok, why = ota_mod.flash(port, blob)
            print(f"[bridge] ota: {'flashed ' + version if ok else 'FAILED: ' + why}",
                  file=sys.stderr)
            raise Reflashed(None if ok else why)

        def self_update(version, artifact):
            """Replace this program, then hand the cable to the new one.

            The port is closed first. On Windows the replacement is started
            before this process exits, and two daemons racing for one serial
            port is a failure that would look exactly like a broken board.
            """
            try:
                blob = update.download(update.platform_key(), artifact)
            except Exception as e:
                print(f"[update] download failed: {e}", file=sys.stderr)
                return False
            ok, message = update.apply(blob, self_bin, version)
            print(f"[update] {message}", file=sys.stderr)
            if not ok:
                return False
            try:
                ser.close()
            except Exception:
                pass
            update.restart_from_daemon(self_bin)     # does not return
            return True

        bridge = Bridge(write_msg=send, fetch_usage=fetch,
                        flash_image=flash_image,
                        report_failure=report_failure,
                        set_preferred=bus.set_preferred,
                        self_update=self_update,
                        pending=update.PendingFirmware(
                            os.path.join(clauge_home, "pending_fw.json")))
        report_failure = None   # handed to the Bridge above; never repeated
        # No reset means no boot `hello`, so nothing would trigger the
        # greeting -- see Bridge.greet.
        if already_running:
            bridge.greet()
        next_poll = time.monotonic()
        # The rollback copy is kept until a board has actually talked to this
        # build. Running at all is weak evidence; holding a conversation with
        # the hardware is the thing the update was for.
        proven = False
        try:
            while True:
                # in_waiting first, then a blocking read(1) as the idle wait.
                #
                # ser.read(n) does NOT return early on partial data: it waits
                # for n bytes or the full timeout. With a 0.2 s timeout that
                # cost 0.2 s per OTA chunk -- and since the transfer is
                # stop-and-wait, that stall WAS the transfer rate: 213 B/s
                # measured, ~100 minutes for a 1.3 MB image. Draining what has
                # actually arrived keeps the link busy instead.
                data = ser.read(ser.in_waiting or 1)
                if data:
                    # Echo raw board console (logs + its [usage] prints) for visibility.
                    sys.stderr.buffer.write(data)
                    sys.stderr.buffer.flush()
                    for msg in reader.feed(data):
                        print(f"[bridge] <- {msg}", file=sys.stderr)
                        bridge.on_message(msg)
                        if not proven:
                            update.cleanup(self_bin)
                            proven = True
                if time.monotonic() >= next_poll:
                    # Poll only while the board is provably alive (pings within
                    # the liveness window): the usage endpoint is aggressively
                    # rate-limited, and a boardless daemon fetching all day
                    # would burn that budget for nothing. The hello handler
                    # still pushes immediately on (re)connect.
                    if bridge.board_alive():
                        bridge.poll_once()
                    next_poll = time.monotonic() + POLL_INTERVAL_S
                # Not gated on board_alive(): drift is a fact about this
                # machine, not about the cable. The same _upkeep runs from
                # inside wait_for_port, so an unplugged machine repairs a
                # wiped hook too -- the watchdog's own interval keeps either
                # path from checking more often than it should.
                _upkeep()
                if time.monotonic() >= next_update:
                    next_update = time.monotonic() + UPDATE_INTERVAL_S
                    try:
                        _self_update_tick(self_bin)
                    except Exception as e:
                        # Never take the bridge down over an update check. The
                        # gauge working is worth more than the gauge being new.
                        print(f"[update] check failed: {e}", file=sys.stderr)
        except Reflashed as r:
            # Expected: the port is already closed and the board is rebooting
            # into what we just wrote. Give it a moment, then reconnect.
            #
            # Any failure reason travels with it. Without this the board sat
            # on "keep the cable connected" until its own deadline expired and
            # then blamed the timeout, while the daemon had the real reason
            # ("chip has flash encryption", "esptool not found") in its log and
            # no way to say it.
            report_failure = r.why
            time.sleep(2)
            continue
        except (serial.SerialException, OSError) as e:
            print(f"[bridge] serial lost: {e}; reconnecting", file=sys.stderr)
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1)


if __name__ == "__main__":
    main()
