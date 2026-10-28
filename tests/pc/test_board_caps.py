"""The board says what it can show; the daemon believes it, not a version.

Theme contract, Addendum 3. svc and theme lines used to go only to firmware
numbered 1.4.0 or later, while this branch's firmware still said 1.3.4 -- so
a board built from it was sent neither, and a release bumped to anything
short of 1.4.0 would have shipped both features dead (final review, MUST 2).
The board's hello now carries "caps", and so does its pref, which is its
answer to a welcome: a daemon that restarts under a running board hears no
hello, only the pref.
"""
import ast
import json
import os
import pathlib
import re

import claude_usage_bridge
from claude_usage_bridge import learn_board
from pc import protocol
from pc import theme
from pc.bridge import Bridge, THEME_QUIET_S

ROOT = pathlib.Path(__file__).resolve().parents[2]
_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)
_ONE_PAGE = protocol.usage(10.0, None, 20.0, None, [], provider="claude")
BOTH = ["svc", "theme"]


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


class Watch:
    def __init__(self):
        self.wanted = []

    def want(self, providers):
        self.wanted.append(set(providers))

    def shown(self, provider):
        return ("ok", "")


class Opener:
    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return True


def make(tmp_path, caps_hint=None):
    home = str(tmp_path / ".blink")
    theme.save(home, {"id": 5})
    sent, clock, opener, watch = [], Clock(), Opener(), Watch()
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: dict(_ONE_PAGE),
               now=clock, wall=lambda: (1752444000, 0),
               status_watch=watch, theme_sync=theme.ThemeSync(home),
               open_builder=opener, run_async=lambda fn: fn(),
               board_caps_hint=caps_hint, **_NO_FEED)
    return b, sent, clock, opener, watch


def kinds(sent, t):
    return [m for m in sent if m["t"] == t]


def hello(fw, caps=None):
    m = {"t": "hello", "v": 2, "board": "cyd", "board_id": "ab", "fw": fw,
         "reset": "0x1"}
    if caps is not None:
        m["caps"] = caps
    return m


def settle(b, clock):
    """The board's theme_now, then a fast tick past the quiet window."""
    b.on_message({"t": "theme_now", "v": 2, "id": 0, "why": "boot"})
    clock.t += THEME_QUIET_S
    b.poll_if_changed()


# --------------------------------------------------------------- protocol


def test_caps_are_read_from_the_message():
    assert protocol.board_caps({"caps": ["svc", "theme"]}) == {"svc", "theme"}
    assert protocol.board_caps({"caps": ["theme"]}) == {"theme"}


def test_absent_or_malformed_caps_are_no_caps():
    for msg in ({}, {"caps": None}, {"caps": "svc"}, {"caps": {"svc": 1}},
                {"caps": [1, None, ["svc"]]}):
        assert protocol.board_caps(msg) == frozenset(), msg


def test_the_version_gates_are_gone():
    # A second way to decide the same thing is a way for the two to disagree.
    for name in ("FW_SHOWS_SVC", "FW_TAKES_THEME", "fw_shows_svc",
                 "fw_takes_theme"):
        assert not hasattr(protocol, name), name


# ----------------------------------------------------------------- bridge


def test_a_board_numbered_1_3_4_with_caps_gets_svc_and_theme(tmp_path):
    b, sent, clock, opener, _ = make(tmp_path)
    b.on_message(hello("1.3.4", BOTH))
    assert kinds(sent, "svc") == [protocol.svc("claude", "ok")]
    settle(b, clock)
    assert kinds(sent, "theme") == [theme.message("save", {"id": 5})]
    b.on_message({"t": "theme_edit", "v": 2})
    assert kinds(sent, "theme_edit") == [protocol.theme_edit_reply(True)]
    assert opener.calls == 1


def test_a_shipped_1_3_4_board_gets_neither(tmp_path):
    b, sent, clock, opener, watch = make(tmp_path)
    b.on_message(hello("1.3.4"))
    settle(b, clock)
    b.poll_once()
    assert kinds(sent, "svc") == [] and kinds(sent, "theme") == []
    assert watch.wanted == []       # nobody's status page asked on its behalf


def test_a_board_without_the_theme_cap_is_answered_ok_0(tmp_path):
    b, sent, _, opener, _ = make(tmp_path)
    b.on_message(hello("1.3.4", ["svc"]))
    b.on_message({"t": "theme_edit", "v": 2})
    assert kinds(sent, "theme_edit") == [protocol.theme_edit_reply(False)]
    assert opener.calls == 0        # no builder that could not deliver


