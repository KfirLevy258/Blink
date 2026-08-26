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
    assert msg["models"] == []
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
    assert msg["models"] == []


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
    its owner simply paused to read -- observed on hardware.
    """
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 50, "resets_at": now + 3600},
        "seven_day": {"used_percentage": 20, "resets_at": now + 90000}}}
    msg = ss.map_statusline(payload, now, now - 600)   # ten minutes idle
    assert msg["stale"] is False


def test_a_window_that_has_reset_reads_zero_rather_than_stale():
    """A reading taken a minute before the reset says 50% when usage is back
    at zero -- but "zero" is knowable, so report it instead of a warning.

    The whole message used to be flagged stale here, which put the board in
    its amber state until Claude Code next rendered. On a window that rolls
    over overnight that is hours of a warning about nothing.
    """
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 50, "resets_at": now - 1},
        "seven_day": {"used_percentage": 20, "resets_at": now + 90000}}}
    msg = ss.map_statusline(payload, now, now - 5)     # five seconds old
    assert msg["stale"] is False
    assert msg["session_pct"] == 0.0
    # Unknown, not guessed forward: the next window starts on the next
    # message, so there is no honest time to show yet.
    assert msg["session_resets_in_s"] == -1


def test_a_reset_window_does_not_drag_down_the_other_one():
    """The weekly figure was collateral damage: one window resetting marked
    the entire message stale, including a seven-day reading that was fine.
    """
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 95, "resets_at": now - 1},
        "seven_day": {"used_percentage": 70, "resets_at": now + 90000}}}
    msg = ss.map_statusline(payload, now, now - 5)
    assert msg["weekly_pct"] == 70
    assert msg["weekly_resets_in_s"] == 90000
    assert msg["stale"] is False


def test_an_old_payload_past_its_reset_stays_stale_and_is_not_zeroed():
    """The inverse case, and the reason _rolled_over() is gated on freshness.

    A three-day-old file also has a long-past resets_at, but any amount of
    usage may have happened since it was written -- from claude.ai, from the
    phone -- so 0% would be the confident lie this module exists to avoid.
    """
    now = 1_000_000
    payload = {"rate_limits": {
        "five_hour": {"used_percentage": 50, "resets_at": now - 200_000},
        "seven_day": {"used_percentage": 20, "resets_at": now - 100_000}}}
    msg = ss.map_statusline(payload, now, now - 3 * 86400)
    assert msg["stale"] is True
    assert msg["session_pct"] == 50      # left exactly as found
    assert msg["weekly_pct"] == 20



# --- context window and model name (handoff doc S3A1) ---------------------


def test_context_window_is_extracted():
    payload = {"context_window": {"used_percentage": 55},
               "rate_limits": {"five_hour": {"used_percentage": 3.0,
                                             "resets_at": 1_787_203_200}}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert msg["ctx_pct"] == 55.0


def test_model_display_name_is_extracted():
    payload = {"model": {"id": "claude-opus-5", "display_name": "Opus 5"},
               "rate_limits": {}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert msg["model"] == "Opus 5"


def test_absent_context_window_omits_the_key_entirely():
    """An absent key is how 'unknown' is spelled on the wire -- a sentinel
    would spend line budget to say nothing. The firmware defaults to -1."""
    msg = ss.map_statusline({"rate_limits": {}}, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert "ctx_pct" not in msg
    assert "model" not in msg


def test_a_context_percentage_out_of_range_is_refused():
    """340% means we misread the field, not that the context is 340% full."""
    payload = {"context_window": {"used_percentage": 340}, "rate_limits": {}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert "ctx_pct" not in msg


def test_a_non_numeric_context_percentage_is_refused():
    payload = {"context_window": {"used_percentage": "lots"}, "rate_limits": {}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert "ctx_pct" not in msg


def test_a_stale_payload_drops_context_and_model_but_keeps_the_windows():
    """The windows survive going stale; these two must not.

    A billing window is still running, so its last known figure is still worth
    an amber dot. A context percentage describes one conversation that an
    abandoned Claude Code has very likely left, and a model name from
    yesterday names yesterday's model.
    """
    payload = {
        "context_window": {"used_percentage": 55},
        "model": {"display_name": "Opus 5"},
        "rate_limits": {"five_hour": {"used_percentage": 37.0,
                                      "resets_at": 1_787_300_000}},
    }
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000 - ss.STALE_AFTER_S - 1)
    assert msg["stale"] is True
    assert msg["session_pct"] == 37.0     # kept
    assert "ctx_pct" not in msg           # dropped
    assert "model" not in msg             # dropped


def test_every_message_names_its_provider_and_source():
    msg = ss.map_statusline({"rate_limits": {}}, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert msg["provider"] == "claude"
    assert msg["src"] == "cli"


def test_the_real_capture_fits_the_board_line_limit():
    """The board DROPS an over-long line whole (proto.c:367-371) -- no
    truncation, no error, the panel just stops updating. Every field added to
    this message spends that budget, so the real capture is pinned here."""
    from pc import protocol
    payload = json.loads(FIXTURE.read_text())
    msg = ss.map_statusline(payload, now_epoch=1_787_300_000,
                            mtime_epoch=1_787_300_000)
    raw, why = protocol.encode_checked(msg)
    assert why is None, why
    assert len(raw) <= protocol.MAX_LINE_BYTES
