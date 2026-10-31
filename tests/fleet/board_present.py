"""Wait until this machine can see the board, or until it cannot.

    python -m tests.fleet.board_present --usb 1A86:7523 --wait 90
    python -m tests.fleet.board_present --usb 1A86:7523 --wait 90 --gone

The VM fleet hands one board from this Mac to each UTM guest in turn, and the
handoff is not the same moment as the guest seeing it: UTM redirects the USB
device, the guest enumerates it, and Windows binds a driver -- seconds, and
on a guest that has never seen the board before, longer. An agent started in
that gap runs its preflight against a port that is not there yet, and three
failed settles read exactly like a dead board. So the orchestrator runs this
first, in the guest, and only starts the agent once it exits 0; and runs it
here afterwards, so the next VM is not handed a board this Mac has not got
back yet.

By USB VID:PID, the way the daemon itself finds the board
(claude_usage_bridge.py, KNOWN_USB_SERIAL), and never by port name: COM3 on
the Windows VM, /dev/ttyUSB0 on Ubuntu, /dev/cu.usbserial-* here.

Exit 0 when the condition holds, 1 when it did not within --wait seconds,
2 for arguments that make no sense or no pyserial to look with. It prints
the port it found and nothing else about it -- no serial traffic is read.
"""
import argparse
import sys
import time

POLL_S = 1.0


def parse_usb(text):
    """(vid, pid) from "1A86:7523"; ValueError for anything else."""
    parts = str(text).split(":")
    if len(parts) != 2 or not all(len(p) == 4 for p in parts):
        raise ValueError(f"{text!r} is not a USB id; it is VID:PID in hex,"
                         f" e.g. 1A86:7523.")
    return int(parts[0], 16), int(parts[1], 16)


def matching_ports(vid, pid, comports):
    """The device names of every port with this VID:PID."""
    return sorted(p.device for p in comports()
                  if getattr(p, "vid", None) == vid
                  and getattr(p, "pid", None) == pid)


def wait(vid, pid, seconds, gone=False, comports=None, sleep=time.sleep,
         now=time.monotonic):
    """(condition met, the ports last seen)."""
    deadline = now() + max(0.0, seconds)
    while True:
        ports = matching_ports(vid, pid, comports)
        if bool(ports) != gone:
            return True, ports
        if now() >= deadline:
            return False, ports
        sleep(POLL_S)


def main(argv=None, comports=None, sleep=time.sleep, now=time.monotonic):
    ap = argparse.ArgumentParser(prog="tests.fleet.board_present")
    ap.add_argument("--usb", required=True, help="VID:PID, e.g. 1A86:7523")
    ap.add_argument("--wait", type=float, default=0.0,
                    help="Seconds to keep looking")
    ap.add_argument("--gone", action="store_true",
                    help="Wait for the board to be absent instead")
    args = ap.parse_args(argv)
    try:
        vid, pid = parse_usb(args.usb)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if comports is None:
        try:
            from serial.tools.list_ports import comports
        except ImportError as e:
            print(f"pyserial is not installed for {sys.executable} ({e}),"
                  f" so this interpreter cannot look for a board -- and"
                  f" cannot run the daemon either.", file=sys.stderr)
            return 2
    met, ports = wait(vid, pid, args.wait, gone=args.gone,
                      comports=comports, sleep=sleep, now=now)
    if args.gone:
        print(f"board {args.usb} is gone" if met else
              f"board {args.usb} is still here after {args.wait:.0f} s:"
              f" {', '.join(ports)}")
    else:
        print(f"board {args.usb} is at {', '.join(ports)}" if met else
              f"no board {args.usb} after {args.wait:.0f} s")
    return 0 if met else 1


if __name__ == "__main__":
    raise SystemExit(main())
