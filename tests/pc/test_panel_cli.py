"""`blink config panel standard|alternate` (issue #23).

The board cannot tell its two display panels apart, so the owner says which
one it has. The CLI leaves an owner-only request for the running daemon
(pc/settings_request.py), the daemon sends one `panel` line on the link it
already holds, matches the board's console answer to a fixed word, and
writes that word back; the CLI prints what it means.

Until 2026-11-25 the CLI borrowed the serial port instead, and both its open
and the daemon's restart reset the board under a command that promised no
reboot (a reset before ota_mark_healthy reverts a fresh update). So the
tests below also prove the CLI never opens a port and never touches the
service. The daemon half is driven by calling claude_usage_bridge's
PanelPickup directly, as tests/pc/test_settings_cli.py does settings_pickup;
no real port, no real launchd/systemd/schtasks.
"""
import io
import json
import os
import stat
import time
import types

import pytest

import claude_usage_bridge as b
from pc import cli, private_file, protocol, settings_request
from tests.support.home import inside

LOG_LINES = {"[panel] request taken", "[panel] request file refused",
             "[panel] answer not written"} | {
    f"[panel] answered ({r})" for r in settings_request.PANEL_RESULTS}


class Clock:
    """One second per reading, shared by the CLI and the stand-in daemon."""

    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 1.0
        return self.t


@pytest.fixture
def home():
    return cli.blink_home()


@pytest.fixture(autouse=True)
def no_port_no_service(monkeypatch):
    """The whole point of the new route: nothing here may open the serial
    port, borrow it, or stop or start the background service."""
    import serial
    from pc import service_ctl

    def refuse(what):
        def call(*a, **k):
            pytest.fail(f"blink config panel {what}")
        return call
    monkeypatch.setattr(serial, "Serial", refuse("opened a serial port"))
    monkeypatch.setattr(service_ctl, "stop_service", refuse("stopped the service"))
    monkeypatch.setattr(service_ctl, "start_service",
                        refuse("started the service"))
    monkeypatch.setattr(cli, "_with_port_borrowed", refuse("borrowed the port"))
    monkeypatch.setattr(cli, "_tell_board", refuse("talked to the port"))
    monkeypatch.setattr(cli, "_kill_recorded_daemon", refuse("killed the app"))


def private(home, name):
    return os.path.join(home, private_file.PRIVATE_DIR, name)


def leave(home, name, obj):
    """A file of our choosing where a taker looks, under the caller's home."""
    inside(home, private(home, name))
    private_file.write_private(home, name, json.dumps(obj).encode())


class Desk:
    """A running daemon's PanelPickup with a board behind it, serviced each
    time the CLI sleeps -- the serial loop's passes, in miniature.

    `answer` is the console line the board prints for the panel message, or
    None for firmware that predates it. `writer` stands in for the daemon's
    send(): encode_checked, then the wire."""

    def __init__(self, home, clock, answer=None, via="usb", ok=True):
        self.log = io.StringIO()
        self.pickup = b.PanelPickup(home, log=self.log, clock=clock)
        self.answer, self.via, self.ok = answer, via, ok
        self.wire = []
        self.told = False

    def writer(self, m):
        if not self.ok:
            return False
        raw, _why = protocol.encode_checked(m)
        if raw is None:
            return False
        self.wire.append(raw)
        return True

    def sleep(self, _s):
        if self.wire and self.answer and not self.told:
            self.told = True
            self.pickup.heard((self.answer + "\r\n").encode())
        self.pickup.tick(self.via, self.writer if self.via else None)

    def lines(self):
        return self.log.getvalue().splitlines()


def run(home, value, desk=None, clock=None):
    clock = clock or Clock()
    args = types.SimpleNamespace(setting="panel", value=value)
    return cli.cmd_config(args, clock=clock,
                          sleep=desk.sleep if desk else (lambda _s: None))


# --- the request file ---------------------------------------------------------

NOW = 1_800_000_000.0       # a fixed wall clock for the request's "t"


