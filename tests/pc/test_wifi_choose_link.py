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


# ---- main() in a child process ---------------------------------------------

CHILD = """
import sys
import serial
import claude_usage_bridge as b

marker = sys.argv[1]

def busy(*a, **k):
    raise serial.SerialException("busy")

def probed(*a, **k):
    open(marker, "w").close()
    return False

b.serial.Serial = busy
b.probe_is_our_board = probed
b.candidate_ports = lambda: ["/dev/always-busy"]
b.usb_topology = lambda: (("/dev/always-busy", 1, 2, ""),)
b.main(["--port", "/dev/always-busy"])
"""


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")
@pytest.mark.parametrize("force", [False, True])
def test_main_reaches_wifi_with_no_serial_port_and_a_busy_one(tmp_path, force):
    home = tmp_path / "home"
    home.mkdir()
    board = FakeWifiBoard(KEY, hello_at_start=True).start()
    tap = tmp_path / "tap.ndjson"
    probe_marker = tmp_path / "probed"
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(CHILD), encoding="utf-8")
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               PYTHONPATH=ROOT, PYTHONDONTWRITEBYTECODE="1",
               BLINK_SKIP_SERVICE="1", BLINK_TAP=str(tap))
    env.pop("BLINK_FORCE_WIFI", None)
    if force:
        env["BLINK_FORCE_WIFI"] = "1"
    proc = None
    try:
        wifi_pair.save(str(home / ".blink"), "board", KEY, "127.0.0.1",
                       board.address[1])
        proc = subprocess.Popen(
            [sys.executable, str(script), str(probe_marker)], env=env,
            cwd=str(tmp_path), stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        def seen():
            try:
                recs = [json.loads(l) for l in tap.read_text().splitlines()]
            except (OSError, ValueError):
                return set()
            return {(r["dir"], r["msg"].get("t")) for r in recs if "msg" in r}
        end = time.monotonic() + 10
        while time.monotonic() < end and not {("tx", "welcome"), ("rx", "hello")} <= seen():
            if proc.poll() is not None:
                break
            time.sleep(0.05)
        got = seen()
        proc.send_signal(signal.SIGTERM)        # the daemon's own exit path
        out, err = proc.communicate(timeout=20)
        assert {("tx", "welcome"), ("rx", "hello")} <= got, err.decode(errors="replace")
        assert proc.returncode == 0
        assert not probe_marker.exists()   # no probe on a busy port or a WifiLink
        log = err.decode(errors="replace")
        assert "[wifi] connected" in log
        assert "127.0.0.1" not in log
        link = json.loads((home / ".blink" / "link.json").read_text())
        assert link["link"] == "wifi"
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.communicate()
        board.stop()
