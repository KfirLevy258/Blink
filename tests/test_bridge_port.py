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
