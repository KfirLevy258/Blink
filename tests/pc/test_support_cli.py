"""`blink support` (issue #20): the zip it writes and what it keeps out.

Every log line and status row here is written by this file; nothing is
captured from a real desk. The planted values are the shapes that have
reached bridge.log before (whole messages until 2026-10-30, an unquoted SSID
from deleted firmware, a "[usage] session" line with its figures) plus the
ones a pattern list would miss: an id with no hex digit in it, an id made of
letters a-f only, a bare user name, the computer's name as a bare word, a
usage figure shaped like a version number.
"""
import os
import time
import zipfile

import pytest

from pc import cli, support
from tests.support.home import inside

FIGURE = "8675309"          # a usage figure nothing else in the bundle has
VERSION_FIGURE = "42.17.93"  # a usage figure shaped like a version
USER = "zorblat"
HOST = "quimbleton-mbp"     # the computer's name; "quimbleton" is a word
UUID = "3f2a9c1e-1b2c-4d5e-8f90-123456789abc"
HEX = "deadbeefcafebabe1234"
LETTER_HEX = "fadedcafebead"   # a hex id with no digit: letters a-f only
NO_VOWEL = "qxzvbnmtrk"        # a word-length id with no vowel
SHORT_CAPS_ID = "QXZRTPLK"     # eight capitals, as short as a CAPS word
LONG_CAPS_ID = "ZXQWERTYUIOPLKJ"
SSID = "home-net-SECRET"
PORT = "/dev/cu.usbserial-A1B2C3"
BOARD_ID = "kelpfrond"         # a board id that is a plain word
WORD_ID = "glimmerwick"        # an id that passes every shape rule
CUE_FIGURE = "1.02.03"         # a usage figure after a word that names one
WIRE = '{"t":"usage","s":%s}' % FIGURE

LEGACY = [
    # Whole messages, as the daemon printed them before 2026-10-30.
    '2026-10-29 09:00:00 [bridge] <- {"t":"usage","session_pct":%s,'
    '"id":"%s"}' % (FIGURE, UUID),
    "2026-10-29 09:00:01 [bridge] <- {'t': 'ping', 'v': 2, 'up_ms': %s}"
    % FIGURE,
    '{"t":"usage","five_hour":%s}' % FIGURE,
    "[u[bridge] <- {'t': 'ping'",
    "[cfg] mode=1 ssid=%s token=present" % SSID,
    "[usage] session %s%% (12s) STALE" % FIGURE,
    "[usage] session %s used" % VERSION_FIGURE,
    "[wifi] joined %s" % LETTER_HEX,
    "[usage] board %s percent" % CUE_FIGURE,
    "[usage] firmware %s percent" % CUE_FIGURE,
]
CURRENT = [
    "2026-11-24 10:00:00 [bridge] -> usage",
    "2026-11-24 10:00:01 [bridge] session %s" % UUID,
    "2026-11-24 10:00:02 [bridge] device %s" % HEX,
    "2026-11-24 10:00:03 [bridge] board serial %s" % LONG_CAPS_ID,
    "2026-11-24 10:00:04 [bridge] hello %s" % USER,
    "2026-11-24 10:00:05 [proto] ota failed: SuperSecretToken",
    "2026-11-24 10:00:06 [bridge] opened {home}/.blink/x".replace(
        "{home}", "HOMEDIR"),
    "2026-11-24 10:00:07 [bridge] five-hour at %s percent" % FIGURE,
    "Traceback (most recent call last):",
    '  File "HOMEDIR/x.py", line 3, in <module>',
    "ValueError: secret-value-%s" % FIGURE,
    "2026-11-24 10:00:08 [ota] update landed 1.5.1",
    "2026-11-24 10:00:09 [wifi] connecting quimbleton",
    "2026-11-24 10:00:10 [bridge] token %s %s" % (NO_VOWEL, SHORT_CAPS_ID),
    "2026-11-24 10:00:11 [usage] weekly %s -> %s" % (VERSION_FIGURE,
                                                     VERSION_FIGURE),
    "2026-11-24 10:00:12 [bridge] board %s ready" % WORD_ID,
    "2026-11-24 10:00:13 [wifi] network --%s" % WORD_ID,
]


