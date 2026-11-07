"""Pairing through the Bridge: scan, join, forget on the serial thread."""
import gc
import os
import queue
import types
import threading
import time

from pc import private_file, protocol, settings_ops, wifi_pair
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
    b.on_message({"t": "wifi_state", "v": 1, "st": "off", "link": "usb"})
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
    b.on_message({"t": "wifi_state", "v": 1, "st": "joining", "link": "usb"})
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
    for x in th:
        x.join()
    holders = [r for r in gc.get_referrers(pw) if type(r).__name__ != "frame"]
    assert holders == []
    hexpw = pw.encode().hex()
    # A plain loop, not a comprehension: before Python 3.12 (PEP 709) a
    # comprehension that names pw is a nested function, which makes pw a
    # closure cell of this very test, and gc.get_referrers above reported
    # that cell as a holder on 3.9 and 3.11 (Ubuntu, Windows) -- the test
    # holding its own password, not the daemon.
    hits = []
    for m in sent:
        if pw in repr(m) or hexpw in repr(m):
            hits.append(m["t"])
    assert hits == ["wifi_set"]
    assert pw not in repr(box) and pw not in repr(verified)


def test_ops_refused_until_the_board_says_usb(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    b._wifi_link = None
    t, box = call(ops.forget, 2.0)
    pump_until(b, lambda: box)
    assert isinstance(box["e"], settings_ops.BoardOpError) and not sent


def test_forget_keeps_the_file_until_the_board_answers(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    t, box = call(ops.forget, 5.0)
    pump_until(b, lambda: sent)
    assert wifi_pair.load(home) is not None
    b.on_message(up("whatever"))                  # stale, not the answer
    assert wifi_pair.load(home) is not None and t.is_alive()
    b.on_message({"t": "wifi_state", "v": 1, "st": "off", "link": "usb"})
    t.join(3)
    assert wifi_pair.load(home) is None


def test_a_not_private_pairing_dir_finishes_the_op_not_the_daemon(
        tmp_path, monkeypatch):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)

    def boom(*a):
        raise private_file.NotPrivate("x")
    monkeypatch.setattr(wifi_pair, "save", boom)
    b.on_message(up(kid_of(sent)))
    t.join(3)
    assert isinstance(box["e"], settings_ops.BoardOpError) and not verified


def test_a_late_up_still_saves_the_key(tmp_path, capsys):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.join, b"Net", PW, 0.2)
    pump_until(b, lambda: any(m["t"] == "wifi_set" for m in sent))
    t.join(3)
    assert "e" in box                              # the page gave up
    b.pump()
    b.on_message(up(kid_of(sent)))
    assert wifi_pair.load(home)["kid"] == kid_of(sent)
    assert "another computer" not in capsys.readouterr().err


def test_a_late_up_after_the_window_is_not_saved(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.join, b"Net", PW, 0.1)
    pump_until(b, lambda: any(m["t"] == "wifi_set" for m in sent))
    t.join(3)
    b._join["until"] = 0
    b.on_message(up(kid_of(sent)))
    assert wifi_pair.load(home) is None


def test_the_mismatch_sentence_needs_usb_evidence(tmp_path, capsys):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    b.on_message(up("deadbeef", link="wifi"))
    assert wifi_pair.load(home) is not None
    assert "another computer" not in capsys.readouterr().err


def test_another_board_on_the_cable_does_not_delete_this_pairing(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "other-board", b"k" * 32, "10.0.0.5", 49731)
    b.on_message({"t": "wifi_state", "v": 1, "st": "off", "link": "usb"})
    assert wifi_pair.load(home) is not None


def test_join_is_refused_without_a_wire_board_id(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    b._board_id = None
    t, box = call(ops.join, b"Net", PW, 2.0)
    pump_until(b, lambda: box)
    assert isinstance(box["e"], settings_ops.BoardOpError) and not sent


def test_a_stale_fail_before_joining_does_not_end_the_join(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)
    b.on_message({"t": "wifi_state", "v": 1, "st": "fail", "link": "usb"})
    t.join(0.3)
    assert t.is_alive()
    b.on_message({"t": "wifi_state", "v": 1, "st": "joining", "link": "usb"})
    b.on_message({"t": "wifi_state", "v": 1, "st": "fail", "link": "usb"})
    t.join(3)
    assert box["r"]["steps"] == ["sent"]


def test_an_up_with_a_bad_address_saves_nothing(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops)
    m = up(kid_of(sent))
    m["port"] = 0
    b.on_message(m)
    t.join(3)
    assert "e" in box and wifi_pair.load(home) is None and not verified
def test_scan_collects_nets_until_the_last(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.scan, 5.0)
    pump_until(b, lambda: sent)
    for i in range(2):
        b.on_message({"t": "wifi_net", "v": 1, "i": i, "n": 2, "s": "41",
                      "r": -40, "sec": "wpa2"})
    t.join(3)
    assert len(box["r"]) == 2 and sent[0]["t"] == "wifi_scan"


def test_an_empty_scan_ends_on_the_n_zero_marker(tmp_path):
    # The board's end marker for no networks: i=0, n=0, and no s/r/sec.
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.scan, 5.0)
    pump_until(b, lambda: sent)
    b.on_message({"t": "wifi_net", "v": 1, "i": 0, "n": 0})
    t.join(3)
    assert box["r"] == []


