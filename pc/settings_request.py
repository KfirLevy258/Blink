"""The request `blink settings` leaves for the daemon.

The daemon owns the serial port and the page that takes the WiFi password
runs inside it, so the CLI cannot start that page itself. It writes this
file, owner-only through private_file, and the daemon's _upkeep takes it
(spec 8.2, D25, D37, D50). It holds a panel name, a one-time link token and a
request id: never a password, never a key. The token is generated here with
`secrets`; BuilderServer refuses a short one.

`blink config panel` leaves the second request here, for the board's display
module, and reads the daemon's answer back: see PANEL_REQUEST_FILE below.
"""
from __future__ import annotations

import json
import math
import os
import secrets
import time

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
    obj = _take(home, REQUEST_FILE, "settings_request")
    if not isinstance(obj, dict) or set(obj) != {"panel", "k", "id"} \
            or obj["panel"] not in PANELS \
            or not all(isinstance(obj[x], str) for x in ("k", "id")):
        return None
    return obj


# `blink config panel standard|alternate` (issue #23): which display module
# the board has, handed to the daemon the same way.
#
# Until 2026-11-25 the CLI stopped the background service, opened the serial
# port itself and started the service again. Both halves reset the board --
# the CLI's open pulsed RTS, and the restarted daemon's open is a second
# reset -- while the command and the README promised "no reboot". A reset
# before ota_mark_healthy makes MCUboot revert a fresh update, and the
# command's own advice for old firmware ("update the board first, then run
# this again") steered people straight into that. The daemon already holds
# the port and the board is already running, so the setting now goes through
# it: no reset, no service stop or start.
#
# Its own file and its own key set, never a variant of the settings request
# above. That one's "panel" means a tab of the settings page (look, connect,
# board), and a request either taker could read as the other's would be one
# where a settings-page tab name reached the board or a board panel name
# opened a page. So this one says "display", and each taker refuses the
# other's shape (tests/pc/test_panel_cli.py).
PANEL_REQUEST_FILE = "panel_request.json"
PANEL_ANSWER_FILE = "panel_answer.json"
# The values `blink config panel` takes: protocol.PANELS, restated rather
# than imported so this module stays a leaf the CLI can load cheaply; the
# test pins the two together.
DISPLAY_PANELS = ("standard", "alternate")
# Every word the daemon can answer with, and nothing else. The answer is
# built from these constants alone: what the board printed is matched
# against fixed text and never copied, not even the errno of its "(%d)"
# (CLAUDE.md, the serial-buffer rule).
#   set         the board named the panel that was asked for
#   mismatch    it named the other one
#   not_stored  "could not store the panel"
#   not_shown   "panel saved; restart the board to show it"
#   unknown     "unknown panel; ignored"
#   cable       "set over the cable only", or the daemon's link is WiFi and
#               it did not send at all
#   silent      sent, and no answer line in time: firmware older than this
#   not_sent    encode_checked refused the line
#   no_board    no board linked, or the link went while the daemon listened
PANEL_RESULTS = ("set", "mismatch", "not_stored", "not_shown", "unknown",
                 "cable", "silent", "not_sent", "no_board")
# How old a panel request may be and still be sent. The request carries the
# wall-clock second it was written ("t") and the taker drops anything older.
#
# Why (review of 2026-11-25): a command that died before its ten-second wait
# ran out -- Ctrl-C, a closed terminal, a killed shell -- left its request in
# ~/.blink/private with nobody waiting on it. The CLI now removes it on any
# exit it lives through, but a SIGKILL or a power cut still leaves one, and a
# daemon started minutes or days later took it and switched the board's
# display module: a screen that went mirrored or colour-swapped long after
# the command was abandoned, for no reason the owner could see.
#
# Fifteen seconds: the CLI stops waiting at cli.PANEL_WAIT_S (10 s) and
# removes the request then, so one still unread past that has nobody waiting
# for its answer. A request can sit untaken behind another for the daemon's
# PANEL_LISTEN_S (3 s) inside those ten; the other five are slack for a slow
# serial-loop pass. The wall clock and not a monotonic one, because the
# writer and the taker are two processes and the case this is for is a
# daemon that did not exist when the request was written.
#
# A time ahead of the taker's by more than PANEL_REQUEST_AHEAD_S is refused
# too: no writer of ours dates one in the future, and a far-future time
# would otherwise make a file immortal. Five seconds, as usage_refresh's
# MOVED_SLACK_S, for a clock stepped back between the write and the take;
# the CLI then says the app did not answer, and running it again works.
# os.stat's mtime would have needed no new key, but private_file opens by
# descriptor and validates that descriptor, and a second stat by name here
# would read a file other than the one it checked.
PANEL_REQUEST_MAX_AGE_S = 15.0
PANEL_REQUEST_AHEAD_S = 5.0


