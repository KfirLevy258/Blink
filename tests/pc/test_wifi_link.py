import hashlib
import hmac
import json
import re
import socket
import struct
import time

import pytest

from pc import wifi_link, wifi_pair
from tests.support.fake_wifi_board import FakeWifiBoard

KEY = bytes(range(32))


def _mac(key, *parts):
    return hmac.new(key, b"".join(parts), hashlib.sha256).digest()


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def board():
    made = []

    def make(key=KEY, **kw):
        b = FakeWifiBoard(key, **kw).start()
        made.append(b)
        return b
    yield make
    for b in made:
        b.stop()


def _pair(home, port, key=KEY):
    wifi_pair.save(str(home), "board", key, "127.0.0.1", port)


def _nodisc(key):
    return None


def _until(cond, secs=3.0):
    end = time.monotonic() + secs
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


def _read_line(link):
    got = b""
    end = time.monotonic() + 3
    while not got.endswith(b"\n") and time.monotonic() < end:
        got += link.read(512)
    return got


def _fake_air(key, answer_port, stranger=False, log=None):
    """sendto/recv pair standing in for the LAN: the board answers each query."""
    pending = []

    def sendto(data, addr):
        if log is not None:
            log.append(addr)
        nq = data[4:20]
        port = struct.pack(">H", answer_port)
        if stranger:
            pending.append((b"BLA\x01" + b"\x00" * 16 + port, ("127.0.0.9", 1)))
        pending.append((b"BLA\x01" + _mac(key, b"blink-disc-1 a", nq, port)[:16] + port,
                        ("127.0.0.1", 5)))

    def recv(timeout_s):
        if pending:
            return pending.pop(0)
        time.sleep(min(timeout_s, 0.01))
        return None
    return sendto, recv


def test_read_write_through_the_fake_board(tmp_path, board):
    b = board(hello_at_start=True)
    _pair(tmp_path, b.address[1])
    link, out = wifi_link.connect(str(tmp_path), discover=_nodisc)
    assert out == "connected" and link.kind == "wifi"
    assert json.loads(_read_line(link))["t"] == "hello"
    link.write(b'{"t":"welcome","v":1}\n')
    assert _until(lambda: link.in_waiting)
    assert json.loads(_read_line(link))["t"] == "wifi_state"
    assert b.lines[0]["t"] == "welcome"
    link.close()


def test_session_drop_raises_oserror(tmp_path, board):
    b = board()
    _pair(tmp_path, b.address[1])
    link, _ = wifi_link.connect(str(tmp_path), discover=_nodisc)
    _read_line(link)
    b.stop()
    with pytest.raises(OSError):
        for _ in range(50):
            link.read(512)
    with pytest.raises(OSError):
        link.write(b"{}\n")


def test_a_stale_address_falls_back_to_discovery(tmp_path, board):
    b = board()
    _pair(tmp_path, _free_port())
    seen = []
    link, out = wifi_link.connect(
        str(tmp_path), discover=lambda k: seen.append(1) or ("127.0.0.1", b.address[1]))
    assert out == "connected" and seen
    assert wifi_pair.load(str(tmp_path))["port"] == b.address[1]
    link.close()


def test_an_impostor_m1_is_an_ordinary_failure(tmp_path, board, capsys):
    imp = board(close_after_m1=True)
    real = board()
    _pair(tmp_path, imp.address[1])
    link, out = wifi_link.connect(
        str(tmp_path), discover=lambda k: ("127.0.0.1", real.address[1]))
    assert out == "connected"
    assert wifi_pair.load(str(tmp_path))["port"] == real.address[1]
    link.close()


def test_a_wrong_key_everywhere_is_failed(tmp_path, board, capsys):
    b = board(wrong_key=True)
    _pair(tmp_path, b.address[1])
    link, out = wifi_link.connect(
        str(tmp_path), discover=lambda k: ("127.0.0.1", b.address[1]))
    assert link is None and out == "failed"
    err = capsys.readouterr().err
    assert "another computer" not in err and "handshake refused" in err


def test_not_paired_and_not_found(tmp_path):
    assert wifi_link.connect(str(tmp_path)) == (None, "not paired")
    _pair(tmp_path, _free_port())
    assert wifi_link.connect(str(tmp_path), discover=_nodisc) == (None, "not found")


def test_discovery_sends_only_to_broadcast_through_the_injected_sendto():
    log = []
    sendto, recv = _fake_air(KEY, 49731, log=log)
    assert wifi_link.discover(KEY, sendto, recv, gap_s=0.05) == ("127.0.0.1", 49731)
    assert log == [("255.255.255.255", 49731)]
    # nothing answers: three queries, all to broadcast, then None
    log.clear()
    assert wifi_link.discover(KEY, lambda d, a: log.append(a), lambda t: None,
                              tries=3, gap_s=0.01) is None
    assert log == [("255.255.255.255", 49731)] * 3


def test_discovery_ignores_strangers_answers():
    sendto, recv = _fake_air(KEY, 1234, stranger=True)
    assert wifi_link.discover(KEY, sendto, recv, gap_s=0.05) == ("127.0.0.1", 1234)
    sendto, recv = _fake_air(bytes(32), 1234)       # answers tagged with another key
    assert wifi_link.discover(KEY, sendto, recv, tries=1, gap_s=0.05) is None


