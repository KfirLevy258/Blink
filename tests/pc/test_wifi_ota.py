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
    assert b.ota_sealing()      # no verdict yet: the reboot drops the session
    with pytest.raises(OSError):
        _drive(b, link, lambda: False, secs=5)


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


def test_pings_are_answered_while_the_window_is_full(rig):
    blob = _blob(200000)
    board, link = rig(ack_delay_s=5.0, ping_interval_s=0.3)
    b = _bridge(blob, link)
    _offer(b)
    # Window full: 32 KiB in the board, its first ack still 5 s away.
    assert _drive(b, link, lambda: len(board.ota_sink) >= wifi_ota.WINDOW)
    time.sleep(0.2)
    assert len(board.ota_sink) == wifi_ota.WINDOW
    t0, before = time.monotonic(), board.pongs
    assert _drive(b, link, lambda: board.pongs > before, secs=1.0)
    assert time.monotonic() - t0 < 1.0
    assert len(board.ota_sink) == wifi_ota.WINDOW      # acks were still held


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
                              "why": wifi_ota.STALLED}
    assert not any(m.get("t") == "ota_end" for m in link.lines)
    assert not b.ota_streaming() and not b.ota_sealing()


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
    assert not b.ota_sealing()
    # The failure is reported: the session is gone, so what the daemon can say
    # is that it never sealed, and a new Bridge (new connection) starts at 0.
    board2, link2 = rig()
    b2 = _bridge(blob, link2)
    _offer(b2)
    assert _drive(b2, link2, lambda: len(board2.ota_sink) > 0)
    assert bytes(board2.ota_sink)[:512] == blob[:512]
    assert not any(m.get("t") == "ota_end" for m in board2.lines)


def test_a_failed_stream_is_reported_and_the_next_offer_restarts_from_zero():
    link = FakeLink()
    clock = [0.0]
    b = _bridge(_blob(100000), link, now=lambda: clock[0])
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    first = list(link.blobs)
    clock[0] += 31
    b.pump()
    assert link.lines[-1]["t"] == "ota_error"
    link.blobs.clear()
    b.on_message({"t": "ota_query", "cur": CUR})
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    assert link.blobs[0] == (0, 512) and first[0] == (0, 512)


def test_ota_begin_has_no_size_and_ota_end_goes_through_the_normal_write():
    link = FakeLink()
    b = _bridge(_blob(600), link)
    _offer(b)
    begin = [m for m in link.lines if m.get("t") == "ota_begin"][0]
    assert "size" not in begin
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    b.on_message({"t": "ota_ack", "off": 600})
    b.pump()
    assert link.lines[-1] == {"t": "ota_end", "v": protocol.VERSION, "size": 600}


def test_ota_end_only_on_the_ack_equal_to_the_size():
    link = FakeLink()
    b = _bridge(_blob(200000), link)
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    sent = sum(n for _, n in link.blobs)
    assert sent == wifi_ota.WINDOW
    b.on_message({"t": "ota_ack", "off": sent})          # all of the window, mid-image
    b.pump()
    assert not any(m.get("t") == "ota_end" for m in link.lines)
    assert b.ota_streaming()


def test_an_ack_beyond_what_was_sent_is_ignored():
    link = FakeLink()
    b = _bridge(_blob(200000), link)
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    n = len(link.blobs)
    b.on_message({"t": "ota_ack", "off": 10 * wifi_ota.WINDOW})
    b.pump()
    assert len(link.blobs) == n                          # window did not open
    assert not any(m.get("t") == "ota_end" for m in link.lines)


def test_a_resumed_install_over_wifi_offers_before_it_begins():
    """The board forgets its consented manifest when a session drops (S9.7),
    so a resume sends a fresh ota_avail first: avail, resume, begin."""
    from pc import update
    blob = _blob(2000)
    link = FakeLink()
    m = _manifest(blob)

    class Pending:
        def read(self):
            return m["version"], 0

        def clear(self):
            pass
    b = Bridge(write_msg=lambda msg: link.write(protocol.encode(msg)),
               fetch_usage=lambda: {}, fetch_manifest=lambda: m,
               fetch_firmware=lambda: blob, fetch_signed_manifest=lambda: m,
               link=link, pending=Pending())
    b._board_fw = CUR
    b._resume_pending()
    kinds = [x.get("t") for x in link.lines if x.get("t", "").startswith("ota_")]
    assert kinds == ["ota_avail", "ota_resume", "ota_begin"]