def _secrets(home):
    return [FIGURE, VERSION_FIGURE, "42.17", USER, "quimbleton", UUID,
            "3f2a9c1e", HEX, "deadbeef", LETTER_HEX, NO_VOWEL,
            SHORT_CAPS_ID, LONG_CAPS_ID, SSID, "SECRET", PORT, "usbserial",
            BOARD_ID, WORD_ID, CUE_FIGURE, "1.02", "SuperSecret", "secret-value", str(home), '"t"',
            "'t'", "{", "Wire"]


def _write_logs(home):
    d = os.path.join(str(home), ".blink")
    os.makedirs(inside(home, d), exist_ok=True)
    with open(inside(home, os.path.join(d, "bridge.log.1")), "w") as f:
        f.write("\n".join(LEGACY) + "\n")
    with open(inside(home, os.path.join(d, "bridge.log")), "w") as f:
        f.write("\n".join(l.replace("HOMEDIR", str(home))
                          for l in CURRENT) + "\n")


class Forbidden(BaseException):
    """Not an Exception, so cmd_support's `except Exception` cannot catch
    it and carry on to a success: a forbidden call fails the command."""


class _RunningService:
    """A service that is up, and refuses anything but being looked at.
    Every call is recorded before the refusal, so a call is seen even if
    something were to swallow the refusal."""
    calls = []

    def status(self):
        self.calls.append("status")
        return f"running (pid {FIGURE}, {PORT})"

    def __getattr__(self, name):
        def refuse(*_a, **_k):
            _RunningService.calls.append(name)
            raise Forbidden(f"blink support called service.{name}")
        return refuse


@pytest.fixture
def desk(tmp_path, monkeypatch, move_home):
    """A home with a Desktop, both logs, a running service, a board row
    full of identifiers, and no serial port that can be opened.

    The home folder is named for the account, as a real one is: under
    tmp_path's own name ("test_the_firmware_version_...") the words of the
    test's name would be private words and masked everywhere."""
    home = tmp_path / USER
    os.makedirs(inside(tmp_path, home / "Desktop"))
    move_home(home)
    _write_logs(home)
    monkeypatch.setattr(support.getpass, "getuser", lambda: USER)
    monkeypatch.setattr(support.platform, "node", lambda: HOST)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    _RunningService.calls = []
    monkeypatch.setattr(cli, "backend", lambda: _RunningService())
    monkeypatch.setattr(cli, "_board_lines", lambda: [
        f"Board       {PORT} (CH340) -- id {BOARD_ID}, firmware 1.5.1"])
    serial = pytest.importorskip("serial")
    del OPENED[:]

    def no_port(*a, **_k):
        OPENED.append(len(a))
        raise Forbidden("blink support opened a serial port")
    monkeypatch.setattr(serial, "Serial", no_port)
    monkeypatch.setattr(serial, "serial_for_url", no_port)
    return home


OPENED = []


def _today_zip(home, suffix=""):
    return home / "Desktop" / "blink-support-{}{}.zip".format(
        time.strftime("%Y-%m-%d"), suffix)


def _members(path):
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def test_the_zip_holds_status_system_log_and_manifest(desk, capsys):
    assert cli.main(["support"]) == 0
    files = _members(_today_zip(desk))
    assert set(files) == {"MANIFEST.txt", "status.txt", "system.txt",
                          "bridge.log"}
    assert cli.RELEASE_VERSION in files["system.txt"]
    assert "Firmware    unknown" in files["system.txt"]
    assert cli.RELEASE_VERSION in files["status.txt"]
    assert "running" in files["system.txt"]
    assert "firmware 1.5.1" in files["status.txt"]
    assert "[bridge] -> usage" in files["bridge.log"]
    # The log keeps no number, versions included; system.txt has them.
    assert "[ota] update landed <x>" in files["bridge.log"]