def test_the_request_is_owner_only_and_holds_the_panel_an_id_and_a_time(home):
    rid = settings_request.write_panel_request(home, "alternate",
                                               now=lambda: NOW)
    p = private(home, settings_request.PANEL_REQUEST_FILE)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    with open(p, encoding="utf-8") as f:
        assert json.load(f) == {"display": "alternate", "id": rid, "t": NOW}
    assert len(rid) >= 16


def test_the_writer_refuses_a_value_that_is_not_a_panel(home):
    with pytest.raises(ValueError):
        settings_request.write_panel_request(home, "connect")
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))


def test_the_taker_deletes_the_request_as_it_reads_it(home):
    rid = settings_request.write_panel_request(home, "standard",
                                               now=lambda: NOW)
    assert settings_request.take_panel_request(home, now=lambda: NOW) == {
        "display": "standard", "id": rid, "t": NOW}
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))
    assert settings_request.take_panel_request(home) is None
    assert os.listdir(os.path.join(home, private_file.PRIVATE_DIR)) == []


@pytest.mark.parametrize("obj", [
    {"display": "mirrored", "id": "x", "t": NOW},       # not a panel
    {"display": "standard", "t": NOW},                  # no id
    {"display": "standard", "id": 7, "t": NOW},         # id not a string
    {"display": ["standard"], "id": "x", "t": NOW},     # unhashable
    {"display": "standard", "id": "x", "t": NOW, "k": "y"},  # an extra key
    {"panel": "standard", "id": "x", "t": NOW},         # the other key name
    {"panel": "look", "k": "k" * 40, "id": "x"},        # the settings request
    {"display": "standard", "id": "x"},                 # no time: written
                                                        # before it was dated
    {"display": "standard", "id": "x", "t": str(NOW)},  # time not a number
    {"display": "standard", "id": "x", "t": True},      # bool is an int
    {"display": "standard", "id": "x", "t": None},
    {"display": "standard", "id": "x", "t": float("nan")},   # NaN in JSON
    {"display": "standard", "id": "x", "t": float("inf")},   # Infinity
    {"display": "standard", "id": "x", "t": float("-inf")},
    ["standard", "x"],
])
def test_the_taker_refuses_any_other_shape_and_deletes_it(home, obj):
    leave(home, settings_request.PANEL_REQUEST_FILE, obj)
    assert settings_request.take_panel_request(home, now=lambda: NOW) is None
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))


@pytest.mark.parametrize("age,taken", [
    (0.0, True),
    (settings_request.PANEL_REQUEST_MAX_AGE_S, True),         # the edge
    (settings_request.PANEL_REQUEST_MAX_AGE_S + 0.001, False),
    (cli.PANEL_WAIT_S + settings_request.PANEL_REQUEST_MAX_AGE_S, False),
    (3 * 86400.0, False),       # a daemon started days after the command
    (-settings_request.PANEL_REQUEST_AHEAD_S, True),  # a clock stepped back
    (-settings_request.PANEL_REQUEST_AHEAD_S - 0.001, False),
    (-86400.0, False),          # far ahead: would otherwise never expire
])
def test_the_taker_takes_a_fresh_request_and_drops_an_old_one(home, age,
                                                               taken):
    rid = settings_request.write_panel_request(home, "alternate",
                                               now=lambda: NOW - age)
    got = settings_request.take_panel_request(home, now=lambda: NOW)
    assert got == ({"display": "alternate", "id": rid, "t": NOW - age}
                   if taken else None)
    # Deleted as it is read either way, so a stale one is not met again.
    assert os.listdir(os.path.join(home, private_file.PRIVATE_DIR)) == []


def test_the_age_limit_outlasts_the_clis_wait():
    """Nobody is waiting on a request past the CLI's wait; the limit has to
    be beyond that, or a slow daemon would drop one a command still wants."""
    assert settings_request.PANEL_REQUEST_MAX_AGE_S > cli.PANEL_WAIT_S


