"""Conflict and recency resolution across sources that each see a slice."""
from pc import normalizer
from pc.providers import base

NOW = 1_787_700_000.0


def cli(at, session=base.UNKNOWN, weekly=base.UNKNOWN, s_reset=None,
        w_reset=None, ctx=base.UNKNOWN, model="", state="", stale=False,
        provider="claude"):
    return base.NormalizedUsageFrame(
        provider=provider, src="cli", observed_at=at, session_pct=session,
        weekly_pct=weekly, session_resets_at=s_reset, weekly_resets_at=w_reset,
        ctx_pct=ctx, model=model, state=state, stale=stale)


def desktop(at, session=base.UNKNOWN, weekly=base.UNKNOWN, stale=False,
            provider="claude"):
    return base.NormalizedUsageFrame(
        provider=provider, src="desktop", observed_at=at, session_pct=session,
        weekly_pct=weekly, stale=stale)


def test_nothing_in_nothing_out():
    assert normalizer.merge([]) is None
    assert normalizer.merge([None]) is None


def test_the_fresher_percentage_wins():
    m = normalizer.merge([
        cli(NOW - 3600, session=40.0),
        desktop(NOW - 60, session=55.0),
    ])
    assert m.session_pct == 55.0
    assert m.src == "desktop"


def test_a_source_without_the_field_never_wins_it():
    """The desktop cache has no reset timestamps. Being newest must not let
    it blank the only reset time anybody has."""
    m = normalizer.merge([
        cli(NOW - 3600, session=40.0, s_reset=NOW + 900),
        desktop(NOW - 60, session=55.0),
    ])
    assert m.session_pct == 55.0          # desktop is fresher
    assert m.session_resets_at == NOW + 900   # and CLI still supplies this


def test_context_and_model_survive_from_the_only_source_that_has_them():
    m = normalizer.merge([
        cli(NOW - 300, session=40.0, ctx=61.0, model="Opus 5"),
        desktop(NOW - 60, session=55.0),
    ])
    assert m.ctx_pct == 61.0
    assert m.model == "Opus 5"


def test_higher_does_not_beat_fresher_across_a_reset():
    """The rejected rule, pinned as a test.

    A window that rolled over reads 0% fresh and 90% from just before the
    reset. Preferring the higher number would show 90% for as long as the old
    reading survived -- inventing usage that has already been forgiven.
    """
    m = normalizer.merge([
        desktop(NOW - 600, session=90.0),
        cli(NOW - 10, session=0.0),
    ])
    assert m.session_pct == 0.0


def test_staleness_follows_the_primary_dial_not_the_whole_set():
    """A fresh desktop percentage next to an hours-old CLI reset time is a
    live panel, not a stale one."""
    m = normalizer.merge([
        cli(NOW - 7200, session=40.0, s_reset=NOW + 900, stale=True),
        desktop(NOW - 30, session=55.0, stale=False),
    ])
    assert m.stale is False
    assert m.src == "desktop"


def test_a_stale_winner_keeps_its_amber():
    m = normalizer.merge([desktop(NOW - 90_000, session=55.0, stale=True)])
    assert m.stale is True


def test_frames_with_no_percentage_at_all_produce_nothing():
    """Two blank dials are worse than the board keeping what it has."""
    assert normalizer.merge([cli(NOW, model="Opus 5")]) is None


def test_each_window_resolves_independently():
    m = normalizer.merge([
        cli(NOW - 10, weekly=12.0),
        desktop(NOW - 400, session=88.0),
    ])
    assert m.session_pct == 88.0
    assert m.weekly_pct == 12.0


def test_providers_are_never_merged_into_each_other():
    """Two providers are two accounts with two separate limits."""
    groups = normalizer.group_by_provider([
        cli(NOW, session=10.0),
        cli(NOW, session=90.0, provider="codex"),
    ])
    assert set(groups) == {"claude", "codex"}
    assert len(groups["claude"]) == 1


def test_select_prefers_the_configured_provider_even_when_older():
    chosen = normalizer.select([
        cli(NOW - 3600, session=10.0),
        cli(NOW, session=90.0, provider="codex"),
    ], preferred="claude")
    assert chosen.provider == "claude"


def test_select_falls_back_to_the_freshest_provider():
    chosen = normalizer.select([
        cli(NOW - 3600, session=10.0, provider="gemini"),
        cli(NOW, session=90.0, provider="codex"),
    ], preferred="claude")
    assert chosen.provider == "codex"


def test_select_with_nothing_returns_none():
    assert normalizer.select([], preferred="claude") is None
