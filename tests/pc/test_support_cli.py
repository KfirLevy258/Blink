"""`blink support` (issue #20): the zip it writes and what it keeps out.

Every log line and status row here is written by this file; nothing is
captured from a real desk. The planted values are the shapes that have
reached bridge.log before (whole messages until 2026-10-30, an unquoted
SSID, a "[usage] session" line with its figures) plus the ones a pattern
list would miss: an id with no hex digit in it, a bare user name, a usage
figure shaped like a version, names made of ordinary words ("store build",
"Kfir Office"), and values that are words our own lines use: a board whose
id is "running", a network called "network".
"""
import ast
import json
import os
import re
import time
import zipfile

import pytest

from pc import cli, logbook, support
from tests.support.home import inside

FIGURE = "8675309"           # a usage figure nothing else in the bundle has
VERSION_FIGURE = "42.17.93"  # a usage figure shaped like a version
USER = "zorblat"
UUID = "3f2a9c1e-1b2c-4d5e-8f90-123456789abc"
HEX = "deadbeefcafebabe1234"
SSID = "home-net-SECRET"
PORT = "/dev/cu.usbserial-A1B2C3"
WORD_ID = "glimmerwick"      # an id that passes every shape rule
BOARD_ID = "store build"     # a name made of words our own lines print
WIRE = '{"t":"usage","s":%s}' % FIGURE
SECRETS = [FIGURE, VERSION_FIGURE, "42.17", USER, UUID, "3f2a9c1e", HEX,
           SSID, "SECRET", "usbserial", BOARD_ID, "store", WORD_ID,
           "Kfir", "Office", "secret-value", '"t"', "'t'", "{", "Wire",
           "nottag", "Traceback", "network", "joined"]

LEGACY = [    # bridge.log.1: as the daemon printed before 2026-10-30
    '2026-10-29 09:00:00 [bridge] <- {"t":"usage","session_pct":%s,'
    '"id":"%s"}' % (FIGURE, UUID),
    "2026-10-29 09:00:01 [bridge] <- {'t': 'ping', 'up_ms': %s}" % FIGURE,
    WIRE,
    "[cfg] mode=1 ssid=%s token=present" % SSID,
    "[usage] session %s%% (12s) STALE" % FIGURE,
    "[usage] weekly %s used" % VERSION_FIGURE,
    "[wifi] joined network",
]
CURRENT = [   # bridge.log
    "2026-11-24 10:00:00 [bridge] -> usage",
    "2026-11-24 10:00:01 [bridge] session %s device %s" % (UUID, HEX),
    "2026-11-24 10:00:02 [bridge] hello %s at HOMEDIR/.blink/x" % USER,
    "Traceback (most recent call last):",
    "ValueError: secret-value-%s" % FIGURE,
    "2026-11-24 10:00:03 [ota] update landed 1.5.1",
    "2026-11-24 10:00:04 [bridge] board %s ready" % WORD_ID,
    "2026-11-24 10:00:05 [bridge] connected on %s" % BOARD_ID,
    "2026-11-24 10:00:06 [nottag] update landed",
    "2026-11-24 10:00:07 [bridge] ota: flashed 1.5.1",
    "2026-11-24 10:00:08 [bridge] connected on running",
    # The board's console as logbook.console_line logs it today: constants.
    "2026-11-24 10:00:09 [ota] update REVERTED",
    "2026-11-24 10:00:10 rst:0xf (BROWNOUT_RESET)",
]
STATUS = [    # what a `blink status` could print, values planted
    f"Bridge      running (launchd, pid {FIGURE})",
    "App         " + VERSION_FIGURE,
    f"Board       {PORT} (CH340) -- id running, firmware 1.5.1",
    f"            other serial devices: {PORT} -- version {VERSION_FIGURE}",
    "Link        WiFi, board at 192.168.7.21",
    f"Network     {SSID}",
    "Usage data  fresh (12s old)",
    f"Status line installed inside {BOARD_ID}",
    "Claude Code 2.1.318 (Claude Code)",
    "Codex       Kfir Office",
    "Usage refresh off",
    "",
    "Wire        the next usage message, exactly as the board gets it:",
    WIRE,
]


class Forbidden(BaseException):
    """Not an Exception, so cmd_support's `except Exception` cannot catch
    it and carry on to a success: a forbidden call fails the command."""


class _RunningService:
    """A service that is up and refuses anything but being looked at. A
    call is recorded first, in case something swallowed the refusal."""
    calls = []

    def status(self):
        self.calls.append("status")
        return f"running (launchd, pid {FIGURE}) {PORT}"

    def __getattr__(self, name):
        def refuse(*_a, **_k):
            _RunningService.calls.append(name)
            raise Forbidden(f"blink support called service.{name}")
        return refuse


