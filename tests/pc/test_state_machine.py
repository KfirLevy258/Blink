"""Execution state, derived from events Claude Code already announces.

Two halves: the pure event->state rule, and the directory scan that turns many
sessions and their agents into the handful of numbers the wire can carry.
"""
import json
import os

import pytest

from pc.providers import base
from pc.providers.claude_state import (ABANDONED_AFTER_S,
                                       AGENT_ABANDONED_AFTER_S, STUCK_AFTER_S,
                                       ClaudeStateProvider, derive_state,
                                       worst_of)

NOW = 1_787_700_000.0


# --- the rule -------------------------------------------------------------


def test_a_started_turn_is_running():
    for e in ("UserPromptSubmit", "PreToolUse", "PostToolUse"):
        assert derive_state(e, 2.0) == base.STATE_RUNNING, e


def test_an_opened_session_claims_nothing_and_never_becomes_stuck():
    """`claude`, then nothing: the person is reading, or opened it for later.
    Filed as running, this went red after three minutes and stayed red for
    an hour -- on the most ordinary thing a terminal does. Filed as idle,
    it would now paint the "your turn" amber for a terminal nobody has asked
    anything of. It says nothing."""
    assert derive_state("SessionStart", 2.0) == base.STATE_UNKNOWN
    assert derive_state("SessionStart", STUCK_AFTER_S * 5) == base.STATE_UNKNOWN


def test_a_slot_with_a_nonsense_timestamp_is_ignored(tmp_path):
    """NaN compares false with everything, so neither the stuck test nor the
    abandoned sweep would ever fire: a permanent 'running' nothing collects."""
    d = tmp_path / "state"
    d.mkdir()
    for bad in ("NaN", "Infinity", "1787700000000", "12"):
        (d / "x.state").write_text('{"event":"PreToolUse","t":%s}' % bad)
        counts, _ = ClaudeStateProvider(path=str(d)).scan(NOW)
        assert counts == {}, bad


def test_a_completed_turn_is_idle():
    assert derive_state("Stop", 2.0) == base.STATE_IDLE


def test_an_ended_session_is_not_waiting_on_anyone():
    """idle means "finished, read me". A session that is over has nothing to
    read; it must not hold the amber light for the next hour."""
    assert derive_state("SessionEnd", 2.0) == base.STATE_UNKNOWN


def test_a_notification_is_waiting_not_stuck():
    """Notification is the event Claude Code fires when it needs a human --
    authoritative, where 'is the process blocked on stdin' was a guess."""
    assert derive_state("Notification", 2.0) == base.STATE_WAITING
    assert derive_state("PermissionRequest", 2.0) == base.STATE_WAITING


def test_an_api_error_is_its_own_state():
    """StopFailure carries error: 'rate_limit' among its causes, and on a
    usage gauge that is the headline, not a detail."""
    assert derive_state("StopFailure", 2.0) == base.STATE_FAILED


def test_a_failed_turn_does_not_decay_into_stuck():
    """It is not wedged. It is finished and unsuccessful."""
    assert derive_state("StopFailure", STUCK_AFTER_S * 5) == base.STATE_FAILED


def test_a_person_taking_their_time_is_still_waiting():
    assert derive_state("Notification", STUCK_AFTER_S * 5) == base.STATE_WAITING


def test_a_completed_turn_stays_idle_however_long_the_silence():
    assert derive_state("Stop", STUCK_AFTER_S * 5) == base.STATE_IDLE


def test_a_silent_running_turn_becomes_stuck():
    assert derive_state("PreToolUse", STUCK_AFTER_S + 1) == base.STATE_STUCK


def test_a_slow_build_is_not_stuck():
    """The document says 60 s. A test suite, an npm install and a slow model
    response all routinely exceed that while being perfectly healthy."""
    assert derive_state("PreToolUse", 90.0) == base.STATE_RUNNING


