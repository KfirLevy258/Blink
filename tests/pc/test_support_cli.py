"""`blink support` (issue #20): the zip it writes and what it keeps out.

Every log line and status row here is written by this file; nothing is
captured from a real desk. The planted values are the shapes that have
reached bridge.log before (whole messages until 2026-10-30, an unquoted SSID
from deleted firmware, a "[usage] session" line with its figures) plus the
ones a pattern list would miss: an id with no hex digit in it, an id made of
letters a-f only, a bare user name, the computer's name as a bare word, a
usage figure shaped like a version number, names made of ordinary words
("store build", "Build Release", "Kfir Office"), and values that are words
our own lines use: a board whose id is "running", a network called
"network".
"""
import ast
import os
import re
import time
import zipfile

import pytest

from pc import cli, logbook, support
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
SSID = "home-net-SECRET"
PORT = "/dev/cu.usbserial-A1B2C3"
WORD_ID = "glimmerwick"        # an id that passes every shape rule
CUE_FIGURE = "1.02.03"         # a usage figure after a word that names one
# Names made of ordinary words, which our own lines print elsewhere.
BOARD_ID = "store build"
PROJECT = "Build Release"
FOLDER = "build/release"
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
    "[wifi] joined network",
    "[wifi] joined running on board",
    "[proto] host connected failed",
]
CURRENT = [
    "2026-11-24 10:00:00 [bridge] -> usage",
    "2026-11-24 10:00:01 [bridge] session %s" % UUID,
    "2026-11-24 10:00:02 [bridge] device %s" % HEX,
    "2026-11-24 10:00:04 [bridge] hello %s" % USER,
    "2026-11-24 10:00:05 [proto] ota failed: SuperSecretToken",
    "2026-11-24 10:00:06 [bridge] opened HOMEDIR/.blink/x",
    "2026-11-24 10:00:07 [bridge] five-hour at %s percent" % FIGURE,
    "Traceback (most recent call last):",
    '  File "HOMEDIR/x.py", line 3, in <module>',
    "ValueError: secret-value-%s" % FIGURE,
    "2026-11-24 10:00:08 [ota] update landed 1.5.1",
    "2026-11-24 10:00:09 [wifi] listening quimbleton",
    "2026-11-24 10:00:10 [bridge] token %s %s" % (NO_VOWEL, SHORT_CAPS_ID),
    "2026-11-24 10:00:11 [usage] weekly %s -> %s" % (VERSION_FIGURE,
                                                     VERSION_FIGURE),
    "2026-11-24 10:00:12 [bridge] board %s ready" % WORD_ID,
    "2026-11-24 10:00:13 [wifi] network --%s" % WORD_ID,
    "2026-11-24 10:00:14 [bridge] connected on %s" % BOARD_ID,
    "2026-11-24 10:00:15 [ingest] Desktop data folder: %s" % PROJECT,
    "2026-11-24 10:00:16 [bridge] board found at %s" % FOLDER,
    "2026-11-24 10:00:17 [bridge] <- %s" % WORD_ID,
    "2026-11-24 10:00:18 [nottag] update landed",
    "2026-11-24 10:00:19 [bridge] ota: flashing 1.5.1",
    "2026-11-24 10:00:20 [bridge] connected on running",
]


def _secrets(home):
    return [FIGURE, VERSION_FIGURE, "42.17", USER, "quimbleton", UUID,
            "3f2a9c1e", HEX, "deadbeef", LETTER_HEX, NO_VOWEL,
            SHORT_CAPS_ID, SSID, "SECRET", PORT, "usbserial", BOARD_ID,
            "store", WORD_ID, CUE_FIGURE, "1.02", "SuperSecret",
            "secret-value", PROJECT, "Release", FOLDER, "release",
            str(home), '"t"', "'t'", "{", "Wire", "nottag"]


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
        return f"running (launchd, pid {FIGURE}) {PORT}"

    def __getattr__(self, name):
        def refuse(*_a, **_k):
            _RunningService.calls.append(name)
            raise Forbidden(f"blink support called service.{name}")
        return refuse


OPENED = []


