"""The daemon's WiFi link: reads and writes like the serial port (spec S7.1).

`read`, `in_waiting`, `write`, `close` match what the main loop uses of
serial.Serial, and a dropped session raises OSError (a fixed-message
SessionError), so the loop runs unchanged on either link. Importing this
opens nothing.

Logging (S7.5): fixed sentences only. No address, key, nonce, tag, length or
byte of a frame reaches stderr or an exception message from here.
"""
from __future__ import annotations

import socket
import sys
import time

from pc import private_file, wifi_pair
from pc import wifi_session as ws

_LOST = "link lost"


class WifiLink:
    kind = "wifi"

    def __init__(self, sock, session):
        self._sock, self._s = sock, session
        self._buf = b""
        self._closed = False

    def _pump(self, timeout: float) -> None:
        """One recv into the plaintext buffer; O and G frames from the board
        carry nothing the line reader wants and are dropped."""
        if self._closed:
            ws._fail(_LOST)
        try:
            self._sock.settimeout(timeout)
            data = self._sock.recv(4096)
        except (socket.timeout, BlockingIOError):
            return
        except OSError:
            self.close()
            ws._fail(_LOST)
        if not data:
            self.close()
            ws._fail(_LOST)
        try:
            frames = self._s.feed(data)
        except ws.SessionError:
            self.close()
            raise
        self._buf += b"".join(body for kind, body in frames if kind == "L")

    def read(self, n: int) -> bytes:
        if not self._buf:
            self._pump(0.2)
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    @property
    def in_waiting(self) -> int:
        if not self._buf:
            self._pump(0.0)
        return len(self._buf)

    def _send(self, frame: bytes) -> None:
        if self._closed:
            ws._fail(_LOST)
        try:
            self._sock.settimeout(5.0)
            self._sock.sendall(frame)
        except OSError:
            # A part-written frame leaves the stream unusable for good.
            self.close()
            ws._fail(_LOST)

    def write(self, raw: bytes) -> None:
        self._send(self._s.seal_line(raw))

    def write_blob(self, kind: bytes, offset: int, chunk: bytes) -> None:
        self._send(self._s.seal_blob(kind, offset, chunk))

    def close(self) -> None:
        self._closed = True
        try:
            self._sock.close()
        except OSError:
            pass


def discover(key: bytes, sendto=None, recv=None, tries: int = 3,
             gap_s: float = 1.0):
    """Broadcast up to `tries` queries `gap_s` apart; return (ip, port) of
    the first answer that carries a valid tag for one of our nonces, else
    None. `sendto(data, addr)` and `recv(timeout_s) -> (data, (ip, port))`
    (None or socket.timeout for nothing) are injectable for tests."""
    sock = None
    if sendto is None or recv is None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.bind(("", 0))
        if sendto is None:
            sendto = sock.sendto
        if recv is None:
            def recv(timeout_s):
                sock.settimeout(max(timeout_s, 0.001))
                return sock.recvfrom(64)
    nonces = []
    try:
        for _ in range(tries):
            nq, q = ws.disc_query(key)
            nonces.append(nq)
            try:
                sendto(q, ("255.255.255.255", ws.PORT))
            except OSError:
                continue        # one transient failure must not cost the other tries
            end = time.monotonic() + gap_s
            while True:
                left = end - time.monotonic()
                if left <= 0:
                    break
                try:
                    got = recv(left)
                except OSError:     # includes socket.timeout
                    break
                if not got:
                    break
                data, addr = got
                for n in nonces:
                    port = ws.disc_answer_port(key, n, data)
                    if port:
                        return addr[0], port
    finally:
        if sock is not None:
            sock.close()
    return None


def _say(msg: str) -> None:
    print("[wifi] " + msg, file=sys.stderr)


def connect(home: str, create_connection=socket.create_connection,
            discover=discover, timeout_s: float = 3.0):
    """(WifiLink | None, outcome). Outcomes: "connected", "not paired",
    "not found" (nothing answered), "failed" (a peer answered and the
    handshake did not complete: unauthenticated by design, D36)."""
    pair = wifi_pair.load(home)
    if pair is None:
        return None, "not paired"
    key = pair["key"]
    _say("connecting to the board")
    reached = False

    def attempt(ip, port):
        nonlocal reached
        try:
            sock = create_connection((ip, port), timeout_s)
        except OSError:
            return None
        reached = True
        try:
            return WifiLink(sock, ws.client_handshake(sock, key))
        except OSError:
            sock.close()
            return None

    tried = (pair["ip"], pair["port"])
    link = attempt(*tried)
    if link is None:
        found = discover(key)
        if found is not None and tuple(found) != tried:
            link = attempt(*found)
            if link is not None:
                try:
                    wifi_pair.update_ip(home, *found)
                except (OSError, private_file.NotPrivate):
                    pass        # a failed address refresh must not drop a good link
    if link is not None:
        _say("connected")
        return link, "connected"
    if reached:
        _say("handshake refused")
        return None, "failed"
    _say("board not found")
    return None, "not found"
