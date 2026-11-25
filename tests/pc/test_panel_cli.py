"""`blink config panel standard|alternate` (issue #23).

The board cannot tell its two display panels apart, so the owner says which
one it has; the CLI borrows the serial port from the daemon the way
`blink provision` does, sends one `panel` line, and reports what the board
said. These run against a fake port and a fake service: no real port, no
real launchd/systemd/schtasks (tests/conftest.py's sandbox would refuse the
rest anyway).
"""
import os
import types

import pytest

from pc import cli, protocol


class _Board:
    """A serial port with a board on the end: says hello, then answers the
    one line written to it with `answer` (a list of console lines)."""

    def __init__(self, answer=(), hello=True, explode=False):
        self.before = [b'{"t":"hello","v":2}\n'] if hello else []
        self.after = [a.encode() + b"\r\n" for a in answer]
        self.written = []
        self.explode = explode
        self.closed = 0

    def __call__(self, *a, **k):
        return self

    def open(self):
        pass

    def reset_input_buffer(self):
        pass

    def readline(self):
        if self.explode:
            raise RuntimeError("the cable came out")
        q = self.after if self.written else self.before
        return q.pop(0) if q else b""

    def write(self, b):
        self.written.append(b)

    def flush(self):
        pass

    def close(self):
        self.closed += 1


class _Service:
    def __init__(self):
        self.stops = 0
        self.starts = 0

    def stop(self):
        self.stops += 1
        return types.SimpleNamespace(detail="stopped (test)")

    def start(self):
        self.starts += 1
        return "started"


@pytest.fixture
def rig(monkeypatch):
    """The service is live (so the borrow sequence runs) but faked, time is
    a ticking clock, and the port is whatever the test installs."""
    import serial
    from pc import service_ctl
    svc = _Service()
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda **kw: 0)
    monkeypatch.setattr(cli, "_kill_by_path", lambda *a, **k: None)
    monkeypatch.setattr(service_ctl, "stop_service", svc.stop)
    monkeypatch.setattr(service_ctl, "start_service", svc.start)
    clock = [0.0]

    def tick():
        clock[0] += 1.0
        return clock[0]
    monkeypatch.setattr(cli.time, "monotonic", tick)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)

    def install(board):
        monkeypatch.setattr(serial, "Serial", board)
        return board
    return types.SimpleNamespace(svc=svc, install=install)


def _panel(value):
    return cli.main(["config", "panel", value, "--port", "/dev/none"])


@pytest.mark.parametrize("value", ["standard", "alternate"])
def test_a_confirmed_panel_is_reported_and_succeeds(rig, capsys, value):
    board = rig.install(_Board([f"[cfg] panel set to {value}"]))
    assert _panel(value) == 0
    assert board.written == [protocol.encode_checked(
        protocol.panel(value))[0]]
    out = capsys.readouterr().out
    assert f"panel set to {value}" in out
    assert rig.svc.stops == 1 and rig.svc.starts == 1
    assert board.closed == 1


def test_the_line_goes_through_encode_checked(rig, monkeypatch):
    seen = []
    real = cli.protocol.encode_checked

    def spy(msg):
        seen.append(msg.get("t"))
        return real(msg)
    monkeypatch.setattr(cli.protocol, "encode_checked", spy)
    rig.install(_Board(["[cfg] panel set to alternate"]))
    assert _panel("alternate") == 0
    assert seen == ["panel"]


def test_no_confirmation_names_older_firmware(rig, capsys):
    board = rig.install(_Board([]))
    assert _panel("alternate") == 1
    err = capsys.readouterr().err
    assert "did not confirm" in err
    assert "older than this feature" in err
    assert "Update the board first" in err
    assert len(board.written) == 1
    assert rig.svc.starts == 1


def test_a_board_that_could_not_store_it_fails(rig, capsys):
    rig.install(_Board(["[cfg] could not store the panel (-28)"]))
    assert _panel("standard") == 1
    out, err = capsys.readouterr()
    assert "could not store the panel" in out
    # The board's errno is a value off the wire; it is not repeated.
    assert "-28" not in out + err
    assert rig.svc.starts == 1


