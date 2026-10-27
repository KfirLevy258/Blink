import json
from pc.providers.scripted import ScriptedProvider


def _write(tmp_path, steps, **top):
    doc = {"name": "t", "steps": steps, **top}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


def test_steps_emit_when_due_and_only_once(tmp_path):
    clock = [1000.0]
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "session_pct": 50.0, "state": "running"},
        {"at": 10, "provider": "claude", "session_pct": 102.0, "state": "failed"},
    ])
    sp = ScriptedProvider(p, now=lambda: clock[0])
    first = sp.poll(clock[0])
    assert [f.session_pct for f in first] == [50.0]
    # Advanced ONCE, and then held. A source reports a level: the reading
    # stands until the next step writes over it. This used to go empty, which
    # left whichever of the daemon's two ticks happened to consume a step
    # deciding whether the board ever saw it.
    assert [f.session_pct for f in sp.poll(clock[0])] == [50.0]
    assert [f.session_pct for f in sp.poll(clock[0])] == [50.0]
    clock[0] += 10
    second = sp.poll(clock[0])
    assert [f.session_pct for f in second] == [102.0]
    assert second[0].provider == "claude"
    assert second[0].state == "failed"
    assert [f.session_pct for f in sp.poll(clock[0])] == [102.0]


def test_two_steps_due_at_once_are_still_two_frames(tmp_path):
    """One NEW step per poll, oldest first -- a late start delays, never merges.

    The daemon's first poll can land ten seconds after the provider was
    constructed: the poll gate only fires once the board has pinged, and a
    skipped poll is lost rather than deferred (claude_usage_bridge.py:1076).
    Every due step emitted in that one call would be collapsed by
    IngestionBus.poll into a single usage message, so a scenario whose min_tx
    is its step count -- which is how they are all written -- would come up
    short on a perfectly healthy board.
    """
    clock = [1000.0]
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "session_pct": 10.0},
        {"at": 5, "provider": "claude", "session_pct": 25.0},
    ])
    sp = ScriptedProvider(p, now=lambda: clock[0])
    clock[0] += 12                      # both steps came due before any poll
    assert [f.session_pct for f in sp.poll(clock[0])] == [10.0]
    assert [f.session_pct for f in sp.poll(clock[0])] == [25.0]
    # ...and then the last reading stands, rather than the source vanishing.
    assert [f.session_pct for f in sp.poll(clock[0])] == [25.0]


def test_age_s_backdates_observed_at(tmp_path):
    clock = [5000.0]
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "session_pct": 10.0, "age_s": 7200},
    ])
    sp = ScriptedProvider(p, now=lambda: clock[0])
    (f,) = sp.poll(clock[0])
    assert f.observed_at == 5000.0 - 7200


def test_provider_id():
    import pc.providers.scripted as m
    assert m.ScriptedProvider.__name__  # module imports cleanly


def test_unknown_key_step_is_skipped(tmp_path):
    p = _write(tmp_path, [
        {"at": 0, "provider": "claude", "sesion_pct": 50.0},   # typo key
        {"at": 0, "provider": "claude", "session_pct": 60.0},  # good step
    ])
    sp = ScriptedProvider(p, now=lambda: 1000.0)
    frames = sp.poll(1000.0)
    assert [f.session_pct for f in frames] == [60.0]


def test_step_missing_at_is_skipped(tmp_path):
    p = _write(tmp_path, [
        {"provider": "claude", "session_pct": 10.0},           # no "at"
        {"at": 0, "provider": "claude", "session_pct": 20.0},  # good step
    ])
    sp = ScriptedProvider(p, now=lambda: 1000.0)
    frames = sp.poll(1000.0)
    assert [f.session_pct for f in frames] == [20.0]


def test_scripted_spend_step(tmp_path):
    """The spend page in a scenario, in the wire's own short names, with a
    reset given relative to the step because a scenario file cannot know
    the epoch it will run at -- and a monthly period's is weeks away."""
    p = _write(tmp_path, [
        {"at": 0, "session_pct": 62.8, "plan": "spend", "usd": 314.12,
         "lim": 500, "period": "monthly",
         "session_resets_in_s": 18 * 86400, "age_s": 30},
        {"at": 5, "plan": "unreported", "acct": "enterprise"},
    ])
    clock = [1000.0]
    sp = ScriptedProvider(p, now=lambda: clock[0])
    f = sp.poll(clock[0])[0]
    assert f.plan == "spend"
    assert f.spend_usd == 314.12 and f.spend_limit_usd == 500
    assert f.spend_period == "monthly"
    # Relative to the reading, which is age_s old.
    assert f.session_resets_at == 1000.0 - 30 + 18 * 86400
    clock[0] += 5
    f = sp.poll(clock[0])[0]
    assert f.plan == "unreported" and f.account == "enterprise"
    assert f.session_pct == -1.0


def test_a_spend_step_with_a_bad_short_name_is_dropped_not_fatal(tmp_path):
    p = _write(tmp_path, [
        {"at": 0, "session_pct": 62.8, "plan": "spend", "usd": 314.12,
         "spend_usd": 1.0},                       # two names, one field
        {"at": 0, "session_pct": 20.0},
    ])
    sp = ScriptedProvider(p, now=lambda: 1000.0)
    assert [f.session_pct for f in sp.poll(1000.0)] == [20.0]
