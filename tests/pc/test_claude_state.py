"""claude_state.session_details: the per-session rows of the sessions card.

The state machine itself is pinned in tests/pc/test_state_machine.py. What is
pinned here is what the card adds on top of it: each live session's name,
age, tool and event, read in the SAME pass as the census, so the card and
the panel's light cannot disagree about which sessions exist. Every slot is
written by this file in the shape tools/blink-hook.sh writes.
"""
import json
import os

import pytest

from pc.providers import base, claude_state

NOW = 1_700_000_000.0


def _slot(d, sid, **fields):
    (d / (sid + ".state")).write_text(json.dumps(fields))


def _stamp(path, t):
    os.utime(path, (t, t))


@pytest.fixture
def d(tmp_path):
    p = tmp_path / "state"
    p.mkdir()
    return p


def _prov(d):
    return claude_state.ClaudeStateProvider(path=str(d), sweep=False)


def test_a_slot_with_a_tool_reports_it(d):
    _slot(d, "a", event="PreToolUse", t=int(NOW - 12), name="Blink",
          tool="Edit")
    assert _prov(d).session_details(NOW) == {
        "a": (base.STATE_RUNNING, "Blink", 12.0, "Edit", "PreToolUse")}


def test_a_slot_without_a_tool_reports_none(d):
    _slot(d, "a", event="PostToolUse", t=int(NOW - 3), name="Blink")
    assert _prov(d).session_details(NOW)["a"] == (
        base.STATE_RUNNING, "Blink", 3.0, "", "PostToolUse")


def test_an_old_shim_slot_with_no_name_and_no_tool_still_reads(d):
    # The first shim wrote an event and a clock and nothing else.
    _slot(d, "a", event="Stop", t=int(NOW - 600))
    assert _prov(d).session_details(NOW)["a"] == (
        base.STATE_IDLE, "", 600.0, "", "Stop")


def test_a_tool_that_is_not_a_string_is_dropped(d):
    _slot(d, "a", event="PreToolUse", t=int(NOW - 1), tool=["Edit"])
    assert _prov(d).session_details(NOW)["a"][3] == ""


def test_a_waiting_session_is_aged_by_its_marker_not_its_slot(d):
    # The same-second PreToolUse rewrote the slot two seconds ago; the person
    # was asked five minutes ago, and that is what the row says.
    _slot(d, "a", event="PreToolUse", t=int(NOW - 2), tool="Bash")
    marker = d / ("a" + claude_state.WAITING_MARKER_SUFFIX)
    marker.write_text("")
    _stamp(marker, NOW - 300)
    state, _name, age, tool, event = _prov(d).session_details(NOW)["a"]
    assert state == base.STATE_WAITING and age == 300.0
    assert (tool, event) == ("Bash", "PreToolUse")


def test_a_clock_ahead_gives_age_zero_not_negative(d):
    _slot(d, "a", event="Stop", t=int(NOW + 30))
    assert _prov(d).session_details(NOW)["a"][2] == 0.0


def test_unknown_sessions_have_no_row(d):
    _slot(d, "open", event="SessionStart", t=int(NOW - 5))
    _slot(d, "gone", event="Stop", t=int(NOW - claude_state.ABANDONED_AFTER_S
                                         - 60))
    assert _prov(d).session_details(NOW) == {}


def test_session_states_is_the_same_reading_cut_down(d):
    _slot(d, "a", event="PreToolUse", t=int(NOW - 4), name="A", tool="Write")
    _slot(d, "b", event="Stop", t=int(NOW - 9), name="B")
    prov = _prov(d)
    states, agents = prov.session_states(NOW)
    assert states == {"a": (base.STATE_RUNNING, "A"),
                      "b": (base.STATE_IDLE, "B")}
    assert agents == 0
    # And the reading is remembered for the bus, which builds the card from
    # the same poll that lit the panel.
    assert prov.last_session_details() == {
        "a": (base.STATE_RUNNING, "A", 4.0, "Write", "PreToolUse"),
        "b": (base.STATE_IDLE, "B", 9.0, "", "Stop")}


def test_poll_and_scan_shapes_are_unchanged(d):
    _slot(d, "a", event="PreToolUse", t=int(NOW - 4), name="A", tool="Edit")
    prov = _prov(d)
    counts, names, agents = prov.scan(NOW)
    assert counts == {base.STATE_RUNNING: 1}
    assert names == {base.STATE_RUNNING: ["A"]}
    frames = prov.poll(NOW)
    assert len(frames) == 1 and frames[0].label == "A"


def test_no_directory_is_no_rows(tmp_path):
    prov = claude_state.ClaudeStateProvider(path=str(tmp_path / "nope"),
                                            sweep=False)
    assert prov.session_details(NOW) == {}
    assert prov.last_session_details() == {}
