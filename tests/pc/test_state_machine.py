"""Execution state, derived from events Claude Code already announces."""
import json

from pc.providers import base
from pc.providers.claude_state import (ABANDONED_AFTER_S, STUCK_AFTER_S,
                                       ClaudeStateProvider, derive_state)

NOW = 1_787_700_000.0


def test_a_started_turn_is_running():
    assert derive_state("UserPromptSubmit", 2.0) == base.STATE_RUNNING
    assert derive_state("PreToolUse", 2.0) == base.STATE_RUNNING
    assert derive_state("PostToolUse", 2.0) == base.STATE_RUNNING


def test_a_completed_turn_is_idle():
    assert derive_state("Stop", 2.0) == base.STATE_IDLE
    assert derive_state("SessionEnd", 2.0) == base.STATE_IDLE


def test_a_notification_is_waiting_not_stuck():
    """Notification is the event Claude Code fires when it needs a human --
    authoritative, where 'is the process blocked on stdin' was a guess."""
    assert derive_state("Notification", 2.0) == base.STATE_WAITING


def test_a_person_taking_their_time_is_still_waiting():
    """A prompt waiting for a human is not wedged, however long it waits."""
    assert derive_state("Notification", STUCK_AFTER_S * 5) == base.STATE_WAITING


def test_a_completed_turn_stays_idle_however_long_the_silence():
    """Silence after a finished turn is the expected condition, not a fault."""
    assert derive_state("Stop", STUCK_AFTER_S * 5) == base.STATE_IDLE


def test_a_silent_running_turn_becomes_stuck():
    assert derive_state("PreToolUse", STUCK_AFTER_S + 1) == base.STATE_STUCK


def test_a_slow_build_is_not_stuck():
    """The document says 60 s. A test suite, an npm install and a slow model
    response all routinely exceed that while being perfectly healthy, and an
    alert that cries wolf on every build gets ignored the once it is right."""
    assert derive_state("PreToolUse", 90.0) == base.STATE_RUNNING


def test_an_abandoned_session_says_nothing_rather_than_idle():
    """'idle' is a claim about a live session. An hour of silence is better
    described by leaving the indicator dark."""
    assert derive_state("Stop", ABANDONED_AFTER_S + 1) == base.STATE_UNKNOWN


def test_an_unknown_event_says_nothing():
    """Newer Claude Code, most likely. Silence beats guessing."""
    assert derive_state("SomethingNew", 1.0) == base.STATE_UNKNOWN


def test_a_clock_that_went_backwards_does_not_report_a_confident_state():
    """A laptop waking or an NTP step gives a negative age, which would sail
    under every threshold below it."""
    assert derive_state("PreToolUse", -5000.0) == base.STATE_RUNNING


# --- the provider ---------------------------------------------------------


def test_this_source_carries_no_usage_percentage():
    """It contributes one field. It must never be able to make a panel look
    fresher than its numbers are, which is why merge() cannot pick it as the
    primary source."""
    p = ClaudeStateProvider()
    f = p.parse_cli_event({"event": "PreToolUse", "t": NOW}, NOW)
    assert f.state == base.STATE_RUNNING
    assert f.session_pct == base.UNKNOWN
    assert f.weekly_pct == base.UNKNOWN
    assert f.has_usage() is False


def test_a_missing_state_file_is_silence(tmp_path):
    p = ClaudeStateProvider(path=str(tmp_path / "nope.json"))
    assert p.poll(NOW) == []


def test_malformed_state_is_silence(tmp_path):
    f = tmp_path / "state.json"
    f.write_text("{not json")
    assert ClaudeStateProvider(path=str(f)).poll(NOW) == []


def test_a_state_file_without_a_timestamp_is_silence():
    p = ClaudeStateProvider()
    assert p.parse_cli_event({"event": "PreToolUse"}, NOW) is None


def test_poll_reads_what_the_shim_writes(tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"event": "Stop", "t": NOW - 5}))
    frames = ClaudeStateProvider(path=str(f)).poll(NOW)
    assert frames[0].state == base.STATE_IDLE


def test_the_stuck_threshold_is_configurable(tmp_path):
    f = tmp_path / "state.json"
    f.write_text(json.dumps({"event": "PreToolUse", "t": NOW - 90}))
    assert ClaudeStateProvider(path=str(f), stuck_after_s=60.0
                               ).poll(NOW)[0].state == base.STATE_STUCK
    assert ClaudeStateProvider(path=str(f), stuck_after_s=300.0
                               ).poll(NOW)[0].state == base.STATE_RUNNING
