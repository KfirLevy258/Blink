"""WiFi pairing: the rules for what the board can be given.

Validation only here; the credential file is a later task.
"""
from __future__ import annotations

HEX = frozenset("0123456789abcdefABCDEF")


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
