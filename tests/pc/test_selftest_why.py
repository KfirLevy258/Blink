"""A board whose cipher self-test failed says so, to the scan and to the join.

net_link_refuse_selftest() makes every later scan and join fail for good.
The board used to answer those with "Could not scan. Try again." and
"Network not found", so an owner retried and retyped forever while the real
reason showed only on the board's own Connection screen (PR 9 round 2,
finding 3). The firmware now sends "WiFi self-test failed"; the daemon must
finish a pending scan on it at once, like the ordinary scan failure, and a
join's own `fail` carries it to the page.
"""
import time

from tests.pc.test_wifi_bridge import call, make, pump_until, start_join

WHY = "WiFi self-test failed"


def _fail(why):
    return {"t": "wifi_state", "v": 1, "st": "fail", "link": "usb", "why": why}


def test_a_scan_refused_by_a_failed_selftest_ends_at_once_with_that_reason(
        tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.scan, 5.0)
    pump_until(b, lambda: sent)
    t0 = time.monotonic()
    b.on_message(_fail(WHY))
    t.join(3)
    assert time.monotonic() - t0 < 2
    assert str(box["e"]) == WHY


def test_a_join_refused_by_a_failed_selftest_shows_that_reason(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)
    b.on_message({"t": "wifi_state", "v": 1, "st": "joining", "link": "usb"})
    b.on_message(_fail(WHY))
    t.join(3)
    assert box["r"]["state"]["why"] == WHY
    assert "Network not found" not in str(box["r"])


# --- the firmware side, pinned as source: proto.c is not host-built ---

def _src(name):
    from pathlib import Path
    return (Path(__file__).resolve().parents[2] / "firmware" / "src"
            / name).read_text(encoding="utf-8")


def test_the_firmware_sentences_are_the_ones_the_daemon_matches():
    from pc.bridge import SCAN_FAIL_WHYS
    nl = _src("net_link.c")
    for why in SCAN_FAIL_WHYS:
        assert f'return "{why}";' in nl


def test_proto_reports_the_selftest_reason_for_a_refused_join_and_scan():
    import re
    proto = _src("proto.c")
    # The helper picks the self-test text only when net_link says so.
    assert re.search(r"net_link_why\(\) == NL_WHY_SELFTEST \?\s*"
                     r"NL_WHY_SELFTEST : otherwise", proto)
    # Both places that used to hard-code the ordinary reason go through it.
    assert re.search(r'wifi_send_state\("fail", wifi_refused_why\('
                     r"NL_WHY_NOT_FOUND\), false\);\s*\}\s*out:", proto)
    assert re.search(r'wifi_send_state\("fail", wifi_refused_why\('
                     r"NL_WHY_SCAN\),", proto)
