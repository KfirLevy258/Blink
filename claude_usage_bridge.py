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

POLL_INTERVAL_S = 300
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
            ser = serial.Serial(port, args.baud, timeout=0.2)
        except Exception as e:
            print(f"[bridge] open {port} failed: {e}; retrying in 3s", file=sys.stderr)
            time.sleep(3)
            continue
        print(f"[bridge] connected on {port}", file=sys.stderr)
        reader = protocol.LineReader()
        bridge = Bridge(write_msg=lambda m: ser.write(protocol.encode(m)),
                        fetch_usage=fetch)
        next_poll = time.monotonic()
        try:
            while True:
                data = ser.read(256)
                if data:
                    for msg in reader.feed(data):
                        print(f"[bridge] <- {msg}", file=sys.stderr)
                        bridge.on_message(msg)
                if time.monotonic() >= next_poll:
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
