"""The seven-day boundary, learned once and rolled forward.

Every test here is about when to STOP publishing it. A rolled-forward
timestamp is the only value this project ships that was not directly
observed, so the rules for withdrawing it are the product.
"""
import os
import time

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


# --- fix round 1: NaN/Infinity never survive a round trip (Important 1) ---


def test_a_stored_nan_is_rejected_rather_than_remembered_forever(tmp_path):
    """json.load parses the bare literal NaN by default; parse_constant must
    refuse it so a corrupt anchor cannot persist unnoticed forever."""
    p = tmp_path / "weekly-anchor.json"
    p.write_text('{"resets_at": NaN, "observed_at": 1788933600.0}')
    assert wa.load(str(p)) is None


def test_save_ignores_a_nan_resets_at(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, float("nan"), WED_0600Z)
    assert wa.load(p) is None


def test_save_ignores_an_infinite_observed_at(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, WED_0600Z, float("inf"))
    assert wa.load(p) is None


def test_save_does_not_raise_on_a_non_numeric_value(tmp_path):
    """A hand-built anchor with a missing/None field must not crash save --
    float(None) raising TypeError straight out of it was the old bug."""
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, None, WED_0600Z)
    assert wa.load(p) is None


def test_observe_ignores_a_frame_with_a_nan_reset(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    f = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=WED_0600Z,
        weekly_pct=17.0, weekly_resets_at=float("nan"))
    wa.observe([f], p, WED_0600Z)
    assert wa.load(p) is None


# --- fix round 1: an implausible anchor cannot hang the daemon (Important 2) ---


def test_load_rejects_an_implausible_resets_at(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, -1e18, WED_0600Z)
    assert wa.load(p) is None


def test_observe_ignores_a_frame_with_an_implausible_reset(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    f = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=WED_0600Z,
        weekly_pct=17.0, weekly_resets_at=-1e18)
    wa.observe([f], p, WED_0600Z)
    assert wa.load(p) is None


def test_refuted_by_does_not_hang_on_an_implausible_anchor():
    """A hand-built anchor bypasses load()'s plausibility guard entirely, so
    refuted_by's own arithmetic must stay bounded regardless of how far
    resets_at is from the drop -- a 1970 epoch used to cost ~2,900
    loop iterations and a -1e18 did not terminate in any useful time."""
    a = {"resets_at": -1e18, "observed_at": WED_0600Z - WEEK}
    samples = [
        {"t": int((WED_0600Z - 4 * 86400) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((WED_0600Z - 3 * 86400) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    start = time.monotonic()
    result = wa.refuted_by(a, samples, WED_0600Z)
    assert time.monotonic() - start < 2.0
    assert result in (True, False)


def test_project_does_not_raise_on_an_anchor_missing_a_key():
    """A hand-built anchor dict missing a field must not raise KeyError."""
    assert wa.project({"resets_at": WED_0600Z}, WED_0600Z) is None
    assert wa.project({"observed_at": WED_0600Z}, WED_0600Z) is None


def test_refuted_by_does_not_raise_on_an_anchor_missing_a_key():
    samples = [
        {"t": int((WED_0600Z - 4 * 86400) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((WED_0600Z - 3 * 86400) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    assert wa.refuted_by({"observed_at": WED_0600Z}, samples, WED_0600Z) is False


# --- fix round 1: the headline invariant, pinned (Important 3) ---


def test_refuted_by_never_creates_an_anchor_file(tmp_path):
    """A percentage drop can only refute, never confirm -- pin that it never
    even touches disk when nothing is stored yet."""
    p = str(tmp_path / "weekly-anchor.json")
    a = {"resets_at": WED_0600Z, "observed_at": WED_0600Z - WEEK}
    samples = [
        {"t": int((WED_0600Z - 4 * 86400) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((WED_0600Z - 3 * 86400) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    assert wa.refuted_by(a, samples, WED_0600Z) is True
    assert not os.path.exists(p)


def test_refuted_by_never_rewrites_a_stored_anchor(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, WED_0600Z, WED_0600Z - WEEK)
    before = wa.load(p)
    samples = [
        {"t": int((WED_0600Z - 4 * 86400) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((WED_0600Z - 3 * 86400) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    wa.refuted_by(dict(before), samples, WED_0600Z)
    assert wa.load(p) == before


# --- fix round 1: both withdrawals pinned at their exact edge (Important 4) ---


def test_anchor_survives_exactly_at_the_uncorroborated_boundary():
    a = {"resets_at": WED_0600Z,
         "observed_at": WED_0600Z - wa.ANCHOR_MAX_UNCORROBORATED_S}
    assert wa.project(a, WED_0600Z) == WED_0600Z + WEEK


def test_anchor_withdrawn_one_second_past_the_uncorroborated_boundary():
    a = {"resets_at": WED_0600Z,
         "observed_at": WED_0600Z - wa.ANCHOR_MAX_UNCORROBORATED_S - 1}
    assert wa.project(a, WED_0600Z) is None


def test_a_drop_exactly_at_the_refute_tolerance_does_not_refute():
    lo = WED_0600Z - 5 * 86400
    hi = lo + 3600
    a = {"resets_at": lo - wa.ANCHOR_REFUTE_TOLERANCE_S,
         "observed_at": WED_0600Z - WEEK}
    samples = [
        {"t": int(lo * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int(hi * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    assert wa.refuted_by(a, samples, WED_0600Z) is False


def test_a_drop_one_second_past_the_refute_tolerance_refutes():
    lo = WED_0600Z - 5 * 86400
    hi = lo + 3600
    a = {"resets_at": lo - wa.ANCHOR_REFUTE_TOLERANCE_S - 1,
         "observed_at": WED_0600Z - WEEK}
    samples = [
        {"t": int(lo * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int(hi * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    assert wa.refuted_by(a, samples, WED_0600Z) is True
