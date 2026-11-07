"""The daemon chooses a link: USB or WiFi (spec 7.2, Task 3.3).

The unit tests drive wait_for_link with an injected clock whose sleep advances
it, so a 135 s backoff schedule runs in milliseconds. The orchestration tests
run main() in a child Python (the daemon installs a SIGTERM handler, which
only the main thread of a process may do) against FakeWifiBoard on loopback.
"""
import ast
import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

import claude_usage_bridge as bridge
from pc import wifi_link, wifi_pair
from tests.support.fake_wifi_board import FakeWifiBoard

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
KEY = bytes(range(32))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t

    def sleep(self, d):
        self.t += d
        if self.t > 400:
            raise RuntimeError("the test clock ran away")


def choose(clock, **kw):
    kw.setdefault("home", "h")
    kw.setdefault("exists", lambda p: True)
    kw.setdefault("topology", lambda: ())
    kw.setdefault("autodetect", lambda: None)
    return bridge.wait_for_link(clock=clock, sleep=clock.sleep, **kw)


def failing_connect(clock, times, outcome="not found"):
    def connect(home):
        times.append(clock.t)
        return None, outcome
    return connect


class Done(Exception):
    pass


def test_serial_is_tried_first():
    c = Clock()

    def never(home):
        raise AssertionError("WiFi tried while a port answered")
    assert choose(c, autodetect=lambda: "/dev/a", connect=never) == ("usb", "/dev/a")


def test_wifi_when_serial_has_nothing_on_the_same_poll():
    c = Clock()
    got = choose(c, connect=lambda h: ("LINK", "connected"))
    assert got == ("wifi", "LINK") and c.t == 0


def test_a_charger_board_is_reached_without_any_serial_port(tmp_path):
    board = FakeWifiBoard(KEY).start()
    try:
        wifi_pair.save(str(tmp_path), "board", KEY, "127.0.0.1", board.address[1])
        kind, link = bridge.wait_for_link(
            home=str(tmp_path), autodetect=lambda: [], topology=lambda: (),
            poll_s=0.01)
        assert kind == "wifi" and link.kind == "wifi"
        link.close()
    finally:
        board.stop()


def test_wifi_backoff_5_to_60_while_serial_keeps_polling():
    c, times, polls = Clock(), [], []
    n = {"i": 0}

    def connect(home):
        times.append(c.t)
        n["i"] += 1
        return (("L", "connected") if n["i"] == 6 else (None, "not found"))

    def autodetect():
        polls.append(c.t)
        return None
    choose(c, connect=connect, autodetect=autodetect)
    assert times == [0, 5, 15, 35, 75, 135]
    assert all(b - a <= 3 for a, b in zip(polls, polls[1:]))


def test_force_wifi_skips_serial():
    c = Clock()

    def boom():
        raise AssertionError("serial looked at under BLINK_FORCE_WIFI")
    got = choose(c, autodetect=boom, force_wifi=True,
                 topology=boom, connect=lambda h: ("L", "connected"))
    assert got == ("wifi", "L")


def test_after_ota_polls_every_5s_without_growth():
    c, times = Clock(), []
    choose(c, after_ota=True,
           connect=lambda h: (times.append(c.t) or (("L", "connected") if len(times) == 8
                                                    else (None, "not found"))))
    assert times == [0, 5, 10, 15, 20, 25, 30, 35]


def _busy_port_run(explicit):
    c, opens, conns = Clock(), [], []
    ch = bridge.LinkChooser()
    ports = "/dev/stranger"
    kw = dict(chooser=ch, connect=failing_connect(c, conns))
    if explicit:
        kw["explicit"] = ports
    else:
        kw["autodetect"] = lambda: ports
    while c.t <= 80:
        try:
            kind, target = choose(c, **kw)
        except RuntimeError:
            break
        if kind != "usb":
            break
        opens.append(c.t)
        ch.port_failed(target, c.t)     # what main does when the open raises
        if c.t > 80:
            break
        if len(opens) == 5:
            break
    return opens, conns


def test_a_persistently_busy_port_does_not_starve_wifi():
    opens, conns = _busy_port_run(False)
    assert opens == [0, 5, 15, 35, 75]
    assert conns[0] <= 10


def test_an_explicit_port_that_never_answers_still_yields_to_wifi():
    opens, conns = _busy_port_run(True)
    assert opens == [0, 5, 15, 35, 75]
    assert conns[0] <= 10