def test_a_high_version_without_caps_opens_nothing(tmp_path):
    b, sent, clock, _, _ = make(tmp_path)
    b.on_message(hello("9.9.9"))
    settle(b, clock)
    assert kinds(sent, "svc") == [] and kinds(sent, "theme") == []


def test_each_cap_opens_only_its_own_lines(tmp_path):
    b, sent, clock, _, _ = make(tmp_path)
    b.on_message(hello("1.3.4", ["svc"]))
    settle(b, clock)
    assert kinds(sent, "svc") and kinds(sent, "theme") == []

    b, sent, clock, _, _ = make(tmp_path)
    b.on_message(hello("1.3.4", ["theme"]))
    settle(b, clock)
    assert kinds(sent, "svc") == [] and kinds(sent, "theme")


def test_a_connect_without_a_reset_learns_caps_from_the_pref(tmp_path):
    """No hello on this path: the pref that answers greet()'s welcome says."""
    b, sent, clock, _, _ = make(tmp_path)
    b.greet()
    assert kinds(sent, "svc") == []
    b.on_message({"t": "pref", "v": 2, "provider": "claude", "caps": BOTH})
    settle(b, clock)
    assert kinds(sent, "svc") == [protocol.svc("claude", "ok")]
    assert kinds(sent, "theme") == [theme.message("save", {"id": 5})]


def test_the_remembered_caps_stand_in_until_the_board_speaks(tmp_path):
    b, sent, _, _, _ = make(tmp_path, caps_hint=BOTH)
    b.greet()
    assert kinds(sent, "svc") == [protocol.svc("claude", "ok")]


def test_the_boards_own_word_beats_the_remembered_caps(tmp_path):
    b, sent, clock, _, _ = make(tmp_path, caps_hint=BOTH)
    b.on_message(hello("1.3.4"))           # an older board on the same port
    settle(b, clock)
    assert kinds(sent, "svc") == [] and kinds(sent, "theme") == []


def test_a_later_pref_without_caps_closes_the_gate(tmp_path):
    # Every board that has caps puts them on every pref, so one without is
    # a different board answering.
    b, sent, clock, _, _ = make(tmp_path)
    b.on_message(hello("1.3.4", BOTH))
    b.on_message({"t": "pref", "v": 2, "provider": "claude"})
    del sent[:]
    settle(b, clock)
    b.poll_once()
    assert kinds(sent, "svc") == [] and kinds(sent, "theme") == []


def test_the_daemon_hands_the_remembered_caps_to_the_bridge():
    src = pathlib.Path(claude_usage_bridge.__file__).read_text(encoding="utf-8")
    calls = [n for n in ast.walk(ast.parse(src)) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "Bridge"]
    assert calls and all(
        any(k.arg == "board_caps_hint" for k in c.keywords) for c in calls)


# ------------------------------------------------------------- board.json


def test_board_json_learns_caps_from_hello_and_pref():
    known = learn_board({}, "COM3", hello("1.3.4", ["theme", "svc"]))
    assert known["caps"] == ["svc", "theme"]
    known = learn_board(known, "COM3", {"t": "pref", "provider": "claude"})
    assert known["caps"] == []
    known = learn_board(known, "COM3", {"t": "pref", "caps": BOTH})
    assert known["caps"] == BOTH


def test_other_messages_keep_the_caps_known():
    known = {"port": "COM3", "board_id": "ab", "fw": "1.3.4", "caps": BOTH}
    for m in ({"t": "ping"}, {"t": "ota_query", "cur": "1.3.4"},
              {"t": "theme_now", "id": 0}):
        assert learn_board(known, "COM3", m)["caps"] == BOTH


def test_remember_board_writes_the_caps(tmp_path):
    claude_usage_bridge.remember_board(str(tmp_path), "COM3", "ab", "1.3.4",
                                       caps=BOTH)
    with open(tmp_path / "board.json", encoding="utf-8") as f:
        assert json.load(f)["caps"] == BOTH


# --------------------------------------------------------------- firmware


