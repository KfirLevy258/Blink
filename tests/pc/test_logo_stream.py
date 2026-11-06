"""LogoStream through the real Bridge.pump(), over the fake WiFi board and a
fake serial port (spec S15.2, Task 6.2). The blobs are generated here; sizes
sit around the 512-byte WiFi chunk, the 256-byte USB chunk and the 16 KiB ack
step."""
import base64
import json
import time
import zlib

import pytest

from pc import logo_stream, protocol, wifi_link, wifi_pair
from pc.bridge import Bridge
from tests.support.fake_wifi_board import FakeWifiBoard

KEY = bytes(range(32))


def _blob(n):
    return bytes((i * 11 + 5) & 0xFF for i in range(n))


def _bridge(write):
    return Bridge(write_msg=write, fetch_usage=lambda: {})


def _drive(b, link, until, secs=20.0):
    reader = protocol.LineReader()
    end = time.monotonic() + secs
    while time.monotonic() < end and not until():
        if link.in_waiting:
            for m in reader.feed(link.read(4096)):
                b.on_message(m)
        b.pump()
        time.sleep(0.001)
    return until()


@pytest.fixture
def rig(tmp_path):
    made = []

    def make():
        board = FakeWifiBoard(KEY).start()
        wifi_pair.save(str(tmp_path), "board", KEY, "127.0.0.1",
                       board.address[1])
        link, out = wifi_link.connect(str(tmp_path), discover=lambda k: None)
        assert out == "connected"
        made.append((board, link))
        return board, link
    yield make
    for board, link in made:
        link.close()
        board.stop()


@pytest.mark.parametrize("size", [400003, 513])
def test_logo_stream_over_fake_board_wifi(rig, size):
    blob = _blob(size)
    board, link = rig()
    b = _bridge(lambda m: link.write(protocol.encode(m)))
    b.start_logo(blob, link)
    assert _drive(b, link, lambda: b.logo_result is not None)
    assert b.logo_result == (True, "")
    assert bytes(board.logo_sink) == blob and not board.ota_sink
    begin = [m for m in board.lines if m.get("t") == "logo_begin"]
    assert len(begin) == 1 and begin[0]["size"] == size
    assert begin[0]["crc"] == zlib.crc32(blob)
    assert [m["t"] for m in board.lines].count("logo_end") == 1
    assert not b.logo_streaming()


class FakeUsb:
    """A serial port with the board behind it: a bounded UART RX ring that
    DROPS a line that does not fit (the firmware's 2 KB ring, proto.c), and a
    board that drains the ring once per bridge pass, acks EVERY chunk, and
    answers logo_end with logo_state."""
    kind = "usb"
    RING = 2048

    def __init__(self, present=1):
        self.bridge, self.lines, self.got, self.size = None, [], bytearray(), -1
        self.ring, self.used, self.dropped, self.present = [], 0, 0, present

    def write(self, raw):
        assert len(raw) <= protocol.MAX_LINE_BYTES
        if self.used + len(raw) > self.RING:
            self.dropped += 1
            return
        self.ring.append(raw)
        self.used += len(raw)

    def service(self):
        lines, self.ring, self.used = self.ring, [], 0
        for raw in lines:
            m = json.loads(raw)
            self.lines.append(m)
            t = m["t"]
            if t == "logo_begin":
                self.size = m["size"]
                self.bridge.on_message({"t": "logo_ack", "off": 0})
            elif t == "logo_chunk":
                if m["off"] != len(self.got):
                    continue
                self.got += base64.b64decode(m["d"])
                self.bridge.on_message({"t": "logo_ack", "off": len(self.got)})
            elif t == "logo_end":
                self.bridge.on_message({"t": "logo_state",
                                        "present": self.present})


def _run_usb(blob, present=1):
    usb = FakeUsb(present)
    b = _bridge(lambda m: usb.write(protocol.encode(m)))
    usb.bridge = b
    b.start_logo(blob, usb)
    for _ in range(100000):
        if b.logo_result is not None:
            break
        b.pump()
        usb.service()
    return b, usb


@pytest.mark.parametrize("size", [400003, 513])
def test_logo_stream_over_fake_board_usb(size):
    blob = _blob(size)
    b, usb = _run_usb(blob)
    assert b.logo_result == (True, "")
    assert usb.dropped == 0 and bytes(usb.got) == blob
    assert [m["t"] for m in usb.lines].count("logo_end") == 1
    assert max(len(m["d"]) for m in usb.lines if m["t"] == "logo_chunk") == 344


