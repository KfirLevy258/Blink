"""WifiOtaStream through the real Bridge.pump() (spec S9, Task 5.2).

The images are generated here, never captured. Sizes are chosen around the
512-byte chunk and the 16 KiB ack step, because a release image is never a
multiple of either (spec S9.3).
"""
import hashlib
import os
import time

import pytest

from pc import protocol, wifi_link, wifi_pair, wifi_ota
from pc.bridge import Bridge
from tests.support.fake_wifi_board import FakeWifiBoard

KEY = bytes(range(32))
CUR = "0.4.8"


def _blob(n):
    return bytes((i * 7 + 3) & 0xFF for i in range(n))


def _manifest(blob):
    return {"version": "0.4.9", "size": len(blob),
            "sha256": hashlib.sha256(blob).hexdigest()}


def _bridge(blob, link, now=time.monotonic):
    m = _manifest(blob)
    b = Bridge(write_msg=lambda msg: link.write(protocol.encode(msg)),
               fetch_usage=lambda: {}, fetch_manifest=lambda: m,
               fetch_firmware=lambda: blob, fetch_signed_manifest=lambda: m,
               link=link, now=now)
    b.on_message({"t": "hello", "v": protocol.VERSION, "fw": CUR})
    b.on_message({"t": "ota_query", "cur": CUR})
    return b


def _offer(b):
    b.on_message({"t": "ota_flash"})


class FakeLink:
    """Records writes; inbound lines are fed by the test."""
    kind = "wifi"

    def __init__(self):
        self.lines, self.blobs = [], []

    def write(self, raw):
        import json
        self.lines.append(json.loads(raw))

    def write_blob(self, kind, off, chunk):
        self.blobs.append((off, len(chunk)))

    def unacked(self, acked):
        return sum(n for _, n in self.blobs) - acked


@pytest.fixture
def rig(tmp_path):
    made = []

    def make(**kw):
        board = FakeWifiBoard(KEY, **kw).start()
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


@pytest.mark.parametrize("size", [1000003, 16384, 16385, 512, 513])
def test_a_whole_image_arrives_in_order_through_bridge_pump(rig, size):
    blob = _blob(size)
    board, link = rig()
    b = _bridge(blob, link)
    _offer(b)
    assert _drive(b, link, lambda: any(m.get("t") == "ota_end" for m in board.lines))
    assert bytes(board.ota_sink) == blob
    ends = [m for m in board.lines if m.get("t") == "ota_end"]
    assert len(ends) == 1 and ends[0]["size"] == size
    assert b.take_ota_sealed() is True and b.take_ota_sealed() is False
    assert not b.ota_streaming()


def test_no_chunk_before_the_ready_ack():
    link = FakeLink()
    b = _bridge(_blob(5000), link)
    _offer(b)
    assert link.lines[-1]["t"] == "ota_begin"
    for _ in range(5):
        b.pump()
    assert link.blobs == []
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    assert link.blobs


def test_never_more_than_the_window_unacked():
    link = FakeLink()
    b = _bridge(_blob(200000), link)
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    acked = 0
    for _ in range(40):
        b.pump()
        assert link.unacked(acked) <= wifi_ota.WINDOW
        acked += wifi_ota.ACK_EVERY
        sent = sum(n for _, n in link.blobs)
        b.on_message({"t": "ota_ack", "off": min(acked, sent)})
        acked = min(acked, sent)
    assert [o for o, _ in link.blobs] == sorted(o for o, _ in link.blobs)


def test_pings_are_answered_while_the_window_is_full():
    link = FakeLink()
    b = _bridge(_blob(200000), link)
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    b.pump()
    assert link.unacked(0) == wifi_ota.WINDOW
    n = len(link.blobs)
    t0 = time.monotonic()
    b.on_message({"t": "ping"})
    b.pump()
    assert time.monotonic() - t0 < 1 and link.lines[-1]["t"] == "pong"
    assert len(link.blobs) == n         # still full, still not blocking


def test_a_stall_aborts_with_a_fixed_reason():
    clock = [100.0]
    link = FakeLink()
    b = _bridge(_blob(100000), link, now=lambda: clock[0])
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    clock[0] += 31
    b.pump()
    assert link.lines[-1] == {"t": "ota_error", "v": protocol.VERSION,
                              "why": "update stalled"}
    assert not any(m.get("t") == "ota_end" for m in link.lines)
    assert not b.ota_streaming() and not b.take_ota_sealed()


def test_a_stream_cut_midway_never_seals(rig):
    blob = _blob(300000)
    board, link = rig()
    b = _bridge(blob, link)
    _offer(b)
    assert _drive(b, link, lambda: len(board.ota_sink) > 40000)
    board.stop()
    with pytest.raises(OSError):
        _drive(b, link, lambda: False, secs=10)
    assert not any(m.get("t") == "ota_end" for m in board.lines)
    assert not b.take_ota_sealed()
    # The next offer is a new connection and a new stream, from zero.
    board2 = FakeWifiBoard(KEY).start()
    try:
        link2 = FakeLink()
        b2 = _bridge(blob, link2)
        _offer(b2)
        assert link2.lines[-1]["t"] == "ota_begin"
        b2.on_message({"t": "ota_ack", "off": 0})
        b2.pump()
        assert link2.blobs[0][0] == 0
    finally:
        board2.stop()


def test_unconfirmed_board_is_reported_not_retried():
    link = FakeLink()
    b = _bridge(_blob(5000), link)
    _offer(b)
    b.on_message({"t": "ota_ack", "err": "unconfirmed"})
    b.pump()
    assert link.lines[-1]["why"] == "board has not confirmed its last update"
    assert link.blobs == [] and not b.ota_streaming()
    b.pump()
    assert link.blobs == []


def test_progress_is_logged_by_percent_not_bytes(capsys):
    blob = _blob(100000)
    link = FakeLink()
    b = _bridge(blob, link)
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    off = 0
    while b.ota_streaming():
        b.pump()
        sent = sum(n for _, n in link.blobs)
        if sent > off:
            off = min(sent, off + wifi_ota.ACK_EVERY) if sent < len(blob) else sent
            b.on_message({"t": "ota_ack", "off": off})
    err = capsys.readouterr().err
    prog = [ln for ln in err.splitlines() if "%" in ln]
    assert prog and prog[-1].endswith("ota: 100%")
    for ln in prog:     # the manifest size is logged elsewhere; progress is a percent
        assert ln.startswith("[bridge] ota: ") and ln.split(": ")[1][:-1].isdigit()


def test_a_second_offer_while_streaming_is_ignored():
    link = FakeLink()
    b = _bridge(_blob(5000), link)
    _offer(b)
    n = len(link.lines)
    _offer(b)
    assert len(link.lines) == n