@pytest.fixture
def desk(tmp_path, monkeypatch, move_home):
    """A home with a Desktop, both logs, a running service, a board row
    full of identifiers, and no serial port that can be opened.

    The home folder is named for the account, as a real one is."""
    home = tmp_path / USER
    os.makedirs(inside(tmp_path, home / "Desktop"))
    move_home(home)
    _write_logs(home)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    _RunningService.calls = []
    monkeypatch.setattr(cli, "backend", lambda: _RunningService())
    # The real board_lines, so the row is the one our code prints.
    monkeypatch.setattr(cli, "_board_lines", lambda: cli.board_lines(
        {"port": PORT, "board_id": BOARD_ID, "fw": "1.5.1"},
        [(PORT, "CH340"), ("/dev/cu.%s" % FOLDER, "CP2102")]))
    serial = pytest.importorskip("serial")
    del OPENED[:]

    def no_port(*a, **_k):
        OPENED.append(len(a))
        raise Forbidden("blink support opened a serial port")
    monkeypatch.setattr(serial, "Serial", no_port)
    monkeypatch.setattr(serial, "serial_for_url", no_port)
    return home


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
    assert "App version " + cli.RELEASE_VERSION in files["system.txt"]
    assert "Firmware    unknown" in files["system.txt"]
    assert "Service     running <x>\n" in files["system.txt"]
    assert "App         " + cli.RELEASE_VERSION in files["status.txt"]
    assert "Board       <x>\n" in files["status.txt"]
    # What support reads a log for: which messages went, and what happened.
    log = files["bridge.log"]
    assert "2026-11-24 10:00:00 [bridge] -> usage" in log
    assert "2026-11-24 10:00:19 [bridge] ota: flashing <x>" in log
    # The board's own lines are their tag, as logbook.console_line cuts
    # them today, however much an old log has behind it.
    assert "2026-11-24 10:00:08 [ota] <x>" in log
    assert "[usage] <x>" in log and "session" not in log


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
    log = files["bridge.log"]
    assert "10:00:04 [bridge] <x>" in log
    assert "10:00:14 [bridge] connected on <x>" in log
    assert "10:00:16 [bridge] board found at <x>" in log
    assert "10:00:17 [bridge] <x>" in log
    assert "[cfg] <x>" in log and "[wifi] <x>" in log
    # A value that is a word our own lines use is a value all the same.
    assert "network" not in log and "joined" not in log
    assert "running" not in log and "failed" not in log
    assert "10:00:20 [bridge] connected on <x>" in log
    # Whole messages, the traceback and the line under a tag that is not
    # ours are gone, and counted.
    assert "Traceback" not in log and "ValueError" not in log
    assert support.log_tail(str(desk / ".blink"))[1] == 8


def test_status_keeps_only_the_rows_it_knows(desk, monkeypatch, capsys):
    def status(_args):
        print(f"Bridge      running (launchd, pid {FIGURE})")
        print("App         1.5.1")
        print(f"Board       {PORT} (CH340) -- id {BOARD_ID}, firmware 1.5.1")
        print(f"            other serial devices: {PORT} (CP2102)"
              " -- asked once, not BLINK, left alone")
        print("Link        WiFi, board at 192.168.7.21")
        print(f"Network     {SSID} {HOST}")
        print(f"            {USER} continuation of an unknown row")
        print("Usage data  fresh (12s old)")
        print(f"Status line installed inside {PROJECT}")
        print(f"Status line installed at {FOLDER}")
        print(f"Claude Code WSL ({BOARD_ID}), not running")
        print("Codex       Kfir Office")
        print("Usage refresh off")
        print()
        print("Wire        the next usage message, exactly as the board gets"
              " it:")
        print(WIRE)
    monkeypatch.setattr(cli, "cmd_status", status)
    cli.main(["support"])
    files = _members(_today_zip(desk))
    assert files["status.txt"] == (
        "Bridge      running <x>\n"
        "App         1.5.1\n"
        "Board       <x>\n"
        "Link        WiFi, board at <x>\n"
        "Usage data  fresh <x>\n"
        "Status line installed inside <x>\n"
        "Status line installed at <x>\n"
        "Claude Code WSL <x>\n"
        "Codex       <x>\n"
        "Usage refresh off\n")
    # Two continuation lines, Network, Wire and the message itself: five
    # left out, counted beside the log's own dropped lines.
    assert "(13 of them this time)" in files["MANIFEST.txt"]