def test_an_abandoned_session_says_nothing_rather_than_idle():
    assert derive_state("Stop", ABANDONED_AFTER_S + 1) == base.STATE_UNKNOWN


def test_an_unknown_event_says_nothing():
    assert derive_state("SomethingNew", 1.0) == base.STATE_UNKNOWN


def test_a_clock_that_went_backwards_reads_as_fresh():
    """A negative age is clamped to zero rather than allowed to sail under
    every threshold -- so it is the running state a fresh event earns, not a
    confident 'stuck' or 'abandoned' from a broken measurement."""
    assert derive_state("PreToolUse", -5000.0) == base.STATE_RUNNING


# --- collapsing many sessions to one indicator ----------------------------


def test_worst_of_ranks_a_rate_limit_above_everything():
    """A rate limit is the one condition this gauge exists to surface; a
    wedged tool is a distant second."""
    assert worst_of({base.STATE_FAILED, base.STATE_STUCK,
                     base.STATE_RUNNING}) == base.STATE_FAILED


def test_worst_of_ranks_stuck_above_waiting_above_idle_above_running():
    assert worst_of({base.STATE_STUCK, base.STATE_WAITING}) == base.STATE_STUCK
    assert worst_of({base.STATE_WAITING, base.STATE_RUNNING}) == base.STATE_WAITING
    assert worst_of({base.STATE_WAITING, base.STATE_IDLE}) == base.STATE_WAITING
    assert worst_of({base.STATE_IDLE}) == base.STATE_IDLE


def test_one_finished_session_shows_through_any_number_of_running_ones():
    """The light is a claim on the person. A finished answer is waiting on
    them; the sessions still working are not. Ranking running above idle
    (the old order) hid the finished one behind a green pulse for as long as
    anything else was busy (user decision 2026-08-29)."""
    assert worst_of({base.STATE_RUNNING, base.STATE_IDLE}) == base.STATE_IDLE
    assert worst_of({base.STATE_RUNNING, base.STATE_RUNNING,
                     base.STATE_IDLE}) == base.STATE_IDLE


def test_worst_of_nothing_is_nothing():
    assert worst_of(set()) == base.STATE_UNKNOWN


# --- the directory --------------------------------------------------------


def write_session(d, sid, event, t):
    (d / f"{sid}.state").write_text(json.dumps({"event": event, "t": t}))


def add_agent(d, sid, aid, t=None):
    sd = d / sid
    sd.mkdir(exist_ok=True)
    p = sd / aid
    p.write_text("")
    if t is not None:
        os.utime(p, (t, t))


@pytest.fixture
def state_dir(tmp_path):
    d = tmp_path / "state"
    d.mkdir()
    return d


def provider(state_dir, **kw):
    return ClaudeStateProvider(path=str(state_dir), **kw)


def test_no_directory_at_all_is_silence(tmp_path):
    assert ClaudeStateProvider(path=str(tmp_path / "nope")).poll(NOW) == []


def test_an_empty_directory_is_silence(state_dir):
    assert provider(state_dir).poll(NOW) == []


