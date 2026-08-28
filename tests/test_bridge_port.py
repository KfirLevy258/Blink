"""Port acquisition for the always-on daemon.

install.sh registers the bridge with launchd/systemd, both of which restart it
after every exit. That makes "what happens with no board attached" a question
about a machine's whole uptime rather than about one run.
"""
import claude_usage_bridge as bridge


def test_returns_an_attached_port_immediately(tmp_path, monkeypatch):
    port = tmp_path / "cu.usbmodem1"
    port.write_text("")
    monkeypatch.setattr(bridge, "autodetect_port", lambda: str(port))
    assert bridge.wait_for_port(poll_s=0) == str(port)


def test_waits_instead_of_exiting_when_no_board_is_attached(tmp_path, monkeypatch):
    """Exiting here becomes a respawn every 10s, forever, on an idle machine."""
    port = tmp_path / "cu.usbmodem1"
    calls = {"n": 0}

    def detect():
        calls["n"] += 1
        if calls["n"] < 3:
            return None
        port.write_text("")          # the cable goes in on the third look
        return str(port)

    monkeypatch.setattr(bridge, "autodetect_port", detect)
    assert bridge.wait_for_port(poll_s=0) == str(port)
    assert calls["n"] == 3


def test_announces_the_wait_once_not_per_attempt(tmp_path, monkeypatch, capsys):
    """Otherwise bridge.log grows by a line every three seconds, all day."""
    port = tmp_path / "cu.usbmodem1"
    calls = {"n": 0}

    def detect():
        calls["n"] += 1
        if calls["n"] < 20:
            return None
        port.write_text("")
        return str(port)

    monkeypatch.setattr(bridge, "autodetect_port", detect)
    bridge.wait_for_port(poll_s=0)
    waiting = [ln for ln in capsys.readouterr().err.splitlines() if "waiting" in ln]
    assert len(waiting) == 1


def test_an_explicit_port_that_is_not_there_yet_is_waited_for(tmp_path, monkeypatch):
    """--port names a node that only exists once the board is plugged in."""
    port = tmp_path / "cu.usbserial-42"
    calls = {"n": 0}

    def exists(path):
        calls["n"] += 1
        return calls["n"] >= 3

    monkeypatch.setattr(bridge.os.path, "exists", exists)
    monkeypatch.setattr(bridge, "autodetect_port", lambda: None)
    assert bridge.wait_for_port(str(port), poll_s=0) == str(port)


def test_a_board_returning_on_a_different_node_is_picked_up(tmp_path, monkeypatch):
    """A port resolved once at startup would keep retrying the old node."""
    second = tmp_path / "cu.usbmodem2"
    second.write_text("")
    monkeypatch.setattr(bridge, "autodetect_port", lambda: str(second))
    assert bridge.wait_for_port(poll_s=0) == str(second)


class _Port:
    def __init__(self, device, vid=None, pid=None, manufacturer=None):
        self.device, self.vid, self.pid = device, vid, pid
        self.manufacturer = manufacturer


def _ports(monkeypatch, *ports):
    monkeypatch.setattr(bridge.list_ports, "comports", lambda: list(ports))


def test_finds_the_ch340_this_board_actually_uses(monkeypatch):
    """The real capture from this machine: no manufacturer, usbserial node.

    Matching on the name or manufacturer never found it, and dev.sh masked
    that by always passing --port.
    """
    _ports(monkeypatch,
           _Port("/dev/cu.BLTH"),
           _Port("/dev/cu.usbserial-14140", vid=0x1A86, pid=0x7523,
                 manufacturer=None))
    assert bridge.autodetect_port() == "/dev/cu.usbserial-14140"


def test_finds_a_cp210x_variant(monkeypatch):
    _ports(monkeypatch, _Port("/dev/cu.SLAB_USBtoUART", vid=0x10C4, pid=0xEA60))
    assert bridge.autodetect_port() == "/dev/cu.SLAB_USBtoUART"


def test_finds_a_native_usb_espressif_part(monkeypatch):
    _ports(monkeypatch, _Port("/dev/cu.usbmodem101", vid=0x303A, pid=0x1001))
    assert bridge.autodetect_port() == "/dev/cu.usbmodem101"


def test_ignores_bluetooth_ports(monkeypatch):
    """These are always present on a Mac and must never be opened."""
    _ports(monkeypatch, _Port("/dev/cu.BLTH"), _Port("/dev/cu.BoseQC35II"))
    assert bridge.autodetect_port() is None


def test_name_heuristic_still_covers_an_unknown_chip(monkeypatch):
    _ports(monkeypatch, _Port("/dev/cu.usbmodem-XYZ", vid=0x1234, pid=0x5678))
    assert bridge.autodetect_port() == "/dev/cu.usbmodem-XYZ"


# --- upkeep while there is no board ---------------------------------------


def test_upkeep_runs_while_waiting_for_a_board(monkeypatch):
    """A machine with its board unplugged is exactly where this function
    spends its time. Until the callback existed the daemon did nothing at all
    in that state, so a statusLine hook wiped while the board was out stayed
    wiped until somebody plugged it back in."""
    calls = []
    seen = {"n": 0}

    def fake_detect():
        seen["n"] += 1
        return "/dev/cu.usbserial-1" if seen["n"] > 3 else None

    monkeypatch.setattr(bridge, "autodetect_port", fake_detect)
    monkeypatch.setattr(bridge.os.path, "exists", lambda p: True)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)

    port = bridge.wait_for_port(on_wait=lambda: calls.append(1), poll_s=0)
    assert port == "/dev/cu.usbserial-1"
    assert len(calls) == 3, "upkeep did not run on every poll"


def test_a_board_already_present_does_no_upkeep(monkeypatch):
    """The callback is for waiting. Nothing waited, nothing to do."""
    calls = []
    monkeypatch.setattr(bridge, "autodetect_port",
                        lambda: "/dev/cu.usbserial-1")
    monkeypatch.setattr(bridge.os.path, "exists", lambda p: True)
    bridge.wait_for_port(on_wait=lambda: calls.append(1))
    assert calls == []


def test_failing_upkeep_never_strands_the_wait(monkeypatch, capsys):
    """Failing here would leave a plugged-in board undetected, which is far
    worse than whatever the callback was trying to do."""
    seen = {"n": 0}

    def fake_detect():
        seen["n"] += 1
        return "/dev/cu.usbserial-1" if seen["n"] > 2 else None

    def boom():
        raise RuntimeError("settings.json vanished")

    monkeypatch.setattr(bridge, "autodetect_port", fake_detect)
    monkeypatch.setattr(bridge.os.path, "exists", lambda p: True)
    monkeypatch.setattr(bridge.time, "sleep", lambda s: None)

    assert bridge.wait_for_port(on_wait=boom) == "/dev/cu.usbserial-1"
    assert "upkeep failed" in capsys.readouterr().err
