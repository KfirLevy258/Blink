"""Which port may be reset, and what `blink status` says about the desk."""
import json, os, signal, subprocess, sys, time
import pytest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from claude_usage_bridge import may_reset_port
from pc.cli import board_lines
from pc.version import RELEASE_VERSION


class TestMayReset:
    def test_an_explicit_port_is_the_operators_call(self):
        assert may_reset_port(True, "COM3", None, False)

    def test_a_stranger_is_never_reset(self):
        assert not may_reset_port(False, "COM4", "COM3", False)
        assert not may_reset_port(False, "COM4", "COM3", True)

    def test_the_remembered_port_waits_for_the_patient_probe(self):
        # A board still booting in its usual socket answers within 11 s;
        # resetting it on the 1.5 s pass is the double boot on plug-in.
        assert not may_reset_port(False, "COM3", "COM3", False)
        assert may_reset_port(False, "COM3", "COM3", True)


class TestBoardLines:
    def test_the_board_is_named_with_what_is_known_about_it(self):
        lines = board_lines({"port": "COM3", "board_id": "abc", "fw": "1.2.2"},
                            [("COM3", "CH340")])
        assert lines == ["Board       COM3 (CH340) -- id abc, firmware 1.2.2"]

    def test_other_boards_are_listed_as_left_alone(self):
        lines = board_lines({"port": "COM3", "board_id": "abc", "fw": None},
                            [("COM3", "CH340"), ("COM7", "CH340"), ("COM9", "FTDI")])
        assert lines[0] == "Board       COM3 (CH340) -- id abc"
        assert "COM7 (CH340), COM9 (FTDI)" in lines[1]
        assert "left alone" in lines[1]

    def test_nothing_identified_yet(self):
        lines = board_lines({}, [("COM7", "CH340")])
        assert lines == ["Board       none identified yet -- looking at COM7 (CH340)"]

    def test_the_board_moved_socket(self):
        lines = board_lines({"port": "/dev/cu.usbserial-14220", "board_id": "abc"},
                            [("/dev/cu.usbserial-14330", "CH340")])
        assert lines[0].startswith("Board       none identified yet")
        assert lines[1] == "            last seen on /dev/cu.usbserial-14220"

    def test_unplugged(self):
        assert board_lines({"port": "COM3"}, []) == \
            ["Board       not plugged in (last seen on COM3)"]
        assert board_lines({}, []) == ["Board       not plugged in"]