def test_the_settings_taker_refuses_the_panel_request(home):
    """Their names say different things -- a settings-page tab, a display
    module -- and neither taker may read the other's request."""
    leave(home, settings_request.REQUEST_FILE,
          {"display": "alternate", "id": "x"})
    assert settings_request.take_request(home) is None
    leave(home, settings_request.REQUEST_FILE,
          {"panel": "alternate", "k": "k" * 40, "id": "x"})
    assert settings_request.take_request(home) is None


def test_the_panels_are_the_protocols():
    assert settings_request.DISPLAY_PANELS == protocol.PANELS


# --- the answer file ----------------------------------------------------------

def test_an_answer_for_another_request_is_not_taken(home):
    settings_request.write_panel_answer(home, "someone-else", "set")
    assert settings_request.read_panel_answer(home, "mine") is None
    assert os.path.exists(private(home, settings_request.PANEL_ANSWER_FILE))
    assert settings_request.read_panel_answer(home, "someone-else") == "set"
    assert not os.path.exists(private(home, settings_request.PANEL_ANSWER_FILE))


@pytest.mark.parametrize("obj", [
    {"id": "mine", "result": "reboot"},
    {"id": "mine", "result": "set", "panel": "alternate"},
    {"id": "mine"},
])
def test_an_answer_of_any_other_shape_is_not_taken(home, obj):
    leave(home, settings_request.PANEL_ANSWER_FILE, obj)
    assert settings_request.read_panel_answer(home, "mine") is None


def test_the_answer_writer_takes_only_a_result_word(home):
    with pytest.raises(ValueError):
        settings_request.write_panel_answer(home, "x", "[cfg] panel set to")


# --- the daemon's half --------------------------------------------------------

@pytest.mark.parametrize("line,asked,want", [
    ("[cfg] panel set to standard", "standard", "set"),
    ("[cfg] panel set to alternate", "alternate", "set"),
    ("[cfg] panel set to standard", "alternate", "mismatch"),
    ("[cfg] panel set to alternate", "standard", "mismatch"),
    ("[cfg] panel set to sideways", "standard", "mismatch"),
    ("[cfg] could not store the panel (-28)", "standard", "not_stored"),
    ("[cfg] panel saved; restart the board to show it (-5)", "alternate",
     "not_shown"),
    ("[cfg] unknown panel; ignored", "standard", "unknown"),
    ("[cfg] panel is set over the cable only; ignored", "standard", "cable"),
    ("[cfg] edition set to codex", "standard", None),
    ("[usage] panel: alternate, driver MADCTL kept", "alternate", None),
    ('{"t":"ping"}', "standard", None),
])
def test_each_board_line_is_one_result_word(line, asked, want):
    assert b.panel_answer(line, asked) == want
    assert b.panel_answer(line + "\r", asked) == want


def take(home, value, via="usb", ok=True, answer=None, clock=None):
    """A request left, one pass of a Desk's pickup, and the Desk."""
    clock = clock or Clock()
    rid = settings_request.write_panel_request(home, value)
    desk = Desk(home, clock, answer=answer, via=via, ok=ok)
    desk.pickup.tick(via, desk.writer if via else None)
    return rid, desk


