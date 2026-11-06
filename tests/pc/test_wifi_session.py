"""pc/wifi_session against the fake board (spec S6.2 to S6.4, S5.4)."""
import json
import os
import random
import socket
import struct

import pytest

from pc import wifi_crypto as wc
from pc import wifi_session as ws
from tests.support.fake_wifi_board import FakeWifiBoard

KEY = bytes(range(32))
FIXED = {"handshake failed", "bad frame", "frame too long",
         "frame failed authentication", "verify failed"}


@pytest.fixture
def board():
    made = []

    def make(**kw):
        b = FakeWifiBoard(KEY, **kw).start()
        made.append(b)
        return b
    yield make
    for b in made:
        b.stop()


def connect(b):
    s = socket.create_connection(b.address, timeout=2)
    return s


def pair():
    """Two Sessions that talk to each other: a PC and a stand-in board."""
    k1, k2 = os.urandom(32), os.urandom(32)
    return ws.Session(k1, k2), ws.Session(k2, k1)


def read_until(s, sess, want, n=1):
    got = []
    while len(got) < n:
        data = s.recv(4096)
        assert data
        got += sess.feed(data)
    return got


def test_handshake_with_the_fake_board(board):
    b = board(ping_interval_s=0.1, hello_at_start=True)
    s = connect(b)
    sess = ws.client_handshake(s, KEY)
    got = read_until(s, sess, "L", 2)
    assert json.loads(got[0][1])["caps"] == ["wifi", "logo", "pref"]
    s.sendall(sess.seal_line(b'{"t":"welcome"}\n'))
    seen = []
    while "wifi_state" not in seen:
        seen += [json.loads(x[1])["t"] for x in read_until(s, sess, "L")]
    s.sendall(sess.seal_line(b'{"t":"pong"}\n'))
    s.close()


class _Raw:
    """A socket stand-in that plays canned bytes."""
    def __init__(self, data):
        self.data, self.sent = data, b""

    def settimeout(self, t): pass
    def sendall(self, d): self.sent += d

    def recv(self, n):
        out, self.data = self.data[:n], self.data[n:]
        return out


def test_wrong_m1_magic_is_a_fixed_error():
    with pytest.raises(ws.SessionError, match="^handshake failed$"):
        ws.client_handshake(_Raw(b"BLW\x02" + b"n" * 16), KEY)


def test_m3_from_a_wrong_key_refused(board):
    s = connect(board(wrong_key=True))
    with pytest.raises(ws.SessionError, match="^handshake failed$"):
        ws.client_handshake(s, KEY)


def test_close_after_m1_is_handshake_failed(board):
    s = connect(board(close_after_m1=True))
    with pytest.raises(ws.SessionError) as e1:
        ws.client_handshake(s, KEY)
    s = connect(board(wrong_key=True))
    with pytest.raises(ws.SessionError) as e2:
        ws.client_handshake(s, KEY)
    assert str(e1.value) == str(e2.value) == "handshake failed"


def test_stalled_m1_times_out_as_handshake_failed(board):
    s = connect(board(stall_m1=True))
    with pytest.raises(ws.SessionError, match="^handshake failed$"):
        ws.client_handshake(s, KEY, deadline_s=0.2)


def test_a_line_over_512_is_refused_before_sealing():
    pc, _ = pair()
    with pytest.raises(ws.SessionError, match="^frame too long$"):
        pc.seal_line(b"x" * 513)
    # The counter did not move: the next frame still opens in order.
    pc2, bd = pair()
    with pytest.raises(ws.SessionError):
        pc2.seal_line(b"x" * 513)
    assert bd.feed(pc2.seal_line(b"x" * 512)) == [("L", b"x" * 512)]


def frame_of(key, ctr, n):
    pt = b"L" + b"a" * (n - 1)
    hdr = struct.pack(">H", n)
    ct, tag = wc.aead_seal(key, b"\0\0\0\0" + struct.pack(">Q", ctr), hdr, pt)
    return hdr + ct + tag


def test_len_bounds_shared_with_the_board():
    k = os.urandom(32)
    for n in (0, 518, 65535):               # refused on the header alone
        s = ws.Session(k, k)
        with pytest.raises(ws.SessionError, match="^bad frame$"):
            s.feed(struct.pack(">H", n))
        with pytest.raises(ws.SessionError):   # and the direction stays dead
            s.feed(frame_of(k, 0, 1))
    for n in (1, 517):
        assert len(ws.Session(k, k).feed(frame_of(k, 0, n))[0][1]) == n - 1
    s = ws.Session(k, k)
    s.feed(frame_of(k, 0, 517)[:2])           # no body byte needed to accept


def test_a_frame_under_two_bytes_waits_and_never_raises():
    s = ws.Session(b"k" * 32, b"k" * 32)
    assert s.feed(b"") == [] and s.feed(b"\x00") == []