class TestBlankBoard:
    """Issue #16: a board that has never been flashed.

    `silent` is what the daemon reports in link.json: ports that stayed
    quiet through both hello passes (1.5 s, then 11 s). Only then is a board
    called blank; anything less keeps "none identified yet", because a board
    still booting or still being probed is not blank.
    """

    # Silence is all the daemon measured. A crash-looping or fused,
    # unbooting BLINK is as silent as a blank one, and so is an Arduino
    # clone on the same chip, so the copy says "isn't answering as a BLINK"
    # and names a step anybody can take. It never names a flash script:
    # tools/burn-*.sh needs the Zephyr toolchain and the signing key.
    ONE = "            A USB serial device here isn't answering as a BLINK."
    MANY = ("            The USB serial devices here aren't answering as a"
            " BLINK.")
    IF_ONE = ("            If it is your BLINK, it has no firmware or isn't"
              " starting.")
    IF_MANY = ("            If one is your BLINK, it has no firmware or isn't"
               " starting.")
    REPLUG = "            Unplug it and plug it back in."
    # Several devices: "it" would point at nothing, so the BLINK is named.
    REPLUG_MANY = "            Unplug your BLINK and plug it back in."
    EMAIL = "            If it stays this way, email support@blink-buddy.com"
    STATUS = "            Include what this command prints: blink status"

    def test_one_blank_board(self):
        lines = board_lines({}, [("/dev/cu.usbserial-1", "CH340")],
                            silent=["/dev/cu.usbserial-1"])
        assert lines == [
            "Board       /dev/cu.usbserial-1 (CH340)",
            self.ONE, self.IF_ONE, self.REPLUG, self.EMAIL, self.STATUS]

    def test_several_blank_boards(self):
        lines = board_lines({}, [("COM3", "CH340"), ("COM7", "CP210x")],
                            silent=["COM7", "COM3"])
        assert lines[0] == "Board       COM3 (CH340), COM7 (CP210x)"
        assert lines[1:] == [self.MANY, self.IF_MANY, self.REPLUG_MANY,
                             self.EMAIL, self.STATUS]

    def test_nobody_is_told_to_flash(self):
        # The owner's blocking point: a customer cannot run the burn
        # scripts, and the quiet device may not be a BLINK at all.
        for ports in ([("COM7", "CH340")],
                      [("COM3", "CH340"), ("COM7", "CP210x")]):
            text = "\n".join(board_lines({}, ports,
                                         silent=[d for d, _ in ports]))
            for word in ("burn", "tools/", "flash", "checkout", "eFuse"):
                assert word not in text, word

    def test_the_command_is_the_one_this_install_types(self):
        lines = board_lines({}, [("COM7", "CH340")], blink_cmd="./blink",
                            silent=["COM7"])
        assert lines[-1].endswith(": ./blink status")

    def test_a_port_still_being_probed_keeps_the_old_line(self):
        lines = board_lines({}, [("COM3", "CH340"), ("COM7", "CH340")],
                            silent=["COM3"])
        assert lines == ["Board       none identified yet -- looking at"
                         " COM3 (CH340), COM7 (CH340)"]

    def test_a_silent_port_that_is_not_here_is_ignored(self):
        lines = board_lines({}, [("COM7", "CH340")], silent=["COM3"])
        assert lines == ["Board       none identified yet -- looking at COM7 (CH340)"]
        assert board_lines({}, [], silent=["COM3"]) == ["Board       not plugged in"]

    def test_the_remembered_port_is_still_named(self):
        lines = board_lines({"port": "COM3", "board_id": "abc"},
                            [("COM7", "CH340")], silent=["COM7"])
        assert lines[1] == TestBlankBoard.ONE
        assert lines[-1] == "            last seen on COM3"

    def test_an_identified_board_is_unchanged(self):
        # Nothing silent, or only some other port: the old single line.
        known = {"port": "COM3", "board_id": "abc", "fw": "1.2.2"}
        for silent in ((), ["COM9"]):
            lines = board_lines(known, [("COM3", "CH340")], silent=silent)
            assert lines == [
                "Board       COM3 (CH340) -- id abc, firmware 1.2.2"]

    def test_a_quiet_remembered_port_is_not_called_the_blink(self):
        # A port name is a socket, not a device: an Arduino clone plugged
        # in where the BLINK was takes the same name. While it is silent
        # it gets the same wording as any quiet device, with no id, no
        # firmware and no diagnosis borrowed from the board remembered
        # there. A BLINK crash-looping on its old port reads the same,
        # which is where "or isn't starting" is meant to be seen.
        known = {"port": "COM3", "board_id": "abc", "fw": "1.2.2"}
        lines = board_lines(known, [("COM3", "CH340")], silent=["COM3"])
        assert lines == [
            "Board       COM3 (CH340)",
            self.ONE, self.IF_ONE, self.REPLUG, self.EMAIL, self.STATUS,
            "            last seen on COM3"]
        text = "\n".join(lines)
        assert "abc" not in text and "1.2.2" not in text

    def test_a_quiet_remembered_port_beside_one_still_probed(self):
        # Not every port is silent yet, so nothing is concluded; and the
        # other port is not written off as "not BLINK, left alone" on the
        # strength of an identity nobody has confirmed.
        known = {"port": "COM3", "board_id": "abc", "fw": "1.2.2"}
        lines = board_lines(known, [("COM3", "CH340"), ("COM7", "CH340")],
                            silent=["COM3"])
        assert lines == [
            "Board       none identified yet -- looking at"
            " COM3 (CH340), COM7 (CH340)",
            "            last seen on COM3"]

    def test_the_copy_is_sentence_case(self):
        lines = board_lines({}, [("COM7", "CH340")], silent=["COM7"])
        for line in lines[1:]:
            text = line.strip()
            words = text.split()
            # One capital to start; the rest are names.
            assert text[0].isupper(), line
            assert all(not w[0].isupper()
                       or w.strip(".,") in ("BLINK", "USB")
                       for w in words[1:]), line