def answer_file(home):
    with open(private(home, settings_request.PANEL_ANSWER_FILE),
              encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.parametrize("value", ["standard", "alternate"])
def test_the_pickup_sends_the_panel_line_through_encode_checked(
        home, monkeypatch, value):
    seen = []
    real = protocol.encode_checked

    def spy(m):
        seen.append(m.get("t"))
        return real(m)
    monkeypatch.setattr(protocol, "encode_checked", spy)
    rid, desk = take(home, value)
    assert desk.wire == [real(protocol.panel(value))[0]]
    assert seen == ["panel"]
    assert desk.pickup.pending is not None


@pytest.mark.parametrize("line,want", [
    ("[cfg] panel set to alternate", "set"),
    ("[cfg] panel set to standard", "mismatch"),
    ("[cfg] could not store the panel (-28)", "not_stored"),
    ("[cfg] panel saved; restart the board to show it (-116)", "not_shown"),
    ("[cfg] unknown panel; ignored", "unknown"),
    ("[cfg] panel is set over the cable only; ignored", "cable"),
])
def test_the_boards_line_becomes_the_answer_and_nothing_else(home, line, want):
    rid, desk = take(home, "alternate")
    # Torn across two reads, and behind a line of no interest, as a real
    # read loop hands it over.
    data = ("[proto] host status\r\n" + line + "\r\n").encode()
    desk.pickup.heard(data[:30])
    desk.pickup.heard(data[30:])
    assert desk.pickup.pending is None
    assert answer_file(home) == {"id": rid, "result": want}
    p = private(home, settings_request.PANEL_ANSWER_FILE)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(p).st_mode) == 0o600
    with open(p, encoding="utf-8") as f:
        # Less the id, which is random URL-safe text and could hold "-28".
        raw = f.read().replace(rid, "")
    for leak in ("-28", "-116", "alternate", "standard", "[cfg]"):
        assert leak not in raw
    assert set(desk.lines()) <= LOG_LINES
    assert desk.lines() == ["[panel] request taken",
                            f"[panel] answered ({want})"]


def test_no_answer_by_the_deadline_is_silent(home):
    clock = Clock()
    rid, desk = take(home, "standard", clock=clock)
    desk.pickup.heard(b"[proto] host status\r\n")
    desk.pickup.tick("usb", desk.writer)
    assert desk.pickup.pending is not None          # still listening
    clock.t += b.PANEL_LISTEN_S
    desk.pickup.tick("usb", desk.writer)
    assert desk.pickup.pending is None
    assert answer_file(home) == {"id": rid, "result": "silent"}


def test_over_wifi_nothing_is_sent_and_the_answer_is_cable(home):
    rid, desk = take(home, "alternate", via="wifi")
    assert desk.wire == []
    assert answer_file(home) == {"id": rid, "result": "cable"}


def test_with_no_board_linked_the_answer_is_no_board(home):
    rid, desk = take(home, "alternate", via=None)
    assert desk.wire == []
    assert answer_file(home) == {"id": rid, "result": "no_board"}


def test_a_link_lost_while_listening_is_no_board_not_silent(home):
    rid, desk = take(home, "alternate")
    desk.pickup.link_down()
    assert answer_file(home) == {"id": rid, "result": "no_board"}
    desk.pickup.link_down()                         # nothing pending: no-op
    assert desk.lines()[-1] == "[panel] answered (no_board)"


def test_a_refused_line_is_not_sent(home):
    rid, desk = take(home, "alternate", ok=False)
    assert desk.wire == []
    assert answer_file(home) == {"id": rid, "result": "not_sent"}


def test_a_second_request_waits_for_the_first_answer(home):
    rid1, desk = take(home, "alternate")
    rid2 = settings_request.write_panel_request(home, "standard")
    desk.pickup.tick("usb", desk.writer)
    assert os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))
    assert len(desk.wire) == 1
    desk.pickup.heard(b"[cfg] panel set to alternate\n")
    assert answer_file(home) == {"id": rid1, "result": "set"}
    desk.pickup.tick("usb", desk.writer)
    assert len(desk.wire) == 2
    desk.pickup.heard(b"[cfg] panel set to standard\n")
    assert answer_file(home) == {"id": rid2, "result": "set"}


def test_a_stale_request_is_dropped_and_nothing_is_sent_or_answered(home):
    """The command that wrote it is gone (a Ctrl-C it did not live through,
    a daemon started days later): sending it would switch the display under
    nobody."""
    old = time.time() - settings_request.PANEL_REQUEST_MAX_AGE_S - 60
    rid = settings_request.write_panel_request(home, "alternate",
                                               now=lambda: old)
    desk = Desk(home, Clock())
    desk.pickup.tick("usb", desk.writer)
    assert desk.wire == []
    assert desk.pickup.pending is None
    assert os.listdir(os.path.join(home, private_file.PRIVATE_DIR)) == []
    # Not a word in the log, not even "request taken": nothing was.
    assert desk.lines() == []
    assert rid not in desk.log.getvalue()


