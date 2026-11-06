"""Pairing through the Bridge: scan, join, forget on the serial thread."""
import gc
import os
import queue
import threading
import time

from pc import protocol, settings_ops, wifi_pair
from pc.bridge import Bridge

_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)
PW = "hunter2-pass"


def make(tmp_path, caps=("wifi",), verify=None, run_async=None):
    home = str(tmp_path / ".blink")
    os.makedirs(home)
    sent, verified, threads = [], [], []

    def v(ip, port, key):
        verified.append((ip, port, key, threading.current_thread()))
        if verify:
            verify()

    def spawn(fn):
        t = threading.Thread(target=fn)
        threads.append(t)
        t.start()
    ops = settings_ops.BoardOps(queue.SimpleQueue())
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None,
               board_ops=ops, home=home, verify=v,
               run_async=run_async or spawn, **_NO_FEED)
    b.on_message({"t": "hello", "v": 1, "fw": "1.4.1", "board_id": "abcd",
                  "caps": list(caps)})
    sent.clear()
    return b, ops, sent, home, verified, threads


def call(fn, *a):
    """Run a page-thread call; returns (thread, box)."""
    box = {}

    def run():
        try:
            box["r"] = fn(*a)
        except Exception as e:
            box["e"] = e
    t = threading.Thread(target=run)
    t.start()
    return t, box


def pump_until(b, cond, secs=3.0):
    end = time.monotonic() + secs
    while not cond() and time.monotonic() < end:
        b.pump()
        time.sleep(0.005)


def start_join(b, sent, ops, timeout=5.0):
    t, box = call(ops.join, b"Net", PW, timeout)
    pump_until(b, lambda: any(m["t"] == "wifi_set" for m in sent))
    return t, box


def kid_of(sent):
    return wifi_pair.kid(bytes.fromhex(
        next(m for m in sent if m["t"] == "wifi_set")["k"]))


def up(kid, link="usb"):
    return {"t": "wifi_state", "v": 1, "st": "up", "link": link,
            "ip": "10.0.0.5", "port": 49731, "kid": kid}


def test_join_sends_one_wifi_set_and_saves_on_up(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)
    assert [m["t"] for m in sent] == ["wifi_set"]
    assert wifi_pair.load(home) is None
    b.on_message(up(kid_of(sent)))
    t.join(3)
    doc = wifi_pair.load(home)
    assert doc["ip"] == "10.0.0.5" and doc["kid"] == kid_of(sent)
    assert doc["board_id"] == "abcd"
    assert [m["t"] for m in sent].count("wifi_set") == 1


def test_join_fail_saves_nothing(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)
    b.on_message({"t": "wifi_state", "v": 1, "st": "fail", "link": "usb",
                  "why": "bad password"})
    t.join(3)
    assert wifi_pair.load(home) is None and not verified
    assert box["r"]["steps"] == ["sent"]


def test_join_runs_one_verify_and_reports_real_steps(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)
    b.on_message(up(kid_of(sent)))
    t.join(3)
    assert len(verified) == 1 and verified[0][:2] == ("10.0.0.5", 49731)
    assert box["r"]["steps"] == ["sent", "joined", "proved", "encrypted"]


def test_join_verify_failure_keeps_two_steps_and_says_so(tmp_path):
    def boom():
        raise OSError("x")
    b, ops, sent, home, verified, th = make(tmp_path, verify=boom)
    t, box = start_join(b, sent, ops)
    b.on_message(up(kid_of(sent)))
    t.join(3)
    assert box["r"]["steps"] == ["sent", "joined"]
    assert box["r"]["problem"]


def test_join_never_calls_verify_from_the_serial_thread(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)
    b.on_message(up(kid_of(sent)))
    t.join(3)
    assert verified[0][3] is not threading.current_thread()


def test_kid_mismatch_removes_the_pairing_and_says_so_once(tmp_path, capsys):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    other = up("deadbeef")
    b.on_message(other)
    assert wifi_pair.load(home) is None
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    b.on_message(other)
    err = capsys.readouterr().err
    assert err.count("set up from another computer") == 1


def test_ops_refused_when_board_lacks_the_wifi_cap(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path, caps=())
    t, box = call(ops.scan, 2.0)
    pump_until(b, lambda: box)
    assert isinstance(box["e"], settings_ops.BoardOpError) and not sent


def test_ops_refused_over_wifi(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    b.on_message(up("deadbeef", link="wifi"))
    t, box = call(ops.forget, 2.0)
    pump_until(b, lambda: box)
    assert isinstance(box["e"], settings_ops.BoardOpError) and not sent


def test_the_password_is_not_kept(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    pw = "".join(["uniq", "ue-pass", "word-1"])
    t, box = call(ops.join, b"Net", pw, 5.0)
    pump_until(b, lambda: any(m["t"] == "wifi_set" for m in sent))
    b.on_message(up(kid_of(sent)))
    t.join(3)
    t.join()
    holders = [r for r in gc.get_referrers(pw)
               if not isinstance(r, type(gc.get_referrers.__self__))
               and type(r).__name__ not in ("frame", "function")]
    assert holders == [] or all(type(r).__name__ == "cell" for r in holders)
    assert pw not in repr([m for m in sent if m["t"] != "wifi_set"])


def test_scan_collects_nets_until_the_last(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.scan, 5.0)
    pump_until(b, lambda: sent)
    for i in range(2):
        b.on_message({"t": "wifi_net", "v": 1, "i": i, "n": 2, "s": "41",
                      "r": -40, "sec": "wpa2"})
    t.join(3)
    assert len(box["r"]) == 2 and sent[0]["t"] == "wifi_scan"


def test_forget_removes_the_file_and_returns_the_state(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    t, box = call(ops.forget, 5.0)
    pump_until(b, lambda: sent)
    assert wifi_pair.load(home) is None and sent[0]["t"] == "wifi_forget"
    b.on_message({"t": "wifi_state", "v": 1, "st": "off", "link": "usb"})
    t.join(3)
    assert box["r"]["st"] == "off" and box["r"]["usb"] is True


def test_ops_never_block_the_serial_loop(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.scan, 1.0)
    time.sleep(0.05)
    t0 = time.monotonic()
    b.pump()
    b.pump()
    assert time.monotonic() - t0 < 0.05
    t.join(3)
