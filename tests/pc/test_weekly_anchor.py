"""The seven-day boundary, learned once and rolled forward.

Every test here is about when to STOP publishing it. A rolled-forward
timestamp is the only value this project ships that was not directly
observed, so the rules for withdrawing it are the product.
"""
import json

from pc import weekly_anchor as wa
from pc.providers import base

WED_0600Z = 1788933600.0          # 2026-09-09T06:00:00Z
WEEK = 604800.0


def test_projects_a_future_boundary_unchanged():
    a = {"resets_at": WED_0600Z, "observed_at": WED_0600Z - WEEK}
    assert wa.project(a, WED_0600Z - 3600) == WED_0600Z


def test_rolls_a_past_boundary_forward_by_whole_weeks():
    a = {"resets_at": WED_0600Z, "observed_at": WED_0600Z}
    assert wa.project(a, WED_0600Z + WEEK + 10) == WED_0600Z + 2 * WEEK


def test_withdraws_an_anchor_nobody_has_corroborated_for_eight_weeks():
    a = {"resets_at": WED_0600Z, "observed_at": WED_0600Z - 9 * WEEK}
    assert wa.project(a, WED_0600Z) is None


def test_saves_and_loads_a_round_trip(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, WED_0600Z, WED_0600Z - 100)
    assert wa.load(p) == {"resets_at": WED_0600Z,
                          "observed_at": WED_0600Z - 100}


def test_loading_a_missing_or_corrupt_file_is_not_an_error(tmp_path):
    assert wa.load(str(tmp_path / "absent.json")) is None
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert wa.load(str(bad)) is None


def test_observe_records_an_exact_reset_from_any_frame(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    f = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=WED_0600Z - 100,
        weekly_pct=17.0, weekly_resets_at=WED_0600Z)
    wa.observe([f], p, WED_0600Z - 100)
    assert wa.load(p)["resets_at"] == WED_0600Z


def test_observe_ignores_a_frame_with_no_weekly_reset(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    f = base.NormalizedUsageFrame(
        provider="claude", src="desktop", observed_at=WED_0600Z,
        weekly_pct=17.0, weekly_resets_at=None)
    wa.observe([f], p, WED_0600Z)
    assert wa.load(p) is None


def test_a_drop_far_from_the_predicted_boundary_refutes_the_anchor():
    """The percentage drop cannot confirm a boundary -- its resolution is
    days -- but a drop three days away proves this anchor is wrong."""
    a = {"resets_at": WED_0600Z, "observed_at": WED_0600Z - WEEK}
    samples = [
        {"t": int((WED_0600Z - 4 * 86400) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((WED_0600Z - 3 * 86400) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    assert wa.refuted_by(a, samples, WED_0600Z) is True


def test_a_drop_near_the_predicted_boundary_does_not_refute():
    a = {"resets_at": WED_0600Z, "observed_at": WED_0600Z - WEEK}
    prev = WED_0600Z - WEEK
    samples = [
        {"t": int((prev - 1800) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((prev + 1800) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    assert wa.refuted_by(a, samples, WED_0600Z) is False