@pytest.fixture
def desk(tmp_path, monkeypatch, move_home):
    """A home named for the account, as a real one is, with a Desktop, both
    logs, a board.json, a running service, a status full of identifiers and
    no serial port that can be opened."""
    home = tmp_path / USER
    blink = home / ".blink"
    os.makedirs(inside(tmp_path, home / "Desktop"))
    os.makedirs(inside(tmp_path, blink))
    move_home(home)
    for name, lines in (("bridge.log.1", LEGACY), ("bridge.log", CURRENT)):
        with open(inside(home, blink / name), "w", encoding="utf-8") as f:
            f.write("\n".join(lines).replace("HOMEDIR", str(home)) + "\n")
    with open(inside(home, blink / "board.json"), "w",
              encoding="utf-8") as f:
        json.dump({"port": PORT, "board_id": BOARD_ID, "fw": "1.5.1"}, f)
    monkeypatch.setattr(cli, "cmd_status",
                        lambda _args: print("\n".join(STATUS)))
    monkeypatch.setattr(cli, "claude_version", lambda home=None: (
        "2.1.318 (Claude Code)\nApp " + VERSION_FIGURE, (2, 1, 318)))
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    _RunningService.calls = []
    monkeypatch.setattr(cli, "backend", lambda: _RunningService())
    serial = pytest.importorskip("serial")

    def no_port(*_a, **_k):
        _RunningService.calls.append("serial")
        raise Forbidden("blink support opened a serial port")
    monkeypatch.setattr(serial, "Serial", no_port)
    monkeypatch.setattr(serial, "serial_for_url", no_port)
    return home


def _today_zip(folder, suffix=""):
    return folder / "blink-support-{}{}.zip".format(
        time.strftime("%Y-%m-%d"), suffix)


def _members(path):
    with zipfile.ZipFile(path) as z:
        assert z.testzip() is None
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def test_the_zip_holds_what_support_reads_and_nothing_planted(desk, capsys):
    assert cli.main(["support"]) == 0
    files = _members(_today_zip(desk / "Desktop"))
    assert set(files) == {"MANIFEST.txt", "status.txt", "system.txt",
                          "bridge.log"}
    for name, text in files.items():
        for secret in SECRETS + [str(desk)]:
            assert secret not in text, (name, secret)
    # Continuation lines, App, Network, Wire and the message itself are
    # left out; a version in a row is a value like any other.
    assert files["status.txt"] == (
        "Bridge      running <x>\n"
        "Board       <x>\n"
        "Link        WiFi, board at <x>\n"
        "Usage data  fresh <x>\n"
        "Status line installed inside <x>\n"
        "Claude Code <x>\n"
        "Codex       <x>\n"
        "Usage refresh off\n")
    # The versions are the ones the command asked for itself.
    system = files["system.txt"]
    assert "App version " + cli.RELEASE_VERSION + "\n" in system
    assert "Firmware    1.5.1\n" in system
    assert "Claude Code 2.1.318\n" in system
    assert "Service     running <x>\n" in system
    assert files["bridge.log"] == (
        "[cfg] <x>\n[usage] <x>\n[usage] <x>\n[wifi] <x>\n"
        "2026-11-24 10:00:00 [bridge] -> usage\n"
        "2026-11-24 10:00:01 [bridge] <x>\n"
        "2026-11-24 10:00:02 [bridge] <x>\n"
        "2026-11-24 10:00:03 [ota] <x>\n"
        "2026-11-24 10:00:04 [bridge] <x>\n"
        "2026-11-24 10:00:05 [bridge] connected on <x>\n"
        "2026-11-24 10:00:07 [bridge] ota: flashed <x>\n"
        "2026-11-24 10:00:08 [bridge] connected on <x>\n"
        "2026-11-24 10:00:09 [ota] update REVERTED\n"
        "2026-11-24 10:00:10 rst:0xf (BROWNOUT_RESET)\n")
    # Six log lines and five of status, left out and counted.
    assert "(11 of them this time)" in files["MANIFEST.txt"]
    out = capsys.readouterr().out
    assert "Collected   blink status\nCollected   system info\n" \
        "Collected   bridge.log (redacted)\n" in out
    assert "Wrote       ~" + os.sep + "Desktop" + os.sep + \
        "blink-support-" in out
    assert "\n\nSend this file to support." in out
    assert str(desk) not in out
    # The service was looked at and nothing else; no port was opened.
    assert set(_RunningService.calls) == {"status"}