def write_panel_request(home: str, display: str, now=time.time) -> str:
    """The request id. Keys are exactly display, id and t (wall-clock
    seconds); `display` must be one of DISPLAY_PANELS, so the CLI cannot
    leave one the daemon refuses."""
    if display not in DISPLAY_PANELS:
        raise ValueError("not a display panel")
    rid = secrets.token_urlsafe(12)
    body = json.dumps({"display": display, "id": rid, "t": now()})
    private_file.write_private(home, PANEL_REQUEST_FILE, body.encode("ascii"))
    return rid


def _take(home: str, name: str, stem: str):
    """Rename, read, delete: the parsed JSON of <private>/<name>, or None
    when there is no file or it does not parse. NotPrivate and OSError
    propagate. Shared by both takers."""
    # Renamed to a name of our own first, so two takers cannot both read it
    # and a refused file is gone either way (it used to be refused on every
    # serial-loop pass).
    here = os.path.join(private_file.private_dir(home), name)
    mine = f"{stem}.{secrets.token_hex(4)}.taken"
    try:
        os.rename(here, os.path.join(os.path.dirname(here), mine))
    except FileNotFoundError:
        return None
    try:
        raw = private_file.open_private(home, mine)
    finally:
        private_file.remove_private(home, mine)
    if raw is None:
        return None
    try:
        return json.loads(raw.decode("utf-8"))
    except ValueError:
        return None


def take_panel_request(home: str, now=time.time) -> dict | None:
    """The panel request, deleted as it is read, or None when there is none
    or it is not exactly {"display": one of DISPLAY_PANELS, "id": str,
    "t": a time no more than PANEL_REQUEST_MAX_AGE_S old and no more than
    PANEL_REQUEST_AHEAD_S ahead}. A stale one is deleted all the same and
    nothing of it is returned or said. NotPrivate for a file that is not
    provably owner-only, as take_request."""
    obj = _take(home, PANEL_REQUEST_FILE, "panel_request")
    if not isinstance(obj, dict) or set(obj) != {"display", "id", "t"} \
            or not isinstance(obj["display"], str) \
            or obj["display"] not in DISPLAY_PANELS \
            or not isinstance(obj["id"], str):
        return None
    t = obj["t"]
    # bool is an int to isinstance; json.loads also takes NaN and Infinity,
    # and NaN compares false against every bound, so it is refused by name.
    if isinstance(t, bool) or not isinstance(t, (int, float)) \
            or not math.isfinite(t):
        return None
    age = now() - t
    if age > PANEL_REQUEST_MAX_AGE_S or age < -PANEL_REQUEST_AHEAD_S:
        return None
    return obj


def write_panel_answer(home: str, rid: str, result: str) -> None:
    """The daemon's answer: exactly the request's id and one of
    PANEL_RESULTS. One file name, so a second answer replaces the first; the
    CLI reads only an answer carrying its own id."""
    if result not in PANEL_RESULTS or not isinstance(rid, str):
        raise ValueError("not a panel answer")
    body = json.dumps({"id": rid, "result": result})
    private_file.write_private(home, PANEL_ANSWER_FILE, body.encode("ascii"))


def read_panel_answer(home: str, rid: str) -> str | None:
    """The result word answering request `rid`, removing the answer as it is
    read; None while there is no answer, or the one there is for another
    request (left alone for the command that asked it) or not one of ours.
    NotPrivate and OSError propagate."""
    raw = private_file.open_private(home, PANEL_ANSWER_FILE)
    if raw is None:
        return None
    try:
        obj = json.loads(raw.decode("utf-8"))
    except ValueError:
        return None
    if not isinstance(obj, dict) or set(obj) != {"id", "result"} \
            or obj["id"] != rid or not isinstance(obj["result"], str) \
            or obj["result"] not in PANEL_RESULTS:
        return None
    private_file.remove_private(home, PANEL_ANSWER_FILE)
    return obj["result"]
