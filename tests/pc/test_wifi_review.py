"""PR #9 review fixes in the daemon: ops that must not hang, a forget that
fails loudly, and the pairing-removal rule's other cases."""
import queue
import threading
import time

import pytest

from pc import protocol, settings_ops, wifi_pair
from pc.bridge import Bridge
from tests.pc.test_wifi_bridge import (
    PW, call, kid_of, make, pump_until, start_join, up)

OFF = {"t": "wifi_state", "v": 1, "st": "off", "link": "usb"}


# --- 1. forget: the board's "fail" ends it, with the reason ---

def test_forget_fail_tells_the_page_and_keeps_the_pairing(tmp_path, capsys):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    t, box = call(ops.forget, 5.0)
    pump_until(b, lambda: sent)
    t0 = time.monotonic()
    b.on_message({"t": "wifi_state", "v": 1, "st": "fail", "link": "usb",
                  "why": "Could not forget WiFi. Try again."})
    t.join(3)
    assert time.monotonic() - t0 < 2
    assert isinstance(box["e"], settings_ops.BoardOpError)
    assert "could not erase" in str(box["e"])
    assert wifi_pair.load(home) is not None
    err = capsys.readouterr().err
    assert "[wifi] the board could not erase its WiFi settings" in err
    assert "10.0.0.5" not in err


def test_an_unrelated_fail_does_not_end_a_pending_forget(tmp_path):
    # M1: a stored rejoin failing ("not found") can already be on the wire
    # when wifi_forget is written; the board then erases and sends "off".
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    t, box = call(ops.forget, 5.0)
    pump_until(b, lambda: sent)
    b.on_message({"t": "wifi_state", "v": 1, "st": "fail", "link": "usb",
                  "why": "Network not found"})
    t.join(0.3)
    assert t.is_alive()
    b.on_message(OFF)
    t.join(3)
    assert "e" not in box and wifi_pair.load(home) is None


# --- 2 and 3. a lost link fails ops now and clears the link state ---

def test_link_down_fails_the_op_in_flight_at_once(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path, caps=("wifi", "logo"))
    t, box = call(ops.logo, b"x" * 10, 300.0)
    pump_until(b, lambda: b._op is not None)
    t0 = time.monotonic()
    ops.link_down()
    t.join(3)
    assert not t.is_alive() and time.monotonic() - t0 < 2
    assert "dropped" in str(box["e"])


def test_link_down_fails_a_queued_op_and_the_next_bridge_skips_it(tmp_path):
    ops = settings_ops.BoardOps(queue.SimpleQueue())
    t, box = call(ops.forget, 300.0)
    deadline = time.monotonic() + 5
    while ops.queue.empty() and time.monotonic() < deadline:
        time.sleep(0.005)               # until the op is really queued
    ops.link_down()
    t.join(3)
    assert isinstance(box["e"], settings_ops.BoardOpError)
    assert ops.queue.get_nowait().abandoned    # a later Bridge drops it


def test_an_op_with_no_link_fails_without_waiting():
    ops = settings_ops.BoardOps(queue.SimpleQueue())
    ops.link_down()
    t0 = time.monotonic()
    with pytest.raises(settings_ops.BoardOpError) as e:
        ops.scan(300.0)
    assert time.monotonic() - t0 < 1 and ops.queue.empty()
    assert "not connected" in str(e.value)


def test_a_new_bridge_means_a_link_again(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    ops.link_down()
    Bridge(write_msg=lambda m: None, fetch_usage=lambda: None,
           board_ops=ops, home=home, fetch_manifest=lambda: None,
           fetch_signed_manifest=lambda: None)
    t, box = call(ops.scan, 0.2)
    t.join(3)
    assert "did not answer" in str(box["e"])    # it waited: the link is up


def test_link_down_clears_what_the_page_reports(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    b.on_message({"t": "wifi_state", "v": 1, "st": "up", "link": "wifi",
                  "s": "x"})
    ops.note_device(fw="1.5.0")
    assert ops.state()["link"] == "wifi"
    ops.link_down()
    assert ops.state() == {"usb": False} and ops.device() == {"link": None}


# --- pairing removal: the two cases the suite lacked ---

def test_same_board_off_with_no_kid_removes_the_pairing(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    b.on_message(OFF)
    assert wifi_pair.load(home) is None


@pytest.mark.parametrize("st", ["fail", "joining", "idle"])
def test_same_board_other_state_with_no_kid_keeps_the_pairing(tmp_path, st):
    b, ops, sent, home, verified, th = make(tmp_path)
    wifi_pair.save(home, "abcd", b"k" * 32, "10.0.0.5", 49731)
    b.on_message({"t": "wifi_state", "v": 1, "st": st, "link": "usb"})
    assert wifi_pair.load(home) is not None


# --- 6. save and verify failures reach bridge.log ---

def test_a_late_save_failure_is_logged(tmp_path, monkeypatch, capsys):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = start_join(b, sent, ops, timeout=0.2)
    t.join(3)                                    # the page gave up
    b.pump()

    def boom(*a):
        raise OSError("disk")
    monkeypatch.setattr(wifi_pair, "save", boom)
    b.on_message(up(kid_of(sent)))
    err = capsys.readouterr().err
    assert "[wifi] could not save the pairing" in err and "disk" not in err


def test_a_failed_verify_is_logged_by_class_only(tmp_path, capsys):
    def bad():
        raise OSError("10.0.0.5 refused")
    b, ops, sent, home, verified, th = make(tmp_path, verify=bad)
    t, box = start_join(b, sent, ops)
    b.on_message(up(kid_of(sent)))
    t.join(3)
    for x in th:
        x.join(3)
    err = capsys.readouterr().err
    assert "did not answer over WiFi (OSError)" in err
    assert "10.0.0.5" not in err
    assert "could not reach it" in box["r"]["problem"]


# --- firmware contract: a refused scan, a failed logo erase ---

def test_a_refused_scan_ends_at_once_with_its_sentence(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path)
    t, box = call(ops.scan, 5.0)
    pump_until(b, lambda: sent)
    t0 = time.monotonic()
    b.on_message({"t": "wifi_state", "v": 1, "st": "fail", "link": "usb",
                  "why": "Could not scan. Try again."})
    t.join(3)
    assert time.monotonic() - t0 < 2
    assert str(box["e"]) == "Could not scan. Try again."


def test_logo_state_err_is_kept_for_the_page(tmp_path):
    b, ops, sent, home, verified, th = make(tmp_path, caps=("wifi", "logo"))
    t, box = call(ops.logo_remove, 5.0)
    pump_until(b, lambda: sent)
    b.on_message({"t": "logo_state", "v": 1, "present": 0, "err": 1})
    t.join(3)
    assert box["r"]["err"] == 1


def test_link_down_leaves_an_op_that_already_finished(tmp_path):
    # M2: _live holds an op after done is set until the page thread wakes.
    ops = settings_ops.BoardOps(queue.SimpleQueue())
    op = settings_ops._Op("forget")
    ops._live.add(op)
    op.result = {"ok": 1}
    op.done.set()                       # the Bridge's _finish ran
    ops.link_down()
    assert op.error is None and not op.abandoned and op.result == {"ok": 1}
