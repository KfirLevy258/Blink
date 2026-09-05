"""The bus: many providers polled, one message out, nothing allowed to escape."""
import ast
import json
import pathlib
import re

from pc import ingest, weekly_anchor
from pc.providers import base
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider
from pc.providers.weekly_anchor import WeeklyAnchorProvider

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


def test_two_named_sessions_in_the_same_state_leave_the_board_unnamed():
    """Codex can name a session now, so both providers can arrive holding a
    name for the same state. There is one line and no honest rule for
    choosing between two projects that are equally true of it, so neither is
    shown.

    Both frames here claim n_wait=0 while sitting in STATE_WAITING -- a
    provider that reports a named session in a state while claiming no
    sessions in it is inconsistent, and that inconsistency is exactly what
    must not slip a name onto the panel. It is also the one scenario where
    the summed-count guard (n <= 1) is satisfied by both frames on its own
    and cannot be the thing doing the refusing: only the exclusivity guard
    (exactly one frame holding the state) can be responsible for the ""
    below, which is the guard this test exists to pin.
    """
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", state=base.STATE_WAITING,
                               label="LiveClaudeUi", n_wait=0)),
                   Fixed(named(provider="codex", at=NOW - 30,
                               state=base.STATE_WAITING,
                               label="Blink", n_wait=0))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "waiting"
    assert bus.session_pair() == ("", 0)


def test_a_codex_name_shows_when_it_is_the_only_holder_of_the_state():
    """The other half of the same rule, and the one that would be lost by a
    lazy "Claude wins" precedence: a Claude session merely working beside a
    waiting Codex session leaves exactly one holder of the state the panel is
    showing, and that one is named whichever provider it came from.
    """
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", state=base.STATE_RUNNING,
                               label="LiveClaudeUi", n_run=1)),
                   Fixed(named(provider="codex", at=NOW - 30,
                               state=base.STATE_WAITING,
                               label="Blink", n_wait=1))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "waiting"
    assert bus.session_pair() == ("Blink", 1)