def test_the_phrase_rule():
    redact = support.redact_log_line
    for line in ("[bridge] -> usage", "[bridge] <- hello",
                 "[bridge] -> (unknown type)", "[bridge] board found at <x>",
                 "[update] installing <x>", "[sleep]", "(board line)"):
        assert redact("2026-11-25 10:00:00 " + line) == \
            "2026-11-25 10:00:00 " + line
    # What is written is the phrase as listed, not the line's own letters,
    # and a phrase is its tag's own.
    assert redact("[bridge] CONNECTED   ON somewhere") == \
        "[bridge] connected on <x>"
    assert redact("[sleep] connected on") == "[sleep] <x>"
    # Nothing after the phrase shows, and one that does not open the line
    # is not looked for.
    assert redact("[bridge] connected on running board usage") == \
        "[bridge] connected on <x>"
    assert redact("[bridge] Kfir Office connected on") == "[bridge] <x>"
    # A word is listed whole: one glued to something else is not that word.
    for glued in ("usage=7", "usage42", "--usage", "usage,", "(usage)"):
        assert redact("[bridge] -> " + glued) == "[bridge] <x>", glued
    for value in ("東京", "قهوة", "★★★", "١٢٣", "-"):
        assert redact(f"[bridge] connected on {value}") == \
            "[bridge] connected on <x>", value
    # Not ours: no tag, a tag nobody listed, a whole message behind a tag.
    assert redact("[nottag] hello") is None
    assert redact("plain words") is None
    assert redact("[bridge] <- {'t': 'ping'") is None


def test_a_board_line_is_kept_only_when_it_is_exactly_a_constant():
    redact = support.redact_log_line
    for known in (logbook.CONSOLE_MESSAGES + logbook.CONSOLE_RENDERED
                  + logbook.CONSOLE_EXTERNAL):
        assert redact("2026-11-25 10:00:00 " + known) == \
            "2026-11-25 10:00:00 " + known
    # One word more and it is not the vetted text any longer.
    assert redact("[ota] update landed 1.5.1") == "[ota] <x>"
    assert redact("[wifi] joined network") == "[wifi] <x>"
    assert redact("rst:0xf (BROWNOUT_RESET),boot:0x13") is None


def test_a_status_value_cannot_open_a_row_or_pass_as_a_version():
    # A value that is one of our own words, a figure shaped like a version,
    # and a device description with a line break in it, under every
    # separator splitlines knows.
    text = ("Board       /dev/x (CH340\n"
            "Bridge      %s\n"
            "            version %s app 1.02.03\n"
            "Board       not plugged in (last seen on running)\n"
            "Codex       installed running fresh\n"
            "Usage data  %s old\n" % ((VERSION_FIGURE,) * 3))
    for sep in ("\n", "\r\n", "\r", "\x0b", "\x0c", "\x1c", "\x85",
                " ", " "):
        assert support.status_rows(text.replace("\n", sep)) == (
            "Board       <x>\n"
            "Bridge      <x>\n"
            "Board       not plugged in <x>\n"
            "Codex       <x>\n"
            "Usage data  <x>\n", 1), repr(sep)
    field = support.system_field
    assert field("Python", "3.12.1") == "Python      3.12.1"
    assert field("Firmware", "1.02") == "Firmware    unknown"
    assert field("Firmware", "1.5.1 at 42.17.93") == "Firmware    unknown"
    assert field("Machine", "store-build", support.MACHINES) == \
        "Machine     unknown"


def test_no_phrase_runs_on_past_the_end_of_another():
    # Where one line ends its fixed words the next thing may be filled in,
    # and a longer phrase from there would let that place spell words. The
    # comparison is without the punctuation a word ends in: "on" runs on
    # into "on, but" though "on," is not "on". Such a phrase is kept only
    # when it is the whole text, and no two phrases are the same words.
    keep = support.keep_phrase
    for key, phrases in {**support.PHRASES, **support.STATUS_PHRASES}.items():
        words = [tuple(support._bare(p)) for p in phrases]
        assert all(words) and len(set(words)) == len(words), key
        for phrase, a in zip(phrases, words):
            longer = [b for b in words if b != a and b[:len(a)] == a]
            assert keep(phrase, phrases) == phrase, (key, phrase)
            for b in longer:
                rest = " ".join(b[len(a):])
                assert keep(phrase + " " + rest, phrases) == support.MASK, \
                    (key, phrase, rest)
            if not longer:
                assert keep(phrase + " zorblat", phrases) == \
                    phrase + " " + support.MASK, (key, phrase)
    refresh = support.STATUS_PHRASES["Usage refresh"]
    assert keep("on", refresh) == "on"
    assert keep("on but glimmerwick", refresh) == support.MASK
    assert keep("on, but 3 failed", refresh) == "on, but <x>"
    assert keep("off zorblat", refresh) == support.MASK
    # The board's console tags list only what the daemon prints under them:
    # no single word, which is what a board line's first word could be.
    for tag in logbook.CONSOLE_TAGS:
        assert all(len(p.split()) >= 2 for p in support.PHRASES[tag]), tag


