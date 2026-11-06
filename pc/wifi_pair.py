"""WiFi pairing: the rules for what the board can be given, and the file
that remembers which board we paired with and its key (spec 7.3).

The file lives in <~/.blink>/private and goes through pc/private_file, so it
is owner-only from creation and checked by handle on every load. `home`
throughout is the ~/.blink directory, resolved by the caller at call time.
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import secrets
import sys

from pc import private_file

HEX = frozenset("0123456789abcdefABCDEF")
PAIR_FILE = "wifi_pair.json"
_LOWER_HEX = frozenset("0123456789abcdef")
# Said once per process: the daemon loads on every reconnect, and one line
# is enough to tell a customer's log that the board is treated as unpaired.
_said_not_private = False


def password_problem(p):
    """None when `p` is a usable WPA2/WPA3 passphrase, else one sentence for
    the page. 8-63 printable ASCII characters, or exactly 64 hex digits (a raw
    WPA PSK, which is how a 64-character secret is meant)."""
    if not all(" " <= c <= "~" for c in p):
        return "The password can only use ASCII characters."
    if len(p) < 8:
        return "The password needs at least 8 characters."
    if len(p) > 64 or (len(p) == 64 and any(c not in HEX for c in p)):
        return "The password can be at most 63 characters, or 64 hex digits."
    return None


def ssid_problem(ssid):
    """None when `ssid` (bytes) is a legal 802.11 SSID, else one sentence."""
    if not ssid:
        return "The network name is empty."
    if len(ssid) > 32:
        return "The network name can be at most 32 bytes."
    if b"\x00" in ssid:
        return "The network name cannot contain a zero byte."
    return None


def pair_path(home: str) -> str:
    return os.path.join(home, private_file.PRIVATE_DIR, PAIR_FILE)


def new_key() -> bytes:
    return secrets.token_bytes(32)


def kid(key: bytes) -> str:
    """Names the key without revealing it: the board and the daemon compare
    this to know they hold the same one."""
    return hashlib.sha256(key).digest()[:4].hex()


def save(home: str, board_id: str, key: bytes, ip: str, port: int) -> None:
    doc = {"v": 1, "board_id": board_id, "key": key.hex(), "kid": kid(key),
           "ip": ip, "port": port}
    private_file.write_private(home, PAIR_FILE, json.dumps(doc).encode())


def _hex(s, n):
    return isinstance(s, str) and len(s) == n and set(s) <= _LOWER_HEX


def load(home: str) -> dict | None:
    """{"board_id", "key" (32 bytes), "kid", "ip", "port"}, or None when
    there is no pairing, it is the wrong shape, or the file is not provably
    ours alone (said once, in a fixed sentence: never the reason's details,
    the path or any of the content)."""
    global _said_not_private
    try:
        raw = private_file.open_private(home, PAIR_FILE)
    except private_file.NotPrivate:
        if not _said_not_private:
            _said_not_private = True
            print("[wifi] the pairing file is not owner-only, so the board"
                  " is treated as unpaired", file=sys.stderr)
        return None
    if raw is None:
        return None
    try:
        doc = json.loads(raw)
    except ValueError:              # bad JSON, or not UTF-8
        return None
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    key, port = doc.get("key"), doc.get("port")
    if not (isinstance(doc.get("board_id"), str) and doc["board_id"]
            and _hex(key, 64) and doc.get("kid") == kid(bytes.fromhex(key))
            and isinstance(doc.get("ip"), str) and doc["ip"]
            and type(port) is int and 0 < port < 65536):
        return None
    return {"board_id": doc["board_id"], "key": bytes.fromhex(key),
            "kid": doc["kid"], "ip": doc["ip"], "port": port}


def update_ip(home: str, ip: str, port: int) -> None:
    """The board moved (DHCP): keep the key, change where to reach it.
    Nothing to update when there is no valid pairing."""
    doc = load(home)
    if doc is not None:
        save(home, doc["board_id"], doc["key"], ip, port)


def remove(home: str) -> bool:
    """Forget the pairing, and any temp a write interrupted mid-way left.
    True if there was a pairing file to remove."""
    path = pair_path(home)
    for tmp in glob.glob(glob.escape(path) + ".*.tmp"):
        try:
            os.remove(tmp)
        except OSError:
            pass
    try:
        os.remove(path)
    except FileNotFoundError:
        return False
    return True