def test_a_failed_codex_session_outranks_a_waiting_claude_one():
    """base.SEVERITY puts failed first, and protocol.frame_to_usage sends
    worst_of(primary, secondary). A Codex turn that died on a usage limit is
    the loudest thing on the desk and takes the line, name and all.
    """
    bus = ingest.IngestionBus(
        providers=[Fixed(named(provider="claude", state=base.STATE_WAITING,
                               label="LiveClaudeUi", n_wait=1)),
                   Fixed(named(provider="codex", at=NOW - 30,
                               state=base.STATE_FAILED,
                               label="Blink", n_stuck=1))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["state"] == "failed"
    assert bus.session_pair() == ("Blink", 1)


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


class _Clock:
    """A wall clock the test moves by hand, since the cooldown is in seconds."""

    def __init__(self, t=NOW):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, seconds):
        self.t += seconds


class Flaky(base.ProviderParser):
    """Raises while `broken` is set, otherwise yields one frame. Counts polls."""

    def __init__(self, broken=True, provider="claude"):
        self.broken = broken
        self.calls = 0
        self._provider = provider

    def get_provider_id(self):
        return self._provider

    def poll(self, now_epoch):
        self.calls += 1
        if self.broken:
            raise RuntimeError("file was being rewritten as it was read")
        return [frame(provider=self._provider, at=now_epoch)]


def test_a_transient_failure_costs_a_cooldown_not_the_whole_session():
    """The failure that motivated this is a file caught mid-rewrite.

    The daemon polls every two seconds now, so a provider gets thirty chances
    a minute to lose that race -- and under the old rule the first one it lost
    retired it until the user restarted the daemon.
    """
    clock = _Clock()
    flaky = Flaky()
    bus = ingest.IngestionBus(providers=[flaky], now=clock)
    assert bus.poll() is None

    # The file finished being written a moment later. Nothing tells the bus.
    flaky.broken = False
    clock.advance(ingest.FIRST_RETRY_AFTER_S - 1)
    assert bus.poll() is None, "retried before the cooldown was up"

    clock.advance(1)
    assert bus.poll() is not None


def test_one_broken_instance_does_not_silence_its_twin():
    """Two instances of one provider class are two sources, not one.

    The quarantine was keyed by class name, so a second Codex home -- or the
    same parser pointed at another directory -- went dark because its twin
    raised. On the panel that reads as the source simply having no data.
    """
    clock = _Clock()
    broken, healthy = Flaky(broken=True), Flaky(broken=False)
    bus = ingest.IngestionBus(providers=[broken, healthy], now=clock)

    for _ in range(3):
        assert bus.poll() is not None
        clock.advance(2)
    assert healthy.calls == 3
    assert broken.calls == 1


def test_the_backoff_is_capped_so_a_fixed_provider_comes_back(capsys):
    """Doubling without a ceiling is a permanent skip with extra steps.

    A user who signs in, or upgrades the other application, gets the panel
    back within the ceiling and without being told to restart anything.
    """
    clock = _Clock()
    flaky = Flaky()
    bus = ingest.IngestionBus(providers=[flaky], now=clock)

    for _ in range(20):
        bus.poll()
        clock.advance(ingest.MAX_RETRY_AFTER_S)
    # Every single tick, because the wait stops growing at the ceiling.
    # Uncapped, the twentieth retry would be years out.
    assert flaky.calls == 20
    # And twenty failures still cost exactly one log line. Retrying is only
    # affordable because it is quiet.
    assert capsys.readouterr().err.count("will be skipped") == 1


def test_a_provider_that_recovers_says_so_and_can_be_reported_again(capsys):
    """One line per transition -- that is what "reported once" has to mean.

    Silence while it keeps failing, because a traceback thirty times a minute
    is its own defect. But a provider that came back and then broke again is
    a new fault, and the log would otherwise carry only its first death.
    """
    clock = _Clock()
    flaky = Flaky()
    bus = ingest.IngestionBus(providers=[flaky], now=clock)
    bus.poll()
    assert "will be skipped" in capsys.readouterr().err

    flaky.broken = False
    clock.advance(ingest.FIRST_RETRY_AFTER_S)
    bus.poll()
    assert "working again" in capsys.readouterr().err

    flaky.broken = True
    clock.advance(2)
    bus.poll()
    assert "will be skipped" in capsys.readouterr().err


def test_a_clock_that_went_backwards_does_not_strand_a_provider():
    """self._now is wall time, and wall time is not monotonic.

    A sleeping laptop or an NTP step can put `now` before the moment the
    provider was quarantined, which arithmetic alone would turn back into the
    permanent skip this cooldown exists to end.
    """
    clock = _Clock()
    flaky = Flaky()
    bus = ingest.IngestionBus(providers=[flaky], now=clock)
    bus.poll()

    flaky.broken = False
    clock.advance(-3600)
    assert bus.poll() is not None


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


def test_the_panel_reports_the_age_of_the_last_claude_code_reading(tmp_path):
    """The field bug, end to end (2026-09-02).

    Claude Code was used six hours ago and its five-hour window has since
    expired, so the status line no longer carries a percentage. Claude
    Desktop was last open 57 hours ago. The message must carry the CLI
    reading and ITS age -- the desktop sample's 57 hours is an honest answer
    to a question nobody asked.
    """
    import json
    import os

    from pc.providers.claude_cli import ClaudeCliProvider

    p = tmp_path / "statusline.json"

    def write(doc, mtime):
        p.write_text(json.dumps(doc), encoding="utf-8")
        os.utime(p, (mtime, mtime))

    cli = ClaudeCliProvider(path=str(p))
    desktop = Fixed(frame(src="desktop", at=NOW - 57 * 3600, session=0.0,
                          weekly=0.0))

    write({"rate_limits": {"five_hour": {"used_percentage": 27.0}}},
          NOW - 6 * 3600)
    now = [NOW - 6 * 3600 + 1]
    bus = ingest.IngestionBus(providers=[cli, desktop],
                              now=lambda: now[0])
    bus.poll()

    write({"rate_limits": {}}, NOW - 60)
    now[0] = NOW
    msg = bus.poll()

    assert msg["src"] == "cli"
    assert msg["session_pct"] == 27.0
    assert msg["age_s"] == 6 * 3600
    assert msg["stale"] is True

    now[0] = NOW + 60
    assert bus.poll()["age_s"] == 6 * 3600 + 60


def _sleep_absent_after_s():
    """SLEEP_ABSENT_AFTER_S, read out of the firmware header it lives in.

    Hard-coding 14400 here would let the two sides drift apart silently, and
    that number is the whole point of the test below: it is the gate the
    daemon must not hand a reason to fire. Reading it means a rename or a
    move raises here rather than quietly stopping the test from testing
    anything.
    """
    root = pathlib.Path(__file__).resolve().parents[2]
    text = (root / "firmware" / "src" / "sleep_gate.h").read_text()
    m = re.search(r"^#define\s+SLEEP_ABSENT_AFTER_S\s+(\d+)", text,
                  re.MULTILINE)
    assert m, "SLEEP_ABSENT_AFTER_S is not in firmware/src/sleep_gate.h"
    return int(m.group(1))


def test_a_live_status_line_keeps_the_board_awake_behind_an_old_dial(tmp_path):
    """The regression this whole field exists for (final review, F-A).

    Claude Code is open and rewriting its status line every minute. Its
    five-hour window expired overnight, so the rewrite carries only the
    seven-day figure -- the shape pc/providers/claude_cli's own docstring
    describes, and the shape the field report describes. The remembered
    five-hour reading from twelve hours ago wins the session dial and brings
    its own mtime with it.

    So the dial IS twelve hours old, and the wire says so, honestly. What
    must not follow is the board deciding that nobody is at the desk: the
    file under all of this was written five seconds ago. With only one age
    on the wire it was 43200, the four-hour gate fired, and the panel closed
    its eyes on an owner who was watching it.
    """
    import os

    p = tmp_path / "statusline.json"

    def write(doc, mtime):
        p.write_text(json.dumps(doc), encoding="utf-8")
        os.utime(p, (mtime, mtime))

    cli = ClaudeCliProvider(path=str(p))
    now = [NOW - 12 * 3600 + 1]
    bus = ingest.IngestionBus(providers=[cli], now=lambda: now[0])

    # Overnight: a real five-hour reading, which the provider remembers.
    # No reset stamp on it, and that is load-bearing rather than lazy: a
    # stamp the clock has since passed is proof the window ended, and a
    # reading of a window that has ended is refused outright rather than
    # aged (pc/statusline_source._rolled_over). This is the other case --
    # nothing here says the twelve-hour-old reading has been superseded, so
    # it is offered, and offered with its own honest age.
    write({"rate_limits": {"five_hour": {"used_percentage": 27.0},
                           "seven_day": {"used_percentage": 26.0}}},
          NOW - 12 * 3600)
    assert bus.poll()["session_pct"] == 27.0

    # This morning: the five-hour window is gone from the rewrite, the
    # seven-day figure is not, and the file is five seconds old.
    write({"rate_limits": {"seven_day": {"used_percentage": 41.0}}}, NOW - 5)
    now[0] = NOW
    msg = bus.poll()

    assert msg["src"] == "cli"
    assert msg["session_pct"] == 27.0        # the remembered dial
    assert msg["weekly_pct"] == 41.0         # from the live rewrite
    assert msg["age_s"] == 12 * 3600         # and it really is that old
    assert msg["active_age_s"] == 5          # but the desk is not

    # The question the firmware asks, with the numbers this message carries.
    # The first line is the bug; the second is why the board stays awake.
    #
    # The threshold is pinned as well as read. Twelve hours and five seconds
    # straddle every plausible value, so the two comparisons below pass
    # whatever the header says -- confirmed by a survey that changed
    # SLEEP_ABSENT_AFTER_S from four hours to thirty minutes and watched all
    # 620 tests stay green. Reading it out of the firmware header keeps the
    # daemon and the board describing one number; this line is what notices
    # if the number itself moves under a test that was written for it.
    absent_after = _sleep_absent_after_s()
    assert absent_after == 4 * 3600
    assert msg["age_s"] >= absent_after
    assert msg["active_age_s"] < absent_after


def test_a_five_hour_window_that_ended_leaves_the_dial_empty_not_wrong(tmp_path):
    """The same morning, with the one difference that decides it: the
    overnight payload said WHEN its five-hour window would roll, and the
    clock has passed it.

    That reading is not old, it is about a window that no longer exists, so
    the dial goes to unknown rather than showing usage the account has
    already been forgiven. Everything else on the message survives: the
    seven-day figure from the live rewrite, and an active age that keeps the
    board awake for the person sitting in front of it.
    """
    import os

    p = tmp_path / "statusline.json"

    def write(doc, mtime):
        p.write_text(json.dumps(doc), encoding="utf-8")
        os.utime(p, (mtime, mtime))

    cli = ClaudeCliProvider(path=str(p))
    now = [NOW - 12 * 3600 + 1]
    bus = ingest.IngestionBus(providers=[cli], now=lambda: now[0])

    write({"rate_limits": {"five_hour": {"used_percentage": 27.0,
                                         "resets_at": NOW - 10 * 3600},
                           "seven_day": {"used_percentage": 26.0}}},
          NOW - 12 * 3600)
    assert bus.poll()["session_pct"] == 27.0     # still inside its window

    write({"rate_limits": {"seven_day": {"used_percentage": 41.0}}}, NOW - 5)
    now[0] = NOW
    msg = bus.poll()

    assert msg["session_pct"] == -1.0
    assert msg["session_resets_in_s"] == -1
    assert msg["weekly_pct"] == 41.0
    assert msg["active_age_s"] == 5


# --- Task 10 fix round 1: the learning wiring, and the account boundary ----
#
# _sandboxed_home (tests/conftest.py, autouse) points HOME/USERPROFILE at
# tmp_path for every test in this file, and weekly_anchor.anchor_path() calls
# expanduser at call time -- so weekly_anchor.anchor_path() below is already
# scoped under this test's own tmp_path. No path is injected here; that is
# the point being pinned.


def test_a_codex_weekly_reset_is_not_learned_as_the_claude_anchor():
    """observe() must never cross accounts. codex_cli.py emits its own
    weekly_resets_at, and on a machine running both tools that boundary must
    not become the anchor WeeklyAnchorProvider later republishes with
    provider="claude" -- a different account's reset, presented as this
    one's."""
    codex_frame = base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=NOW,
        weekly_pct=9.0, weekly_resets_at=NOW + 3 * 86400)
    bus = ingest.IngestionBus(providers=[Fixed(codex_frame)],
                              now=lambda: NOW)
    bus.poll_frames()
    assert weekly_anchor.load(weekly_anchor.anchor_path()) is None


