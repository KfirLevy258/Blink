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


def test_old_payload_is_marked_stale():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 5.0,
                                             "resets_at": 1_787_203_200}}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000 - 600)
    assert msg["stale"] is True


def test_reset_countdown_never_goes_negative():
    payload = {"rate_limits": {"five_hour": {"used_percentage": 99.0,
                                             "resets_at": 1_787_100_000}}}
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert msg["session_resets_in_s"] == 0


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
