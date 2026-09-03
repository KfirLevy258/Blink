"""The bus: many providers polled, one message out, nothing allowed to escape."""
import ast
import json
import pathlib

from pc import ingest
from pc.providers import base
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider

NOW = 1_787_700_000.0


class Boom(base.ProviderParser):
    def get_provider_id(self):
        return "boom"

    def poll(self, now_epoch):
        raise RuntimeError("upstream changed shape")


class Fixed(base.ProviderParser):
    def __init__(self, frame):
        self._frame = frame

    def get_provider_id(self):
        return self._frame.provider

    def poll(self, now_epoch):
        return [self._frame]


def frame(provider="claude", src="cli", at=NOW, session=50.0, weekly=20.0):
    return base.NormalizedUsageFrame(
        provider=provider, src=src, observed_at=at,
        session_pct=session, weekly_pct=weekly)


def test_a_provider_that_raises_does_not_take_the_bus_down(capsys):
    bus = ingest.IngestionBus(providers=[Boom(), Fixed(frame())],
                              now=lambda: NOW)
    msg = bus.poll()
    assert msg["session_pct"] == 50.0
    assert "will be skipped" in capsys.readouterr().err


def test_a_broken_provider_is_reported_once_not_every_poll(capsys):
    bus = ingest.IngestionBus(providers=[Boom(), Fixed(frame())],
                              now=lambda: NOW)
    bus.poll()
    capsys.readouterr()
    bus.poll()
    bus.poll()
    assert capsys.readouterr().err == ""


def test_no_sources_at_all_returns_none():
    bus = ingest.IngestionBus(providers=[], now=lambda: NOW)
    assert bus.poll() is None


def test_the_message_names_the_winning_source():
    bus = ingest.IngestionBus(
        providers=[Fixed(frame(src="cli", at=NOW - 900, session=10.0)),
                   Fixed(frame(src="desktop", at=NOW, session=77.0))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["session_pct"] == 77.0
    assert msg["src"] == "desktop"
    assert msg["provider"] == "claude"


def test_a_provider_can_be_added_at_runtime():
    bus = ingest.IngestionBus(providers=[], now=lambda: NOW)
    assert bus.poll() is None
    bus.add_provider(Fixed(frame(provider="codex")))
    assert bus.poll()["provider"] == "codex"


def test_the_two_real_providers_compose_end_to_end(tmp_path):
    """CLI supplies resets, context and model; desktop supplies a fresher
    session percentage. The merged message carries all four."""
    statusline = tmp_path / "statusline.json"
    statusline.write_text(json.dumps({
        "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": NOW + 900},
            "seven_day": {"used_percentage": 20, "resets_at": NOW + 90_000},
        },
    }))
    import os
    os.utime(statusline, (NOW - 900, NOW - 900))

    cache = tmp_path / "plan-usage-history.json"
    cache.write_text(json.dumps({"version": 2, "samples": [
        {"t": int(NOW * 1000), "org": "o", "u": {"fh": 77, "sd": 44}}]}))

    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(statusline)),
                   ClaudeDesktopProvider(path=str(cache))],
        now=lambda: NOW)
    msg = bus.poll()

    assert msg["session_pct"] == 77.0          # desktop, fresher
    assert msg["weekly_pct"] == 44.0           # desktop, fresher
    assert msg["session_resets_in_s"] == 900   # CLI, the only source with it
    assert msg["src"] == "desktop"


# --- the project name, which rides beside the usage message ----------------


def named(provider="claude", src="hook", at=NOW, session=50.0, weekly=20.0,
          state=base.STATE_WAITING, label="LiveClaudeUi", **counts):
    f = frame(provider=provider, src=src, at=at, session=session,
              weekly=weekly)
    f.state = state
    f.label = label
    for k, v in counts.items():
        setattr(f, k, v)
    return f


def test_the_daemon_wires_the_fetch_that_carries_the_name():
    """The regression this exists for, and the reason it reads the daemon's
    own source instead of building a fetch of its own.

    claude_usage_bridge said `fetch = bus.poll` while every test built its
    fetch through make_fetch. A bound method proxies attribute reads to the
    plain function underneath, so the Bridge's
    getattr(fetch, "session_pair", None) was None on every real desk and the
    board was never named -- with the whole suite green, because no test
    ever touched the object the daemon actually constructs.

    So take the daemon's expression verbatim and run it. A test that builds
    its own fetch cannot see the two ways diverge; this one cannot miss it.
    """
    src = pathlib.Path(ingest.__file__).resolve().parents[1] / \
        "claude_usage_bridge.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    assigns = [n for n in ast.walk(tree)
               if isinstance(n, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == "fetch"
                       for t in n.targets)]
    assert assigns, "the daemon has no `fetch` any more; move this guard"
    for node in assigns:
        scope = {"ingest": ingest,
                 "bus": ingest.IngestionBus(providers=[Fixed(named(n_wait=1))],
                                            now=lambda: NOW)}
        fetch = eval(compile(ast.Expression(node.value), "<daemon>", "eval"),
                     scope)
        fetch()
        assert fetch.session_pair() == ("LiveClaudeUi", 1), ast.dump(node)