def test_console_bytes_with_nothing_pending_are_dropped(home):
    desk = Desk(home, Clock())
    desk.pickup.heard(b"[cfg] panel set to alternate\n")
    assert not os.path.exists(private(home, settings_request.PANEL_ANSWER_FILE))
    assert bytes(desk.pickup._buf) == b""


@pytest.mark.skipif(os.name == "nt", reason="POSIX modes")
def test_a_refused_request_folder_is_said_once(home):
    settings_request.write_panel_request(home, "alternate")
    os.chmod(os.path.join(home, private_file.PRIVATE_DIR), 0o755)
    desk = Desk(home, Clock())
    for _ in range(3):
        desk.pickup.tick("usb", desk.writer)
    assert desk.lines() == ["[panel] request file refused"]
    assert desk.wire == []


def test_the_log_never_carries_the_panel_the_id_or_the_boards_text(home):
    rid, desk = take(home, "alternate")
    desk.pickup.heard(b"[cfg] could not store the panel (-28)\n")
    text = desk.log.getvalue()
    for leak in (rid, "alternate", "standard", "-28", "could not store"):
        assert leak not in text
    assert set(desk.lines()) <= LOG_LINES


# --- the CLI ------------------------------------------------------------------

@pytest.mark.parametrize("value", ["standard", "alternate"])
def test_a_confirmed_panel_is_reported_and_succeeds(home, capsys, value):
    clock = Clock()
    desk = Desk(home, clock, answer=f"[cfg] panel set to {value}")
    assert run(home, value, desk, clock) == 0
    assert desk.wire == [protocol.encode_checked(protocol.panel(value))[0]]
    out = capsys.readouterr().out
    assert f"panel set to {value}" in out
    assert "no restart is needed" in out
    # The answer was read and removed; the request was taken.
    assert os.listdir(os.path.join(home, private_file.PRIVATE_DIR)) == []


@pytest.mark.parametrize("line,needles", [
    ("[cfg] panel set to standard", ["different panel"]),
    ("[cfg] could not store the panel (-28)",
     ["could not store the panel", "storage needs a look"]),
    ("[cfg] panel saved; restart the board to show it (-5)",
     ["panel saved as alternate, but not shown yet", "plug it back in"]),
    ("[cfg] unknown panel; ignored", ["did not know that panel"]),
    ("[cfg] panel is set over the cable only; ignored",
     ["only be set over the USB cable", "Plug the board into this computer"]),
    (None, ["did not confirm", "older than this feature",
            "Update the board first"]),
])
def test_every_other_answer_fails_with_its_message(home, capsys, line, needles):
    clock = Clock()
    desk = Desk(home, clock, answer=line)
    assert run(home, "alternate", desk, clock) == 1
    out, err = capsys.readouterr()
    for n in needles:
        assert n in out + err, n
    assert "no restart" not in out + err
    # The board's errno is a value off the wire; it is not repeated.
    for leak in ("-28", "-5", "[cfg]"):
        assert leak not in out + err
    assert not os.path.exists(private(home, settings_request.PANEL_ANSWER_FILE))


def test_over_wifi_the_cli_says_plug_it_in(home, capsys):
    clock = Clock()
    desk = Desk(home, clock, via="wifi")
    assert run(home, "alternate", desk, clock) == 1
    assert "only be set over the USB cable" in capsys.readouterr().err
    assert desk.wire == []


def test_with_no_board_the_cli_says_so(home, capsys):
    clock = Clock()
    desk = Desk(home, clock, via=None)
    assert run(home, "standard", desk, clock) == 1
    assert "No board is connected" in capsys.readouterr().err


def test_a_line_the_app_could_not_send_fails(home, capsys):
    clock = Clock()
    desk = Desk(home, clock, ok=False)
    assert run(home, "standard", desk, clock) == 1
    assert "could not send the setting" in capsys.readouterr().err


