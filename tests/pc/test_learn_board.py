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