def test_a_continuation_line_never_carries_a_version():
    # `blink status` once printed these under a row, and a usage figure
    # shaped like a version came through as one.
    text = ("App         1.5.1\n"
            "            version 42.17.93 app 1.02.03\n"
            "Claude Code 2.1.318 (Claude Code)\n"
            "            the board is on 42.17.93 and this app is 1.02.03\n"
            "Board       not plugged in -- firmware 1.5.1\n"
            "Usage data  42.17.93 old\n"
            "Claude Code not found 1.02.03\n")
    kept, dropped = support.status_rows(text)
    assert kept == ("App         1.5.1\n"
                    "Claude Code 2.1.318 <x>\n"
                    "Board       not plugged in <x>\n"
                    "Usage data  <x>\n"
                    "Claude Code not found <x>\n")
    assert dropped == 2


def test_the_phrase_rule():
    redact = support.redact_log_line
    # The time, the tag, and the phrase the line opens with.
    for line in ("[bridge] -> usage", "[bridge] <- hello",
                 "[bridge] -> (unknown type)", "[bridge] board found at <x>",
                 "[bridge] ota: FAILED: <x>", "[update] installing <x>",
                 "[wifi] link lost <x>", "[sleep]", "[plan]"):
        assert redact("2026-11-25 10:00:00 " + line) == \
            "2026-11-25 10:00:00 " + line
    assert redact("2026-11-24 10:00:00 (board line)") == \
        "2026-11-24 10:00:00 (board line)"
    # What is written is the phrase as listed, not the line's own letters.
    assert redact("[bridge] CONNECTED   ON somewhere") == \
        "[bridge] connected on <x>"
    # A phrase is the tag's own: another tag's is a value like any other.
    assert redact("[sleep] connected on") == "[sleep] <x>"
    assert redact("[plan] session 42%") == "[plan] <x>"
    # Nothing after the phrase shows, whatever it says, and a phrase that
    # does not open the line is not looked for.
    assert redact("[bridge] connected on running board usage") == \
        "[bridge] connected on <x>"
    assert redact("[bridge] Kfir Office connected on") == "[bridge] <x>"
    # A word is listed whole: one glued to something else is not that word.
    for glued in ("usage=7", "usage/7", "usage42", "--usage", "us:age",
                  "usage,", "(usage)"):
        assert redact("[bridge] -> " + glued) == "[bridge] <x>", glued
    # Not ours: no tag, a tag nobody listed, a whole message behind a tag.
    assert redact("[nottag] hello") is None
    assert redact("plain words") is None
    assert redact("[bridge] <- {'t': 'ping'") is None
    # Whatever alphabet a value is in, and bare punctuation too.
    for value in ("東京", "Ωμέγα", "قهوة", "★★★", "١٢٣", "!!!", "-", "(:)"):
        assert redact(f"[bridge] connected on {value}") == \
            "[bridge] connected on <x>", value
    # Thousands of words on one line are still one pass.
    assert redact("[bridge] " + "running " * 5000 + "zz") == "[bridge] <x>"


def test_a_value_that_is_one_of_our_words_does_not_show():
    # Both got through while a listed word was kept wherever it stood: a
    # board whose id is "running" in the Board row, and a network called
    # "network" behind an old "[wifi] joined".
    rows, _ = support.status_rows(
        f"Board       {PORT} (CH340) -- id running, firmware 1.5.1\n"
        "Board       not plugged in (last seen on running)\n"
        "Bridge      registered but FAILING -- exits with code 78\n"
        "Codex       installed running fresh\n")
    assert rows == ("Board       <x>\n"
                    "Board       not plugged in <x>\n"
                    "Bridge      registered but FAILING <x>\n"
                    "Codex       <x>\n")
    assert support.redact_log_line("[wifi] joined network") == "[wifi] <x>"
    assert support.redact_log_line("[usage] session applied") == \
        "[usage] <x>"


def test_no_phrase_runs_on_past_the_end_of_another():
    # Where one line ends its fixed words, the next thing may be filled in:
    # "on, but 3 failed". A second phrase that went on from there ("on, but
    # Claude Code wasn't found") would let that place spell words.
    lists = dict(support.PHRASES)
    lists.update(support.STATUS_PHRASES)
    for key, phrases in lists.items():
        words = [p.lower().split() for p in phrases]
        assert all(words), key
        assert len(set(map(tuple, words))) == len(words), key
        for a in words:
            for b in words:
                assert a is b or b[:len(a)] != a, (key, a, b)
    # The board's console tags list only what the daemon prints under them:
    # no single word, which is what a board line's first word could be.
    for tag in logbook.CONSOLE_TAGS:
        for p in support.PHRASES[tag]:
            assert len(p.split()) >= 2, (tag, p)


