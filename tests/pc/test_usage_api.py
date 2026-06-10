import unittest
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

    def test_missing_windows_default_zero(self):
        m = usage_api.map_usage({})
        self.assertEqual(m["session_pct"], 0.0)
        self.assertEqual(m["weekly_pct"], 0.0)
        self.assertEqual(m["models"], [])


if __name__ == "__main__":
    unittest.main()
