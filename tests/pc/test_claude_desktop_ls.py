"""The provider over Claude Desktop's five-hour record.

The tests that matter here are the refusals. A source that goes quiet is
visibly quiet; a source that publishes a stale number is indistinguishable
from a working one until someone misses their limit.
"""
import json

from pc.providers import base
from pc.providers.claude_desktop_ls import ClaudeDesktopLocalStorageProvider
from tests.support import leveldb_fixture as fx

NOW = 1788613600.0
KEY = (b"_https://claude.ai\x00\x01"
       b"claudeai.ochre_heron_tide.3d2fe603-510b-4256-bd4e-2f2b1b689bef")


def _rec(resets_at, utilization, observed_at):
    return {"resetsAt": resets_at, "utilization": utilization,
            "prevUtilization": 0.0, "observedAt": observed_at,
            "atWall": False, "fired": [], "shown": False,
            "shownCowork": False}


def _store(tmp_path, rec):
    (tmp_path / "000001.log").write_bytes(fx.build_log(
        [[("put", KEY, b"\x01" + json.dumps(rec).encode("utf-8"))]]))
    return str(tmp_path)


def test_publishes_the_percentage_and_the_reset():
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, 0.06, NOW - 60), NOW)
    assert f.provider == "claude"
    assert f.src == "desktop_ls"
    assert f.session_pct == 6.0
    assert f.session_resets_at == NOW + 3600
    assert f.stale is False


def test_utilization_is_a_fraction_not_a_percentage():
    """0.06 is six percent. Read the other way it draws an empty ring on a
    window that is filling."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, 0.06, NOW), NOW)
    assert f.session_pct == 6.0


def test_overage_above_one_hundred_percent_survives():
    """Extra usage carries a window past its limit; 102 is a real reading."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, 1.02, NOW), NOW)
    assert f.session_pct == 102.0


def test_an_expired_window_publishes_no_percentage():
    """Measured on a real machine: the record said 19% for a window that had
    ended 43 minutes earlier."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW - 2580, 0.19, NOW - 12000), NOW)
    assert f.session_pct == base.UNKNOWN
    assert f.session_resets_at is None


def test_an_expired_window_reports_when_it_rolled():
    """The normalizer needs this to stop another source's older reading from
    outliving a reset it never saw."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW - 2580, 0.19, NOW - 12000), NOW)
    assert f.session_rolled_at == NOW - 2580


def test_an_old_record_is_stale():
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, 0.06, NOW - 4000), NOW)
    assert f.stale is True


def test_observed_at_is_the_records_own_timestamp():
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, 0.06, NOW - 300), NOW)
    assert f.observed_at == NOW - 300


def test_a_nonsense_reset_timestamp_is_dropped_not_published():
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(99_999_999_999, 0.06, NOW), NOW)
    assert f.session_resets_at is None
    assert f.session_pct == 6.0


def test_at_the_exact_reset_instant_the_window_has_already_rolled():
    """now_epoch == resetsAt is the whole rule in one instant: `>=`, not `>`.
    Getting this backwards leaves all the other tests green and reopens the
    2026-08-28 bug -- a stale percentage published as current."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW, 0.19, NOW - 100), NOW)
    assert f.session_pct == base.UNKNOWN
    assert f.session_resets_at is None
    assert f.session_rolled_at == NOW


def test_one_tick_before_the_reset_the_percentage_still_publishes():
    """The other side of the same boundary: not yet expired."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 1, 0.19, NOW - 100), NOW)
    assert f.session_pct == 19.0
    assert f.session_resets_at == NOW + 1
    assert f.session_rolled_at is None


def test_zero_utilization_publishes_as_zero_not_unknown():
    """0.0 is a real reading -- the window just refilled -- and must never be
    spelled the same way as 'we don't know'."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, 0.0, NOW), NOW)
    assert f.session_pct == 0.0
    assert f.session_pct != base.UNKNOWN


def test_a_non_dict_record_is_refused_not_raised():
    p = ClaudeDesktopLocalStorageProvider()
    assert p.record_to_frame("not a record", NOW) is None


def test_a_record_missing_utilization_is_refused_not_raised():
    p = ClaudeDesktopLocalStorageProvider()
    rec = _rec(NOW + 3600, 0.06, NOW)
    del rec["utilization"]
    assert p.record_to_frame(rec, NOW) is None


def test_a_non_numeric_utilization_is_refused_not_raised():
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, "abc", NOW), NOW)
    assert f is None


def test_this_source_never_invents_a_weekly_reading():
    """No seven-day data exists in this record at all."""
    p = ClaudeDesktopLocalStorageProvider()
    f = p.record_to_frame(_rec(NOW + 3600, 0.06, NOW), NOW)
    assert f.weekly_pct == base.UNKNOWN
    assert f.weekly_resets_at is None


def test_polling_an_absent_store_is_silent():
    p = ClaudeDesktopLocalStorageProvider(dir_path="/nonexistent/leveldb")
    assert p.poll(NOW) == []


def test_polling_a_real_store_returns_one_frame(tmp_path):
    d = _store(tmp_path, _rec(NOW + 3600, 0.06, NOW - 60))
    p = ClaudeDesktopLocalStorageProvider(dir_path=d)
    frames = p.poll(NOW)
    assert len(frames) == 1
    assert frames[0].session_pct == 6.0


def test_poll_never_raises_on_a_corrupt_store(tmp_path):
    (tmp_path / "000001.log").write_bytes(b"\xff" * 500)
    p = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path))
    assert p.poll(NOW) == []