@pytest.mark.parametrize("value", ["standard", "alternate"])
def test_saved_but_not_shown_says_restart_and_fails(rig, capsys, value):
    """The board stored the panel but its MADCTL write failed
    (firmware/src/panel.h, PANEL_MSG_NOT_SHOWN). That is a board that
    answered, so it is not "older firmware"; and the screen has not changed,
    so it is not a success either."""
    rig.install(_Board(["[cfg] panel saved; restart the board to show it"
                        " (-5)"]))
    assert _panel(value) == 1
    out, err = capsys.readouterr()
    assert f"panel saved as {value}, but not shown yet" in out
    assert "plug it back in" in err
    assert "older than this feature" not in err
    assert "did not confirm" not in err
    assert "no restart" not in out + err
    # The board's errno is a value off the wire; it is not repeated.
    assert "-5" not in out + err
    assert rig.svc.stops == 1 and rig.svc.starts == 1


def test_a_saved_but_not_shown_line_ends_the_wait(rig, monkeypatch):
    """It is one of the answers _tell_board listens for, so the read stops
    on it rather than running out the clock and reporting no answer."""
    seen = []
    real = cli._tell_board

    def spy(serial, port, baud, msg, answers, **kw):
        seen.extend(answers)
        return real(serial, port, baud, msg, answers, **kw)
    monkeypatch.setattr(cli, "_tell_board", spy)
    rig.install(_Board(["[cfg] panel saved; restart the board to show it"
                        " (-5)"]))
    assert _panel("alternate") == 1
    assert "[cfg] panel saved; restart the board to show it" in seen


def test_an_answer_for_a_different_panel_is_not_a_success(rig, capsys):
    rig.install(_Board(["[cfg] panel set to standard"]))
    assert _panel("alternate") == 1
    assert "different panel" in capsys.readouterr().err


def test_no_hello_fails_and_sends_nothing(rig, capsys):
    board = rig.install(_Board(hello=False))
    assert _panel("alternate") == 1
    assert board.written == []
    assert "No hello" in capsys.readouterr().err
    assert rig.svc.starts == 1


def test_an_exception_still_restarts_the_service_once(rig):
    rig.install(_Board(explode=True))
    with pytest.raises(RuntimeError):
        _panel("alternate")
    assert rig.svc.stops == 1 and rig.svc.starts == 1


def test_a_ctrl_c_in_the_settle_sleep_still_restarts_it(rig, monkeypatch):
    rig.install(_Board(["[cfg] panel set to alternate"]))

    def interrupted(_s):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.time, "sleep", interrupted)
    with pytest.raises(KeyboardInterrupt):
        _panel("alternate")
    assert rig.svc.starts == 1


@pytest.mark.parametrize("argv,needle", [
    (["config", "panel", "on"], "standard or alternate"),
    (["config", "panel", "off"], "standard or alternate"),
    (["config", "status-page", "alternate"], "on or off"),
    (["config", "usage-refresh", "standard"], "on or off"),
])
def test_a_value_for_the_wrong_setting_is_refused_without_the_port(
        rig, monkeypatch, capsys, argv, needle):
    import serial

    def no_port(*a, **k):
        pytest.fail("opened a port for a value it should have refused")
    monkeypatch.setattr(serial, "Serial", no_port)
    assert cli.main(argv + ["--port", "/dev/none"]) == 1
    assert needle in capsys.readouterr().err
    assert rig.svc.stops == 0 and rig.svc.starts == 0
    assert not os.path.exists(os.path.join(cli.blink_home(), "config.json"))


def test_skip_service_leaves_the_service_alone(monkeypatch, capsys):
    """conftest sets BLINK_SKIP_SERVICE=1; with it, the board is still
    told, and nothing stops or starts a service."""
    import serial
    from pc import service_ctl
    assert cli._skip_service()
    monkeypatch.setattr(service_ctl, "stop_service",
                        lambda: pytest.fail("stopped a service"))
    monkeypatch.setattr(service_ctl, "start_service",
                        lambda: pytest.fail("started a service"))
    clock = [0.0]

    def tick():
        clock[0] += 1.0
        return clock[0]
    monkeypatch.setattr(cli.time, "monotonic", tick)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    board = _Board(["[cfg] panel set to alternate"])
    monkeypatch.setattr(serial, "Serial", board)
    assert _panel("alternate") == 0
    assert len(board.written) == 1


def test_without_a_value_it_says_the_board_holds_it(monkeypatch, capsys):
    import serial
    monkeypatch.setattr(serial, "Serial",
                        lambda *a, **k: pytest.fail("opened a port"))
    assert cli.main(["config", "panel"]) == 0
    out = capsys.readouterr().out
    assert "stored on the board" in out
    assert "cannot read it back" in out
    assert "blink config panel standard" in out


def test_the_listing_names_the_panel_setting(capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    row = [l for l in out.splitlines() if l.startswith("panel")]
    assert len(row) == 1 and "stored on the board" in row[0]
    assert "status-page" in out and "usage-refresh" in out
