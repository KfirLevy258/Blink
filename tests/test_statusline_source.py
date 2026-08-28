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
    """The whole point of not rolling a stale window over is that its last
    known percentage is the best thing we have. Sending 0 for the countdown
    let the board discard it anyway."""
    now = 1_787_200_000
    payload = {"rate_limits": {"five_hour": {"used_percentage": 99.0,
                                             "resets_at": now - 100_000}}}
    msg = ss.map_statusline(payload, now_epoch=now, mtime_epoch=now - 3 * 86400)
    assert msg["stale"] is True
    assert msg["session_pct"] == 99.0
    assert msg["session_resets_in_s"] == -1


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
    pct, resets, rolled = ss._rolled_over(47.0, at, NOW)
    assert pct == 0.0
    assert resets is None
    assert rolled == at, "the epoch is the evidence the normalizer needs"


def test_a_window_that_has_not_reset_is_untouched():
    at = NOW + 3600
    assert ss._rolled_over(47.0, at, NOW) == (47.0, at, None)


def test_an_unknown_percentage_is_never_zeroed():
    assert ss._rolled_over(-1.0, NOW - 60, NOW) == (-1.0, NOW - 60, None)


def test_no_reset_time_means_no_rollover_claim():
    assert ss._rolled_over(47.0, None, NOW) == (47.0, None, None)