class TestBlankBoardThroughStatus:
    """The handoff, end to end on the host: what the daemon writes into
    link.json is what `blink status` prints from. Testing the record and the
    formatter apart could pass with the two wired wrong."""

    def _status(self, monkeypatch, home):
        import claude_usage_bridge
        from pc import cli, win_driver
        monkeypatch.setattr(cli, "blink_home", lambda: home)
        monkeypatch.setattr(claude_usage_bridge, "describe_ports",
                            lambda: [("COM7", "CH340")])
        monkeypatch.setattr(claude_usage_bridge, "remembered_board",
                            lambda _home: {})
        monkeypatch.setattr(win_driver, "undriven_boards", lambda: [])
        return cli._board_lines()

    def test_marked_silent_then_cleared(self, monkeypatch, tmp_path):
        from pc import link_status
        home = str(tmp_path / ".blink")
        os.makedirs(home)
        # No daemon has written anything: the old wording.
        assert self._status(monkeypatch, home)[0].startswith(
            "Board       none identified yet")
        # Both passes failed: the daemon reports the port silent.
        link_status.write(home, "none", None, silent=["COM7"])
        lines = self._status(monkeypatch, home)
        assert lines[1] == TestBlankBoard.ONE
        # The USB layout changed and the set was cleared: the next record
        # has no silent ports and the board is not called blank.
        link_status.write(home, "none", None, silent=[])
        assert self._status(monkeypatch, home)[0].startswith(
            "Board       none identified yet")

    def test_a_stale_record_is_not_believed(self, monkeypatch, tmp_path):
        from pc import link_status
        home = str(tmp_path / ".blink")
        os.makedirs(home)
        link_status.write(home, "none", None, silent=["COM7"],
                          now=lambda: 1.0)
        assert self._status(monkeypatch, home)[0].startswith(
            "Board       none identified yet")


_DAEMON_CHILD = r'''
import functools, os, sys
import serial
import claude_usage_bridge as b
from pc import wifi_link

replugged = sys.argv[1]     # exists = the USB layout changed
slow = sys.argv[2] == "slow"

# A failed connect must never broadcast on the developer's LAN.
_orig = b.wait_for_link
b.wait_for_link = lambda *a, **k: _orig(
    *a, connect=functools.partial(wifi_link.connect, discover=lambda key: None),
    **k)

PORT = "/dev/blank-cyd"
_exists = os.path.exists
b.os.path.exists = lambda p: True if p == PORT else _exists(p)


class Silent:
    """A CH340 that opens and never says a word: a board with no BLINK
    firmware on it."""
    in_waiting = 0
    dtr = rts = None
    def __init__(self, *a, **k): pass
    def read(self, n=1): return b""
    def write(self, raw): return len(raw)
    def reset_input_buffer(self): pass
    def close(self): pass


def opener(*a, **k):
    # After the re-plug the port is busy, so the daemon never gets to call
    # it silent again and the cleared record stays put for the test.
    if _exists(replugged):
        raise serial.SerialException("busy")
    return Silent()


b.serial.Serial = opener
b.PROBE_S, b.PROBE_PATIENT_S = (30.0, 30.0) if slow else (0.05, 0.1)
b.candidate_ports = lambda: [PORT]
b.usb_topology = lambda: ((PORT, 1, 3 if _exists(replugged) else 2, ""),)
b.main([])
'''