def test_the_firmware_version_comes_from_board_json(desk, capsys):
    import json
    with open(inside(desk, desk / ".blink" / "board.json"), "w") as f:
        json.dump({"port": PORT, "board_id": BOARD_ID, "fw": "1.5.1"}, f)
    cli.main(["support"])
    system = _members(_today_zip(desk))["system.txt"]
    assert "Firmware    1.5.1" in system
    assert BOARD_ID not in system and "usbserial" not in system


def test_nothing_planted_reaches_any_file_of_the_zip(desk, capsys):
    cli.main(["support"])
    (path,) = (desk / "Desktop").iterdir()
    files = _members(path)
    for name, text in files.items():
        for secret in _secrets(desk):
            assert secret not in text, (name, secret)
    # The words around the planted values are still there for support.
    log = files["bridge.log"]
    assert "[wifi] connecting <x>" in log
    assert "[wifi] joined <x>" in log
    assert "[bridge] token <x> <x>" in log
    assert "[usage] session <x> used" in log
    assert "[usage] weekly <x> -> <x>" in log
    assert "[usage] board <x> percent" in log
    assert "[usage] firmware <x> percent" in log
    assert "[bridge] board <x> ready" in log


def test_status_keeps_only_the_rows_it_knows(desk, monkeypatch, capsys):
    def status(_args):
        print("Bridge      running (pid 4242)")
        print("App         1.5.1")
        print(f"Board       {PORT} (CH340) -- id {BOARD_ID}, firmware 1.5.1")
        print(f"            other serial devices: {PORT} (CP2102)")
        print(f"Link        WiFi, board at 192.168.7.21")
        print(f"Network     {SSID} {HOST}")
        print(f"            {USER} continuation of an unknown row")
        print("Usage data  fresh (12s old)")
        print()
        print("Wire        the next usage message, exactly as the board gets"
              " it:")
        print(WIRE)
    monkeypatch.setattr(cli, "cmd_status", status)
    cli.main(["support"])
    files = _members(_today_zip(desk))
    assert files["status.txt"] == (
        "Bridge      running (pid <x>)\n"
        "App         1.5.1\n"
        "Board       <path> (<x>) -- id <x>, firmware 1.5.1\n"
        "            other serial devices: <path> (<x>)\n"
        "Link        WiFi, board at <x>\n"
        "Usage data  fresh (<x> old)\n")
    # Network, its continuation, Wire and the message itself: four rows
    # left out, counted beside the log's own dropped lines.
    buf = __import__("io").StringIO()
    with __import__("contextlib").redirect_stdout(buf):
        status(None)
    assert support.status_rows(buf.getvalue())[1] == 4


def test_the_command_prints_what_it_collected_and_where(desk, capsys):
    cli.main(["support"])
    out = capsys.readouterr().out
    assert "Collected   blink status" in out
    assert "Collected   system info" in out
    assert "Collected   bridge.log (redacted)" in out
    assert "Wrote       ~" + os.sep + "Desktop" + os.sep + \
        "blink-support-" in out
    assert "\n\nSend this file to support." in out
    assert str(desk) not in out


def test_a_second_run_keeps_the_first_bundle(desk, capsys):
    cli.main(["support"])
    first = _today_zip(desk)
    before = first.read_bytes()
    cli.main(["support"])
    cli.main(["support"])
    names = sorted(p.name for p in (desk / "Desktop").iterdir())
    assert names == sorted([first.name, _today_zip(desk, "-2").name,
                            _today_zip(desk, "-3").name])
    assert first.read_bytes() == before


def test_the_running_service_is_only_looked_at(desk, capsys):
    # Forbidden is a BaseException, so a forbidden call would fail main()
    # here rather than be caught; the record is checked as well.
    assert cli.main(["support"]) == 0
    assert _RunningService.calls
    assert set(_RunningService.calls) == {"status"}
    assert OPENED == []


