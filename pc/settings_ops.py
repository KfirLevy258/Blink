"""What the settings page can ask of the board (spec 8.1).

The page's HTTP threads call BoardOps; BoardOps puts an op on a queue and
waits. Only the serial loop's thread (Bridge.pump) ever takes an op off it
and writes to the port, so a slow board, or no board, costs the page a
wait and the serial loop nothing. The password rides inside the op until
the loop has turned it into one wifi_set line, then the op lets go of it.
"""
from __future__ import annotations

import socket
import threading
import time

from pc import protocol, wifi_pair, wifi_session


class BoardOpError(Exception):
    """One sentence for the page. Never carries a password, SSID or key."""


class _Op:
    def __init__(self, kind, args=None):
        self.kind = kind
        self.args = args
        self.done = threading.Event()
        self.result = None
        self.error = None
        self.abandoned = False      # the caller timed out; the loop drops it


class BoardOps:
    def __init__(self, ops_queue, now=time.monotonic):
        self.queue = ops_queue
        self._now = now
        self._lock = threading.Lock()
        self._state = {}
        self._dev = {}

    # --- called from the serial loop (Bridge) ---
    def clear(self):
        with self._lock:
            self._state, self._dev = {}, {}

    def note_state(self, msg):
        with self._lock:
            self._state = {k: msg[k] for k in
                           ("st", "link", "s", "ip", "port", "r", "kid", "why")
                           if k in msg}

    def note_device(self, **kw):
        with self._lock:
            self._dev.update(kw)

    # --- called from page threads ---
    def _run(self, op, timeout_s):
        self.queue.put(op)
        if not op.done.wait(timeout_s):
            op.abandoned = True
            raise BoardOpError("The board did not answer.")
        if op.error:
            raise BoardOpError(op.error)
        return op.result

    def scan(self, timeout_s: float = 15.0) -> list:
        return self._run(_Op("scan"), timeout_s)

    def join(self, ssid: bytes, password: str, timeout_s: float = 30.0) -> dict:
        problem = wifi_pair.ssid_problem(ssid) or wifi_pair.password_problem(password)
        if problem:
            raise BoardOpError(problem)
        return self._run(_Op("join", (bytes(ssid), password)), timeout_s)

    def forget(self, timeout_s: float = 10.0) -> dict:
        return self._run(_Op("forget"), timeout_s)

    def logo(self, blob: bytes, timeout_s: float = 300.0) -> dict:
        """Stream a logo image (pc/logo_encode.build's bytes); the board's
        logo_state once its logo_parse has accepted it. A 512 KB image over
        the cable is about a minute; a fused board's encrypted writes are
        slower."""
        return self._run(_Op("logo", bytes(blob)), timeout_s)

    def logo_remove(self, timeout_s: float = 30.0) -> dict:
        return self._run(_Op("logo_remove"), timeout_s)

    def pref_set(self, timeout_s: float = 10.0, **fields) -> dict:
        """The board's pref after it applied `fields`. A field the board
        refuses gets no answer, and the page hears that it did not answer."""
        return self._run(_Op("pref", protocol.pref_set(**fields)), timeout_s)

    def state(self) -> dict:
        with self._lock:
            return dict(self._state, usb=self._state.get("link") == "usb")

    def device(self) -> dict:
        with self._lock:
            return dict(self._dev, link=self._state.get("link"))


def verify_board(ip, port, key):
    """The one-shot proof of S5.4: raises OSError on any failure."""
    sock = socket.create_connection((ip, port), 5.0)
    try:
        wifi_session.verify(sock, key)
    finally:
        sock.close()