def test_without_a_daemon_it_says_the_app_is_not_running(home, capsys):
    assert run(home, "alternate") == 1
    assert ("The app is not running. Run blink install, or blink run in"
            " another terminal.") in capsys.readouterr().out
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))


def test_a_request_taken_but_never_answered_says_did_not_answer(home, capsys):
    def busy(_s):
        settings_request.take_panel_request(home)
    args = types.SimpleNamespace(setting="panel", value="alternate")
    assert cli.cmd_config(args, clock=Clock(), sleep=busy) == 1
    out = capsys.readouterr().out
    assert "The app did not answer. Try blink config panel again" in out
    assert "not running" not in out


def test_ctrl_c_during_the_wait_removes_the_request_and_still_raises(home):
    def interrupted(_s):
        raise KeyboardInterrupt
    args = types.SimpleNamespace(setting="panel", value="alternate")
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_config(args, clock=Clock(), sleep=interrupted)
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))


@pytest.mark.parametrize("err", [OSError("denied"),
                                 private_file.NotPrivate("x")])
def test_a_failed_cleanup_does_not_mask_the_ctrl_c(home, monkeypatch, err):
    def interrupted(_s):
        raise KeyboardInterrupt

    def refuse(*a, **k):
        raise err
    monkeypatch.setattr(private_file, "remove_private", refuse)
    args = types.SimpleNamespace(setting="panel", value="alternate")
    with pytest.raises(KeyboardInterrupt):
        cli.cmd_config(args, clock=Clock(), sleep=interrupted)


def test_an_unreadable_answer_removes_the_request_too(home, capsys,
                                                      monkeypatch):
    def boom(*a, **k):
        raise OSError("denied")
    monkeypatch.setattr(settings_request, "read_panel_answer", boom)
    assert run(home, "standard") == 1
    assert "Could not read the app's answer" in capsys.readouterr().out
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))


def test_an_answered_command_leaves_no_request_and_spares_the_next_one(home):
    """Answered means the daemon took ours, so a request under that name
    afterwards is a second command's, and removing it would leave that one
    waiting out its ten seconds for nothing."""
    clock = Clock()
    desk = Desk(home, clock, answer="[cfg] panel set to alternate")
    second = []

    def sleep(s):
        desk.sleep(s)
        if not second and os.path.exists(
                private(home, settings_request.PANEL_ANSWER_FILE)):
            second.append(settings_request.write_panel_request(home,
                                                               "standard"))
    args = types.SimpleNamespace(setting="panel", value="alternate")
    assert cli.cmd_config(args, clock=clock, sleep=sleep) == 0
    assert second
    assert settings_request.take_panel_request(home)["id"] == second[0]
    assert os.listdir(os.path.join(home, private_file.PRIVATE_DIR)) == []


def test_an_answer_with_another_id_is_not_accepted(home, capsys):
    settings_request.write_panel_answer(home, "an-earlier-command", "set")
    assert run(home, "alternate") == 1
    out = capsys.readouterr().out
    assert "panel set to" not in out
    assert "not running" in out
    # Left for the command it answers.
    assert os.path.exists(private(home, settings_request.PANEL_ANSWER_FILE))


def test_port_and_baud_are_gone_from_config(capsys):
    with pytest.raises(SystemExit):
        cli.main(["config", "panel", "alternate", "--port", "/dev/none"])
    with pytest.raises(SystemExit):
        cli.main(["config", "panel", "alternate", "--baud", "9600"])


@pytest.mark.parametrize("argv,needle", [
    (["config", "panel", "on"], "standard or alternate"),
    (["config", "panel", "off"], "standard or alternate"),
    (["config", "status-page", "alternate"], "on or off"),
    (["config", "usage-refresh", "standard"], "on or off"),
])
def test_a_value_for_the_wrong_setting_is_refused_before_anything_is_written(
        home, capsys, argv, needle):
    assert cli.main(argv) == 1
    assert needle in capsys.readouterr().err
    assert not os.path.exists(os.path.join(home, "config.json"))
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))


