"""bridge.log says what kind of message passed, never what was in it.

The arrow lines used to print every message whole -- `<- {'t': 'hello',
'board_id': ...}` from the board, `-> {...}` and a digest of every usage
figure going back -- into the file README-full.md asks a customer to email
us. CLAUDE.md forbids any value decoded off the serial buffer in a log line,
at any level. These tests hold the line at the three places that broke it:
the arrow lines, the ingest refusal of an unknown provider, and the ota
"board has" line.

Every value below is a marker string that cannot occur by accident in a log
line, so "not in the log" is a real check and not a coincidence of wording.
"""
import ast
import pathlib

import pytest

from pc import ingest, protocol
from pc.bridge import Bridge
from pc.providers import base

ROOT = pathlib.Path(__file__).resolve().parents[2]

_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)

# Each: (message, the field values that must never reach the log).
REPRESENTATIVE = [
    ({"t": "hello", "v": 2, "fw": "1.4.0", "board_id": "a1b2c3d4e5f6SECRETID",
      "caps": ["theme", "svc"], "board": "cyd"},
     ["a1b2c3d4e5f6SECRETID", "1.4.0", "cyd", "svc"]),
    ({"t": "pref", "v": 2, "provider": "codex", "board_id": "PREFBOARDID77"},
     ["PREFBOARDID77", "codex"]),
    (protocol.usage(73.25, None, 41.5, None, [], session_resets_in_s=12345,
                    weekly_resets_in_s=543210, provider="claude",
                    src="statusline", state="busy", n_sess=7, n_run=3),
     ["73.25", "41.5", "12345", "543210", "statusline", "busy", "claude"]),
    (protocol.session("acme-payroll-migration", 4),
     ["acme-payroll-migration"]),
    (protocol.theme("preview", {"id": "custom", "bg": "#a1b2c3",
                                "fg": "#d4e5f6"}),
     ["preview", "custom", "#a1b2c3", "a1b2c3", "#d4e5f6"]),
    ({"t": "theme_now", "v": 2, "id": "custom", "h": "deadbeef99",
      "why": "boot"},
     ["deadbeef99", "custom", "boot"]),
    ({"t": "ota_query", "v": 2, "cur": "1.3.4"}, ["1.3.4"]),
]


@pytest.mark.parametrize("msg,secrets", REPRESENTATIVE,
                         ids=[m["t"] for m, _ in REPRESENTATIVE])
def test_a_message_is_logged_by_its_type_alone(msg, secrets):
    out = protocol.log_name(msg)
    assert out == msg["t"]
    for s in secrets:
        assert s not in out, s


@pytest.mark.parametrize("t", [
    "hello\nboard_id=a1b2c3",            # a type with a payload riding on it
    "board_id:a1b2c3",
    "x" * 300,
    42,
    None,
    ["hello"],
])
def test_a_type_we_do_not_know_is_a_fixed_placeholder(t):
    """t is off the wire too. A board -- or line noise that parsed -- that
    sent t="<anything>" must not get that text into the log."""
    assert protocol.log_name({"t": t, "board_id": "a1b2c3"}) == \
        protocol.UNKNOWN_TYPE


def test_a_message_with_no_type_at_all_is_the_placeholder():
    assert protocol.log_name({"board_id": "a1b2c3"}) == protocol.UNKNOWN_TYPE
    assert protocol.log_name("not a dict") == protocol.UNKNOWN_TYPE


def test_every_type_the_daemon_builds_is_one_the_log_can_name():
    """A new builder whose type is missing from WIRE_TYPES would log as
    "(unknown type)" -- safe, but useless to support. Caught here instead."""
    built = [protocol.welcome("blink", "1.4.0"), protocol.bye(),
             protocol.pong(), protocol.time_msg(0, 0),
             protocol.usage(1.0, None, 2.0, None, []),
             protocol.edition("claude"), protocol.session("p", 1),
             protocol.status("ok"), protocol.theme("cancel"),
             protocol.theme_edit_reply(True), protocol.theme_edit_done(),
             protocol.ota_none()]
    for m in built:
        assert protocol.log_name(m) == m["t"]


# --- the daemon's own arrow lines -------------------------------------------
#
# send() and the read loop are closures inside claude_usage_bridge.main() and
# cannot be called from a test (see test_daemon_hooks._like_send), so the
# check is on their source: every print whose text has an arrow may
# interpolate protocol.log_name(...) and nothing else. On the old code this
# finds `{msg}`, `{m}` and `{short or m}`.

def _arrow_prints(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", "")
                == "print" and node.args
                and isinstance(node.args[0], ast.JoinedStr)):
            continue
        s = node.args[0]
        text = "".join(v.value for v in s.values
                       if isinstance(v, ast.Constant))
        if "] <- " in text or "] -> " in text:
            yield node.lineno, s


def _is_log_name(expr):
    return (isinstance(expr, ast.Call)
            and isinstance(expr.func, ast.Attribute)
            and expr.func.attr == "log_name"
            and getattr(expr.func.value, "id", "") == "protocol")


def test_the_daemon_logs_messages_by_type_in_both_directions():
    found = list(_arrow_prints(ROOT / "claude_usage_bridge.py"))
    # One receive line and two send lines; fewer means the scan went blind.
    assert len(found) >= 3, found
    for lineno, s in found:
        for v in s.values:
            if isinstance(v, ast.FormattedValue):
                assert _is_log_name(v.value), (
                    f"claude_usage_bridge.py:{lineno} logs "
                    f"{ast.unparse(v.value)!r}, a message's contents; log "
                    f"protocol.log_name(...) instead")


# --- the other two lines that carried a board value -------------------------

def test_the_ota_line_never_carries_the_raw_version(capsys):
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None, **_NO_FEED)
    b.on_message({"t": "ota_query", "v": 2,
                  "cur": "1.3.4 BOARDSECRET \x1b[31m"})
    err = capsys.readouterr().err
    assert "BOARDSECRET" not in err and "\x1b" not in err
    assert "board has an unreadable version" in err


def test_the_ota_line_still_names_a_real_version(capsys):
    """What support reads this line for: which firmware the desk runs."""
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None, **_NO_FEED)
    b.on_message({"t": "ota_query", "v": 2, "cur": "1.3.4"})
    assert "board has 1.3.4," in capsys.readouterr().err


def test_a_version_of_endless_digits_is_not_carried_either(capsys):
    """int() takes any run of digits, so parsing alone still lets a board put
    thousands of characters of its choosing into the line."""
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None, **_NO_FEED)
    b.on_message({"t": "ota_query", "v": 2, "cur": "1.2." + "7" * 400})
    err = capsys.readouterr().err
    assert "7" * 20 not in err
    assert "board has an unreadable version" in err


class _Fixed(base.ProviderParser):
    def get_provider_id(self):
        return "claude"

    def poll(self, now_epoch):
        return []


def test_an_unknown_provider_from_the_board_is_not_named(capsys):
    bus = ingest.IngestionBus(providers=[_Fixed()],
                              preferred_provider="claude",
                              now=lambda: 1_700_000_000)
    assert bus.set_preferred("PROVIDERSECRET\x1b[2J") is False
    err = capsys.readouterr().err
    assert "PROVIDERSECRET" not in err and "\x1b" not in err
    assert "not reporting; keeping 'claude'" in err
