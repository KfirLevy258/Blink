import json
import pathlib

from pc import statusline_source as ss

FIXTURE = pathlib.Path(__file__).parent / "fixtures" / "statusline_payload.json"


def test_maps_both_windows():
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 37.2, "resets_at": 1_787_203_200},
            "seven_day": {"used_percentage": 12.9, "resets_at": 1_787_644_800},
        }
    }
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000, mtime_epoch=1_787_200_000)
    assert msg["session_pct"] == 37.2
    assert msg["weekly_pct"] == 12.9
    assert msg["session_resets_in_s"] == 3200
    assert msg["weekly_resets_in_s"] == 444800
    assert "models" not in msg   # the array never reaches the wire
    assert msg["stale"] is False


def test_absent_five_hour_reports_unknown_not_zero():
    """A missing five_hour window must render '--', never a confident 0%."""
    payload = {"rate_limits": {"seven_day": {"used_percentage": 8.0,
                                             "resets_at": 1_787_644_800}}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert msg["session_pct"] == -1.0
    assert msg["session_resets_in_s"] == -1
    assert msg["weekly_pct"] == 8.0  # the present window is unaffected


def test_absent_seven_day_reports_unknown_not_zero():
    """A missing seven_day window must render '--', never a confident 0%."""
    payload = {"rate_limits": {"five_hour": {"used_percentage": 8.0,
                                             "resets_at": 1_787_203_200}}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert msg["weekly_pct"] == -1.0
    assert msg["weekly_resets_in_s"] == -1
    assert msg["session_pct"] == 8.0  # the present window is unaffected


def test_rate_limits_absent_entirely_reports_unknown_not_zero():
    """No 'rate_limits' key at all -- not even an empty object -- must still
    render both windows as unknown, never a confident 0%."""
    msg = ss.map_statusline({}, now_epoch=1_787_200_000, mtime_epoch=1_787_200_000)
    assert msg["session_pct"] == -1.0
    assert msg["weekly_pct"] == -1.0
    assert msg["session_resets_in_s"] == -1
    assert msg["weekly_resets_in_s"] == -1


def test_an_abandoned_payload_is_marked_stale():
    """Was written against a 120s threshold with a ten-minute-old payload.
    Ten minutes is now deliberately not stale -- it is someone reading their
    screen -- so this pins the case the rule is actually for: a Claude Code
    that stopped writing hours ago, presented as live.
    """
    payload = {"rate_limits": {"five_hour": {"used_percentage": 5.0,
                                             "resets_at": 1_787_203_200}}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000 - 7200)
    assert msg["stale"] is True


def test_a_past_reset_reads_unknown_not_zero():
    """0 is not a spare value: usage_view.c reads a countdown of exactly 0 as
    "this window just rolled over" and zeroes the percentage with it. That is
    right for the firmware's own countdown running out and wrong coming from
    here, because the only payload that reaches _secs_until with a past reset
    is a stale one -- the case _rolled_over() deliberately refuses to zero.
    """
    assert ss._secs_until(1_787_100_000, 1_787_200_000) == -1
    assert ss._secs_until(1_787_300_000, 1_787_200_000) == 100_000
    assert ss._secs_until(None, 1_787_200_000) == -1


def test_a_stale_payload_keeps_its_numbers(tmp_path=None):
    """An old reading is not a wrong one. With no reset stamp there is no
    evidence the window it describes has ended, so its last known percentage
    is still the best thing we have -- age is reported as age and the number
    stands. (A stamp the clock HAS passed is different in kind and is the
    test below.)"""
    now = 1_787_200_000
    payload = {"rate_limits": {"five_hour": {"used_percentage": 99.0}}}
    msg = ss.map_statusline(payload, now_epoch=now, mtime_epoch=now - 3 * 86400)
    assert msg["stale"] is True
    assert msg["session_pct"] == 99.0
    assert msg["session_resets_in_s"] == -1


def test_a_stale_reading_of_a_window_that_has_ended_is_not_offered_at_all():
    """The other half, and the one that was wrong (field defect 2026-09-02).

    A three-day-old file whose five-hour window rolled two days ago is not
    holding "the best number we have" -- it is holding a number about a
    window that no longer exists, and re-offering it is exactly the invented,
    already-forgiven usage pc/normalizer's docstring says the design exists
    to prevent. It goes to UNKNOWN rather than to 0: any amount of usage may
    have happened in the days since, so zero would be a confident lie in the
    other direction.
    """
    now = 1_787_200_000
    payload = {"rate_limits": {"five_hour": {"used_percentage": 99.0,
                                             "resets_at": now - 100_000}}}
    f = ss.map_statusline_frame(payload, now, now - 3 * 86400)
    assert f.stale is True
    assert f.session_pct == -1.0
    assert f.session_resets_at is None
    # And the fact that outlives the reading: some source watched this window
    # empty, which is what stops another source's pre-reset sample from
    # taking the dial (pc/normalizer._survives_rollover).
    assert f.session_rolled_at == now - 100_000


def test_a_window_present_but_missing_its_percentage_is_unknown():
    """The likeliest shape of a payload change: the object is still there, the
    field is not. It used to read as a confident 0%."""
    now = 1_787_200_000
    for w in ({"resets_at": now + 60}, {"used_percentage": None, "resets_at": now + 60},
              {"used_percentage": "n/a", "resets_at": now + 60}):
        msg = ss.map_statusline({"rate_limits": {"five_hour": w}}, now, now)
        assert msg["session_pct"] == -1.0, w

NOW = 1_787_900_000.0


# --- the rollover, and the epoch it now reports ------------------------------
#
# Six tests pinning _rolled_over were deleted from this file on this branch,
# which is how the normalizer came to be able to discard the very mechanism
# they covered. These re-pin it, including the field that fixes that.


def test_a_reset_window_reads_zero_and_reports_when_it_emptied():
    at = NOW - 60
    pct, resets, rolled = ss._rolled_over(47.0, at, NOW, stale=False)
    assert pct == 0.0
    assert resets is None
    assert rolled == at, "the epoch is the evidence the normalizer needs"


def test_a_stale_reading_reports_the_rollover_it_watched_but_claims_no_zero():
    """The same window, the same evidence, a different answer. Zero is what
    a reset MEANS and it is knowable only while the reading is fresh; the
    epoch is a fact about the past and is reported either way. Skipping the
    whole check on an old payload kept neither -- it kept the pre-reset 47%,
    which is the one answer that is definitely wrong."""
    at = NOW - 60
    assert ss._rolled_over(47.0, at, NOW, stale=True) == (-1.0, None, at)


def test_a_window_that_has_not_reset_is_untouched():
    at = NOW + 3600
    assert ss._rolled_over(47.0, at, NOW, stale=False) == (47.0, at, None)
    assert ss._rolled_over(47.0, at, NOW, stale=True) == (47.0, at, None)


def test_an_unknown_percentage_is_never_zeroed():
    """A percentage we never read does not become 0 because the window it
    would have described has ended. The stamp still goes -- it is past, so it
    is neither a countdown nor a reset time anyone has -- and the rollover is
    still reported, because that much IS known."""
    assert ss._rolled_over(-1.0, NOW - 60, NOW, stale=False) == (
        -1.0, None, NOW - 60)


def test_no_reset_time_means_no_rollover_claim():
    assert ss._rolled_over(47.0, None, NOW, stale=False) == (47.0, None, None)
    assert ss._rolled_over(47.0, None, NOW, stale=True) == (47.0, None, None)


# --- restored ---------------------------------------------------------------
#
# These were deleted on the multi-provider branch and only _rolled_over() was
# re-pinned in their place, which left the freshness gate, the reset-to-zero
# rule, the real capture and the read paths untested. The assertions on a
# `models` key are gone with the key itself.


def test_real_capture_maps_without_error():
    """FIXTURE is a real Claude Code 2.1.229 capture (2026-08-21) with
    identifying fields redacted; rate_limits is verbatim -- see its "_comment".
    Asserts the values, not just the keys: this is the payload shape the
    product actually depends on, so a schema change here should fail loudly."""
    payload = json.loads(FIXTURE.read_text())
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert msg["session_pct"] == 25.0
    assert msg["weekly_pct"] == 42.0
    assert msg["session_resets_in_s"] == 120_800
    assert msg["weekly_resets_in_s"] == 524_000


def test_read_payload_missing_file_returns_none_none(tmp_path):
    payload, mtime = ss.read_payload(str(tmp_path / "does_not_exist.json"))
    assert payload is None
    assert mtime is None


def test_read_payload_malformed_json_returns_none_none(tmp_path):
    bad = tmp_path / "statusline.json"
    bad.write_text("{not valid json")
    payload, mtime = ss.read_payload(str(bad))
    assert payload is None
    assert mtime is None


def test_make_fetch_returns_none_with_no_payload(tmp_path):
    fetch = ss.make_fetch(str(tmp_path / "does_not_exist.json"))
    assert fetch() is None


def test_a_short_pause_is_not_stale():
    """The file's age measures how long the user has been idle, not how wrong
    the numbers are. A 120s threshold made the panel flap amber/green while
    its owner simply paused to read -- observed on hardware."""
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 50, "resets_at": now + 3600},
        "seven_day": {"used_percentage": 20, "resets_at": now + 90000}}}
    msg = ss.map_statusline(payload, now, now - 600)   # ten minutes idle
    assert msg["stale"] is False