def test_system_fields_keep_a_version_only_whole():
    field = support.system_field
    assert field("Python", "3.12.1", version=True) == "Python      3.12.1"
    assert field("Firmware", "1.02", version=True) == "Firmware    unknown"
    assert field("Firmware", "1.5.1 at 42.17.93", version=True) == \
        "Firmware    unknown"
    assert field("Service", "running (launchd, pid 4242)") == \
        "Service     running <x>"
    assert field("Service", "store build is running") == "Service     <x>"
    assert field("System", "Darwin", allowed=support.SYSTEMS) == \
        "System      Darwin"
    assert field("Machine", "store-build", allowed=support.MACHINES) == \
        "Machine     unknown"


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
    # ~/.blink is hidden, so the owner is told so and given the whole path.
    assert "The Desktop folder could not be used" in out
    (path,) = (tmp_path / ".blink").glob("blink-support-*.zip")
    assert "            " + str(path) in out
    assert _members(path)["bridge.log"] == ""


def test_a_desktop_that_refuses_the_write_is_not_a_traceback(
        desk, monkeypatch, capsys):
    # macOS, with Terminal refused access to the Desktop folder: the folder
    # is there and the write is a PermissionError.
    real = os.open

    def refuse_desktop(path, flags, *a, **k):
        if os.sep + "Desktop" + os.sep in str(path):
            raise PermissionError(1, "Operation not permitted")
        return real(path, flags, *a, **k)
    monkeypatch.setattr(os, "open", refuse_desktop)
    assert cli.main(["support"]) == 0
    out = capsys.readouterr().out
    assert "The Desktop folder could not be used" in out
    assert list((desk / "Desktop").iterdir()) == []
    (path,) = (desk / ".blink").glob("blink-support-*.zip")
    assert "            " + str(path) in out
    assert set(_members(path)) == {"MANIFEST.txt", "status.txt",
                                   "system.txt", "bridge.log"}


def test_a_write_that_fails_leaves_no_half_file(tmp_path, monkeypatch):
    def full(*_a, **_k):
        raise OSError(28, "No space left on device")
    monkeypatch.setattr(zipfile.ZipFile, "writestr", full)
    with pytest.raises(OSError):
        support.build_bundle(str(tmp_path / "out"), "s\n", "y\n", [])
    assert list((tmp_path / "out").iterdir()) == []


def test_a_status_that_fails_leaves_only_its_type(desk, monkeypatch, capsys):
    def boom(_args):
        print(f"Board       {PORT}")
        raise RuntimeError(f"read {FIGURE} from {PORT}")
    monkeypatch.setattr(cli, "cmd_status", boom)
    assert cli.main(["support"]) == 0
    (path,) = (desk / "Desktop").iterdir()
    status = _members(path)["status.txt"]
    assert status == "blink status could not run (RuntimeError)\n"


def test_the_desktop_is_found_where_the_system_puts_it(tmp_path,
                                                       monkeypatch):
    home = str(tmp_path)
    monkeypatch.setattr(support.sys, "platform", "linux")
    os.makedirs(inside(tmp_path, tmp_path / "Schreibtisch"))
    os.makedirs(inside(tmp_path, tmp_path / ".config"))
    with open(inside(tmp_path, tmp_path / ".config" / "user-dirs.dirs"),
              "w") as f:
        f.write('XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n')
    # One separator throughout, whatever the file wrote: on Windows this
    # test's home has "\" and the file's value "/".
    assert support.desktop_dir(home) == os.path.join(home, "Schreibtisch")

    monkeypatch.setattr(support.sys, "platform", "win32")
    monkeypatch.setattr(support, "_windows_desktop",
                        lambda: os.path.join(os.sep, "elsewhere", "Desktop"))
    assert support.desktop_dir(home) is None   # outside home: not taken
    os.makedirs(inside(tmp_path, tmp_path / "OneDrive - Acme" / "Desktop"))
    assert support.desktop_dir(home) == os.path.join(
        home, "OneDrive - Acme", "Desktop")


