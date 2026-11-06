#!/usr/bin/env python3
"""Read a board's console, safely, while the daemon is stopped.

    tools/console_tail.py [PORT] [--tap FILE]

For the WiFi bench (Task 0.2) and for anyone watching a board boot: a plain
serial monitor prints everything the board says, and CLAUDE.md forbids
persisting what came off the serial buffer. So every line goes through
logbook.tap_console_line, the same allowlist BLINK_TAP uses (D49): a protocol
object as its redacted projection, a known message as its constant, a known
tag bare, the rest "(board line)". There is no raw mode and no flag to get
one; a raw view is `screen` on a bench that is nobody's desk.

It only listens. The port is opened plainly, with no DTR or RTS preset (that
reboots the board: CLAUDE.md, "Hardware"), nothing is written, and the input
buffer is not flushed, so the boot banner that was already waiting is read.

--tap appends {"dir":"console","t":time,"line":...} to FILE, the record
shape Tap.console writes, so tests/fleet/tap_asserts.py can read it.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pc import logbook  # noqa: E402

BAUD = 115200


def _open_port(port):
    import serial
    # Plain open, nothing pre-set: see "THE PORT" at the top of board_touch.py.
    return serial.Serial(port, BAUD, timeout=0.2)


def _autodetect():
    from claude_usage_bridge import candidate_ports
    ports = candidate_ports()
    return ports[0] if ports else None


def main(argv, open_port=None, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("port", nargs="?")
    ap.add_argument("--tap", metavar="FILE")
    args = ap.parse_args(argv)

    port = args.port or _autodetect()
    if not port:
        print("no board found; pass the port", file=sys.stderr)
        return 2
    try:
        ser = (open_port or _open_port)(port)
    except Exception as e:
        # The exception's text names the port and the OS error, never data.
        print(f"cannot open {port}: {type(e).__name__}", file=sys.stderr)
        return 2
    pending = b""
    try:
        while True:
            try:
                chunk = ser.read(256)
            except KeyboardInterrupt:
                return 0
            except Exception as e:
                print(f"the port went away: {type(e).__name__}",
                      file=sys.stderr)
                return 1
            *whole, pending = (pending + chunk).split(b"\n")
            for raw in whole:
                line = logbook.tap_console_line(
                    raw.decode("utf-8", errors="replace").rstrip("\r"))
                print(line, file=out, flush=True)
                if args.tap:
                    with open(args.tap, "a", encoding="utf-8") as f:
                        f.write(json.dumps({"dir": "console",
                                            "t": time.time(),
                                            "line": line}) + "\n")
    finally:
        close = getattr(ser, "close", None)
        if close:
            close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