def test_a_window_that_has_reset_reads_zero_rather_than_stale():
    """A reading taken a minute before the reset says 50% when usage is back
    at zero -- but "zero" is knowable, so report it instead of a warning."""
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 50, "resets_at": now - 1},
        "seven_day": {"used_percentage": 20, "resets_at": now + 90000}}}
    msg = ss.map_statusline(payload, now, now - 5)     # five seconds old
    assert msg["stale"] is False
    assert msg["session_pct"] == 0.0
    assert msg["session_resets_in_s"] == -1


def test_a_reset_window_does_not_drag_down_the_other_one():
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 95, "resets_at": now - 1},
        "seven_day": {"used_percentage": 70, "resets_at": now + 90000}}}
    msg = ss.map_statusline(payload, now, now - 5)
    assert msg["weekly_pct"] == 70
    assert msg["weekly_resets_in_s"] == 90000
    assert msg["stale"] is False


def test_an_old_payload_past_its_reset_is_neither_zeroed_nor_believed():
    """A three-day-old file also has a long-past resets_at, but any amount of
    usage may have happened since -- so 0% would be the confident lie this
    module exists to avoid.

    It does not follow that the pre-reset number may stand, which is what the
    code did for a year and what put an already-forgiven 78% on a real panel
    under a green dot. Both windows here ended before `now`; both go to
    unknown, and both report when they ended.
    """
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 50, "resets_at": now - 200_000},
        "seven_day": {"used_percentage": 20, "resets_at": now - 100_000}}}
    msg = ss.map_statusline(payload, now, now - 3 * 86400)
    assert msg["stale"] is True
    assert msg["session_pct"] == -1.0
    assert msg["weekly_pct"] == -1.0
    f = ss.map_statusline_frame(payload, now, now - 3 * 86400)
    assert (f.session_rolled_at, f.weekly_rolled_at) == (now - 200_000,
                                                         now - 100_000)


def test_a_payload_of_the_wrong_shape_is_silence_not_an_exception():
    """The file is written by a shell shim from whatever Claude Code sent.
    Anything that is not an object with an object under rate_limits must
    read as no data -- an exception here takes the CLI source off the bus
    for the rest of the process (pc/ingest)."""
    now = 1_000_000
    for bad in ([], "x", 3, {"rate_limits": "x"}, {"rate_limits": []},
                {"rate_limits": {"five_hour": "x", "seven_day": 7}}):
        msg = ss.map_statusline(bad, now, now) if isinstance(bad, dict) \
            else ss.map_statusline({"payload": bad}, now, now)
        assert msg["session_pct"] == -1.0, bad
        assert msg["weekly_pct"] == -1.0, bad