def test_failed_candidate_record_clears_on_topology_change():
    c, topo = Clock(), [("a",)]
    ch = bridge.LinkChooser()
    kw = dict(chooser=ch, autodetect=lambda: "/dev/p", topology=lambda: topo[0],
              connect=lambda h: (None, "not found"))
    assert choose(c, **kw) == ("usb", "/dev/p")
    ch.port_failed("/dev/p", c.t)
    topo[0] = ("a", "b")                    # something was plugged in
    assert choose(c, **kw) == ("usb", "/dev/p") and c.t == 0


def test_usb_appearing_closes_the_wifi_link():
    ch, topo, cands = bridge.LinkChooser(), [("a",)], [[]]
    ch.usb_topo = topo[0]
    ask = lambda now: ch.usb_arrived(now, lambda: topo[0], lambda: cands[0])
    assert not ask(1)                       # nothing changed
    topo[0] = ("a", "b")
    assert not ask(3)                       # changed, but no candidate port
    topo[0] = ("a", "b", "/dev/usb")
    cands[0] = ["/dev/usb"]
    assert ask(5)
    assert not ask(7)                       # said once per change


def test_a_dropped_session_reconnects_at_once():
    c, times = Clock(), []
    ch = bridge.LinkChooser()
    ch.wifi_since = 0.0
    c.t = 100.0
    ch.wifi_dropped(c.t)
    n = {"i": 0}

    def connect(h):
        times.append(c.t)
        n["i"] += 1
        return ("L", "connected") if n["i"] == 5 else (None, "not found")
    choose(c, chooser=ch, connect=connect)
    assert [t - 100 for t in times] == [0, 5, 15, 35, 75]


def test_a_session_that_died_at_once_is_not_redialled_at_once():
    ch = bridge.LinkChooser()
    ch.wifi_since = 99.0
    ch.wifi_dropped(100.0)
    assert ch.wifi_due == 105.0


def test_send_still_writes_only_raw():
    tree = ast.parse(open(os.path.join(ROOT, "claude_usage_bridge.py")).read())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")
    send = next(n for n in ast.walk(main) if isinstance(n, ast.FunctionDef) and n.name == "send")
    writes = [n for n in ast.walk(send) if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute) and n.func.attr == "write"]
    assert len(writes) == 1
    assert [ast.unparse(a) for a in writes[0].args] == ["raw"]


def test_no_serial_only_wait_survives_in_main():
    tree = ast.parse(open(os.path.join(ROOT, "claude_usage_bridge.py")).read())
    main = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "main")

    def names(node):
        return {n.func.id for n in ast.walk(node)
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "wait_for_port" not in names(main)
    for w in (n for n in ast.walk(main) if isinstance(n, ast.While)):
        got = names(w)
        assert not ("next_port" in got and "choose" not in got
                    and "wait_for_link" not in got)


def test_a_wifi_link_has_no_dtr_or_rts():
    link = wifi_link.WifiLink(None, None)
    for name in ("dtr", "rts"):
        with pytest.raises(AttributeError):
            setattr(link, name, False)
    assert not hasattr(link, "reset_input_buffer")


def _refuse(addr, timeout=None):
    raise OSError


def test_a_standing_failure_is_said_once(tmp_path, capsys):
    home = str(tmp_path)
    wifi_pair.save(home, "board", KEY, "127.0.0.1", 1)
    for _ in range(3):
        wifi_link.connect(home, discover=lambda k: None,
                          create_connection=_refuse)
    err = capsys.readouterr().err
    assert err.count("board not found") == 1 and err.count("connecting") == 1
    wifi_link.reset_standing()          # what a USB connect does
    wifi_link.connect(home, discover=lambda k: None, create_connection=_refuse)
    assert capsys.readouterr().err.count("board not found") == 1


def test_a_wifi_link_refuses_a_flash_before_anything_is_downloaded():
    from pc.bridge import Bridge
    from pc.version import PROTO_VERSION
    sent, fetched = [], []
    manifest = {"version": "0.4.9", "size": 5,
                "sha256": "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"}
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: {},
               fetch_manifest=lambda: manifest,
               fetch_firmware=lambda: fetched.append(1) or b"hello",
               flash_image=lambda blob, v: fetched.append("flash"),
               fetch_signed_manifest=lambda: manifest,
               flash_refusal="plug the board in to update")
    b.on_message({"t": "hello", "v": PROTO_VERSION, "fw": "0.4.8"})
    b.on_message({"t": "ota_query", "cur": "0.4.8"})
    sent.clear()
    b.on_message({"t": "ota_flash"})
    assert [m["t"] for m in sent] == ["ota_error"]
    assert "plug the board in" in json.dumps(sent[0]) and fetched == []