def test_the_daemon_hands_that_very_fetch_to_the_bridge():
    """The other half of the guard above, and the half it cannot do.

    Evaluating `fetch = ...` only proves the daemon BUILDS the right object.
    It says nothing about the object reaching the Bridge, so an inline
    `Bridge(..., fetch_usage=bus.poll, ...)` one line further down would
    leave the assignment correct, this file green, and every real desk
    unnamed -- the identical failure to the one above, moved by a line.

    So insist the keyword is the bare name `fetch`: the local the guard
    above just ran and vouched for, not a fresh expression nobody checked.
    An attribute, a call, a lambda -- anything but the name -- is a second
    construction path, and a second path is exactly what went wrong once.
    """
    src = pathlib.Path(ingest.__file__).resolve().parents[1] / \
        "claude_usage_bridge.py"
    tree = ast.parse(src.read_text(encoding="utf-8"))
    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Name) and n.func.id == "Bridge"]
    assert calls, "the daemon builds no Bridge any more; move this guard"
    for call in calls:
        kw = [k for k in call.keywords if k.arg == "fetch_usage"]
        assert kw, "Bridge built without fetch_usage: " + ast.dump(call)
        for k in kw:
            assert isinstance(k.value, ast.Name) and k.value.id == "fetch", (
                "fetch_usage must pass the verified `fetch` local, not "
                + ast.dump(k.value))


def test_the_label_survives_the_whole_bus_path():
    """The one that matters. session_pair() reads the frame select_pair
    returned, and select_pair runs EVERY frame through normalizer.merge()
    even when a provider produced only one -- so a merge that rebuilds the
    frame field by field and forgets `label` would leave the board unnamed
    with every unit test on this page still green.
    """
    bus = ingest.IngestionBus(providers=[Fixed(named(n_wait=1))],
                              now=lambda: NOW)
    bus.poll()
    assert bus.session_pair() == ("LiveClaudeUi", 1)


def test_the_label_survives_a_merge_of_two_sources():
    """Two sources for one provider is the case that actually merges: the
    hook knows the project and the CLI status line knows the percentages,
    and the frame that reaches the board is neither of them.
    """
    hook = named(src="hook", at=NOW, session=base.UNKNOWN,
                 weekly=base.UNKNOWN, n_wait=1)
    cli = named(src="cli", at=NOW - 60, state=base.STATE_UNKNOWN, label="")
    bus = ingest.IngestionBus(providers=[Fixed(hook), Fixed(cli)],
                              now=lambda: NOW)
    msg = bus.poll()
    assert msg["session_pct"] == 50.0            # from the CLI frame
    assert bus.session_pair() == ("LiveClaudeUi", 1)   # from the hook frame


def test_a_second_provider_in_a_worse_state_takes_the_name_away():
    """The panel shows ONE light for the whole desk -- worst_of(claude,
    codex) -- so a named Claude session sitting idle beside a waiting Codex
    one must not put its name under "Waiting for you". That is a wrong
    sentence rather than a vague one.
    """
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", state=base.STATE_IDLE,
                               label="MyProject", n_idle=1)),
                   Fixed(named(provider="codex", at=NOW - 30, label="",
                               state=base.STATE_WAITING, n_wait=1))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["provider"] == "claude"          # still the primary ring
    assert msg["state"] == "waiting"            # but the desk is waiting
    assert bus.session_pair() == ("", 1)


def test_two_providers_agreeing_on_the_state_keep_the_name():
    """The other half: the name survives a second provider that is merely
    busy, because the state on the panel is still the named one's."""
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", n_wait=1)),
                   Fixed(named(provider="codex", at=NOW - 30, label="",
                               state=base.STATE_RUNNING, n_run=1))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "waiting"
    assert bus.session_pair() == ("LiveClaudeUi", 1)


def test_sessions_on_both_providers_sharing_a_state_are_not_named():
    """Naming one of several is refused rather than guessed -- the same rule
    claude_state.poll applies before it ever sets `label`. It has to hold
    across providers too, or two waiting Codex sessions would silently
    rename themselves after the one Claude project."""
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", n_wait=1)),
                   Fixed(named(provider="codex", at=NOW - 30, label="",
                               state=base.STATE_WAITING, n_wait=2))],
        now=lambda: NOW)
    bus.poll()
    assert bus.session_pair() == ("", 3)