def test_a_second_run_keeps_the_first_bundle(desk, capsys):
    cli.main(["support"])
    first = _today_zip(desk / "Desktop")
    before = first.read_bytes()
    cli.main(["support"])
    cli.main(["support"])
    assert sorted(p.name for p in (desk / "Desktop").iterdir()) == sorted(
        _today_zip(desk, s).name for s in ("", "-2", "-3"))
    assert first.read_bytes() == before


def test_no_log_and_no_desktop_still_writes_a_bundle(tmp_path, capsys):
    assert cli.main(["support"]) == 0
    out = capsys.readouterr().out
    assert "Skipped     bridge.log (none yet)" in out
    # ~/.blink is hidden, so the owner is told so and given the whole path.
    assert "The Desktop folder could not be used" in out
    path = _today_zip(tmp_path / ".blink")
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
    path = _today_zip(desk / ".blink")
    assert "            " + str(path) in out
    assert len(_members(path)) == 4


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
    assert _members(_today_zip(desk / "Desktop"))["status.txt"] == \
        "blink status could not run (RuntimeError)\n"


def test_the_desktop_is_found_where_the_system_puts_it(tmp_path,
                                                       monkeypatch):
    home = str(tmp_path)
    monkeypatch.setattr(support.sys, "platform", "linux")
    os.makedirs(inside(tmp_path, tmp_path / "Schreibtisch"))
    os.makedirs(inside(tmp_path, tmp_path / ".config"))
    with open(inside(tmp_path, tmp_path / ".config" / "user-dirs.dirs"),
              "w", encoding="utf-8") as f:
        f.write('XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n')
    # One separator throughout, whatever the file wrote: on Windows this
    # test's home has "\" and the file's value "/".
    assert support.desktop_dir(home) == os.path.join(home, "Schreibtisch")
    # The registry's answer, when it is inside this home and when not.
    monkeypatch.setattr(support.sys, "platform", "win32")
    onedrive = os.path.join(home, "OneDrive - Acme", "Desktop")
    os.makedirs(inside(tmp_path, onedrive))
    monkeypatch.setattr(support, "_windows_desktop", lambda: onedrive)
    assert support.desktop_dir(home) == onedrive
    monkeypatch.setattr(support, "_windows_desktop",
                        lambda: os.path.join(os.sep, "elsewhere", "Desktop"))
    assert support.desktop_dir(home) is None


def test_phrases_lists_every_tag_our_lines_open_with():
    """A line under a tag support.PHRASES does not list is dropped from
    every bundle, silently, so a tag added to the code has to be added
    there. The tags are read again from the source: the one each string
    literal opens with, docstrings and the inner pieces of an f-string
    left out. Only the tags are held this way, not the phrases."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    paths = [os.path.join(root, n)
             for n in ("claude_usage_bridge.py", "blink_main.py")]
    for d, _dirs, names in os.walk(os.path.join(root, "pc")):
        paths += [os.path.join(d, n) for n in names
                  if n.endswith(".py") and n != "support.py"]
    tags = set(logbook.CONSOLE_TAGS)
    for path in paths:
        with open(path, encoding="utf-8") as f:
            tree = ast.parse(f.read())
        skip = set()
        for n in ast.walk(tree):
            if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
                skip.add(id(n.value))
            if isinstance(n, ast.JoinedStr):
                skip.update(id(c) for c in ast.walk(n) if c is not n)
        for n in ast.walk(tree):
            head = n.values[0] if isinstance(n, ast.JoinedStr) and n.values \
                else n
            if id(n) not in skip and isinstance(head, ast.Constant) \
                    and isinstance(head.value, str):
                m = re.match(r"\[([a-z_-]+)\](?:\s|$)", head.value)
                if m:
                    tags.add(m.group(1))
    listed = set(support.PHRASES)
    assert tags == listed, (
        f"support.PHRASES: add {sorted(tags - listed)}, "
        f"drop {sorted(listed - tags)}")
