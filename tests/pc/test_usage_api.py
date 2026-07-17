import unittest
from datetime import datetime, timezone
from pc import usage_api

RAW = {
    "five_hour": {"utilization": 61.0, "resets_at": "2026-06-08T21:50:01Z"},
    "seven_day": {"utilization": 26.0, "resets_at": "2026-06-10T06:00:01Z"},
    "seven_day_sonnet": {"utilization": 2.0, "resets_at": "2026-06-10T06:00:01Z"},
    "seven_day_opus": None,
    "extra_usage": {"used_credits": 0.0, "monthly_limit": 8000},
}


class TestMap(unittest.TestCase):
    def test_maps_headline_fields(self):
        m = usage_api.map_usage(RAW)
        self.assertEqual(m["t"], "usage")
        self.assertEqual(m["session_pct"], 61.0)
        self.assertEqual(m["session_resets_at"], "2026-06-08T21:50:01Z")
        self.assertEqual(m["weekly_pct"], 26.0)

    def test_models_array_includes_present_models_only(self):
        m = usage_api.map_usage(RAW)
        names = {x["name"]: x["weekly_pct"] for x in m["models"]}
        self.assertEqual(names, {"sonnet": 2.0})  # opus is null -> omitted

    def test_models_from_weekly_scoped_limits(self):
        """Current accounts: flat model windows are null; per-model weekly
        usage rides in limits[] as weekly_scoped entries (live 2026-07-17)."""
        raw = dict(RAW, seven_day_sonnet=None, limits=[
            {"kind": "session", "percent": 12, "resets_at": "A"},
            {"kind": "weekly_all", "percent": 23, "resets_at": "B"},
            {"kind": "weekly_scoped", "percent": 43, "resets_at": "C",
             "scope": {"model": {"id": None, "display_name": "Fable"}},
             "is_active": True},
        ])
        m = usage_api.map_usage(raw)
        names = {x["name"]: x["weekly_pct"] for x in m["models"]}
        self.assertEqual(names, {"fable": 43.0})

    def test_scoped_limit_wins_over_flat_window_for_same_model(self):
        raw = dict(RAW, seven_day_fable={"utilization": 10.0, "resets_at": "X"},
                   limits=[{"kind": "weekly_scoped", "percent": 43,
                            "scope": {"model": {"display_name": "Fable"}}}])
        m = usage_api.map_usage(raw)
        fable = [x for x in m["models"] if x["name"] == "fable"]
        self.assertEqual(fable, [{"name": "fable", "weekly_pct": 43.0}])

    def test_missing_windows_default_zero(self):
        m = usage_api.map_usage({})
        self.assertEqual(m["session_pct"], 0.0)
        self.assertEqual(m["weekly_pct"], 0.0)
        self.assertEqual(m["models"], [])


class TestResetsInSeconds(unittest.TestCase):
    """The board has no clock in USB mode, so it cannot turn an absolute
    resets_at timestamp into a countdown. The daemon sends the remaining
    seconds alongside it and the board ticks down locally."""

    def test_computes_seconds_until_reset(self):
        now = datetime(2026, 6, 8, 21, 0, 1, tzinfo=timezone.utc)
        m = usage_api.map_usage(RAW, now=now)
        self.assertEqual(m["session_resets_in_s"], 50 * 60)   # 21:00:01 -> 21:50:01
        # Jun 8 21:00:01 -> Jun 10 06:00:01 == 33 h
        self.assertEqual(m["weekly_resets_in_s"], 33 * 3600)

    def test_past_reset_clamps_to_zero(self):
        now = datetime(2027, 1, 1, tzinfo=timezone.utc)
        m = usage_api.map_usage(RAW, now=now)
        self.assertEqual(m["session_resets_in_s"], 0)

    def test_missing_or_malformed_timestamp_is_minus_one(self):
        m = usage_api.map_usage({})
        self.assertEqual(m["session_resets_in_s"], -1)  # -1 == unknown, not "now"
        m = usage_api.map_usage({"five_hour": {"utilization": 1.0, "resets_at": "junk"}})
        self.assertEqual(m["session_resets_in_s"], -1)


if __name__ == "__main__":
    unittest.main()
