"""Which link the daemon is using, for `blink status` (spec 7.5).

The daemon writes link.json on every link change and every 60 s; status reads
it and treats anything older than 180 s as "the app is not reporting", so a
dead daemon never leaves a stale "USB cable" on screen. The only value that
reaches the screen from the file is an address that parses as one: a damaged
or hand-edited file cannot print arbitrary text.
"""
from __future__ import annotations

import ipaddress
import json
import os
import time

LINK_FILE = "link.json"


def write(home: str, link: str, ip: str | None, now=time.time,
          silent=()) -> None:
    """`silent` is the daemon's LinkChooser.silent: the serial ports that
    opened and stayed quiet through BOTH hello passes, the 1.5 s one and the
    11 s one (PROBE_PATIENT_S, long enough to hear a board that is still
    booting). They are carried here because `blink status` cannot find out
    for itself: the daemon holds the port, and status never opens one --
    an open with the wrong line settings reboots the board (CLAUDE.md).
    Without this, a blank board read "none identified yet" for ever, which
    an owner took for "still working on it" (issue #16).

    Port names only, the same names board.json already stores; never a
    byte of what was (not) heard on them."""
    path = os.path.join(home, LINK_FILE)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    rec = {"link": link, "ip": ip, "t": now()}
    if silent:
        rec["silent"] = sorted(silent)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(rec, f)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def read(home: str, now=time.time, max_age_s: float = 180.0) -> dict | None:
    try:
        with open(os.path.join(home, LINK_FILE), encoding="utf-8") as f:
            rec = json.load(f)
        age = now() - float(rec["t"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not isinstance(rec, dict) or age > max_age_s or age < -max_age_s:
        return None
    return rec


def silent_ports(rec: dict | None) -> list[str]:
    """The ports the daemon gave up on as silent, from a record read().

    A missing or stale record (no daemon running), one written before the
    key existed, or a damaged one all give []: status then keeps its old
    "none identified yet" wording rather than calling a board blank on a
    guess. Only short plain strings survive, so a hand-edited file cannot
    print arbitrary text, as with the address in status_line."""
    if not isinstance(rec, dict):
        return []
    got = rec.get("silent")
    if not isinstance(got, list):
        return []
    return [p for p in got
            if isinstance(p, str) and 0 < len(p) <= 128
            and p.isprintable()]


def status_line(rec: dict | None, paired: bool) -> str:
    if rec is None:
        return "Link        unknown (app not reporting)"
    if rec.get("link") == "usb":
        return "Link        USB cable"
    if rec.get("link") == "none" and not paired:
        # The daemon is running and nothing answers on either link; there is
        # no WiFi to say anything about.
        return "Link        no board connected"
    ip = rec.get("ip")
    try:
        ip = str(ipaddress.IPv4Address(ip)) if ip else None
    except ValueError:
        ip = None
    if rec.get("link") == "wifi" and ip:
        return "Link        WiFi, board at " + ip
    return "Link        WiFi set up, board not found"
