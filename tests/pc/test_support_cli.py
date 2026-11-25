"""`blink support` (issue #20): the zip it writes and what it keeps out.

Every log line and status row here is written by this file; nothing is
captured from a real desk. The planted values are the shapes that have
reached bridge.log before (whole messages until 2026-10-30, an unquoted SSID
from deleted firmware, a "[usage] session" line with its figures) plus the
ones a word or pattern list would miss: an id with no hex digit in it, an id
made of letters a-f only, a bare user name, the computer's name as a bare
word, a usage figure shaped like a version number, and -- the review of
round 4 -- names and folders made only of words our own lines print
("store build", "Build Release", "build/release"), an account called
"desktop" and a computer called "WiFi".
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
WORD_ID = "glimmerwick"        # an id that passes every shape rule
CUE_FIGURE = "1.02.03"         # a usage figure after a word that names one
# Names made only of words our own lines print, in an order none of them
# has: a board id, a Codex project, a folder. A word list keeps every one.
BOARD_ID = "store build"
PROJECT = "Build Release"
FOLDER = "build/release"
# A name that is exactly one of our own whole lines (pc/cli.py prints it
# for a WSL distribution that is gone): the review of round 5 found that a
# value equal to one of our pieces came through.
OUR_TEXT = "is not on this computer"
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
    "2026-11-24 10:00:09 [wifi] listening quimbleton",
    "2026-11-24 10:00:10 [bridge] token %s %s" % (NO_VOWEL, SHORT_CAPS_ID),
    "2026-11-24 10:00:11 [usage] weekly %s -> %s" % (VERSION_FIGURE,
                                                     VERSION_FIGURE),
    "2026-11-24 10:00:12 [bridge] board %s ready" % WORD_ID,
    "2026-11-24 10:00:13 [wifi] network --%s" % WORD_ID,
    # Lines our code does print, with a filled-in value made of our words.
    "2026-11-24 10:00:14 [bridge] connected on %s" % BOARD_ID,
    "2026-11-24 10:00:15 [ingest] Desktop data folder: %s" % PROJECT,
    "2026-11-24 10:00:16 [bridge] board found at %s" % FOLDER,
    "2026-11-24 10:00:17 [bridge] <- %s" % WORD_ID,
    "2026-11-24 10:00:18 [bridge] connected on %s" % OUR_TEXT,
    "2026-11-24 10:00:19 [ingest] Desktop data folder: %s" % OUR_TEXT,
    "2026-11-24 10:00:20 [wsl] check failed: %s" % OUR_TEXT,
]


def _secrets(home):
    return [FIGURE, VERSION_FIGURE, "42.17", USER, "quimbleton", UUID,
            "3f2a9c1e", HEX, "deadbeef", LETTER_HEX, NO_VOWEL,
            SHORT_CAPS_ID, LONG_CAPS_ID, SSID, "SECRET", PORT, "usbserial",
            BOARD_ID, WORD_ID, CUE_FIGURE, "1.02", "SuperSecret",
            "secret-value", PROJECT, "Release", FOLDER, "release/",
            OUR_TEXT, str(home), '"t"', "'t'", "{", "Wire"]


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
        return f"running (launchd, pid {FIGURE}) {PORT} {OUR_TEXT}"

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
    # The real board_lines, so the row is the one our code prints, with a
    # board id made of our own words.
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
    assert "App         " + cli.RELEASE_VERSION in files["status.txt"]
    assert "Service     running (launchd, pid <x>) <path>" in \
        files["system.txt"]
    assert "Board       <path>" in files["status.txt"]
    assert "other serial devices: <path> -- asked once, not BLINK, left" \
        " alone" in files["status.txt"]
    assert "[bridge] -> usage" not in files["bridge.log"]
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
    # What our code wrote around the planted values is still there.
    log = files["bridge.log"]
    assert "[usage] session <x>" in log
    assert "[ingest] Desktop data folder: <x>" in log
    assert "[wsl] check failed: <x>" in log
    # A message type is masked like any value under [bridge]: an error
    # printed as "[bridge] " + err reads the same.
    assert "10:00:00 [bridge] <x>" in log
    assert "10:00:17 [bridge] <x>" in log
    # A tag that our code also prints with one bare value ("[bridge] " +
    # err) keeps its tag and nothing else, and so does a line our code
    # does not print.
    assert "10:00:16 [bridge] <path>" in log
    assert "10:00:14 [bridge] <x>" in log
    for tag in ("[wifi]", "[bridge]", "[usage]", "[cfg]"):
        assert f"{tag} <x>" in log


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
        print()
        print("Wire        the next usage message, exactly as the board gets"
              " it:")
        print(WIRE)
    monkeypatch.setattr(cli, "cmd_status", status)
    cli.main(["support"])
    files = _members(_today_zip(desk))
    assert files["status.txt"] == (
        "Bridge      running (launchd, pid <x>)\n"
        "App         1.5.1\n"
        "Board       <path> (<x>) <x>\n"
        "            other serial devices: <path> -- asked once, not BLINK,"
        " left alone\n"
        "Link        WiFi, board at <x>\n"
        "Usage data  fresh (<x>s old)\n"
        "Status line installed inside <x>\n"
        "Status line installed at <path>\n"
        "Claude Code WSL (<x>), not running\n")
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


def test_the_cover_rule():
    keep = support.scrub_text
    assert keep("Bridge      not checked (BLINK_SKIP_SERVICE)") == \
        "Bridge      not checked (<x>)"
    # A version only straight after the word its row names, and once.
    assert keep("App         1.5.1", version_after="app", keep=3) == \
        "App         1.5.1"
    assert keep("App         1.5.1") == "App         <x>"
    assert keep("Claude Code 2.1.318 (Claude Code)",
                version_after="code") == "Claude Code 2.1.318 <x>"
    assert keep("Claude Code 42.17.93 -- TOO OLD, needs 1.02.03+ (panel"
                " will stay blank)", version_after="code") == \
        "Claude Code 42.17.93 -- TOO OLD, needs <x>+ (panel will stay blank)"
    assert keep("Claude Code 1.02.03 -- could not read a version from"
                " that") == "Claude Code <x> -- could not read a version" \
        " from that"
    assert keep("            the board is on 42.17.93 and this app is"
                " 1.02.03 -- they ship together") == \
        "            the board is on <x> and this app is <x> -- they ship" \
        " together"
    # What our code filled in is masked whatever it is made of: a figure,
    # a path, a name of our own words, a name of no words at all.
    assert keep("Usage data  fresh (42% old)") == "Usage data  fresh (<x>"
    assert keep("Status line installed at /Users/zorblat/.claude/x.sh") == \
        "Status line installed at <path>"
    assert keep("Status line installed at C:\\Users\\a") == \
        "Status line installed at <path>"
    assert keep("Status line installed inside Build Release") == \
        "Status line installed inside <x>"
    assert keep("Status line installed inside not plugged in") == \
        "Status line installed inside <x>"
    assert keep("Status line installed at build/release") == \
        "Status line installed at <path>"
    assert keep("Kfir Office") == "<x>"
    assert keep("session 42%") == "<x>"
    # Case for case: a piece of ours in other capitals is not ours.
    assert keep("STATUS LINE INSTALLED INSIDE x") == "<x>"
    assert support.redact_log_line("[nottag] hello") is None
    assert support.redact_log_line("plain words") is None
    assert support.redact_log_line("2026-11-24 10:00:00 (board line)") == \
        "2026-11-24 10:00:00 (board line)"
    assert support.redact_log_line("[bridge] -> pong") == "[bridge] <x>"
    assert support.redact_log_line("[bridge] -> pong 42") == "[bridge] <x>"
    # A shape is laid from the line's first character, never found inside
    # it: our own text after a value we do not know is not ours.
    assert keep("Kfir Office -- they ship together") == "<x>"
    assert support.redact_log_line("[bridge] x not running") == "[bridge] <x>"


def test_only_a_listed_join_is_followed_by_our_text():
    # pc/bridge.py prints "[bridge] logo: " + an error string. A legacy
    # line whose value is itself one of our templates kept it whole in
    # round 6; only the reviewed joins in JOINS may be followed now.
    redact = support.redact_log_line
    assert redact("[bridge] logo: not running") == "[bridge] <x>"
    assert redact("[bridge] logo: running") == "[bridge] <x>"
    assert redact("[ota] could not read the release feed: not running") == \
        "[ota] could not read the release feed: <x>"
    keep = support.scrub_text
    assert keep("Bridge      running (launchd, pid 7)") == \
        "Bridge      running (launchd, pid <x>)"
    assert keep("Claude Code WSL (not running), not running") == \
        "Claude Code WSL (<x>), not running"
    # A join is followed by one template, never a chain of them.
    assert keep("Bridge      not running not running") == \
        "Bridge      not running <x>"
    for tpl in support.JOINS:
        assert tpl in support.TEMPLATES


def test_a_value_with_no_ascii_letter_is_masked():
    # Round 7 kept a filled-in run with no ASCII letter or digit, so a
    # network or computer named in another script came through whole.
    # Round 8 still showed one of bare ASCII punctuation, so a network
    # named "!!!" came through (review, 2026-11-25).
    redact = support.redact_log_line
    for value in ("東京", "Ωμέγα", "قهوة", "★★★", "١٢٣", "!!!", "-", "(:)"):
        for line in (redact(f"[wifi] joined {value}"),
                     redact(f"[ingest] Desktop data folder: {value}"),
                     support.scrub_text(
                         f"Status line installed inside {value}")):
            assert value not in line, line
            assert line.endswith(support.MASK), line
    # The punctuation in our own text is still shown, so rows keep their
    # shape; only spacing is shown of what was filled in.
    assert support.scrub_text("Usage data  fresh (") == "Usage data  fresh ("
    assert support.scrub_text("Usage data  fresh (12s old)") == \
        "Usage data  fresh (<x>s old)"


def test_a_value_that_is_one_of_our_lines_under_a_bare_tag_is_masked():
    # claude_usage_bridge.py prints f"[bridge] {err}", and a log from
    # before 2026-10-30 printed whole values after a tag. Round 8 left
    # the bare-tag reading out, so "[bridge] board found at" -- a value
    # word for word one of our longer [bridge] lines -- was shown whole
    # (review, 2026-11-25). Every tag with a bare-tag template now keeps
    # only its tag.
    redact = support.redact_log_line
    bare = sorted({support._TAG.match(t[0]).group(1)
                   for t in support.TEMPLATES
                   if support._TAG.match(t[0]) and support._generic(t)})
    assert "bridge" in bare and "wifi" in bare and "update" in bare
    for tpl in support.TEMPLATES:
        t = support._TAG.match(tpl[0])
        if not t or t.group(1) not in bare:
            continue
        line = "".join(p if p is not None else "x" for p in tpl)
        if "{" in line or "}" in line:
            continue
        out = redact("2026-11-25 10:00:00 " + line)
        if out is None or support._ARROW_TYPE.match(line[t.end():]):
            continue
        head = "2026-11-25 10:00:00 [%s]" % t.group(1)
        assert out.startswith(head), (tpl, out)
        rest = out[len(head):].replace(support.MASK, "").replace(
            support.PATH_MASK, "")
        assert not rest.strip(), (tpl, out)
    assert redact("[bridge] board found at") == "[bridge] <x>"
    assert redact("[bridge] board found at /dev/x") == "[bridge] <path>"
    assert redact("[update] restarting into the new version") == \
        "[update] <x>"


def test_a_longer_template_does_not_make_a_value_ours():
    # TEMPLATES holds both '[usage] session' + a value and the longer
    # '[usage] session applied'. Round 7 laid the longer one, so a legacy
    # line whose value was "applied" kept it. A character is ours now only
    # where every template that reads the whole line agrees.
    assert ("[usage] session",) in support.TEMPLATES
    assert ("[usage] session applied",) in support.TEMPLATES
    redact = support.redact_log_line
    assert "applied" not in redact("[usage] session applied")
    assert redact("[usage] session applied").startswith("[usage] session")
    # Every log line of ours that starts with another of ours: what comes
    # after the shorter one may be a value appended to it, so it is masked.
    shapes = [t for t in support.TEMPLATES
              if len(t) == 1 and t[0].startswith("[")
              and t not in support.JOINS]
    pairs = 0
    for (short,) in shapes:
        if support._generic((short,)):
            continue  # a bare tag: the residual named in _generic
        for (long,) in shapes:
            if len(long) <= len(short) or not long.startswith(short):
                continue
            words = support._TOKEN.findall(long[len(short):])
            words = [w for w in words if support._ALNUM.match(w)]
            if not words:
                continue
            out = redact(long)
            if out is None:
                continue
            tag = support._TAG.match(short)
            if tag and out == tag.group(0) + " " + support.MASK:
                continue  # a tag that also prints one bare value: all masked
            pairs += 1
            assert out.startswith(short), (short, long, out)
            assert words[-1] not in out[len(short):], (short, long, out)
    assert pairs >= 1


def test_a_long_line_of_our_pieces_is_not_a_recursion():
    # Round 6's matcher recursed once per piece and hit Python's recursion
    # limit on a line like this one, which aborted the whole bundle.
    line = "[bridge] " + "running" * 5000
    assert support.redact_log_line(line) == "[bridge] <x>"
    assert support.scrub_text("Bridge      " + "running" * 5000).startswith(
        "Bridge      running")


def test_a_private_word_is_masked_even_in_our_own_text():
    keep = support.scrub_text
    # A computer called "WiFi": the word is masked where our text has it.
    assert keep("Link        WiFi, board at 10.0.0.2", frozenset({"wifi"}),
                keep=4) == "Link        <x>, board at <x>"
    # An account called "desktop": masked in our text, but the row's label
    # and the log's tag are kept.
    private = frozenset({"desktop"})
    assert keep("Desktop     no usage cache (Claude Desktop installed but"
                " never signed in)", private, keep=7) == \
        "Desktop     no usage cache (Claude <x> installed but never signed in)"
    # What our code filled in is masked anyway, our own words or not.
    assert keep("Claude Code not found -- running on Claude Desktop alone",
                private) == "Claude Code not found -- running on <x>"
    assert keep("Desktop     usage cache present but could not be read",
                private, keep=7) == \
        "Desktop     usage cache present but could not be read"
    assert support.redact_log_line("[desktop] x", private) == "[desktop] <x>"
    assert support.redact_log_line("[ingest] Desktop data folder: x",
                                   private) == "[ingest] <x> data folder: <x>"


def test_system_fields_keep_a_version_only_whole():
    field = support.system_field
    assert field("Python", "3.12.1", version=True) == "Python      3.12.1"
    assert field("Firmware", "1.02", version=True) == "Firmware    unknown"
    assert field("Firmware", "1.5.1 at 42.17.93", version=True) == \
        "Firmware    unknown"
    assert field("Service", "running (launchd, pid 4242)") == \
        "Service     running (launchd, pid <x>)"
    assert field("System", "Darwin", allowed=support.SYSTEMS) == \
        "System      Darwin"
    assert field("Machine", "store-build", allowed=support.MACHINES) == \
        "Machine     unknown"


# --- TEMPLATES and LOG_TAGS are derived again from the source ------------
#
# Both lists are written out in pc/support.py, so that what a bundle keeps
# does not depend on reading our own code at run time (a frozen build has
# only bytecode, and the bytecode holds the docstrings too). These tests
# derive them again with ast, docstrings left out, and fail on any
# difference, naming it: a line added to the code has its shape added to
# TEMPLATES by a person who looked at it, and a new [tag] is added to
# LOG_TAGS instead of being silently dropped from every bundle.

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
# The functions that print the rows of `blink status`, and the files they
# are in. _wire_line is left out: the Wire row is never kept.
STATUS_SOURCES = {
    os.path.join("pc", "cli.py"): {
        "cmd_status", "hook_shim_status_note", "statusline_shim_status_note",
        "_shim_repair_line", "_refresh_status", "_session_window_line",
        "_codex_session_line", "_age", "board_lines", "_codex_hook_status",
        "_source_lines", "_source_lines_desktop",
        "_print_other_places_looked", "_source_lines_codex",
        "desktop_staleness_note", "status",
    },
    os.path.join("pc", "win_driver.py"): {"summary", "advice"},
    os.path.join("pc", "link_status.py"): {"status_line"},
}
_TAG_AT_START = __import__("re").compile(r"^\[([a-z_-]+)\](?:\s|$)")


def _sources():
    out = ["claude_usage_bridge.py", "blink_main.py"]
    for d, dirs, files in os.walk(os.path.join(ROOT, "pc")):
        dirs[:] = sorted(x for x in dirs if x != "__pycache__")
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(d, f), ROOT)
            if f.endswith(".py") and rel != os.path.join("pc", "support.py"):
                out.append(rel)
    return out


def _literals(tree, within=None):
    """The elements of each string literal under `within` (default the
    whole tree), docstrings and bare string statements left out: its
    constant text, and None for each value an f-string fills in."""
    import ast
    bare, inner = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant):
            bare.add(id(n.value))
        if isinstance(n, ast.JoinedStr):
            inner.update(id(c) for c in ast.walk(n) if c is not n)
    for n in ast.walk(within or tree):
        if id(n) in bare or id(n) in inner:
            continue
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            yield [n.value]
        elif isinstance(n, ast.JoinedStr):
            els = []
            for v in n.values:
                if isinstance(v, ast.Constant):
                    if els and isinstance(els[-1], str):
                        els[-1] += v.value
                    else:
                        els.append(v.value)
                elif not els or els[-1] is not None:
                    els.append(None)
            yield els


def _templates_of(els):
    """A literal's shapes, one per line it prints: constant pieces with
    None between them where a value is filled in. A shape that starts with
    a hole, or whose first piece has no letter or digit, vouches for
    nothing and is left out."""
    import re
    lines, cur = [], []
    for e in els:
        if e is None:
            cur.append(None)
            continue
        for k, part in enumerate(e.split("\n")):
            if k:
                lines.append(cur)
                cur = []
            if part:
                cur.append(part)
    lines.append(cur)
    for line in lines:
        shape = []
        for x in line:
            if x is None and shape and shape[-1] is None:
                continue
            shape.append(x)
        if not shape or shape[0] is None \
                or not re.search(r"[A-Za-z0-9]", shape[0]):
            continue
        yield tuple(shape)


def _head(els):
    return els[0] if els and isinstance(els[0], str) else ""


def _derived():
    import ast
    from pc import logbook
    tags, shapes = set(logbook.CONSOLE_TAGS), set()
    for rel in _sources():
        with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
            tree = ast.parse(f.read())
        for els in _literals(tree):
            m = _TAG_AT_START.match(_head(els))
            if m:
                tags.add(m.group(1))
                shapes.update(_templates_of(els))
        names = STATUS_SOURCES.get(rel, ())
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and fn.name in names:
                for els in _literals(tree, fn):
                    shapes.update(_templates_of(els))
    return tags, shapes


def test_log_tags_are_every_tag_our_lines_open_with():
    tags, _ = _derived()
    assert tags == support.LOG_TAGS, (
        "LOG_TAGS differs from the [tag] literals in the source: add "
        f"{sorted(tags - support.LOG_TAGS)}, drop "
        f"{sorted(support.LOG_TAGS - tags)}")


def _key(shape):
    return tuple("" if x is None else x for x in shape), \
        tuple(x is None for x in shape)


def test_templates_are_the_shapes_our_lines_print(tmp_path):
    _, shapes = _derived()
    have = set(support.TEMPLATES)
    assert len(have) == len(support.TEMPLATES), "a template is listed twice"
    if shapes != have:
        # Named a batch at a time, ready to paste into pc/support.py (or to
        # take out of it): the whole list is longer than a failure message
        # is worth reading.
        missing = sorted(shapes - have, key=_key)
        extra = sorted(have - shapes, key=_key)
        pytest.fail(
            "TEMPLATES differs from the shapes of our printed lines: "
            f"{len(missing)} missing, {len(extra)} extra.\n"
            "missing (the first 60):\n"
            + "".join(f"    {s!r},\n" for s in missing[:60])
            + "extra (the first 60):\n"
            + "".join(f"    {s!r},\n" for s in extra[:60]),
            pytrace=False)


def test_the_list_is_fixed_and_every_shape_starts_with_our_text():
    import re
    # Read from a constant, not from our own code: the frozen build answers
    # the same as a checkout, and a missing source cannot empty it.
    assert len(support.TEMPLATES) > 300
    assert not hasattr(support, "vocabulary")
    assert not hasattr(support, "FRAGMENTS")
    for t in support.TEMPLATES:
        assert isinstance(t[0], str) and re.search(r"[A-Za-z0-9]", t[0]), t
        # Pieces and holes take turns.
        for i, x in enumerate(t):
            assert (x is None) == (i % 2 == 1), t


def test_a_message_type_line_keeps_no_type():
    # Review (2026-11-25): `f"[bridge] {err}"` with err "-> usage" is the
    # same line as protocol.log_name's, so neither keeps the type.
    redact = support.redact_log_line
    for line in ("[bridge] -> usage", "[bridge] <- hello",
                 "[bridge] -> (unknown type)"):
        assert redact("2026-11-25 10:00:00 " + line) == \
            "2026-11-25 10:00:00 [bridge] <x>", line


def test_a_name_holding_our_closing_piece_does_not_show_its_tail():
    # Review (2026-11-25): a WSL distribution named "x), not running" made
    # the row read up to its first "), ", so the name's own "not running"
    # was called ours. A piece is ours only where the first and the last
    # place it can sit agree.
    for name in ("x), not running", "x), running", "a), b), c",
                 "), not installed"):
        line = f"Claude Code WSL ({name}), running"
        assert support.scrub_text(line) == "Claude Code WSL (<x>", name
        tail = name.split("), ", 1)[1]
        kept, _ = support.status_rows(line + "\n")
        assert name not in kept, (name, kept)
        body = kept.split("Claude Code WSL (", 1)[1]
        for word in tail.replace(")", " ").replace(",", " ").split():
            # Every word of the name's tail is masked; "running" may still
            # appear once at most, as the state our code printed.
            if word != "running":
                assert word not in body, (name, kept)
        assert body.count("running") <= 1, (name, kept)


def test_a_value_that_is_our_own_text_is_still_masked():
    # The review of round 5: a WSL distribution called "not running" was
    # one of our own pieces, so the loose cover kept it inside the very row
    # that names it. A hole is never searched for our text now.
    for name in ("not running", "not installed", "running",
                 "is not on this computer", "Claude Desktop"):
        text = (f"Claude Code WSL ({name}), not running\n"
                "            the one Blink was set up for is gone; another"
                f" distribution is called {name} now --\n"
                f"            renamed from {name} -- run `blink install`\n"
                f"Status line installed inside {name}\n")
        kept, _ = support.status_rows(text)
        assert name not in kept.replace("), not running", ")"), (name, kept)
        assert support.redact_log_line(
            f"[ota] could not read the release feed: {name}") == \
            "[ota] could not read the release feed: <x>"
        assert support.redact_log_line(
            f"[bridge] connected on {name}") == "[bridge] <x>"


def test_continuation_lines_keep_no_number_and_only_under_some_rows():
    text = (
        "App         1.5.1\n"
        "            version 42.17.93 app 1.02.03\n"
        "Board       not plugged in -- firmware 1.5.1\n"
        "            the board is on 42.17.93 and this app is 1.02.03\n"
        "Usage data  fresh\n"
        "            session 42.17.93\n")
    kept, dropped = support.status_rows(text)
    assert kept == (
        "App         1.5.1\n"
        "Board       not plugged in <x>\n"
        "            the board is on <x> and this app is <x>\n"
        "Usage data  <x>\n")
    assert dropped == 2


def test_the_desktop_is_found_where_the_system_puts_it(tmp_path,
                                                       monkeypatch):
    home = str(tmp_path)
    monkeypatch.setattr(support.sys, "platform", "linux")
    os.makedirs(inside(tmp_path, tmp_path / "Schreibtisch"))
    os.makedirs(inside(tmp_path, tmp_path / ".config"))
    with open(inside(tmp_path, tmp_path / ".config" / "user-dirs.dirs"),
              "w") as f:
        f.write('XDG_DESKTOP_DIR="$HOME/Schreibtisch"\n')
    assert support.desktop_dir(home) == home + os.sep + "Schreibtisch"

    monkeypatch.setattr(support.sys, "platform", "win32")
    monkeypatch.setattr(support, "_windows_desktop",
                        lambda: os.path.join(os.sep, "elsewhere", "Desktop"))
    assert support.desktop_dir(home) is None   # outside home: not taken
    os.makedirs(inside(tmp_path, tmp_path / "OneDrive - Acme" / "Desktop"))
    assert support.desktop_dir(home) == os.path.join(
        home, "OneDrive - Acme", "Desktop")


def test_no_desktop_says_so_louder(tmp_path, capsys):
    assert cli.main(["support"]) == 0
    out = capsys.readouterr().out
    assert "No Desktop folder was found" in out
    assert os.path.join(".blink", "blink-support-") in out


def test_private_words_let_no_name_off(monkeypatch):
    monkeypatch.setattr(support.getpass, "getuser", lambda: "desktop")
    monkeypatch.setattr(support.platform, "node", lambda: "WiFi")
    private = support.private_words("/somewhere/desktop")
    assert {"desktop", "wifi"} <= private
    monkeypatch.setattr(support.platform, "node", lambda: HOST)
    monkeypatch.setattr(support.getpass, "getuser", lambda: USER)
    private = support.private_words("/somewhere/" + USER)
    assert {"quimbleton", "mbp", USER} <= private
    assert support.redact_log_line("[wifi] joined quimbleton", private) == \
        "[wifi] <x>"
    assert support.redact_log_line("[ota] could not read the release feed:"
                                   " quimbleton", private) == \
        "[ota] could not read the release feed: <x>"


# The owner asked for one run on a built binary (round 4), because a list
# read from our own code at run time could come back empty under PyInstaller
# and mask everything. TEMPLATES is a constant now, so nothing reads code at
# run time, and this proves it the way a frozen build would meet it: every
# module of ours compiled to bytecode inside a zip, with no .py source beside
# it (PyInstaller's PYZ holds the same), sys.frozen set, imported by an
# isolated interpreter (-I: no cwd, no site, no PYTHON* variables), with
# sockets and serial refused. `sh tools/build_binary.sh` itself needs the
# owner's approval to run here, so the binary is still theirs to try once.
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
assert support.TEMPLATES and support.LOG_TAGS
cli._skip_service = lambda: True
cli._board_lines = lambda: []
sys.exit(cli.main(["support"]))
'''


def _bytecode_zip(path):
    """Every module of ours as bytecode in `path`, the way PYZ holds them."""
    import importlib.util
    import marshal
    root = os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__))))
    sources = [os.path.join(root, "claude_usage_bridge.py"),
               os.path.join(root, "blink_main.py")]
    for d, dirs, files in os.walk(os.path.join(root, "pc")):
        dirs[:] = [x for x in dirs if x != "__pycache__"]
        sources += [os.path.join(d, f) for f in files if f.endswith(".py")]
    with zipfile.ZipFile(path, "w") as z:
        for src in sources:
            with open(src, "rb") as f:
                code = compile(f.read(), os.path.relpath(src, root), "exec",
                               dont_inherit=True)
            header = importlib.util.MAGIC_NUMBER + bytes(12)
            name = os.path.relpath(src, root).replace(os.sep, "/")
            z.writestr(name + "c", header + marshal.dumps(code))
    return path


def test_a_frozen_shaped_build_keeps_our_text_and_masks_the_rest(desk):
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
    # Our text came through, so the list was there: an empty one masks all.
    assert "[ota] update landed <x>" in files["bridge.log"]
    for secret in _secrets(desk):
        for name, text in files.items():
            assert secret not in text, (secret, name)