def test_no_log_and_no_desktop_still_writes_a_bundle(tmp_path, capsys):
    assert cli.main(["support"]) == 0
    out = capsys.readouterr().out
    assert "Skipped     bridge.log (none yet)" in out
    (path,) = (tmp_path / ".blink").glob("blink-support-*.zip")
    assert _members(path)["bridge.log"] == ""


def test_a_status_that_fails_leaves_only_its_type(desk, monkeypatch, capsys):
    def boom(_args):
        print(f"Board       {PORT}")
        raise RuntimeError(f"read {FIGURE} from {PORT}")
    monkeypatch.setattr(cli, "cmd_status", boom)
    assert cli.main(["support"]) == 0
    (path,) = (desk / "Desktop").iterdir()
    status = _members(path)["status.txt"]
    assert status == "blink status could not run (RuntimeError)\n"


def test_the_token_rule():
    keep = support.scrub_text
    assert keep("Bridge      running") == "Bridge      running"
    assert keep("App         1.5.1") == "App         1.5.1"
    assert keep("Claude Code 2.1.318") == "Claude Code 2.1.318"
    assert keep("firmware 1.5.0 -> 1.5.1") == "firmware 1.5.0 -> 1.5.1"
    assert keep("update 1.5.0") == "update <x>"
    assert keep("board 1.02.03") == "board <x>"
    assert keep("firmware 1.5.1", versions=False) == "firmware <x>"
    assert keep("session 42.17.93") == "session <x>"
    assert keep("42.17.93 -> 1.5.1") == "<x> -> <x>"
    assert keep("run `blink install`") == "run `blink install`"
    assert keep("HOST LOST") == "HOST LOST"
    assert keep("session 42%") == "session <x>"
    assert keep("cost 1.50") == "cost <x>"
    assert keep("ssid=mine") == "ssid=<x>"
    assert keep("at C:\\Users\\a") == "at <path>"
    assert keep("email a@b.example") == "email <x>"
    # A private word is masked even when our own code prints it too.
    assert keep("hello board", frozenset({"board"})) == "hello <x>"
    assert keep("hello zorblat", frozenset({"zorblat"})) == "hello <x>"
    assert keep("`claude --version`") == "`claude --version`"
    assert keep("busy/idle USB/WiFi") == "busy/idle USB/WiFi"
    assert keep("/dev/ttyUSB0 a/b2") == "<path> <path>"
    assert keep("`ABC123`") == "`<x>`"
    assert keep("ab/zorblat", frozenset({"zorblat"})) == "<path>"
    assert keep("id fadedcafebead") == "id <x>"
    assert keep("id CAFEBABE") == "id <x>"
    assert keep("id qxzvbnmtrk QXZRTPLK") == "id <x> <x>"
    assert keep("id abcdefghijklmnopq") == "id <x>"
    # A word is kept only if our own code prints it, whatever its shape.
    assert keep("board kelpfrond glimmerwick") == "board <x> <x>"
    assert keep("--glimmerwick") == "<x>"
    assert "glimmerwick" not in support.vocabulary()
    assert {"board", "session", "running"} <= support.vocabulary()
    assert support.redact_log_line("[nottag] hello") is None
    assert support.redact_log_line("plain words") is None


def test_the_hostname_is_masked_as_a_bare_word(monkeypatch):
    monkeypatch.setattr(support.platform, "node", lambda: HOST)
    monkeypatch.setattr(support.getpass, "getuser", lambda: USER)
    private = support.private_words("/somewhere/" + USER)
    assert support.redact_log_line(
        "[wifi] connecting quimbleton for zorblat", private) == \
        "[wifi] connecting <x> for <x>"
    # "mbp" has no vowel and is masked by the id rule anyway; the common
    # hostname words are not private, so ordinary lines keep them.
    assert "pro" not in support.private_words("/x/MacBook-Pro")