def test_a_failed_session_reports_the_stuck_count():
    """claude_state.poll folds a failed session into n_stuck, so `failed`
    has to read that count and not zero -- otherwise two failed sessions say
    "Session failed" where the panel should say "Session failed - 2
    sessions"."""
    bus = ingest.IngestionBus(
        providers=[Fixed(named(state=base.STATE_FAILED, label="",
                               n_stuck=2))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "failed"
    assert bus.session_pair() == ("", 2)


def test_the_count_belongs_to_the_state_on_the_panel():
    """The count that goes out is the one for the frame's own state -- the
    same rule the normalizer applies to the counts themselves. A running
    count beside a `waiting` state would be a panel naming a number that
    describes some other session."""
    bus = ingest.IngestionBus(
        providers=[Fixed(named(label="", n_run=2, n_wait=3, n_idle=9))],
        now=lambda: NOW)
    bus.poll()
    assert bus.session_pair() == ("", 3)


def test_session_pair_before_any_poll_is_empty():
    bus = ingest.IngestionBus(providers=[], now=lambda: NOW)
    assert bus.session_pair() == ("", 0)


# --- the board owns the primary-provider preference ------------------------


def test_the_board_can_choose_the_primary_provider():
    """The user picks it on the settings screen; the daemon follows. It does
    not live here, because a preference that resets whenever the daemon
    restarts is not a preference."""
    bus = ingest.IngestionBus(
        providers=[Fixed(frame(provider="claude", session=10.0)),
                   Fixed(frame(provider="codex", session=90.0))],
        preferred_provider="claude", now=lambda: NOW)
    assert bus.poll()["provider"] == "claude"
    assert bus.set_preferred("codex") is True
    assert bus.poll()["provider"] == "codex"


def test_an_unknown_provider_is_refused_not_applied(capsys):
    """select_pair falls back to the freshest when its preference matches
    nothing, so a typo would silently hand the outer ring to whichever source
    wrote last -- which reads as a merge bug, not a bad setting."""
    bus = ingest.IngestionBus(
        providers=[Fixed(frame(provider="claude", session=10.0))],
        preferred_provider="claude", now=lambda: NOW)
    assert bus.set_preferred("gemini") is False
    assert bus.poll()["provider"] == "claude"
    assert "not reporting" in capsys.readouterr().err


def test_an_empty_preference_changes_nothing():
    bus = ingest.IngestionBus(providers=[Fixed(frame())], now=lambda: NOW)
    assert bus.set_preferred("") is False
    assert bus.set_preferred(None) is False


def test_a_broken_provider_is_actually_skipped_not_just_silenced():
    """The docstring said skipped; the loop went on polling it every cycle
    with only the log line suppressed, so a second, different failure was
    invisible."""
    class Counting:
        calls = 0
        def get_provider_id(self):
            return "x"
        def poll(self, now):
            Counting.calls += 1
            raise RuntimeError("still broken")
    bus = ingest.IngestionBus(providers=[Counting(), Fixed(frame())],
                              now=lambda: NOW)
    bus.poll()
    bus.poll()
    bus.poll()
    assert Counting.calls == 1


def test_two_real_files_reach_the_wire_together(tmp_path):
    """End to end: the real Desktop cache and the real Codex log, through the
    bus, onto one usage message. This is the path a Desktop+Codex customer's
    board is fed by, and until now nothing exercised it with real input."""
    from pc.providers.claude_desktop import ClaudeDesktopProvider
    from pc.providers.codex_cli import CodexCliProvider
    from tests.pc.test_claude_desktop import DESKTOP_FIXTURE
    from tests.pc.test_codex_cli import _fixture_root
    # A minute after the Codex reading, which is the newer of the two files
    # by six hours -- so the Desktop reading is carried as stale, which is
    # exactly the mixed-freshness case the per-page flags exist for.
    now = 1787875431.383 + 60
    bus = ingest.IngestionBus(
        providers=[ClaudeDesktopProvider(path=DESKTOP_FIXTURE),
                   CodexCliProvider(root=_fixture_root(tmp_path))],
        now=lambda: now)
    msg = bus.poll()
    assert msg["provider"] == "claude" and msg["src"] == "desktop"
    assert msg["session_pct"] == 24.0 and msg["weekly_pct"] == 25.0
    assert msg["session_resets_in_s"] == -1          # Desktop has none
    assert msg["stale"] is True                       # six hours old
    assert "burn_pph" not in msg                      # no rate off a stale file
    assert msg["p2"] == "codex"
    assert msg["p2_weekly_pct"] == 9.0
    assert msg["p2_w_in_s"] > 0                       # Codex has its reset time
    assert msg["p2_stale"] is False
    from pc import protocol
    assert len(protocol.encode(msg)) <= protocol.MAX_LINE_BYTES