def test_poll_frames_learns_a_live_claude_weekly_reset():
    """The other half of the same wiring: a claude frame's weekly reset IS
    learned, which is the entire point of calling observe() from here."""
    claude_frame = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW,
        weekly_pct=17.0, weekly_resets_at=NOW + 3 * 86400)
    bus = ingest.IngestionBus(providers=[Fixed(claude_frame)],
                              now=lambda: NOW)
    bus.poll_frames()
    anchor = weekly_anchor.load(weekly_anchor.anchor_path())
    assert anchor is not None
    assert anchor["resets_at"] == NOW + 3 * 86400
    assert anchor["observed_at"] == NOW


def test_the_anchor_does_not_relearn_its_own_republished_frame():
    """The loop-closing case. WeeklyAnchorProvider stamps its frame with the
    anchor's ORIGINAL observed_at rather than `now`, and observe() only
    overwrites on a STRICTLY newer observation -- so feeding the anchor's own
    frame back through the bus must leave the stored file untouched, not
    bump its observed_at forward to the moment it was merely re-read."""
    resets_at = NOW - 2 * 86400          # already past; project() rolls it
    observed_at = NOW - 10 * 86400       # well inside the 8-week corroboration window
    weekly_anchor.save(weekly_anchor.anchor_path(), resets_at, observed_at)
    before = weekly_anchor.load(weekly_anchor.anchor_path())

    bus = ingest.IngestionBus(providers=[WeeklyAnchorProvider()],
                              now=lambda: NOW)
    bus.poll_frames()

    after = weekly_anchor.load(weekly_anchor.anchor_path())
    assert after == before
