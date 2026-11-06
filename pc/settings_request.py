"""The request `blink settings` leaves for the daemon.

The daemon owns the serial port and the page that takes the WiFi password
runs inside it, so the CLI cannot start that page itself. It writes this
file, owner-only through private_file, and the daemon's _upkeep takes it
(spec 8.2, D25, D37, D50). It holds a panel name, a one-time link token and a
request id: never a password, never a key. The token is generated here with
`secrets`; BuilderServer refuses a short one.
"""
from __future__ import annotations

import json
import secrets

from pc import private_file

REQUEST_FILE = "settings_request.json"
PANELS = ("look", "connect", "board")


def write_request(home: str, panel: str) -> tuple[str, str]:
    """(token, request_id). Keys are exactly panel, k and id."""
    token, rid = secrets.token_urlsafe(32), secrets.token_urlsafe(12)
    body = json.dumps({"panel": panel, "k": token, "id": rid})
    private_file.write_private(home, REQUEST_FILE, body.encode("ascii"))
    return token, rid


def take_request(home: str) -> dict | None:
    """The request, deleted as it is read, or None when there is none or it
    is not one of ours (unknown panel, wrong shape). A file that is not
    provably owner-only raises NotPrivate, as open_private does: the daemon
    must not act on something another account could have written."""
    raw = private_file.open_private(home, REQUEST_FILE)
    if raw is None:
        return None
    private_file.remove_private(home, REQUEST_FILE)
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or set(obj) != {"panel", "k", "id"} \
            or obj["panel"] not in PANELS \
            or not all(isinstance(obj[x], str) for x in ("k", "id")):
        return None
    return obj
