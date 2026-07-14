#!/usr/bin/env python3
"""Serial daemon: bridge Claude usage to an ESP32 over USB-C.

Run inside the Zephyr venv (has pyserial) or `pip install pyserial`:
    python3 claude_usage_bridge.py --port /dev/cu.usbmodemXXXX
"""
import argparse
import sys
import time

import serial  # pyserial
from serial.tools import list_ports

from pc import protocol, usage_api
from pc.bridge import Bridge

POLL_INTERVAL_S = 60
PING_GRACE_LOG_S = 30


def autodetect_port():
    for p in list_ports.comports():
        if "usbmodem" in p.device or (p.manufacturer or "").lower().startswith("espressif"):
            return p.device
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()
    port = args.port or autodetect_port()
    if not port:
        sys.exit("No serial port found; pass --port /dev/cu.usbmodemXXXX")

    token = usage_api.read_token()
    if not token:
        sys.exit("No Claude OAuth token (Keychain/creds).")

    def fetch():
        return usage_api.map_usage(usage_api.fetch_usage_raw(token))

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
            print(f"[bridge] open {port} failed: {e}; retrying in 3s", file=sys.stderr)
            time.sleep(3)
            continue
        print(f"[bridge] connected on {port}", file=sys.stderr)
        reader = protocol.LineReader()

        def send(m):
            print(f"[bridge] -> {m}", file=sys.stderr)
            ser.write(protocol.encode(m))

        bridge = Bridge(write_msg=send, fetch_usage=fetch)
        next_poll = time.monotonic()
        try:
            while True:
                data = ser.read(256)
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
        except (serial.SerialException, OSError) as e:
            print(f"[bridge] serial lost: {e}; reconnecting", file=sys.stderr)
            try:
                ser.close()
            except Exception:
                pass
            time.sleep(1)


if __name__ == "__main__":
    main()