@pytest.mark.skipif(sys.platform == "win32", reason="SIGTERM is POSIX")
class TestBlankBoardFromTheDaemon:
    """The handoff with the real daemon: main() in a child marks a silent
    port in link.json and clears it, and `blink status` is read from that
    record. A test that writes link.json itself would pass with the daemon
    never writing it at all."""

    PORT = "/dev/blank-cyd"

    def _start(self, tmp_path, slow=False):
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        self.home = tmp_path / "home"
        self.blink = self.home / ".blink"
        self.blink.mkdir(parents=True, exist_ok=True)
        self.replugged = tmp_path / "replugged"
        script = tmp_path / "child.py"
        script.write_text(_DAEMON_CHILD, encoding="utf-8")
        env = dict(os.environ, HOME=str(self.home), USERPROFILE=str(self.home),
                   PYTHONPATH=root, PYTHONDONTWRITEBYTECODE="1",
                   BLINK_SKIP_SERVICE="1",
                   BLINK_OTA_DIR=str(tmp_path / "no-feed"))
        env.pop("BLINK_FORCE_WIFI", None)
        self.proc = subprocess.Popen(
            [sys.executable, str(script), str(self.replugged),
             "slow" if slow else "fast"],
            env=env, cwd=str(tmp_path),
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

    def _stop(self):
        if self.proc.poll() is None:
            self.proc.send_signal(signal.SIGTERM)
        try:
            _, err = self.proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            _, err = self.proc.communicate()
        return err.decode(errors="replace")

    def _record(self):
        try:
            return json.loads((self.blink / "link.json").read_text())
        except (OSError, ValueError):
            return None

    def _wait(self, cond, secs=15):
        end = time.monotonic() + secs
        while time.monotonic() < end and self.proc.poll() is None:
            if cond():
                return True
            time.sleep(0.05)
        return cond()

    def _status(self, monkeypatch):
        import claude_usage_bridge
        from pc import cli, win_driver
        monkeypatch.setattr(cli, "blink_home", lambda: str(self.blink))
        monkeypatch.setattr(claude_usage_bridge, "describe_ports",
                            lambda: [(self.PORT, "CH340")])
        monkeypatch.setattr(win_driver, "undriven_boards", lambda: [])
        return cli._board_lines()

    def test_marked_silent_then_cleared_by_a_layout_change(
            self, monkeypatch, tmp_path):
        self._start(tmp_path)
        try:
            # Both passes heard nothing: the record names the port at once,
            # not on the next 60 s beat.
            assert self._wait(lambda: (self._record() or {}).get("silent")
                              == [self.PORT]), self._record()
            lines = self._status(monkeypatch)
            assert lines[0] == f"Board       {self.PORT} (CH340)", lines
            assert lines[1] == TestBlankBoard.ONE, lines
            # Re-plugged: the layout changed, the daemon forgets the set and
            # the record follows within a poll.
            self.replugged.write_text("1")
            assert self._wait(lambda: self._record() is not None
                              and "silent" not in self._record()), \
                self._record()
            assert self._status(monkeypatch)[0].startswith(
                "Board       none identified yet"), self._status(monkeypatch)
        finally:
            log = self._stop()
        assert self.proc.returncode == 0, log

    def test_a_previous_daemons_record_is_cleared_before_probing(
            self, monkeypatch, tmp_path):
        """A restart: the last daemon's fresh link.json names the port
        silent, and this one is about to listen to it for 30 s. Status must
        not call it blank while that happens."""
        from pc import link_status
        (tmp_path / "home" / ".blink").mkdir(parents=True)
        link_status.write(str(tmp_path / "home" / ".blink"), "none", None,
                          silent=[self.PORT])
        self._start(tmp_path, slow=True)
        try:
            assert self._wait(lambda: self._record() is not None
                              and "silent" not in self._record(), 10), \
                self._record()
            assert self._status(monkeypatch)[0].startswith(
                "Board       none identified yet")
        finally:
            log = self._stop()
        assert self.proc.returncode == 0, log


class TestBoardAheadOfTheApp:
    """The board running a LATER release than the app on this computer.

    They ship from one tag and install together, so this means something split
    them: a hand-flash, or an app update that failed after the firmware went
    on. The half left behind is the one the panel cannot reach -- the board's
    update row says the app is old and no tap on it can help -- so the only
    place that can name the fix is here.
    """

    def _ahead(self):
        major, minor, patch = (int(n) for n in RELEASE_VERSION.split("."))
        return f"{major + 1}.{minor}.{patch}"

    def test_it_says_so_and_names_the_command(self):
        lines = board_lines({"port": "COM3", "board_id": "abc",
                             "fw": self._ahead()},
                            [("COM3", "CH340")], blink_cmd="blink")
        assert any(RELEASE_VERSION in l and "ship together" in l for l in lines), lines
        assert any(l.strip() == "run: blink update" for l in lines), lines

    def test_it_says_nothing_when_the_halves_match(self):
        lines = board_lines({"port": "COM3", "fw": RELEASE_VERSION},
                            [("COM3", "CH340")])
        assert not any("ship together" in l for l in lines), lines

    def test_it_says_nothing_when_the_board_is_behind(self):
        """The ordinary case, and the one the update row already handles: the
        board can fetch its own firmware with a tap."""
        lines = board_lines({"port": "COM3", "fw": "0.9.0"},
                            [("COM3", "CH340")])
        assert not any("ship together" in l for l in lines), lines

    def test_an_unplugged_board_is_still_reported(self):
        """board.json remembers the firmware, so the mismatch is knowable with
        nothing on the desk -- and that is when someone reads `blink status`."""
        lines = board_lines({"port": "COM3", "fw": self._ahead()}, [])
        assert any("ship together" in l for l in lines), lines

    def test_a_board_that_never_said_its_firmware_says_nothing(self):
        for known in ({"port": "COM3"}, {"port": "COM3", "fw": None},
                      {"port": "COM3", "fw": "not a version"}):
            lines = board_lines(known, [("COM3", "CH340")])
            assert not any("ship together" in l for l in lines), (known, lines)