def test_status_none_depends_on_a_pairing(tmp_path):
    from pc import link_status as ls
    ls.write(str(tmp_path), "none", None, now=lambda: 1000.0)
    rec = ls.read(str(tmp_path), now=lambda: 1000.0)
    assert ls.status_line(rec, True) == "Link        WiFi set up, board not found"
    assert ls.status_line(rec, False) == "Link        no board connected"


# ---- main() in a child process ---------------------------------------------

CHILD = """
import json, os, sys, functools
import serial
import claude_usage_bridge as b
from pc import wifi_link

marker = sys.argv[1]
mode = sys.argv[2]

def note(what):
    with open(marker, "a") as f:
        f.write(what + "\\n")

# A failed connect must never broadcast on the developer's LAN.
_orig = b.wait_for_link
b.wait_for_link = lambda *a, **k: _orig(
    *a, connect=functools.partial(wifi_link.connect, discover=lambda key: None), **k)

PORT = "/dev/stranger"
_exists = os.path.exists
b.os.path.exists = lambda p: True if p == PORT else _exists(p)

class Silent:
    in_waiting = 0
    dtr = rts = None
    def __init__(self, *a, **k): note("open")
    def read(self, n=1): return b""
    def write(self, raw): return len(raw)
    def reset_input_buffer(self): pass
    def close(self): pass

def busy(*a, **k):
    note("open")
    raise serial.SerialException("busy")

def probed(*a, **k):
    note("probe")
    return False

if mode in ("busy", "greet-once"):
    b.serial.Serial = busy
    b.candidate_ports = lambda: [PORT]
elif mode == "silent":
    b.serial.Serial = Silent
    b.PROBE_S, b.PROBE_PATIENT_S = 0.05, 0.1
    b.candidate_ports = lambda: [PORT]
else:   # no serial port at all
    def nope(*a, **k):
        note("serial opened")
        raise serial.SerialException("busy")
    b.serial.Serial = nope
    b.candidate_ports = lambda: []
if mode != "silent":
    b.probe_is_our_board = probed
b.usb_topology = lambda: ((PORT, 1, 2, ""),) if b.candidate_ports() else ()

if mode == "greet-once":
    from pc import bridge as pb, wifi_session as ws
    real = pb.Bridge.greet
    state = {"n": 0}
    def greet(self):
        state["n"] += 1
        if state["n"] == 1:
            raise ws.SessionError("link lost")
        return real(self)
    pb.Bridge.greet = greet

b.main(["--port", PORT] if mode == "busy" else [])
"""


class Daemon:
    """main() in a child Python, stopped with SIGTERM like the service is."""

    def __init__(self, tmp_path, mode, force=False, board=None, pair=True,
                 board_json=None):
        self.home = tmp_path / "home"
        (self.home / ".blink").mkdir(parents=True)
        self.tap = tmp_path / "tap.ndjson"
        self.marker = tmp_path / "marker"
        script = tmp_path / "child.py"
        script.write_text(textwrap.dedent(CHILD), encoding="utf-8")
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                   PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1",
                   BLINK_SKIP_SERVICE="1", BLINK_TAP=str(self.tap))
        env.pop("BLINK_FORCE_WIFI", None)
        # The child is outside conftest's network guard. Without this, a
        # hello carrying `fw` made it fetch the real release feed from
        # GitHub, and under Docker's emulation that fetch outlasted the
        # test's wait for board.json (2026-11-07). A missing directory
        # reads as "feed unreachable" at once.
        env["BLINK_OTA_DIR"] = str(tmp_path / "no-feed")
        if force:
            env["BLINK_FORCE_WIFI"] = "1"
        if pair and board is not None:
            wifi_pair.save(str(self.home / ".blink"), "board", KEY, "127.0.0.1",
                           board.address[1])
        if board_json is not None:
            (self.home / ".blink" / "board.json").write_text(json.dumps(board_json))
        self.proc = subprocess.Popen(
            [sys.executable, str(script), str(self.marker), mode], env=env,
            cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def seen(self):
        try:
            recs = [json.loads(l) for l in self.tap.read_text().splitlines()]
        except (OSError, ValueError):
            return []
        return [(r["dir"], r["msg"].get("t")) for r in recs if "msg" in r]

    def marks(self):
        try:
            return self.marker.read_text().split()
        except OSError:
            return []

    def wait(self, cond, secs=10):
        end = time.monotonic() + secs
        while time.monotonic() < end and self.proc.poll() is None:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)    # the daemon's own exit
        try:
            _, err = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err = self.proc.communicate()
        self.log = err.decode(errors="replace")
        return self.log

    def board(self):
        try:
            return json.loads((self.home / ".blink" / "board.json").read_text())
        except (OSError, ValueError):
            return {}

    def link(self):
        return json.loads((self.home / ".blink" / "link.json").read_text())


