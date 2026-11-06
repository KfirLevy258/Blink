#!/usr/bin/env python3
"""Talk to a paired board over WiFi the way the daemon will, and say what held.

    tools/wifi_probe.py --key-file KEY [--addr IP] [--lengths | --hold | --verify]
    tools/wifi_probe.py --new-key-file PATH

The bench check of Task 2.6: the PC cipher in pc/wifi_session and the board's
C cipher must agree at every plaintext length, because a length-dependent bug
(a block boundary, a 16-bit header, a buffer sized one short) only shows on
the frame that hits it. --lengths sends a pong padded to each size from 14 to
513 bytes of plaintext (500 frames), then the 517-byte maximum O frame, then
waits for the board's own ping: a board that survived has a counter that
survived.

It prints fixed sentences and counts only. Never a key, an address, a frame
length other than the one that failed, or any byte (CLAUDE.md, the serial
buffer rule applies to the network). The key file is 64 hex digits, as the
pairing file's "key"; --new-key-file writes a fresh one, owner-only and
never over an existing file, for the pairing script to load into the board.
"""
from __future__ import annotations

import argparse
import json
import os
import select
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pc import protocol, wifi_session as ws  # noqa: E402

FIRST_PT, LAST_PT = 14, 513      # 'L' + `{"t":"pong"}\n` is 14; a 512 line is 513


def _read_key(path):
    try:
        with open(path) as f:
            key = bytes.fromhex(f.read().strip())
    except (OSError, ValueError):
        return None
    return key if len(key) == 32 else None


def _discover(key, port, wait_s=3.0):
    nq, q = ws.disc_query(key)
    u = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        u.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        u.sendto(q, ("255.255.255.255", port))
        end = time.monotonic() + wait_s
        while time.monotonic() < end:
            u.settimeout(max(end - time.monotonic(), 0.01))
            try:
                data, src = u.recvfrom(64)
            except OSError:
                break
            p = ws.disc_answer_port(key, nq, data)
            if p:
                return src[0], p
    finally:
        u.close()
    return None


class _Conn:
    def __init__(self, sock, sess):
        self.sock, self.sess = sock, sess
        self.frames = self.pings = 0
        self.lines = []

    def pump(self, wait_s):
        """Read what is there for up to wait_s; False when the board hung up."""
        r, _, _ = select.select([self.sock], [], [], wait_s)
        if not r:
            return True
        data = self.sock.recv(4096)
        if not data:
            return False
        for kind, body in self.sess.feed(data):
            self.frames += 1
            if kind == "L":
                try:
                    msg = json.loads(body)
                except ValueError:
                    continue
                t = msg.get("t") if isinstance(msg, dict) else None
                self.lines.append(t)
                if t == "ping":
                    self.pings += 1
                    self.sock.sendall(self.sess.seal_line(b'{"t":"pong"}\n'))
        return True

    def wait_for(self, t, timeout_s):
        end, seen = time.monotonic() + timeout_s, len(self.lines)
        while time.monotonic() < end:
            if not self.pump(0.1):
                return False
            if t in self.lines[seen:]:
                return True
        return False


def _lengths(c, out):
    for n in range(FIRST_PT, LAST_PT + 1):
        raw = b'{"t":"pong"' + b" " * (n - 14) + b"}\n"
        try:
            c.sock.sendall(c.sess.seal_line(raw))
            # A hung-up board shows as EOF or a failed send; looking after
            # every frame is what pins it to the length that did it.
            if not c.pump(0.005):
                raise OSError
        except OSError:
            print("fail: length %d" % n, file=out)
            return 1
    try:
        c.sock.sendall(c.sess.seal_blob(b"O", 0, b"\0" * 512))
    except OSError:
        print("fail: length 517", file=out)
        return 1
    before = c.pings
    end = time.monotonic() + 11
    try:
        while c.pings == before and time.monotonic() < end:
            if not c.pump(0.1):
                break
    except OSError:
        pass
    if c.pings == before:
        print("fail: no ping after the frames", file=out)
        return 1
    print("ok: %d lengths" % (LAST_PT - FIRST_PT + 1), file=out)
    return 0


def _hold(c, out, seconds):
    end = None if seconds is None else time.monotonic() + seconds
    shown = False
    try:
        while end is None or time.monotonic() < end:
            if not c.pump(0.2):
                print("fail: board closed", file=out)
                return 1
            if "theme_edit" in c.lines and not shown:
                shown = True
                print("ok: theme_edit received", file=out)
    except KeyboardInterrupt:
        pass
    except (OSError, ws.SessionError):
        print("fail: session lost after %d board frames" % c.frames, file=out)
        return 1
    print("ok: %d board frames" % c.frames, file=out)
    return 0


def main(argv, out=sys.stdout) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--key-file")
    ap.add_argument("--new-key-file")
    ap.add_argument("--addr")
    ap.add_argument("--port", type=int, default=ws.PORT)
    ap.add_argument("--lengths", action="store_true")
    ap.add_argument("--hold", action="store_true")
    ap.add_argument("--verify", action="store_true")
    ap.add_argument("--seconds", type=float, help="end --hold after this long")
    a = ap.parse_args(argv)

    if a.new_key_file:
        try:
            fd = os.open(a.new_key_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(os.urandom(32).hex() + "\n")
        except OSError:
            print("fail: key file not written", file=out)
            return 2
        print("ok: key written", file=out)
        return 0
    key = _read_key(a.key_file) if a.key_file else None
    if key is None:
        print("fail: no usable key file", file=out)
        return 2
    addr, port = a.addr, a.port
    if not addr:
        found = _discover(key, port)
        if not found:
            print("fail: board not found", file=out)
            return 1
        addr, port = found
    try:
        sock = socket.create_connection((addr, port), timeout=3)
    except OSError:
        print("fail: cannot connect", file=out)
        return 1
    try:
        if a.verify:
            try:
                ws.verify(sock, key)
            except ws.SessionError:
                print("handshake refused", file=out)
                return 1
            print("ok: verified", file=out)
            return 0
        try:
            sess = ws.client_handshake(sock, key)
        except ws.SessionError:
            print("handshake refused", file=out)
            return 1
        c = _Conn(sock, sess)
        try:
            ok = c.wait_for("hello", 5)
            raw, _ = protocol.encode_checked(protocol.welcome("wifi_probe", "0"))
            sock.sendall(sess.seal_line(raw))
            print("ok: session open", file=out)
            if not (ok and c.wait_for("wifi_state", 5)):
                print("fail: no wifi_state", file=out)
                return 1
            print("ok: state received", file=out)
            if a.lengths:
                return _lengths(c, out)
            if a.hold:
                return _hold(c, out, a.seconds)
        except (OSError, ws.SessionError):
            print("fail: session lost", file=out)
            return 1
        return 0
    finally:
        sock.close()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