def test_without_a_value_it_says_the_board_holds_it(home, capsys):
    assert cli.main(["config", "panel"]) == 0
    out = capsys.readouterr().out
    assert "stored on the board" in out
    assert "cannot read it back" in out
    assert "blink config panel standard" in out
    assert not os.path.exists(private(home, settings_request.PANEL_REQUEST_FILE))


def test_the_listing_names_the_panel_setting(capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    row = [l for l in out.splitlines() if l.startswith("panel")]
    assert len(row) == 1 and "stored on the board" in row[0]
    assert "status-page" in out and "usage-refresh" in out


# --- added in the test pass: the daemon's line splitting and failure paths ----

def test_the_answer_survives_every_split_point_and_bare_lf_or_crlf(home):
    line = b"[cfg] panel set to alternate"
    for eol in (b"\n", b"\r\n"):
        data = b"[proto] x\r\n" + line + eol
        for cut in range(1, len(data)):
            rid, desk = take(home, "alternate")
            desk.pickup.heard(data[:cut])
            desk.pickup.heard(data[cut:])
            assert answer_file(home) == {"id": rid, "result": "set"}


def test_an_answer_glued_behind_a_protocol_line_and_noise_is_found(home):
    rid, desk = take(home, "standard")
    desk.pickup.heard(b'{"t":"ping"}\xff\xfe\x00junk[cfg] panel set to standard'
                      b"\r\n[usage] more\r\n")
    assert answer_file(home) == {"id": rid, "result": "set"}


def test_non_utf8_bytes_never_raise_and_never_buffer_without_bound(home):
    rid, desk = take(home, "standard")
    desk.pickup.heard(b"\xff\xfe\x80\n")            # a whole undecodable line
    assert desk.pickup.pending is not None
    for _ in range(50):
        desk.pickup.heard(b"x" * 1000)              # no newline, ever
    assert len(desk.pickup._buf) <= 4096


def test_a_failing_answer_write_does_not_raise_or_leave_it_pending(home,
                                                                   monkeypatch):
    rid, desk = take(home, "standard")

    def boom(*a, **k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(settings_request, "write_panel_answer", boom)
    desk.pickup.heard(b"[cfg] panel set to standard\n")
    assert desk.pickup.pending is None
    assert desk.lines()[-1] == "[panel] answer not written"
    assert "28" not in desk.log.getvalue()

    def refused(*a, **k):
        raise private_file.NotPrivate("x")
    monkeypatch.setattr(settings_request, "write_panel_answer", refused)
    settings_request.write_panel_request(home, "alternate")
    desk.pickup.tick("usb", desk.writer)
    due = desk.pickup.pending[2]
    desk.pickup.clock = lambda: due + 1
    desk.pickup.tick("usb", desk.writer)            # the deadline path too
    assert desk.pickup.pending is None
    assert desk.lines()[-1] == "[panel] answer not written"


def test_a_writer_that_raises_leaves_it_pending_for_link_down(home):
    # The serial loop's handler runs choose(), which calls link_down().
    settings_request.write_panel_request(home, "standard")
    desk = Desk(home, Clock())

    def gone(m):
        raise OSError("port vanished")
    with pytest.raises(OSError):
        desk.pickup.tick("usb", gone)
    assert desk.pickup.pending is not None
    desk.pickup.link_down()
    assert desk.pickup.pending is None


def test_a_late_board_line_after_link_down_is_dropped(home):
    rid, desk = take(home, "standard")
    desk.pickup.link_down()
    assert answer_file(home) == {"id": rid, "result": "no_board"}
    desk.pickup.heard(b"[cfg] panel set to standard\n")     # late: dropped
    assert answer_file(home) == {"id": rid, "result": "no_board"}


@pytest.mark.parametrize("obj", [
    "not json at all", [], None, 7, {"id": 1, "result": "set"},
    {"id": "x", "result": ["set"]}, {"id": "x", "result": "[cfg] panel set to"},
    {"id": "x", "result": "set", "extra": 1}, {"result": "set"},
])
def test_a_hostile_answer_file_is_neither_trusted_nor_a_traceback(
        home, capsys, obj):
    rid = settings_request.write_panel_request(home, "standard")
    inside(home, private(home, settings_request.PANEL_ANSWER_FILE))
    raw = obj.encode() if isinstance(obj, str) else json.dumps(obj).encode()
    private_file.write_private(home, settings_request.PANEL_ANSWER_FILE, raw)
    assert settings_request.read_panel_answer(home, rid) is None
    assert run(home, "standard", clock=Clock()) == 1
    assert "not running" in capsys.readouterr().out


def test_the_cli_says_plainly_when_it_cannot_leave_the_request(home, capsys,
                                                               monkeypatch):
    def boom(*a, **k):
        raise private_file.NotPrivate("private directory not owner-only")
    monkeypatch.setattr(settings_request, "write_panel_request", boom)
    assert run(home, "standard") == 1
    assert "Could not leave the request" in capsys.readouterr().out


def test_the_cli_says_plainly_when_the_answer_cannot_be_read(home, capsys,
                                                            monkeypatch):
    def boom(*a, **k):
        raise OSError("denied")
    monkeypatch.setattr(settings_request, "read_panel_answer", boom)
    assert run(home, "standard") == 1
    assert "Could not read the app's answer" in capsys.readouterr().out


# --- added in the hardening test pass (abandoned requests) --------------------

def test_the_whole_path_on_the_real_clock_is_taken_within_the_limit(home,
                                                                    capsys):
    """CLI writes (time.time), PanelPickup takes with the default clock, the
    answer is read: a fresh request must never be judged stale."""
    clock = Clock()
    desk = Desk(home, clock, answer="[cfg] panel set to alternate")
    assert run(home, "alternate", desk, clock) == 0
    assert desk.lines() == ["[panel] request taken",
                            "[panel] answered (set)"]
    assert "panel set to alternate" in capsys.readouterr().out


def test_a_timeout_whose_cleanup_is_refused_still_gives_the_message(
        home, capsys, monkeypatch):
    def refuse(*a, **k):
        raise private_file.NotPrivate("x")
    monkeypatch.setattr(private_file, "remove_private", refuse)
    assert run(home, "alternate") == 1
    assert "The app is not running" in capsys.readouterr().out


def test_the_settings_request_shape_is_untouched(home):
    token, rid = settings_request.write_request(home, "board")
    with open(private(home, settings_request.REQUEST_FILE),
              encoding="utf-8") as f:
        assert json.load(f) == {"panel": "board", "k": token, "id": rid}
    assert settings_request.take_request(home) == {
        "panel": "board", "k": token, "id": rid}


def test_the_panel_taker_refuses_a_settings_request(home):
    leave(home, settings_request.PANEL_REQUEST_FILE,
          {"panel": "look", "k": "k" * 40, "id": "x", "t": NOW})
    assert settings_request.take_panel_request(home, now=lambda: NOW) is None


def test_a_stale_request_leaves_nothing_on_the_log_or_stderr(home, capsys):
    old = time.time() - 3 * 86400
    settings_request.write_panel_request(home, "alternate", now=lambda: old)
    desk = Desk(home, Clock())
    desk.pickup.tick("usb", desk.writer)
    out, err = capsys.readouterr()
    assert desk.log.getvalue() == ""
    assert (out, err) == ("", "")


def test_importing_settings_request_resolves_no_home():
    import subprocess
    import sys
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    code = (
        "import os, sys\n"
        "sys.path.insert(0, sys.argv[1])\n"
        "def no(*a, **k): raise SystemExit('resolved a home at import')\n"
        "os.path.expanduser = no\n"
        "try:\n"
        "    import pwd\n"
        "    pwd.getpwuid = no\n"
        "except ImportError:\n"
        "    pass\n"
        "import pc.settings_request\n")
    r = subprocess.run([sys.executable, "-s", "-c", code, root], cwd=root,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == ""