HANDSHAKE = {("tx", "welcome"), ("rx", "hello")}
posix = pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")


@pytest.fixture
def fake_board():
    made = []

    def make(**kw):
        b = FakeWifiBoard(KEY, hello_at_start=True, **kw).start()
        made.append(b)
        return b
    yield make
    for b in made:
        b.stop()


@posix
@pytest.mark.parametrize("force", [False, True])
def test_main_reaches_wifi_with_no_serial_port_and_a_busy_one(
        tmp_path, fake_board, force):
    d = Daemon(tmp_path, "busy", force=force, board=fake_board())
    try:
        assert d.wait(lambda: HANDSHAKE <= set(d.seen()))
        if force:
            time.sleep(3.5)         # a flapping link would reconnect by now
        assert d.link()["link"] == "wifi"
    finally:
        log = d.stop()
    assert d.proc.returncode == 0, log
    assert "probe" not in d.marks()
    assert "127.0.0.1" not in log
    assert log.count("[wifi] connected") == 1, log


@posix
def test_restart_reconnects_over_wifi_without_serial_probe(tmp_path, fake_board):
    d = Daemon(tmp_path, "nothing", board=fake_board())
    try:
        assert d.wait(lambda: HANDSHAKE <= set(d.seen()))
    finally:
        log = d.stop()
    # No probe_is_our_board, no serial open, and a WifiLink raises on dtr/rts,
    # so a pulse would have been a "link lost" here.
    assert d.marks() == [] and "link lost" not in log
    assert d.proc.returncode == 0


@posix
def test_a_session_error_on_the_first_write_is_a_dropped_link(tmp_path, fake_board):
    d = Daemon(tmp_path, "greet-once", board=fake_board())
    try:
        assert d.wait(lambda: HANDSHAKE <= set(d.seen()), 15)
    finally:
        log = d.stop()
    assert d.proc.returncode == 0, log
    assert "[wifi] link lost" in log and "Traceback" not in log


@posix
def test_a_silent_stranger_is_not_probed_again_while_the_layout_is_unchanged(tmp_path):
    d = Daemon(tmp_path, "silent", pair=False)
    try:
        assert d.wait(lambda: d.marks().count("open") >= 2, 15)
        time.sleep(7)               # past D51's first sit-out (5 s) and more
    finally:
        log = d.stop()
    assert d.marks().count("open") == 2, d.marks()    # fast pass + patient pass
    assert log.count("nothing here answers") == 1, log


@posix
def test_with_no_pairing_a_busy_port_is_retried_every_three_seconds(tmp_path):
    d = Daemon(tmp_path, "busy", pair=False)
    try:
        assert d.wait(lambda: d.marks().count("open") >= 3, 12)
    finally:
        d.stop()
    assert d.marks().count("open") >= 3     # D51 would have said 0, 5, 15 s


@posix
def test_a_waiting_daemon_writes_none(tmp_path):
    d = Daemon(tmp_path, "nothing", pair=False)
    try:
        assert d.wait(lambda: (d.home / ".blink" / "link.json").exists(), 10)
        assert d.link()["link"] == "none"
    finally:
        d.stop()


@posix
@pytest.mark.parametrize("same", [True, False])
def test_learn_board_over_wifi_keeps_the_port_only_for_the_same_board(
        tmp_path, fake_board, same):
    hello = {"t": "hello", "v": 1, "board_id": "aaaa" if same else "bbbb",
             "fw": "1.0.0", "caps": []}
    d = Daemon(tmp_path, "nothing", board=fake_board(extra_lines=[hello]),
               board_json={"port": "/dev/known", "board_id": "aaaa"})
    try:
        assert d.wait(lambda: d.seen().count(("rx", "hello")) >= 2)
        # The tap records a message before it is handled, so wait for the
        # record the second hello writes rather than a fixed sleep.
        assert d.wait(lambda: d.board().get("fw") == "1.0.0")
    finally:
        d.stop()
    rec = d.board()
    assert rec["port"] == ("/dev/known" if same else None)