def test_forget_removes_the_file_and_returns_the_state(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    t, box = call(ops.forget, 5.0)
    pump_until(b, lambda: sent)
    assert sent[0]["t"] == "wifi_forget"
    b.on_message({"t": "wifi_state", "v": 1, "st": "off", "link": "usb"})
    t.join(3)
    assert wifi_pair.load(home) is None
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


# --- the logo and the preferences, from the page (spec S15.2, S15.3) ---


def test_pref_set_is_one_line_under_512():
    msg = protocol.pref_set(main_src="claude", focus=1, view="burn",
                            bright=100)
    raw, _ = protocol.encode_checked(msg)
    assert raw.endswith(b"\n") and raw.count(b"\n") == 1
    assert len(raw) <= protocol.MAX_LINE_BYTES


def test_logo_ops_refused_without_the_cap(tmp_path):
    b, ops, sent, *_ = make(tmp_path, caps=("wifi",))
    for fn, args in ((ops.logo, (b"x" * 64,)), (ops.logo_remove, ()),
                     (ops.pref_set, ())):
        t, box = call(fn, *args)
        pump_until(b, lambda: not t.is_alive())
        assert isinstance(box.get("e"), settings_ops.BoardOpError), fn
    assert sent == []


def test_pref_set_answers_with_the_boards_pref(tmp_path):
    b, ops, sent, *_ = make(tmp_path, caps=("pref",))
    t, box = call(lambda: ops.pref_set(focus=1, bright=20))
    pump_until(b, lambda: any(m["t"] == "pref_set" for m in sent))
    assert [m for m in sent if m["t"] == "pref_set"] == [
        protocol.pref_set(focus=1, bright=20)]
    b.on_message({"t": "pref", "v": 1, "provider": "claude", "focus": 1,
                  "bright": 20, "view": 0, "caps": ["pref"]})
    t.join(3)
    assert box["r"] == {"provider": "claude", "focus": 1, "bright": 20,
                        "view": 0}


def test_logo_op_streams_over_usb_and_ends_on_logo_state(tmp_path):
    b, ops, sent, *_ = make(tmp_path, caps=("logo",))
    blob = bytes(range(256)) * 3
    t, box = call(ops.logo, blob)
    pump_until(b, lambda: any(m["t"] == "logo_begin" for m in sent))
    b.on_message({"t": "logo_ack", "v": 1, "off": 0})
    for _ in range(10):
        b.pump()
        for m in [m for m in sent if m["t"] == "logo_chunk"]:
            b.on_message({"t": "logo_ack", "v": 1,
                          "off": m["off"] + 256})
        sent[:] = [m for m in sent if m["t"] != "logo_chunk"]
    pump_until(b, lambda: any(m["t"] == "logo_end" for m in sent))
    b.on_message({"t": "logo_state", "v": 1, "present": 1, "bytes": 768,
                  "frames": 1})
    pump_until(b, lambda: not t.is_alive())
    assert box["r"] == {"present": 1, "bytes": 768, "frames": 1}


def test_logo_remove_ends_on_logo_state(tmp_path):
    b, ops, sent, *_ = make(tmp_path, caps=("logo",))
    t, box = call(ops.logo_remove)
    pump_until(b, lambda: any(m["t"] == "logo_remove" for m in sent))
    b.on_message({"t": "logo_state", "v": 1, "present": 0, "bytes": 0,
                  "frames": 0})
    t.join(3)
    assert box["r"]["present"] == 0


def test_a_refused_logo_is_a_sentence(tmp_path):
    b, ops, sent, *_ = make(tmp_path, caps=("logo",))
    t, box = call(ops.logo, b"x" * 64)
    pump_until(b, lambda: any(m["t"] == "logo_begin" for m in sent))
    b.on_message({"t": "logo_ack", "v": 1, "off": 0, "err": "refused"})
    pump_until(b, lambda: not t.is_alive())
    assert str(box["e"]) == "The board refused the logo"


def test_a_refused_pref_set_answers_the_page_at_once(tmp_path):
    b, ops, sent, *_ = make(tmp_path, caps=("pref",))
    t, box = call(lambda: ops.pref_set(bright=20))
    pump_until(b, lambda: any(m["t"] == "pref_set" for m in sent))
    b.on_message({"t": "pref", "v": 1, "provider": "claude", "focus": 0,
                  "bright": 100, "view": 0, "ok": 0, "caps": ["pref"]})
    t.join(1)
    assert not t.is_alive()
    assert isinstance(box["e"], settings_ops.BoardOpError)


def test_the_boards_pref_is_published_for_the_pages_first_paint(tmp_path):
    b, ops, *_ = make(tmp_path, caps=("pref",))
    assert "pref" not in ops.device()
    b.on_message({"t": "pref", "v": 1, "provider": "codex", "focus": 1,
                  "bright": 60, "view": 0, "board_id": "abcd", "ok": 1})
    assert ops.device()["pref"] == {"provider": "codex", "focus": 1,
                                    "bright": 60, "view": 0}
    # A refused pref_set's line is not a change of state.
    b.on_message({"t": "pref", "v": 1, "provider": "claude", "focus": 0,
                  "bright": 20, "view": 0, "ok": 0})
    assert ops.device()["pref"]["bright"] == 60


def test_a_logo_is_refused_while_an_ota_is_streaming(tmp_path):
    b, ops, sent, *_ = make(tmp_path, caps=("logo",))
    b._ota_stream = types.SimpleNamespace(
        end_sent=False, went_quiet=False, pump=lambda *a: None, done=lambda: None)
    t, box = call(ops.logo, bytes(range(256)) * 3)
    pump_until(b, lambda: not t.is_alive())
    assert isinstance(box.get("e"), settings_ops.BoardOpError)
    assert not any(m["t"] == "logo_begin" for m in sent)
