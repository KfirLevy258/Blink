"""The seven-day boundary, learned once and rolled forward.

Every test here is about when to STOP publishing it. A rolled-forward
timestamp is the only value this project ships that was not directly
observed, so the rules for withdrawing it are the product.
"""
import os
import time

from pc import normalizer
from pc import weekly_anchor as wa
from pc.providers import base
from pc.providers.weekly_anchor import WeeklyAnchorProvider

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
    """The write guard is what stops the module poisoning its own file, and
    it must be pinned directly -- asserting only wa.load(p) is None would
    pass just as well if save() wrote the NaN and load() caught it on the
    way back out, which is the read guard's job, not this one's."""
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, float("nan"), WED_0600Z)
    assert not os.path.exists(p)


def test_save_ignores_an_infinite_observed_at(tmp_path):
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, WED_0600Z, float("inf"))
    assert not os.path.exists(p)


def test_save_does_not_raise_on_a_non_numeric_value(tmp_path):
    """A hand-built anchor with a missing/None field must not crash save --
    float(None) raising TypeError straight out of it was the old bug."""
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, None, WED_0600Z)
    assert not os.path.exists(p)


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


def test_load_rejects_a_far_future_observed_at(tmp_path):
    """observed_at is the field the uncorroborated withdrawal runs on -- a
    corrupt far-future value would make `now - observed_at` hugely negative,
    so the anchor would never age out. Guarding resets_at alone is not
    enough; this pins the field that actually matters for that rule."""
    p = str(tmp_path / "weekly-anchor.json")
    wa.save(p, WED_0600Z, 1e17)
    assert wa.load(p) is None


def test_observe_ignores_a_frame_with_a_far_future_observed_at(tmp_path):
    """A frame claiming to have been observed in the far future must not
    become the anchor -- it would win every future recency contest in
    observe()'s own current["observed_at"] >= best[1] check, permanently
    poisoning the one-shot memory with no path back."""
    p = str(tmp_path / "weekly-anchor.json")
    f = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=1e17,
        weekly_pct=17.0, weekly_resets_at=WED_0600Z)
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


# --- Task 10: the provider that publishes the anchor, and its ranking rule ---


def test_the_provider_publishes_only_the_reset(tmp_path):
    p = str(tmp_path / "a.json")
    wa.save(p, WED_0600Z, WED_0600Z - WEEK)
    frames = WeeklyAnchorProvider(path=p).poll(WED_0600Z - 3600)
    assert len(frames) == 1
    assert frames[0].weekly_resets_at == WED_0600Z
    assert frames[0].weekly_pct == base.UNKNOWN
    assert frames[0].session_pct == base.UNKNOWN


def test_the_projection_never_outranks_a_live_reading(tmp_path):
    """A source that actually saw the boundary must win, and does so purely
    through observed_at -- no special case in the normalizer."""
    p = str(tmp_path / "a.json")
    wa.save(p, WED_0600Z, WED_0600Z - 4 * WEEK)
    now = WED_0600Z - 3600
    anchor_frame = WeeklyAnchorProvider(path=p).poll(now)[0]
    live = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=now - 30,
        weekly_pct=17.0, weekly_resets_at=WED_0600Z + 1234)
    merged = normalizer.merge([anchor_frame, live])
    assert merged.weekly_resets_at == WED_0600Z + 1234


def test_no_anchor_means_no_frame(tmp_path):
    assert WeeklyAnchorProvider(path=str(tmp_path / "absent.json")).poll(
        WED_0600Z) == []


# --- Task 10 fix round 1: wiring refuted_by through history_provider -------


class _FakeHistory:
    """A stub history_provider: samples(now_epoch) -> a fixed list, in the
    same shape plan-usage-history.json's own "samples" array uses."""

    def __init__(self, samples):
        self._samples = samples

    def samples(self, now_epoch):
        return self._samples


def test_a_refuted_anchor_publishes_nothing(tmp_path):
    """refuted_by is wired into poll(): a percentage drop far from the
    anchor's predicted boundary must stop the projection from reaching the
    panel, rather than sitting unused for up to eight weeks while only the
    uncorroborated-timeout withdrawal can act."""
    p = str(tmp_path / "a.json")
    wa.save(p, WED_0600Z, WED_0600Z - WEEK)
    samples = [
        {"t": int((WED_0600Z - 4 * 86400) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((WED_0600Z - 3 * 86400) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    provider = WeeklyAnchorProvider(
        path=p, history_provider=_FakeHistory(samples))
    assert provider.poll(WED_0600Z) == []


def test_an_unrefuted_anchor_still_publishes(tmp_path):
    """The same wiring must not withdraw an anchor nothing contradicts --
    refuted_by only ever refutes, never confirms, so a drop that lands near
    the predicted boundary leaves the projection exactly as it would be with
    no history_provider at all."""
    p = str(tmp_path / "a.json")
    wa.save(p, WED_0600Z, WED_0600Z - WEEK)
    prev = WED_0600Z - WEEK
    samples = [
        {"t": int((prev - 1800) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((prev + 1800) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    provider = WeeklyAnchorProvider(
        path=p, history_provider=_FakeHistory(samples))
    frames = provider.poll(WED_0600Z - 3600)
    assert len(frames) == 1
    assert frames[0].weekly_resets_at == WED_0600Z


def test_with_no_history_provider_refutation_cannot_fire(tmp_path):
    """Default behaviour, unchanged. With no sample source at all the anchor
    still publishes -- refutation is a bonus check layered on top of
    project(), never a requirement for publishing."""
    p = str(tmp_path / "a.json")
    wa.save(p, WED_0600Z, WED_0600Z - WEEK)
    frames = WeeklyAnchorProvider(path=p).poll(WED_0600Z - 3600)
    assert len(frames) == 1
    assert frames[0].weekly_resets_at == WED_0600Z


def test_a_broken_history_provider_does_not_cost_the_projection(tmp_path):
    """Same rule every other source in this project already carries: a
    provider must not raise. A history_provider that does costs only the
    refutation check, never the projection itself."""
    p = str(tmp_path / "a.json")
    wa.save(p, WED_0600Z, WED_0600Z - WEEK)

    class _Boom:
        def samples(self, now_epoch):
            raise RuntimeError("upstream changed shape")

    frames = WeeklyAnchorProvider(path=p, history_provider=_Boom()).poll(
        WED_0600Z - 3600)
    assert len(frames) == 1
    assert frames[0].weekly_resets_at == WED_0600Z


def test_refutation_never_writes_to_the_anchor_file(tmp_path):
    """refuted_by only ever answers yes/no. Pin that checking it -- refuted
    or not -- leaves the stored anchor untouched; this wiring must never be
    the thing that saves to ~/.blink/weekly-anchor.json."""
    p = str(tmp_path / "a.json")
    wa.save(p, WED_0600Z, WED_0600Z - WEEK)
    before = wa.load(p)
    samples = [
        {"t": int((WED_0600Z - 4 * 86400) * 1000), "u": {"fh": 5, "sd": 80}},
        {"t": int((WED_0600Z - 3 * 86400) * 1000), "u": {"fh": 5, "sd": 4}},
    ]
    WeeklyAnchorProvider(
        path=p, history_provider=_FakeHistory(samples)).poll(WED_0600Z)
    assert wa.load(p) == before
