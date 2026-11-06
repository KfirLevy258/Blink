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


def write(home: str, link: str, ip: str | None, now=time.time) -> None:
    path = os.path.join(home, LINK_FILE)
    tmp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"link": link, "ip": ip, "t": now()}, f)
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


def status_line(rec: dict | None, paired: bool) -> str:
    if rec is None:
        return "Link        unknown (app not reporting)"
    if rec.get("link") == "usb":
        return "Link        USB cable"
    ip = rec.get("ip")
    try:
        ip = str(ipaddress.IPv4Address(ip)) if ip else None
    except ValueError:
        ip = None
    if rec.get("link") == "wifi" and ip:
        return "Link        WiFi, board at " + ip
    return "Link        WiFi set up, board not found"