def test_replayed_and_reordered_frames_refused():
    pc, bd = pair()
    f0, f1 = pc.seal_line(b"a\n"), pc.seal_line(b"b\n")
    with pytest.raises(ws.SessionError, match="authentication"):
        bd.feed(f1)                          # a gap
    pc, bd = pair()
    f0 = pc.seal_line(b"a\n")
    assert bd.feed(f0)
    with pytest.raises(ws.SessionError, match="authentication"):
        bd.feed(f0)                          # a replay
    with pytest.raises(ws.SessionError):     # failed open closed the direction
        bd.feed(pc.seal_line(b"c\n"))


def test_feed_reassembles_frames_split_anywhere():
    pc, bd = pair()
    lines = [b'{"t":"a"}\n', b"y" * 512, b"z\n"]
    wire = b"".join(pc.seal_line(x) for x in lines)
    for step in (1, 3, 17, 100):
        pc, bd = pair()
        wire = b"".join(pc.seal_line(x) for x in lines)
        got = []
        for i in range(0, len(wire), step):
            got += bd.feed(wire[i:i + step])
        assert got == [("L", x) for x in lines]


def test_errors_never_carry_data():
    rng = random.Random(7)
    for _ in range(300):
        pc, bd = pair()
        blob = bytearray(pc.seal_line(bytes(rng.randrange(256) for _ in range(rng.randrange(1, 40)))))
        for _ in range(rng.randrange(1, 4)):
            blob[rng.randrange(len(blob))] ^= 1 << rng.randrange(8)
        try:
            bd.feed(bytes(blob) + os.urandom(rng.randrange(0, 20)))
        except ws.SessionError as e:
            assert str(e) in FIXED
    for bad in (lambda: pair()[0].seal_blob(b"X", 0, b""),
                lambda: pair()[0].seal_blob(b"O", 2 ** 32, b""),
                lambda: pair()[0].seal_blob(b"O", 0, b"x" * 513),
                lambda: ws.client_handshake(_Raw(os.urandom(20)), KEY)):
        with pytest.raises(ws.SessionError) as e:
            bad()
        assert str(e.value) in FIXED


def test_discovery_round_trip():
    sent = []
    nq, dgram = ws.disc_query(KEY)
    assert len(dgram) == ws.Q_LEN and dgram[:4] == b"BLQ\x01" and dgram[4:20] == nq
    sent.append(dgram)                       # what the injected sendto got
    import hashlib, hmac
    port = struct.pack(">H", ws.PORT)
    tag = hmac.new(KEY, b"blink-disc-1 a" + nq + port, hashlib.sha256).digest()[:16]
    ans = b"BLA\x01" + tag + port
    assert len(ans) == ws.A_LEN
    assert ws.disc_answer_port(KEY, nq, ans) == ws.PORT
    # and the board's own check of the query
    qtag = hmac.new(KEY, b"blink-disc-1 q" + nq, hashlib.sha256).digest()[:16]
    assert dgram[20:] == qtag


def test_discovery_answer_with_bad_tag_is_none():
    nq, _ = ws.disc_query(KEY)
    port = struct.pack(">H", ws.PORT)
    assert ws.disc_answer_port(KEY, nq, b"BLA\x01" + b"\0" * 16 + port) is None
    assert ws.disc_answer_port(KEY, nq, b"BLA\x01") is None
    assert ws.disc_answer_port(KEY, nq, b"XXXX" + b"\0" * 18) is None


def test_seal_blob_kinds(board):
    pc, bd = pair()
    for kind in (b"O", b"G"):
        got = bd.feed(pc.seal_blob(kind, 70000, b"chunk"))
        assert got == [(kind.decode(), struct.pack(">I", 70000) + b"chunk")]


def test_blobs_reach_the_fake_board_and_ack(board):
    b = board()
    s = connect(b)
    sess = ws.client_handshake(s, KEY)
    s.sendall(sess.seal_line(b'{"t":"ota_begin","size":20000}\n'))
    img = os.urandom(20000)
    for off in range(0, len(img), 512):
        s.sendall(sess.seal_blob(b"O", off, img[off:off + 512]))
    acks = []
    while 20000 not in acks:
        for k, body in read_until(s, sess, "L"):
            m = json.loads(body)
            if m["t"] == "ota_ack":
                acks.append(m["off"])
    assert bytes(b.ota_sink) == img
    assert 0 in acks and any(a % 16384 == 0 and a for a in acks)
    s.close()


def test_verify_round_trip_and_failure(board):
    b = board(verify_window=True)
    s = connect(b)
    ws.verify(s, KEY)
    s.close()
    s = connect(board(usb_active=True))      # no window: refused at accept
    with pytest.raises(ws.SessionError, match="^handshake failed$"):
        ws.verify(s, KEY)
    s = connect(board(verify_window=True))
    with pytest.raises(ws.SessionError, match="^handshake failed$"):
        ws.verify(s, os.urandom(32))
