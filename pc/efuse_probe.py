"""Is flash encryption burned into the chip on this port? One line, then exit.

    python -m pc.efuse_probe --port /dev/cu.usbserial-14220
    flash_encryption disabled

The daemon used to ask `espefuse summary` this question. espefuse imports
espsecure, espsecure imports `cryptography`, and `cryptography` is a 12 MB
native library -- a third of every customer's download, carried to read one
eFuse bit (2026-08-30). esptool's own chip class reads that bit straight
from the register, with nothing behind it but pyserial, so this asks there.

Runs as its own process, like the flash itself: the port is handed over
whole, and a hung probe cannot take the daemon with it. Under the frozen
build the interpreter is the program (`blink -m pc.efuse_probe`, see
blink_main.py).

The chip is left reset into the application afterwards. The probe reaches
it through the ROM download loader, and a board left there shows a black
screen until something resets it -- the flash that follows would, but a
refusal would not.
"""
import argparse
import sys


def flash_encryption_enabled(port):
    from esptool.cmds import detect_chip

    esp = detect_chip(port)
    try:
        return bool(esp.get_flash_encryption_enabled())
    finally:
        try:
            esp.hard_reset()
        finally:
            esp._port.close()


def main(argv=None):
    ap = argparse.ArgumentParser(prog="efuse_probe")
    ap.add_argument("--port", required=True)
    args = ap.parse_args(argv)
    on = flash_encryption_enabled(args.port)
    print("flash_encryption", "enabled" if on else "disabled")
    return 0


if __name__ == "__main__":
    sys.exit(main())