def test_one_running_session(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.state == base.STATE_RUNNING
    assert (f.n_run, f.n_sessions()) == (1, 1)


def test_two_sessions_do_not_overwrite_each_other(state_dir):
    """The whole reason for one file per session. A single global slot showed
    whichever fired most recently and silently misreported the other."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    write_session(state_dir, "s2", "Notification", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_sessions() == 2
    assert (f.n_run, f.n_wait) == (1, 1)
    assert f.state == base.STATE_WAITING     # worst of the two


def test_the_indicator_shows_the_worst_session(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    write_session(state_dir, "s2", "Stop", NOW - 5)
    write_session(state_dir, "s3", "StopFailure", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.state == base.STATE_FAILED
    assert f.n_sessions() == 3


def test_agents_are_counted_exactly(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    add_agent(state_dir, "s1", "a1")
    add_agent(state_dir, "s1", "a2")
    add_agent(state_dir, "s1", "a3")
    assert provider(state_dir).poll(NOW)[0].n_agents == 3


def test_agents_are_summed_across_sessions(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    write_session(state_dir, "s2", "PreToolUse", NOW - 5)
    add_agent(state_dir, "s1", "a1")
    add_agent(state_dir, "s2", "b1")
    add_agent(state_dir, "s2", "b2")
    assert provider(state_dir).poll(NOW)[0].n_agents == 3


def test_a_session_with_no_agents_counts_zero(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    assert provider(state_dir).poll(NOW)[0].n_agents == 0


def test_an_abandoned_agent_is_swept_not_counted_forever(state_dir):
    """Its SubagentStop never fired because the session died mid-flight.
    A panel that says '3 agents' forever is worse than one that says
    nothing."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    add_agent(state_dir, "s1", "live")
    # Older than the SESSION threshold but younger than the agent one: an
    # agent file's mtime is its start, and a long run is not a dead one.
    add_agent(state_dir, "s1", "long", t=NOW - ABANDONED_AFTER_S - 100)
    add_agent(state_dir, "s1", "dead", t=NOW - AGENT_ABANDONED_AFTER_S - 100)
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_agents == 2
    assert not (state_dir / "s1" / "dead").exists()
    assert (state_dir / "s1" / "long").exists()
    assert (state_dir / "s1" / "live").exists()


def test_an_abandoned_session_is_swept_entirely(state_dir):
    """A killed terminal leaves files and nothing else will collect them."""
    write_session(state_dir, "s1", "PreToolUse", NOW - ABANDONED_AFTER_S - 100)
    add_agent(state_dir, "s1", "a1")
    assert provider(state_dir).poll(NOW) == []
    assert not (state_dir / "s1.state").exists()
    assert not (state_dir / "s1").exists()


def test_sweeping_can_be_turned_off(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - ABANDONED_AFTER_S - 100)
    provider(state_dir, sweep=False).poll(NOW)
    assert (state_dir / "s1.state").exists()


def test_a_live_session_is_never_swept(state_dir):
    write_session(state_dir, "s1", "Stop", NOW - 5)
    provider(state_dir).poll(NOW)
    assert (state_dir / "s1.state").exists()


def test_a_malformed_session_file_is_skipped_not_fatal(state_dir):
    (state_dir / "bad.state").write_text("{not json")
    write_session(state_dir, "good", "PreToolUse", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.n_sessions() == 1


def test_a_session_file_without_a_timestamp_is_skipped(state_dir):
    (state_dir / "s1.state").write_text(json.dumps({"event": "PreToolUse"}))
    assert provider(state_dir).poll(NOW) == []


def test_stray_files_in_the_directory_are_ignored(state_dir):
    (state_dir / "README").write_text("hello")
    (state_dir / "s1.state").write_text(json.dumps(
        {"event": "PreToolUse", "t": NOW - 5}))
    assert provider(state_dir).poll(NOW)[0].n_sessions() == 1


def test_this_source_carries_no_usage_percentage(state_dir):
    """It contributes execution fields. It must never be able to make a panel
    look fresher than its numbers are, which is why merge() cannot pick it as
    the primary source."""
    write_session(state_dir, "s1", "PreToolUse", NOW - 5)
    f = provider(state_dir).poll(NOW)[0]
    assert f.session_pct == base.UNKNOWN
    assert f.weekly_pct == base.UNKNOWN
    assert f.has_usage() is False


def test_the_stuck_threshold_is_configurable(state_dir):
    write_session(state_dir, "s1", "PreToolUse", NOW - 90)
    assert provider(state_dir, stuck_after_s=60.0
                    ).poll(NOW)[0].state == base.STATE_STUCK
    assert provider(state_dir, stuck_after_s=300.0
                    ).poll(NOW)[0].state == base.STATE_RUNNING