def test_a_32k_window_over_usb_overflows_the_ring(monkeypatch):
    """The fake ring is real: with the WiFi window the stream floods it, lines
    are dropped and the image never arrives."""
    monkeypatch.setattr(logo_stream, "WINDOW_USB", 32 * 1024)
    b, usb = _run_usb(_blob(20000))
    assert usb.dropped > 0 and bytes(usb.got) != _blob(20000)


def test_the_stream_is_not_done_until_the_board_says_present():
    b, usb = _run_usb(_blob(1000), present=0)
    assert b.logo_result == (False, logo_stream.REFUSED)
    assert logo_stream.REFUSED == "The board refused the logo"
    # logo_end sent, no logo_state yet: still running
    usb = FakeUsb()
    b = _bridge(lambda m: usb.write(protocol.encode(m)))
    usb.bridge = b
    usb.size = 0
    b.start_logo(_blob(300), usb)
    usb.service = lambda: None
    for _ in range(5):
        b.pump()
        for m in [{"t": "logo_ack", "off": 0}]:
            b.on_message(m)
    b.on_message({"t": "logo_ack", "off": 256})
    b.on_message({"t": "logo_ack", "off": 300})
    b.pump()
    assert b.logo_streaming() and b.logo_result is None


def test_oversize_blob_is_refused_before_transmission(rig):
    board, link = rig()
    b = _bridge(lambda m: link.write(protocol.encode(m)))
    with pytest.raises(ValueError):
        b.start_logo(_blob(1000003), link)
    with pytest.raises(ValueError):
        logo_stream.LogoStream(_blob(0x80001), "wifi")
    b.pump()
    time.sleep(0.2)
    assert not [m for m in board.lines if m.get("t") == "logo_begin"]
    logo_stream.LogoStream(_blob(0x80000), "usb")           # exactly the cap fits


def test_usb_logo_chunk_fits_the_line():
    """A 256-byte chunk is 344 base64 characters; the whole logo_chunk line,
    with the largest offset (0x7FF00), measures 389 bytes against the board's
    512. A 384-byte chunk (512 characters) would not fit, which is why USB
    stops at 256."""
    line = protocol.encode(protocol.logo_chunk(0x7FF00, bytes(range(256))))
    assert len(line) == 389 < protocol.MAX_LINE_BYTES
    raw, why = protocol.encode_checked(protocol.logo_chunk(0x7FF00, bytes(384)))
    assert raw is None and why


def test_stall_and_refusal_end_the_stream_without_sealing():
    t = [0.0]
    sent = []

    class L:
        kind = "wifi"

        def write(self, raw):
            sent.append(json.loads(raw)["t"])

        def write_blob(self, kind, off, chunk):
            sent.append("G")

    s = logo_stream.LogoStream(_blob(5000), "wifi", now=lambda: t[0])
    s.pump(L())
    t[0] = 31.0
    s.pump(L())
    assert s.done() == (False, logo_stream.STALLED) and "logo_end" not in sent
    s = logo_stream.LogoStream(_blob(5000), "wifi", now=lambda: t[0])
    s.pump(L())
    s.ack(0, "bad")
    assert s.done() == (False, logo_stream.REFUSED)
    # no chunk before the ready ack
    s = logo_stream.LogoStream(_blob(5000), "wifi", now=lambda: t[0])
    s.pump(L())
    s.pump(L())
    assert "G" not in sent


def test_window_is_bounded():
    n = []

    class L:
        kind = "wifi"

        def write(self, raw):
            pass

        def write_blob(self, kind, off, chunk):
            n.append(len(chunk))

    s = logo_stream.LogoStream(_blob(200000), "wifi")
    s.pump(L())
    s.ack(0)
    s.pump(L())
    assert sum(n) == logo_stream.WINDOW


def test_tap_never_carries_logo_bytes():
    secret = bytes(range(200))
    msg = protocol.logo_chunk(512, secret)
    red = protocol.redact(msg)
    assert red == {"t": "logo_chunk", "off": 512}
    assert base64.b64encode(secret).decode() not in json.dumps(red)
    assert protocol.log_name(msg) == "logo_chunk"
    assert protocol.redact(protocol.logo_begin(9, 1234)) == {"t": "logo_begin", "size": 9}
    assert protocol.redact({"t": "logo_ack", "off": 16384}) == {"t": "logo_ack", "off": 16384}
    assert protocol.redact({"t": "logo_state", "present": 1, "bytes": 5,
                            "frames": 2, "x": "y"}) == {
        "t": "logo_state", "present": 1, "bytes": 5, "frames": 2}
    assert protocol.redact(protocol.pref_set(bright=40, focus=1)) == {
        "t": "pref_set", "bright": 40, "focus": 1}
    with pytest.raises(ValueError):
        protocol.pref_set(ssid="x")
