"""The daemon's half of the WiFi session: handshake, frame codec, discovery.

Spec S6.2 (handshake), S6.3 (frames), S6.4 (discovery), S7.4 (crypto). Pure
codec plus two helpers that drive a socket they are handed; importing this
opens nothing. Standard library and pc/wifi_crypto only.

Nothing here ever puts received bytes, keys, nonces, tags or lengths into an
exception message or a log line (CLAUDE.md): a SessionError carries one of
four fixed strings (plus "verify failed"), so a log line built from it cannot
leak a buffer. The same fixed strings are what a wrong key and a board that
hung up after M1 both produce (D36), so the error does not tell a stranger
which of the two happened.

Two defects the host check found in the throwaway spike peer are closed
here on purpose: a failed open kills that direction for good (a counter in
doubt is never resynchronised), and a frame shorter than its own header is
refused cleanly rather than raising struct.error.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import time

from pc import wifi_crypto as wc

PORT = 49731
M1_LEN, M2_LEN, M3_LEN, Q_LEN, A_LEN, PT_MAX = 20, 48, 32, 36, 22, 517
LINE_MAX = 512
TAG_LEN = 16
_COUNTER_END = 2 ** 64 - 1

M1_MAGIC = b"BLW\x01"
Q_MAGIC = b"BLQ\x01"
A_MAGIC = b"BLA\x01"


class SessionError(OSError):
    """Message is always one of the fixed strings; see the module docstring."""


def _fail(msg: str):
    raise SessionError(msg) from None


def _mac(key: bytes, *parts: bytes) -> bytes:
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


def _nonce(ctr: int) -> bytes:
    return b"\x00\x00\x00\x00" + struct.pack(">Q", ctr)


class Session:
    """Two directions, each with its own key and strictly ordered counter."""

    def __init__(self, k_tx: bytes, k_rx: bytes):
        self._ktx, self._krx = k_tx, k_rx
        self._ctx = self._crx = 0
        self._rx_dead = self._tx_dead = False
        self._buf = b""

    def _seal(self, typ: bytes, body: bytes) -> bytes:
        pt = typ + body
        if self._tx_dead or self._ctx == _COUNTER_END:
            _fail("bad frame")
        if len(pt) > PT_MAX:
            _fail("frame too long")
        hdr = struct.pack(">H", len(pt))
        ct, tag = wc.aead_seal(self._ktx, _nonce(self._ctx), hdr, pt)
        self._ctx += 1
        return hdr + ct + tag

    def seal_line(self, raw: bytes) -> bytes:
        if len(raw) > LINE_MAX:
            _fail("frame too long")
        if not raw:
            _fail("bad frame")
        return self._seal(b"L", raw)

    def seal_blob(self, kind: bytes, offset: int, chunk: bytes) -> bytes:
        if kind not in (b"O", b"G") or not 0 <= offset < 2 ** 32:
            _fail("bad frame")
        if len(chunk) > LINE_MAX:
            _fail("frame too long")
        return self._seal(kind, struct.pack(">I", offset) + chunk)

    def feed(self, data: bytes) -> list:
        if self._rx_dead:
            _fail("bad frame")
        self._buf += data
        out = []
        while len(self._buf) >= 2:
            n = (self._buf[0] << 8) | self._buf[1]
            # Checked before a body byte is read: a reader that let 518
            # through would swallow 18 bytes of the next frame (S6.3).
            if n == 0 or n > PT_MAX:
                self._kill_rx()
                _fail("bad frame")
            end = 2 + n + TAG_LEN
            if len(self._buf) < end:
                break
            frame, self._buf = self._buf[:end], self._buf[end:]
            if self._crx == _COUNTER_END:
                self._kill_rx()
                _fail("bad frame")
            try:
                pt = wc.aead_open(self._krx, _nonce(self._crx), frame[:2],
                                  frame[2:2 + n], frame[2 + n:])
            except wc.AuthError:
                self._kill_rx()
                _fail("frame failed authentication")
            self._crx += 1
            if pt[:1] not in (b"L", b"O", b"G"):
                self._kill_rx()
                _fail("bad frame")
            out.append((pt[:1].decode("ascii"), pt[1:]))
        return out

    def _kill_rx(self):
        self._rx_dead = True
        self._buf = b""


def _recv_exact(sock, n: int, end: float) -> bytes:
    got = b""
    while len(got) < n:
        left = end - time.monotonic()
        if left <= 0:
            raise socket.timeout
        sock.settimeout(left)
        chunk = sock.recv(n - len(got))
        if not chunk:
            raise ConnectionError
        got += chunk
    return got


def client_handshake(sock, key: bytes, deadline_s: float = 2.0) -> Session:
    end = time.monotonic() + deadline_s
    try:
        m1 = _recv_exact(sock, M1_LEN, end)
        if m1[:4] != M1_MAGIC:       # a future 0x02 lands here, same words
            _fail("handshake failed")
        nb, np = m1[4:], os.urandom(16)
        sock.settimeout(max(end - time.monotonic(), 0.001))
        sock.sendall(np + _mac(key, b"blink-wifi-1 pc", nb, np))
        m3 = _recv_exact(sock, M3_LEN, end)
    except (OSError, ValueError):
        _fail("handshake failed")
    if not hmac.compare_digest(m3, _mac(key, b"blink-wifi-1 board", nb, np)):
        _fail("handshake failed")
    keys = wc.hkdf_sha256(nb + np, key, b"blink-wifi-1 session", 64)
    return Session(keys[:32], keys[32:])      # k_pb out, k_bp in


def verify(sock, key: bytes, deadline_s: float = 5.0) -> None:
    """S5.4 / D65: handshake, one sealed verify line, one sealed verified."""
    end = time.monotonic() + deadline_s
    s = client_handshake(sock, key, min(deadline_s, 2.0))
    try:
        sock.settimeout(max(end - time.monotonic(), 0.001))
        sock.sendall(s.seal_line(b'{"t":"verify"}\n'))
        while True:
            left = end - time.monotonic()
            if left <= 0:
                _fail("verify failed")
            sock.settimeout(left)
            data = sock.recv(4096)
            if not data:
                _fail("verify failed")
            for kind, body in s.feed(data):
                if kind == "L":
                    try:
                        ok = json.loads(body).get("t") == "verified"
                    except (ValueError, AttributeError):
                        ok = False
                    if ok:
                        return
                    _fail("verify failed")
    except SessionError:
        _fail("verify failed")
    except OSError:
        _fail("verify failed")


def disc_query(key: bytes) -> tuple:
    nq = os.urandom(16)
    return nq, Q_MAGIC + nq + _mac(key, b"blink-disc-1 q", nq)[:16]


def disc_answer_port(key: bytes, nq: bytes, data: bytes):
    if len(data) != A_LEN or data[:4] != A_MAGIC:
        return None
    port = data[20:22]
    want = _mac(key, b"blink-disc-1 a", nq, port)[:16]
    if not hmac.compare_digest(data[4:20], want):
        return None
    return struct.unpack(">H", port)[0]
