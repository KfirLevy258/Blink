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


def test_absent_window_reports_unknown_not_zero():
    """A missing window must render '--', never a confident 0%."""
    msg = ss.map_statusline({"rate_limits": {}}, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
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


def test_real_captured_payload_maps_without_error():
    payload = json.loads(FIXTURE.read_text())
    msg = ss.map_statusline(payload, now_epoch=1_787_200_000,
                            mtime_epoch=1_787_200_000)
    assert "session_pct" in msg