# ---- the daemon's loop (claude_usage_bridge.main) ----------------------

def test_no_exit_while_nests_and_a_stop_waits_for_the_outer_hold():
    import claude_usage_bridge as cub
    cub._TERM["pending"] = False
    with cub.no_exit_while("firmware write"):
        with cub.no_exit_while("app update"):
            pass
        assert cub._TERM["holding"] == "firmware write"
        cub._TERM["pending"] = True
        with cub.no_exit_while("again"):
            pass                                   # inner exit must not exit
        assert cub._TERM["holding"] == "firmware write"
        cub._TERM["pending"] = False


def _main_ast():
    import ast
    import pathlib
    src = pathlib.Path(__file__).resolve().parents[2] / "claude_usage_bridge.py"
    tree = ast.parse(src.read_text("utf-8"))
    return next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")


def test_every_reconnect_handler_releases_the_stream_hold_before_it_can_block():
    import ast
    handlers = [h for t in ast.walk(_main_ast()) if isinstance(t, ast.Try)
                for h in t.handlers
                if any(isinstance(n, ast.Name) and n.id == "choose"
                       for st in h.body for n in ast.walk(st)
                       for n in [getattr(n, "func", n)])
                and any(k in ast.dump(h.type)
                        for k in ("LeaveWifi", "Reflashed", "OSError"))]
    assert len(handlers) == 3
    for h in handlers:
        calls = [n for st in h.body for n in ast.walk(st)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)]
        names = [(n.lineno, n.col_offset, n.func.id) for n in calls]
        rel = [x for x in names if x[2] == "_release"]
        block = [x for x in names if x[2] in ("choose", "sleep")
                 or (x[2] == "sleep")]
        assert rel, ast.dump(h.type)
        assert all(rel[0][:2] < b[:2] for b in block)


def test_the_daily_update_tick_is_skipped_while_streaming():
    import ast
    ifs = [n for n in ast.walk(_main_ast()) if isinstance(n, ast.If)
           and "_self_update_tick" in ast.dump(n)
           and "next_update" in ast.dump(n.test)]
    assert ifs and "ota_streaming" in ast.dump(ifs[0].test)


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
    while not b.ota_sealing():
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


def test_stall_and_reason_texts_fit_the_panel():
    for t in (wifi_ota.STALLED, wifi_ota.REJECTED, wifi_ota.REFUSED,
              wifi_ota.UNCONFIRMED):
        assert len(t) <= 47


def test_a_bad_hash_at_ota_end_is_a_failure_not_a_success(rig):
    blob = _blob(20000)
    board, link = rig(bad_hash=True)
    b = _bridge(blob, link)
    _offer(b)
    assert _drive(b, link, lambda: b.ota_sealing())
    assert _drive(b, link, lambda: not b.ota_streaming())
    assert not b.ota_sealing()          # reconnect must not poll as after an OTA
    assert board.lines[-1] == {"t": "ota_error", "v": protocol.VERSION,
                               "why": wifi_ota.REJECTED}


def test_the_sealing_stream_waits_for_a_verdict_then_stalls_out():
    clock = [0.0]
    link = FakeLink()
    b = _bridge(_blob(600), link, now=lambda: clock[0])
    _offer(b)
    b.on_message({"t": "ota_ack", "off": 0})
    b.pump()
    b.on_message({"t": "ota_ack", "off": 600})
    b.pump()
    assert b.ota_sealing() and b.ota_streaming()
    clock[0] += 31
    b.pump()
    assert not b.ota_sealing() and link.lines[-1]["why"] == wifi_ota.STALLED


def test_a_refused_begin_sends_a_fresh_offer_and_does_not_loop(rig):
    blob = _blob(5000)
    board, link = rig(forget_avail=True)    # a board reset since its offer
    b = _bridge(blob, link)
    _offer(b)
    assert _drive(b, link, lambda: not b.ota_streaming())
    assert _drive(b, link, lambda: sum(m.get("t") == "ota_avail"
                                       for m in board.lines) >= 3)
    _drive(b, link, lambda: False, secs=0.3)
    kinds = [m.get("t") for m in board.lines]
    assert kinds.count("ota_begin") == 1 and kinds.count("ota_avail") == 3    # hello, query, then the fresh one
    assert board.ota_sink == b"" and not b.ota_streaming()
