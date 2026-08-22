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
from pc import protocol, statusline_source
from pc.bridge import Bridge

POLL_INTERVAL_S = 60
PING_GRACE_LOG_S = 30


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


def wait_for_port(explicit=None, poll_s=3.0):
    """Block until there is a board to talk to, then return its device path.

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
        if port and os.path.exists(port):
            if announced:
                print(f"[bridge] board found at {port}", file=sys.stderr)
            return port
        if not announced:
            # Once, not per attempt: this is the steady state on a machine
            # whose board is simply unplugged, and it is not an error.
            print(f"[bridge] waiting for the board ({explicit or 'USB'})...",
                  file=sys.stderr)
            announced = True
        time.sleep(poll_s)


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
    port = wait_for_port(args.port)

    # Claude Code owns the credential and computes these numbers; we read the file
    # its statusline shim writes. Nothing here authenticates to Anthropic.
    fetch = statusline_source.make_fetch()
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
            ser.dtr = False          # GPIO0 HIGH -> boot the app, not the ROM loader
            ser.rts = True           # EN LOW  -> hold in reset
            time.sleep(0.15)
            ser.rts = False          # EN HIGH -> release; board boots
            time.sleep(0.3)
            ser.reset_input_buffer()
        except Exception as e:
            # Deduplicated: a board left unplugged, or a port the user lacks
            # permission to open, would otherwise write this same line to
            # bridge.log every three seconds for as long as the service runs.
            err = f"open {port} failed: {e}"
            if err != last_err:
                print(f"[bridge] {err}; waiting for the board", file=sys.stderr)
                last_err = err
            time.sleep(3)
            port = wait_for_port(args.port)
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
            ser.write(protocol.encode(m))

        # The board approved an update. esptool needs the port to itself, so
        # close it, write slot0, and let the outer reconnect loop pick the
        # board back up -- esptool resets it into the new image on the way out.
        class Reflashed(Exception):
            pass

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
            raise Reflashed()

        bridge = Bridge(write_msg=send, fetch_usage=fetch,
                        flash_image=flash_image)
        next_poll = time.monotonic()
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
                if time.monotonic() >= next_poll:
                    # Poll only while the board is provably alive (pings within
                    # the liveness window): the usage endpoint is aggressively
                    # rate-limited, and a boardless daemon fetching all day
                    # would burn that budget for nothing. The hello handler
                    # still pushes immediately on (re)connect.
                    if bridge.board_alive():
                        bridge.poll_once()
                    next_poll = time.monotonic() + POLL_INTERVAL_S
        except Reflashed:
            # Expected: the port is already closed and the board is rebooting
            # into what we just wrote. Give it a moment, then reconnect.
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