# --- the tags are derived again from the source -----------------------------
#
# A line under a tag support.PHRASES does not list is dropped from every
# bundle, silently, so a tag added to the code has to be added there. Only
# the tags are held to the source this way; the phrases under each are a
# person's choice, and a changed log line fails nothing here.

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
_TAG_AT_START = re.compile(r"^\[([a-z_-]+)\](?:\s|$)")


def _sources():
    out = ["claude_usage_bridge.py", "blink_main.py"]
    for d, dirs, files in os.walk(os.path.join(ROOT, "pc")):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        out += [os.path.relpath(os.path.join(d, f), ROOT)
                for f in sorted(files) if f.endswith(".py")]
    out.remove(os.path.join("pc", "support.py"))
    return out


def _tags_in_source():
    """The tag each string literal opens with, docstrings and the inner
    pieces of an f-string left out."""
    tags = set(logbook.CONSOLE_TAGS)
    for rel in _sources():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        skip = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
                skip.add(id(n.value))
            if isinstance(n, ast.JoinedStr):
                skip.update(id(c) for c in ast.walk(n) if c is not n)
        for n in ast.walk(tree):
            if id(n) in skip:
                continue
            head = n.values[0] if isinstance(n, ast.JoinedStr) and n.values \
                else n
            if isinstance(head, ast.Constant) and isinstance(head.value, str):
                m = _TAG_AT_START.match(head.value)
                if m:
                    tags.add(m.group(1))
    return tags


def test_events_lists_every_tag_our_lines_open_with():
    tags = _tags_in_source()
    assert tags == set(support.PHRASES), (
        "support.PHRASES differs from the [tag] literals in the source: add "
        f"{sorted(tags - set(support.PHRASES))}, drop "
        f"{sorted(set(support.PHRASES) - tags)}")
    assert support.LOG_TAGS == set(support.PHRASES)


# One run in the shape a built binary has: every module of ours compiled to
# bytecode inside a zip, with no .py source beside it (PyInstaller's PYZ
# holds the same), sys.frozen set, imported by an isolated interpreter (-I:
# no cwd, no site, no PYTHON* variables), with sockets refused. The lists are
# constants, so this is expected to pass; it is here because an earlier
# shape read them from our own code at run time, where an empty answer under
# PyInstaller would have masked every word. `sh tools/build_binary.sh`
# itself is still the owner's to try once.
FROZEN_CHILD = r'''
import os, socket, sys, zipfile
sys.path[:0] = [sys.argv[1]]
sys.frozen = True
def refuse(*a, **k):
    raise SystemExit("frozen child reached for the network")
socket.socket.connect = refuse
socket.getaddrinfo = refuse
from pc import cli, support
assert support.__spec__.origin.startswith(sys.argv[1]), support.__spec__.origin
cli._skip_service = lambda: True
cli._board_lines = lambda: []
sys.exit(cli.main(["support"]))
'''


def _bytecode_zip(path):
    """Every module of ours as bytecode in `path`, the way PYZ holds them."""
    import importlib.util
    import marshal
    with zipfile.ZipFile(path, "w") as z:
        for rel in _sources() + [os.path.join("pc", "support.py")]:
            with open(os.path.join(ROOT, rel), "rb") as f:
                code = compile(f.read(), rel, "exec", dont_inherit=True)
            header = importlib.util.MAGIC_NUMBER + bytes(12)
            z.writestr(rel.replace(os.sep, "/") + "c",
                       header + marshal.dumps(code))
    return path


def test_a_frozen_shaped_build_keeps_our_words_and_masks_the_rest(desk):
    import subprocess
    import sys
    pyz = _bytecode_zip(inside(desk, desk / "blink.pyz"))
    with zipfile.ZipFile(pyz) as z:
        assert not [n for n in z.namelist() if n.endswith(".py")]
        assert "pc/support.pyc" in z.namelist()
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
    run = subprocess.run([sys.executable, "-I", "-c", FROZEN_CHILD, pyz],
                         cwd=str(desk), env=env, capture_output=True,
                         text=True, encoding="utf-8", timeout=120)
    assert run.returncode == 0, run.stderr[-2000:]
    assert "Collected   bridge.log (redacted)" in run.stdout
    files = _members(_today_zip(desk))
    assert "Frozen      yes" in files["system.txt"]
    assert "[bridge] -> usage" in files["bridge.log"]
    assert "[bridge] connected on <x>" in files["bridge.log"]
    for secret in _secrets(desk):
        for name, text in files.items():
            assert secret not in text, (secret, name)