def test_write_blob_kinds(tmp_path, board):
    b = board()
    _pair(tmp_path, b.address[1])
    link, _ = wifi_link.connect(str(tmp_path), discover=_nodisc)
    link.write(b'{"t":"ota_begin","size":3}\n')
    link.write(b'{"t":"logo_begin","size":2}\n')
    link.write_blob(b"O", 0, b"abc")
    link.write_blob(b"G", 0, b"xy")
    assert _until(lambda: bytes(b.ota_sink) == b"abc" and bytes(b.logo_sink) == b"xy")
    with pytest.raises(OSError):
        link.write_blob(b"X", 0, b"z")
    link.close()


def test_no_log_line_carries_an_address(tmp_path, board, capsys):
    good, wrong, imp = board(), board(wrong_key=True), board(close_after_m1=True)
    for p, found in ((good.address[1], None), (_free_port(), good.address[1]),
                     (wrong.address[1], wrong.address[1]),
                     (imp.address[1], None)):
        _pair(tmp_path, p)
        link, _ = wifi_link.connect(
            str(tmp_path),
            discover=(lambda k, f=found: ("127.0.0.1", f) if f else None))
        if link:
            link.close()
    err = capsys.readouterr().err
    assert err
    assert not re.search(r"\d+\.\d+\.\d+\.\d+", err)
    assert not re.search(r"[0-9a-fA-F]{5,}", err)


# ---- fix round 1 ----

from pc import private_file, wifi_session as ws

FIXED = {"link lost", "bad frame", "frame too long",
         "frame failed authentication", "handshake failed", "verify failed"}


class StubSock:
    """recv replays a script: bytes, b'' (EOF) or an exception instance."""

    def __init__(self, script=(), send_error=None):
        self.script, self.send_error, self.closed = list(script), send_error, 0

    def settimeout(self, t):
        pass

    def recv(self, n):
        item = self.script.pop(0) if self.script else socket.timeout()
        if isinstance(item, Exception):
            raise item
        return item

    def sendall(self, data):
        if self.send_error:
            raise self.send_error

    def close(self):
        self.closed += 1


def _pair_sessions():
    kt, kr = bytes([1]) * 32, bytes([2]) * 32
    return ws.Session(kt, kr), ws.Session(kr, kt)       # link side, peer side


def _assert_dead(link):
    for call in (lambda: link.read(8), lambda: link.write(b"x\n")):
        with pytest.raises(OSError) as e:
            call()
        assert str(e.value) in FIXED


def test_every_failure_path_closes_the_socket():
    mine, peer = _pair_sessions()
    bad = bytearray(peer.seal_line(b'{"t":"ping"}\n'))
    bad[-1] ^= 1
    cases = [
        (StubSock([b""]), lambda l: l.read(8)),                       # EOF
        (StubSock([bytes(bad)]), lambda l: l.read(8)),                # corrupt tag
        (StubSock([OSError()]), lambda l: l.read(8)),                 # recv error
        (StubSock(send_error=BrokenPipeError()), lambda l: l.write(b"x\n")),
    ]
    for sock, act in cases:
        link = wifi_link.WifiLink(sock, _pair_sessions()[0] if sock.script != [bytes(bad)] else mine)
        with pytest.raises(OSError):
            act(link)
        assert sock.closed >= 1
        _assert_dead(link)


def test_a_failed_handshake_closes_the_socket(tmp_path):
    _pair(tmp_path, 1)
    sock = StubSock([b""])
    link, out = wifi_link.connect(str(tmp_path), create_connection=lambda a, t: sock,
                                  discover=_nodisc)
    assert link is None and out == "failed" and sock.closed >= 1


def test_a_frame_split_across_two_recvs():
    mine, peer = _pair_sessions()
    frame = peer.seal_line(b'{"t":"ping"}\n')
    link = wifi_link.WifiLink(StubSock([frame[:7], frame[7:]]), mine)
    assert link.read(512) == b""
    assert link.read(512) == b'{"t":"ping"}\n'


def test_a_failed_address_refresh_keeps_the_link(tmp_path, board, monkeypatch):
    b = board(hello_at_start=True)
    _pair(tmp_path, _free_port())

    def boom(*a):
        raise private_file.NotPrivate

    monkeypatch.setattr(wifi_pair, "update_ip", boom)
    link, out = wifi_link.connect(
        str(tmp_path), discover=lambda k: ("127.0.0.1", b.address[1]))
    assert out == "connected"
    assert json.loads(_read_line(link))["t"] == "hello"
    link.close()


def test_the_serial_loop_contract():
    mine, peer = _pair_sessions()
    link = wifi_link.WifiLink(StubSock(), mine)
    t = time.monotonic()
    assert link.in_waiting == 0
    assert time.monotonic() - t < 0.1
    assert link.read(1) == b""
    link = wifi_link.WifiLink(StubSock([peer.seal_line(b'{"t":"ping"}\n')]), mine)
    assert link.in_waiting == 13
    assert link.read(5) == b'{"t":'
    assert link.in_waiting == 8 and len(link.read(100)) == 8
    # a real idle socket: read(1) gives up within about 0.2 s
    a, b_ = socket.socketpair()
    link = wifi_link.WifiLink(a, mine)
    t = time.monotonic()
    assert link.read(1) == b""
    assert 0.1 < time.monotonic() - t < 0.5
    t = time.monotonic()
    assert link.in_waiting == 0
    assert time.monotonic() - t < 0.1
    link.close()
    b_.close()


def test_discovery_survives_a_transient_sendto_error():
    sendto0, recv = _fake_air(KEY, 4321)
    calls = []

    def sendto(d, a):
        calls.append(1)
        if len(calls) == 1:
            raise OSError
        sendto0(d, a)

    assert wifi_link.discover(KEY, sendto, recv, gap_s=0.05) == ("127.0.0.1", 4321)
    assert len(calls) == 2
