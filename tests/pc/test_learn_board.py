"""board.json learns from every message, not only the first one."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from claude_usage_bridge import learn_board


def test_a_pref_first_keeps_what_was_known():
    known = {"port": "COM3", "board_id": "abc", "fw": "1.2.0"}
    assert learn_board(known, "COM3", {"t": "pref", "provider": "codex"}) == known


def test_a_hello_after_a_pref_fills_in_the_nulls():
    known = learn_board({}, "COM3", {"t": "pref", "provider": "codex"})
    assert known == {"port": "COM3", "board_id": None, "fw": None}
    hello = {"t": "hello", "board_id": "20500d342b68", "fw": "1.2.1"}
    assert learn_board(known, "COM3", hello) == \
        {"port": "COM3", "board_id": "20500d342b68", "fw": "1.2.1"}


def test_a_new_firmware_replaces_the_old_one():
    known = {"port": "COM3", "board_id": "abc", "fw": "1.2.0"}
    assert learn_board(known, "COM3", {"t": "hello", "board_id": "abc",
                                       "fw": "1.2.1"})["fw"] == "1.2.1"


def test_the_port_is_always_the_one_in_use():
    known = {"port": "COM3", "board_id": "abc", "fw": "1.2.0"}
    assert learn_board(known, "COM9", {"t": "ping"})["port"] == "COM9"


def test_an_ota_query_teaches_the_firmware():
    """ota_query spells it `cur`. A board only says hello when it boots, so
    after a USB flash every message the daemon sees is an ota_query and the
    old version used to persist -- `blink status` reported 1.2.4 for a board
    answering `cur: 1.2.5` in the same log (2026-08-31)."""
    known = {"port": "COM3", "board_id": "abc", "fw": "1.2.4"}
    assert learn_board(known, "COM3",
                       {"t": "ota_query", "v": 2, "cur": "1.2.5"})["fw"] == "1.2.5"


def test_an_ota_query_without_a_version_keeps_what_was_known():
    known = {"port": "COM3", "board_id": "abc", "fw": "1.2.5"}
    for msg in ({"t": "ota_query", "v": 2}, {"t": "ota_query", "v": 2, "cur": ""}):
        assert learn_board(known, "COM3", msg)["fw"] == "1.2.5", msg


def test_a_hello_still_wins_over_cur():
    """Both present is not a real message, but hello is the authoritative
    one, so it must not be quietly outranked by field order."""
    known = {"port": "COM3", "board_id": "abc", "fw": "1.2.0"}
    msg = {"t": "hello", "board_id": "abc", "fw": "1.2.5", "cur": "1.2.4"}
    assert learn_board(known, "COM3", msg)["fw"] == "1.2.5"
