"""A loopback stand-in for the board's WiFi side (spec S5.4, S7.4).

Generated behaviour only: no capture, no real key, nothing outside loopback.
It speaks the board's half of pc/wifi_session's protocol on its own thread,
written against the spec rather than against pc/wifi_session, so a codec bug
shared by both halves would still need the C side to find it (Task 2.6).

Knobs mirror the failures the daemon must survive: a wrong-key M3, a stalled
or abandoned handshake, garbage where a frame belongs, a close on a frame of
one chosen length, and the verify window (D65). `usb_active` is the one
addition to the planned signature: a board whose cable is in, which refuses
the handshake at accept unless the verify window is open.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import socket
import struct
import threading
import time

from pc import wifi_crypto as wc

ACK_STEP = 16 * 1024


def _mac(key, *parts):
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


def _nonce(c):
    return b"\x00\x00\x00\x00" + struct.pack(">Q", c)


class FakeWifiBoard:
    def __init__(self, key: bytes, port: int = 0, *, wrong_key=False,
                 stall_m1=False, close_after_m1=False, garbage_frame=False,
                 ping_interval_s: float = 10.0, close_on_length=None,
                 verify_window: bool = False, usb_active: bool = False):
        self.key, self.port = key, port
        self.wrong_key, self.stall_m1 = wrong_key, stall_m1
        self.close_after_m1, self.garbage_frame = close_after_m1, garbage_frame
        self.ping_interval_s, self.close_on_length = ping_interval_s, close_on_length
        self.verify_window = verify_window
        self.usb_active = usb_active or verify_window
        self.lines, self.pongs = [], 0
        self.ota_sink, self.logo_sink = bytearray(), bytearray()
        self.address = None
        self._srv = None
        self._stop = threading.Event()
        self._thread = None
        self._conns = []

    def start(self):
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", self.port))
        self._srv.listen(1)
        self._srv.settimeout(0.1)
        self.address = self._srv.getsockname()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        return self

    def stop(self):
        self._stop.set()
        for c in list(self._conns):
            try:
                c.close()
            except OSError:
                pass
        if self._thread:
            self._thread.join(2)
        if self._srv:
            self._srv.close()

    def _run(self):
        while not self._stop.is_set():
            try:
                c, _ = self._srv.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            self._conns.append(c)
            try:
                self._serve(c)
            except (OSError, wc.AuthError, ValueError):
                pass
            finally:
                c.close()

    def _read(self, c, n):
        got = b""
        while len(got) < n:
            if self._stop.is_set():
                raise OSError
            try:
                chunk = c.recv(n - len(got))
            except socket.timeout:
                continue
            if not chunk:
                raise OSError
            got += chunk
        return got

    def _serve(self, c):
        c.settimeout(0.1)
        if self.usb_active and not self.verify_window:
            return                          # closed at accept
        nb = os.urandom(16)
        if self.stall_m1:
            while not self._stop.is_set():
                time.sleep(0.05)
            return
        c.sendall(b"BLW\x01" + nb)
        if self.close_after_m1:
            return
        m2 = self._read(c, 48)
        np = m2[:16]
        if not hmac.compare_digest(m2[16:], _mac(self.key, b"blink-wifi-1 pc", nb, np)):
            return
        k3 = os.urandom(32) if self.wrong_key else self.key
        c.sendall(_mac(k3, b"blink-wifi-1 board", nb, np))
        ks = wc.hkdf_sha256(nb + np, self.key, b"blink-wifi-1 session", 64)
        rx_key, tx_key = ks[:32], ks[32:]
        self._tx, self._rx = [tx_key, 0], [rx_key, 0]
        if self.garbage_frame:
            c.sendall(struct.pack(">H", 5) + os.urandom(21))
            return
        if self.verify_window:
            self._verify_only(c)
            return
        self._line(c, {"t": "hello", "v": 1, "caps": ["wifi", "logo", "pref"]})
        self._session(c)

    def _send(self, c, typ, body):
        key, ctr = self._tx
        pt = typ + body
        hdr = struct.pack(">H", len(pt))
        ct, tag = wc.aead_seal(key, _nonce(ctr), hdr, pt)
        self._tx[1] += 1
        c.sendall(hdr + ct + tag)

    def _line(self, c, msg):
        self._send(c, b"L", json.dumps(msg, separators=(",", ":")).encode() + b"\n")

    def _frame(self, c):
        hdr = self._read(c, 2)
        n = (hdr[0] << 8) | hdr[1]
        if self.close_on_length == n:
            raise OSError
        if n == 0 or n > 517:
            raise OSError
        body = self._read(c, n + 16)
        key, ctr = self._rx
        pt = wc.aead_open(key, _nonce(ctr), hdr, body[:n], body[n:])
        self._rx[1] += 1
        return pt[:1], pt[1:]

    def _verify_only(self, c):
        typ, body = self._frame(c)
        if typ == b"L" and json.loads(body).get("t") == "verify":
            self._line(c, {"t": "verified"})

    def _session(self, c):
        nxt = time.monotonic() + self.ping_interval_s
        sinks = {}
        while not self._stop.is_set():
            if time.monotonic() >= nxt:
                self._line(c, {"t": "ping"})
                nxt += self.ping_interval_s
            c.settimeout(0.05)
            try:
                first = c.recv(1, socket.MSG_PEEK)
            except socket.timeout:
                continue
            if not first:
                return
            c.settimeout(0.1)
            typ, body = self._frame(c)
            if typ == b"L":
                msg = json.loads(body)
                self.lines.append(msg)
                t = msg.get("t")
                if t == "welcome":
                    self._line(c, {"t": "wifi_state", "v": 1, "st": "up",
                                   "link": "wifi"})
                elif t == "pong":
                    self.pongs += 1
                elif t in ("ota_begin", "logo_begin"):
                    kind = b"O" if t == "ota_begin" else b"G"
                    sinks[kind] = [msg.get("size", -1), 0, "ota_ack" if kind == b"O" else "logo_ack"]
                    self._line(c, {"t": sinks[kind][2], "off": 0})
            else:
                off = struct.unpack(">I", body[:4])[0]
                sink = self.ota_sink if typ == b"O" else self.logo_sink
                if off != len(sink):
                    raise OSError
                before = len(sink)
                sink += body[4:]
                size, _, name = sinks.get(typ, [-1, 0, "ota_ack"])
                got = len(sink)
                if got // ACK_STEP > before // ACK_STEP or got == size:
                    self._line(c, {"t": name, "off": got})
