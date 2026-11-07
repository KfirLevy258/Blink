"""Pins on claude_usage_bridge.main's wiring that only a live board would
otherwise exercise (PR 9 review)."""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "claude_usage_bridge.py"),
           encoding="utf-8").read()


def test_every_road_to_choose_drops_the_link_state():
    body = re.search(r"def choose\(after_ota=False\):\n(.*?)return wait_for_link",
                     SRC, re.S).group(1)
    assert "board_ops.link_down()" in body


def test_a_logo_upload_does_not_flood_the_log():
    assert re.search(r'kind not in \("ota_data", "pong", "edit_state",'
                     r' "logo_chunk"\)', SRC)
    assert re.search(r'elif msg\.get\("t"\) == "logo_ack" and "err" not in msg',
                     SRC)


def test_an_ota_whose_session_ended_leaves_an_outcome_line():
    for handler in ("except LeaveWifi:", "except (serial.SerialException, OSError) as e:"):
        i = SRC.index(handler)
        assert "_ota_ended(" in SRC[i:i + 400], handler
    assert "[bridge] ota: FAILED: connection lost" in SRC
    assert "[bridge] ota: sent; waiting for the board to restart" in SRC


def test_ota_ended_says_cut_or_sealed_and_nothing_with_no_stream(capsys):
    import claude_usage_bridge as b
    b._ota_ended(None, False)
    b._ota_ended(object(), False)
    b._ota_ended(object(), True)
    out = capsys.readouterr().err.splitlines()
    assert out == ["[bridge] ota: FAILED: connection lost",
                   "[bridge] ota: sent; waiting for the board to restart"]