def _fw_line(name, buf_name="buf"):
    """The widest line a firmware snprintf can produce, from its source.

    Reads the format of the snprintf in `name` in proto.c and fills each
    conversion at its widest: %d with PROTO_VERSION's digits, %s with the
    widest argument this function passes, %x with eight hex digits.
    Returns (line, buffer size)."""
    src = (ROOT / "firmware" / "src" / "proto.c").read_text()
    body = re.search(r"\n[a-z ]*\b" + name + r"\(void\)\n\{(.*?)\n\}\n",
                     src, re.S).group(1)
    size = int(re.search(r"char " + buf_name + r"\[(\d+)\];", body).group(1))
    call = re.search(r"snprintf\(" + buf_name + r", sizeof\(" + buf_name
                     + r"\),(.*?)\);", body, re.S).group(1)
    fw = re.search(r'#define BLINK_FW_VERSION "([^"]+)"',
                   (ROOT / "firmware" / "src" / "version.h").read_text())
    macros = {"BLINK_FW_VERSION": fw.group(1)}
    for m in re.finditer(r'#define (\w+)\s+"((?:[^"\\]|\\.)*)"', src):
        macros[m.group(1)] = bytes(m.group(2), "utf-8").decode(
            "unicode_escape")
    # The format is the run of adjacent literals and string macros before
    # the first comma; the arguments after it are not part of it.
    fmt, pos = "", 0
    tok_re = re.compile(r'\s*(?:"((?:[^"\\]|\\.)*)"|([A-Z_]+)\b)')
    while True:
        tok = tok_re.match(call, pos)
        if tok is None:
            break
        if tok.group(1) is not None:
            fmt += bytes(tok.group(1), "utf-8").decode("unicode_escape")
        else:
            fmt += macros[tok.group(2)]
        pos = tok.end()
    assert call[pos:].lstrip().startswith(","), call
    widest = {"d": str(protocol.VERSION), "x": "f" * 8, "s": "f" * 32}
    line = re.sub(r"%([dxs])", lambda m: widest[m.group(1)], fmt)
    # Any other conversion (%u, %02x, %lu...) would stay in the line as
    # literal text, under-measure it, and still parse inside a JSON string,
    # so the widest-line checks would pass on nothing. Teach `widest` the
    # new one instead.
    left = re.search(r"%[-0-9.lhz]*[a-zA-Z]", line)
    assert left is None, f"{name}: unfilled conversion {left.group(0)!r}"
    return line, size


def test_the_boards_hello_carries_both_caps_and_fits_its_buffer():
    line, size = _fw_line("send_hello")
    msg = json.loads(line)
    assert msg["t"] == "hello"
    assert protocol.board_caps(msg) == {"svc", "theme"}
    # snprintf truncates silently, and a truncated hello is not JSON.
    assert len(line) + 1 <= size, (len(line), size)


def test_the_boards_pref_carries_both_caps_and_fits_its_buffer():
    line, size = _fw_line("proto_send_pref")
    # The first %s is the provider, at its widest "claude"; the second is
    # the board id at hwinfo's widest, 32 hex digits.
    line = line.replace("f" * 32, "claude", 1)
    msg = json.loads(line)
    assert msg["t"] == "pref"
    assert protocol.board_caps(msg) == {"svc", "theme"}
    # snprintf truncates silently, and a truncated pref is not JSON.
    assert len(line) + 1 <= size, (len(line), size)
    # Measured 107 B. The daemon reads 512 and the board itself drops a
    # longer inbound line whole; a pref anywhere near that is a mistake.
    assert len(line) <= 160, len(line)


def test_the_boards_pref_names_the_board_as_hello_does():
    """Theme contract, Addendum 5: the same id, from the same function."""
    src = (ROOT / "firmware" / "src" / "proto.c").read_text()
    pref, _ = _fw_line("proto_send_pref")
    hello_line, _ = _fw_line("send_hello")
    assert json.loads(pref.replace("f" * 32, "claude", 1))["board_id"] \
        == "f" * 32
    assert "board_id" in json.loads(hello_line)
    for name in ("proto_send_pref", "send_hello"):
        body = re.search(r"\n[a-z ]*\b" + name + r"\(void\)\n\{(.*?)\n\}\n",
                         src, re.S).group(1)
        assert "board_id_hex()" in body, name
        assert "hwinfo_get_device_id" not in body, name


def test_a_pref_with_a_board_id_is_read_as_before(tmp_path):
    # The daemon reads a pref's provider and caps and ignores keys it does
    # not know, so Addendum 5's board_id costs an older daemon nothing.
    picked = []
    home = str(tmp_path / ".blink")
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: dict(_ONE_PAGE),
               now=Clock(), wall=lambda: (1752444000, 0),
               status_watch=Watch(), theme_sync=theme.ThemeSync(home),
               open_builder=Opener(), run_async=lambda fn: fn(),
               set_preferred=lambda p: picked.append(p) or True, **_NO_FEED)
    b.on_message({"t": "pref", "v": 2, "provider": "codex",
                  "board_id": "a1b2c3d4e5f6", "caps": BOTH})
    assert picked == ["codex"]
    assert b._board_caps == {"svc", "theme"}
    known = learn_board({}, "COM3", {"t": "pref", "provider": "codex",
                                     "board_id": "a1b2c3d4e5f6",
                                     "caps": BOTH})
    assert set(known["caps"]) == {"svc", "theme"}
