import json
import os
import pathlib
import subprocess
import sys

import pytest

from pc import usage_refresh as ur
from pc.usage_refresh import (State, Verdict, command, find_claude, judge,
                              load_state, record, run_once, save_state,
                              spacing)

@pytest.fixture(autouse=True)
def _no_system_claude(monkeypatch, tmp_path):
    """find_claude also looks in /opt/homebrew/bin and /usr/local/bin, which
    are this machine's, not the sandbox's: a claude installed there would
    be found by every test that expects none."""
    monkeypatch.setattr(ur, "SYSTEM_BIN_DIRS",
                        (str(tmp_path / "no-system-bin"),))


ZERO = json.dumps({"type": "result", "subtype": "success", "is_error": False,
                   "num_turns": 0, "total_cost_usd": 0, "modelUsage": {},
                   "result": "x"})


def _fake_claude(tmp_path, stdout, exit_code=0, sleep=0):
    d = tmp_path / "fakebin"
    d.mkdir(exist_ok=True)
    rec = tmp_path / "rec.txt"
    p = d / "claude"
    p.write_text("#!/bin/sh\n"
                 f"printf '%s\\n' \"$*\" \"$PWD\" \"$BLINK_REFRESH\" > '{rec}'\n"
                 + (f"sleep {sleep}\n" if sleep else "")
                 + f"cat <<'EOF'\n{stdout}\nEOF\nexit {exit_code}\n")
    p.chmod(0o755)
    return str(p), rec


def test_judge_zero_cost_is_ok():
    assert judge(0, ZERO.encode()) is Verdict.OK


@pytest.mark.parametrize("field,value", [
    ("num_turns", 1), ("total_cost_usd", 0.0001),
    ("modelUsage", {"claude-x": {}})])
@pytest.mark.parametrize("rc", [0, 1])
def test_judge_any_model_use_is_cost(field, value, rc):
    d = json.loads(ZERO)
    d[field] = value
    assert judge(rc, json.dumps(d).encode()) is Verdict.COST


@pytest.mark.parametrize("out", [
    b"", b"not json", b"[]",
    json.dumps({"type": "result", "is_error": True, "num_turns": 0,
                "total_cost_usd": 0}).encode()])
def test_judge_anything_else_is_failed(out):
    assert judge(0, out) is Verdict.FAILED


def test_judge_nonzero_exit_without_model_use_is_failed():
    assert judge(1, ZERO.encode()) is Verdict.FAILED


def test_command_is_exact():
    assert command("/x/claude") == [
        "/x/claude", "-p", "/usage", "--output-format", "json",
        "--no-session-persistence", "--strict-mcp-config", "--mcp-config",
        '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}']


def test_command_never_bare():
    assert "--bare" not in command("c")


@pytest.mark.allow_claude_launch
def test_run_once_runs_in_its_own_folder_with_the_marker(tmp_path):
    exe, rec = _fake_claude(tmp_path, ZERO)
    home = tmp_path / ".blink"
    home.mkdir()
    assert run_once(exe, str(home)) is Verdict.OK
    args, cwd, marker = rec.read_text().splitlines()
    assert os.path.realpath(cwd) == os.path.realpath(home / "refresh")
    assert marker == "1"
    assert args.startswith("-p /usage")


@pytest.mark.allow_claude_launch
def test_run_once_real_timeout_path_maps(tmp_path):
    exe, _ = _fake_claude(tmp_path, ZERO, sleep=0)
    assert run_once(exe, str(tmp_path)) is Verdict.OK


def test_run_once_timeout(tmp_path):
    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    assert run_once("c", str(tmp_path), runner) is Verdict.TIMEOUT


def test_run_once_oserror_is_failed(tmp_path):
    def runner(argv, **kw):
        raise FileNotFoundError("x")
    assert run_once("c", str(tmp_path), runner) is Verdict.FAILED


def test_run_once_passes_no_window_on_windows(tmp_path, monkeypatch):
    seen = {}

    def runner(argv, **kw):
        seen.update(kw)
        return subprocess.CompletedProcess(argv, 0, ZERO.encode(), None)
    monkeypatch.setattr(sys, "platform", "win32")
    assert run_once("c", str(tmp_path), runner) is Verdict.OK
    assert seen["creationflags"] == 0x08000000
    assert seen["timeout"] == ur.RUN_TIMEOUT_S
    assert seen["stdin"] == subprocess.DEVNULL
    assert seen["stderr"] == subprocess.DEVNULL


@pytest.mark.allow_claude_launch
def test_run_once_logs_nothing(tmp_path, capsys):
    exe, _ = _fake_claude(tmp_path, ZERO)
    assert run_once(exe, str(tmp_path)) is Verdict.OK
    exe2, _ = _fake_claude(tmp_path, "garbage", exit_code=1)
    assert run_once(exe2, str(tmp_path)) is Verdict.FAILED
    out, err = capsys.readouterr()
    assert out == "" and err == ""


def test_find_claude_on_path_then_local_bin(tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".local" / "bin").mkdir(parents=True)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert find_claude(str(home)) is None
    local = home / ".local" / "bin" / "claude"
    local.write_text("x")
    assert find_claude(str(home)) == str(local)
    exe, _ = _fake_claude(tmp_path, ZERO)
    monkeypatch.setenv("PATH", os.path.dirname(exe))
    assert find_claude(str(home)) == exe


def test_find_claude_windows_exe(tmp_path, monkeypatch):
    home = tmp_path / "h"
    (home / ".local" / "bin").mkdir(parents=True)
    exe = home / ".local" / "bin" / "claude.exe"
    exe.write_text("x")
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    # shutil.which on a posix box cannot pose as Windows (it needs _winapi),
    # and PATH has no claude here anyway, so stand it in.
    monkeypatch.setattr(ur.shutil, "which", lambda name: None)
    monkeypatch.setattr(sys, "platform", "win32")
    assert find_claude(str(home)) == str(exe)


def test_record_cost_turns_off():
    assert record(State(), Verdict.COST, 10).off_reason == "cost"


def test_record_backoff_after_three_failures():
    s = State()
    for t in (1, 2, 3):
        assert spacing(s) == ur.REFRESH_AFTER_S or t == 3
        s = record(s, Verdict.FAILED, t)
    assert spacing(s) == ur.BACKOFF_S
    s = record(s, Verdict.OK, 4)
    assert spacing(s) == ur.REFRESH_AFTER_S
    assert s.failing_since is None and s.last_run_at == 4


def test_record_timeout_counts_as_failure():
    assert record(State(), Verdict.TIMEOUT, 5).fails == 1


def test_record_gives_up_after_a_day_of_failures():
    s = record(State(), Verdict.FAILED, 0)
    assert s.off_reason == ""
    assert record(s, Verdict.FAILED, ur.GIVE_UP_S).off_reason == "failing"
    s = record(s, Verdict.OK, 100)
    s = record(s, Verdict.FAILED, ur.GIVE_UP_S)
    assert s.off_reason == ""


def test_state_round_trip_and_corrupt_file(tmp_path):
    h = str(tmp_path)
    assert load_state(h) == State()
    s = State(12.5, 2, 3.0, "failing")
    save_state(h, s)
    assert load_state(h) == s
    p = tmp_path / "usage-refresh.json"
    p.write_text("{nope")
    assert load_state(h) == State()
    p.write_text(json.dumps({"run": {"fails": "x"}}))
    assert load_state(h) == State()


def test_save_state_keeps_other_keys(tmp_path):
    h = str(tmp_path)
    (tmp_path / "usage-refresh.json").write_text(
        json.dumps({"ask": {"n": 1}, "run": {"fails": 9}}))
    save_state(h, State(fails=1))
    doc = json.loads((tmp_path / "usage-refresh.json").read_text())
    assert doc["ask"] == {"n": 1}
    assert doc["run"]["fails"] == 1


# --- due: the fallback rule ------------------------------------------------

NOW = 1793828400.0


def _due(**over):
    kw = dict(enabled=True, native=True, board_alive=True, idle_s=60.0,
              cache_at=NOW - ur.REFRESH_AFTER_S, other_at=None,
              state=State(last_run_at=NOW - ur.REFRESH_AFTER_S))
    kw.update(over)
    return ur.due(NOW, **kw)


def test_due_when_every_condition_holds():
    assert _due() is True


@pytest.mark.parametrize("over", [
    {"enabled": False},
    {"native": False},
    {"board_alive": False},
    {"state": State(off_reason="cost")},
    {"state": State(off_reason="failing")},
    {"idle_s": float(ur.IDLE_LIMIT_S)},
    {"other_at": NOW - ur.FRESH_SOURCE_S + 1},
    {"cache_at": NOW - ur.REFRESH_AFTER_S + 1},
    {"state": State(last_run_at=NOW - ur.REFRESH_AFTER_S + 1)},
    {"state": State(last_run_at=NOW - ur.REFRESH_AFTER_S,
                    fails=ur.BACKOFF_AFTER, failing_since=NOW - 7200)},
], ids=lambda o: next(iter(o)))
def test_each_condition_alone_stops_it(over):
    assert _due(**over) is False


def test_no_cache_and_no_run_yet_is_due():
    assert _due(cache_at=None, state=State()) is True


def test_a_terminal_used_14_minutes_ago_blocks_it_and_16_does_not():
    assert _due(other_at=NOW - 14 * 60) is False
    assert _due(other_at=NOW - 16 * 60) is True


def test_a_desktop_chat_reading_10_minutes_old_blocks_it():
    assert _due(other_at=NOW - 10 * 60) is False


def test_a_spend_seat_with_a_fresh_status_line_is_not_due():
    # No exception for a spend seat: the running count covers terminal use.
    assert _due(other_at=NOW - 30) is False


def test_linux_with_no_idle_figure_is_in_use():
    assert _due(idle_s=None) is True


# --- IngestionBus.claude_fresh_at -----------------------------------------

def _bus(held):
    import types

    from pc import ingest
    src = types.SimpleNamespace(get_provider_id=lambda: "claude",
                                poll=lambda now: list(held))
    return ingest.IngestionBus(providers=[src], now=lambda: NOW,
                               verdict=None)


def _frame(src, at, **kw):
    from pc.providers import base
    return base.NormalizedUsageFrame(provider="claude", src=src,
                                     observed_at=at, **kw)


def test_a_desktop_frame_sets_claude_fresh_at():
    bus = _bus([_frame("desktop", NOW - 600, weekly_pct=10.0)])
    assert bus.claude_fresh_at() is None
    bus.poll()
    assert bus.claude_fresh_at() == NOW - 600


def test_a_usage_frame_alone_leaves_it_unchanged():
    from pc.providers import base
    held = [_frame("desktop", NOW - 900, weekly_pct=10.0)]
    bus = _bus(held)
    bus.poll()
    held[:] = [_frame(base.USAGE_SRC_ID, NOW - 5, weekly_pct=12.0)]
    bus.poll()
    assert bus.claude_fresh_at() == NOW - 900


def test_a_frame_with_no_figure_is_ignored():
    bus = _bus([_frame("cli", NOW - 5, state="running")])
    bus.poll()
    assert bus.claude_fresh_at() is None


def test_another_providers_frame_is_ignored():
    from pc.providers import base
    f = base.NormalizedUsageFrame(provider="codex", src="cli",
                                  observed_at=NOW - 5, weekly_pct=10.0)
    bus = _bus([f])
    bus.poll()
    assert bus.claude_fresh_at() is None


# --- RefreshWatch.tick_once ------------------------------------------------

ACCOUNT = "00000000-0000-0000-0000-000000000001"


def _config_doc(fetched_at):
    return {"oauthAccount": {"accountUuid": ACCOUNT},
            "cachedUsageUtilization": {
                "accountUuid": ACCOUNT,
                "fetchedAtMs": int(fetched_at * 1000),
                "utilization": {"five_hour": None,
                                "seven_day": {"utilization": 15.0,
                                              "resets_at": None}}}}


def _write_config(home, fetched_at):
    (home / ".claude.json").write_text(json.dumps(_config_doc(fetched_at)))


class _Watch:
    """A RefreshWatch over tmp_path with every input a test can turn."""

    def __init__(self, tmp_path, monkeypatch, *, clock=NOW, runner=None,
                 enabled=True, alive=True, other=None, idle_s=60.0,
                 home_kind=None):
        from pc import claude_home
        self.home = pathlib.Path(os.environ["HOME"])
        self.blink = tmp_path / ".blink"
        self.blink.mkdir(exist_ok=True)
        self.clock = [clock]
        self.enabled, self.alive = [enabled], [alive]
        self.other, self.idle_s = [other], [idle_s]
        self.idle_asked = 0
        native = claude_home.from_environ({})
        kind = home_kind or claude_home.NATIVE

        def home():
            import dataclasses
            return dataclasses.replace(native, kind=kind)

        def idle():
            self.idle_asked += 1
            return self.idle_s[0]

        kw = {} if runner is None else {"runner": runner}
        self.w = ur.RefreshWatch(
            str(self.blink), home, enabled=lambda: self.enabled[0],
            board_alive=lambda: self.alive[0],
            view=ur.config_view(home), other_at=lambda: self.other[0],
            now=lambda: self.clock[0], idle=idle, **kw)

    def state(self):
        return load_state(str(self.blink))


def _on_path(monkeypatch, exe):
    # In front, not instead: the fake is a shell script and needs cat.
    monkeypatch.setenv("PATH", os.path.dirname(exe) + os.pathsep
                       + os.environ.get("PATH", ""))


def _moving_claude(tmp_path, stdout, fetched_at):
    """A fake claude that answers `stdout` and rewrites ~/.claude.json with
    the cache fetched at `fetched_at`, as a logged-in /usage does."""
    exe, rec = _fake_claude(tmp_path, stdout)
    cfg = pathlib.Path(os.environ["HOME"]) / ".claude.json"
    body = json.dumps(_config_doc(fetched_at))
    text = pathlib.Path(exe).read_text()
    pathlib.Path(exe).write_text(text.replace(
        "cat <<'EOF'", f"cat > '{cfg}' <<'CFG'\n{body}\nCFG\ncat <<'EOF'", 1))
    return exe, rec


@pytest.mark.allow_claude_launch
def test_tick_runs_once_when_due_and_records_the_start(tmp_path, monkeypatch,
                                                       capsys):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    exe, rec = _moving_claude(tmp_path, ZERO, NOW + 2)
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    t.w.tick_once()
    assert rec.exists()
    s = t.state()
    assert s.last_run_at == NOW and s.fails == 0 and s.off_reason == ""
    assert capsys.readouterr().err.splitlines() == ["usage refresh ran"]
    rec.unlink()
    t.clock[0] = NOW + 60
    t.w.tick_once()                      # the cache is fresh now
    assert not rec.exists()


def test_tick_does_not_run_when_not_due(tmp_path, monkeypatch, capsys):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    ran = []

    def runner(argv, **kw):
        ran.append(argv)
        raise AssertionError("ran")
    t = _Watch(tmp_path, monkeypatch, runner=runner,
               other=NOW - 5 * 60)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    t.w.tick_once()
    t.other[0] = None
    t.alive[0] = False
    t.w.tick_once()
    t.alive[0] = True
    t.enabled[0] = False
    t.w.tick_once()
    assert ran == [] and t.state() == State()
    assert capsys.readouterr().err == ""


def test_the_idle_probe_is_asked_only_when_all_else_says_due(tmp_path,
                                                            monkeypatch):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 60)
    t = _Watch(tmp_path, monkeypatch, runner=lambda *a, **k: 1 / 0)
    t.w.tick_once()
    assert t.idle_asked == 0
    t.idle_s[0] = float(ur.IDLE_LIMIT_S + 1)
    t.clock[0] = NOW + 3600
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    t.w.tick_once()
    assert t.idle_asked == 1 and t.state() == State()


def test_a_wsl_home_never_runs(tmp_path, monkeypatch):
    from pc import claude_home
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    t = _Watch(tmp_path, monkeypatch, home_kind=claude_home.WSL,
               runner=lambda *a, **k: 1 / 0)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    t.w.tick_once()
    assert t.state() == State()


@pytest.mark.allow_claude_launch
def test_ok_whose_cache_did_not_move_is_failed(tmp_path, monkeypatch, capsys):
    # What a logged-out Claude Code gives (measured 2026-11-05): a zero-cost
    # success, and no cache written.
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    exe, rec = _fake_claude(tmp_path, ZERO)
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    t.w.tick_once()
    assert rec.exists()
    s = t.state()
    assert s.fails == 1 and s.failing_since == NOW and s.last_run_at == NOW
    assert capsys.readouterr().err.splitlines() == [
        "usage refresh failed (exit 0)"]


@pytest.mark.allow_claude_launch
def test_ok_whose_cache_moved_is_ok(tmp_path, monkeypatch):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    # Within the 5 s slack before the run's start still counts.
    exe, _ = _moving_claude(tmp_path, ZERO, NOW - 4)
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    t.w.tick_once()
    assert t.state().fails == 0 and t.state().last_run_at == NOW


@pytest.mark.allow_claude_launch
def test_cost_turns_the_setting_off_and_says_only_that(tmp_path, monkeypatch,
                                                      capsys):
    from pc import user_config
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    costly = json.loads(ZERO)
    costly.update(num_turns=1, total_cost_usd=0.02, result="secret 42%")
    exe, _ = _moving_claude(tmp_path, json.dumps(costly), NOW)
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    user_config.put(str(t.blink), ur.SETTING, True)
    t.w.tick_once()
    assert t.state().off_reason == "cost"
    assert user_config.load(str(t.blink))[ur.SETTING] is False
    err = capsys.readouterr().err
    assert err.splitlines() == ["usage refresh off (cost)"]
    assert "secret" not in err and "42" not in err


def test_a_day_of_failures_turns_it_off(tmp_path, monkeypatch, capsys):
    from pc import user_config
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               subprocess.CompletedProcess(argv, 2, b"nope", None))
    ur.save_state(str(t.blink), State(last_run_at=NOW - 7200, fails=5,
                                      failing_since=NOW - ur.GIVE_UP_S))
    user_config.put(str(t.blink), ur.SETTING, True)
    t.w.tick_once()
    assert t.state().off_reason == "failing"
    assert user_config.load(str(t.blink))[ur.SETTING] is False
    assert capsys.readouterr().err.splitlines() == [
        "usage refresh off (failing)"]


def test_a_timeout_is_logged_as_one(tmp_path, monkeypatch, capsys):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")

    def runner(argv, **kw):
        raise subprocess.TimeoutExpired(argv, kw["timeout"])
    t = _Watch(tmp_path, monkeypatch, runner=runner)
    t.w.tick_once()
    assert t.state().fails == 1
    assert capsys.readouterr().err.splitlines() == [
        "usage refresh failed (timeout)"]


def test_a_runner_that_raises_leaves_the_watch_ticking(tmp_path, monkeypatch,
                                                       capsys):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    calls = []

    def runner(argv, **kw):
        calls.append(1)
        raise RuntimeError("stdout said 42% for someone@example.com")
    t = _Watch(tmp_path, monkeypatch, runner=runner)
    t.w.tick_once()
    assert t.state().fails == 1 and t.state().last_run_at == NOW
    t.clock[0] = NOW + ur.REFRESH_AFTER_S
    t.w.tick_once()                      # still runs
    assert len(calls) == 2 and t.state().fails == 2
    err = capsys.readouterr().err
    # The type, once for the streak; never the message.
    assert err.splitlines() == ["usage refresh failed (RuntimeError)"]


def test_anything_else_that_raises_is_caught_by_type(tmp_path, monkeypatch,
                                                     capsys):
    t = _Watch(tmp_path, monkeypatch)

    def boom():
        raise ValueError("private")
    t.w._board_alive = boom
    t.w.tick_once()
    t.w.tick_once()
    assert capsys.readouterr().err.splitlines() == [
        "usage refresh failed (ValueError)"]


def test_no_claude_says_so_once_per_streak(tmp_path, monkeypatch, capsys):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    found = [None]
    monkeypatch.setattr(ur, "find_claude", lambda home: found[0])
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               subprocess.CompletedProcess(argv, 3, b"", None))
    for i in range(3):
        t.clock[0] = NOW + 60 * i
        t.w.tick_once()
    assert t.state() == State(missing=True)
    assert capsys.readouterr().err.splitlines() == ["usage refresh skipped"]
    found[0] = "/x/claude"
    t.w.tick_once()
    assert capsys.readouterr().err.splitlines() == [
        "usage refresh failed (exit 3)"]
    found[0] = None
    t.clock[0] = NOW + 7200
    t.w.tick_once()
    assert capsys.readouterr().err.splitlines() == ["usage refresh skipped"]


def test_a_last_run_in_the_future_is_taken_as_now(tmp_path, monkeypatch):
    # A clock set back must not hold the refresh off until it catches up.
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    ran = []
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               ran.append(1) or subprocess.CompletedProcess(argv, 1, b"",
                                                            None))
    ur.save_state(str(t.blink), State(last_run_at=NOW + 86400))
    t.w.tick_once()
    assert ran == [] and t.state().last_run_at == NOW
    t.clock[0] = NOW + ur.REFRESH_AFTER_S
    t.w.tick_once()
    assert ran == [1]


def test_start_names_its_thread(tmp_path, monkeypatch):
    t = _Watch(tmp_path, monkeypatch)
    monkeypatch.setattr(ur.RefreshWatch, "_run", lambda self: None)
    th = t.w.start()
    th.join(5)
    assert th.name == "blink-refresh" and th.daemon


# --- the daemon's wiring ---------------------------------------------------

def _wire(monkeypatch, kind):
    """start_usage_refresh with the setting on and a record of `kind`. The
    thread's loop is replaced by one that returns at once, so a real thread
    named blink-refresh starts and nothing outlives the test."""
    import dataclasses
    import types

    import claude_usage_bridge as cub
    from pc import claude_home, user_config
    blink = os.path.join(os.environ["HOME"], ".blink")
    os.makedirs(blink, exist_ok=True)
    user_config.put(blink, ur.SETTING, True)
    rec = dataclasses.replace(claude_home.from_environ({}), kind=kind)
    monkeypatch.setattr(claude_home, "current", lambda: rec)
    monkeypatch.setattr(ur.RefreshWatch, "_run", lambda self: None)
    bus = types.SimpleNamespace(claude_fresh_at=lambda: None)
    th = cub.start_usage_refresh(blink, bus, lambda: True)
    if th is not None:
        th.join(5)
    return th


def test_the_daemon_starts_the_refresh_on_a_native_home(monkeypatch):
    from pc import claude_home
    th = _wire(monkeypatch, claude_home.NATIVE)
    assert th is not None and th.name == "blink-refresh"


def test_the_daemon_starts_no_refresh_on_a_wsl_home(monkeypatch):
    from pc import claude_home
    th = _wire(monkeypatch, claude_home.WSL)
    assert th is None


def test_main_starts_the_refresh_once_outside_every_loop():
    import ast

    import claude_usage_bridge as cub
    src = pathlib.Path(cub.__file__).resolve().with_name(
        "claude_usage_bridge.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "main")
    parents = {c: p for p in ast.walk(main) for c in ast.iter_child_nodes(p)}
    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "start_usage_refresh"]
    assert len(calls) == 1
    node = calls[0]
    while node in parents:
        node = parents[node]
        assert not isinstance(node, (ast.While, ast.For)), "in a loop"


def test_the_daemon_starts_no_refresh_under_a_scenario(monkeypatch):
    # A scenario's bus replays invented usage; a real run would mix this
    # desk's login into it.
    from pc import claude_home
    monkeypatch.setenv("BLINK_SCENARIO", "x.json")
    assert _wire(monkeypatch, claude_home.NATIVE) is None


# --- review round 1 --------------------------------------------------------

def test_a_spend_frame_alone_leaves_claude_fresh_at_unchanged():
    from pc.providers import base
    held = [_frame("desktop", NOW - 900, weekly_pct=10.0)]
    bus = _bus(held)
    bus.poll()
    held[:] = [_frame(base.SPEND_SRC_ID, NOW - 5, plan=base.PLAN_SPEND,
                      spend_usd=12.0, spend_period="monthly")]
    assert held[0].has_usage()
    bus.poll()
    assert bus.claude_fresh_at() == NOW - 900


def _unwritable(monkeypatch):
    def refuse(blink_home, state):
        raise OSError("disk full")
    monkeypatch.setattr(ur, "save_state", refuse)


def test_a_state_that_cannot_be_saved_still_holds_the_spacing(
        tmp_path, monkeypatch, capsys):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    ran = []
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               ran.append(1) or subprocess.CompletedProcess(argv, 1, b"",
                                                            None))
    _unwritable(monkeypatch)
    for i in range(5):
        t.clock[0] = NOW + 60 * i
        t.w.tick_once()
    assert ran == [1]
    t.clock[0] = NOW + ur.REFRESH_AFTER_S
    t.w.tick_once()
    assert ran == [1, 1]
    err = capsys.readouterr().err.splitlines()
    assert err[:2] == ["usage refresh failed (exit 1)",
                       "usage refresh failed (OSError)"]
    assert all("disk" not in line for line in err)


@pytest.mark.allow_claude_launch
def test_cost_with_an_unsaveable_state_still_turns_it_off(tmp_path,
                                                         monkeypatch):
    from pc import user_config
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    costly = json.loads(ZERO)
    costly.update(num_turns=1, total_cost_usd=0.02)
    exe, rec = _moving_claude(tmp_path, json.dumps(costly), NOW)
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    user_config.put(str(t.blink), ur.SETTING, True)
    _unwritable(monkeypatch)
    t.w.tick_once()
    assert user_config.load(str(t.blink))[ur.SETTING] is False
    rec.unlink()
    t.clock[0] = NOW + 7200
    t.w.tick_once()
    assert not rec.exists()


@pytest.mark.allow_claude_launch
def test_cost_with_neither_write_still_holds_it_off(tmp_path, monkeypatch):
    # Setting and state both unwritable: the watch's own copy holds it.
    from pc import user_config
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    costly = json.loads(ZERO)
    costly.update(num_turns=1, total_cost_usd=0.02)
    exe, rec = _moving_claude(tmp_path, json.dumps(costly), NOW)
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    _unwritable(monkeypatch)

    def no_put(*a, **k):
        raise OSError("read-only")
    monkeypatch.setattr(user_config, "put", no_put)
    t.w.tick_once()
    rec.unlink()
    for h in (2, 30):
        t.clock[0] = NOW + 3600 * h
        t.w.tick_once()
    assert not rec.exists()


def test_a_post_run_view_that_raises_is_a_failure(tmp_path, monkeypatch):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    ran = []
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               ran.append(1) or subprocess.CompletedProcess(
                   argv, 0, ZERO.encode(), None))
    look = t.w._view

    def view(now):
        if ran:
            raise ValueError("unreadable")
        return look(now)
    t.w._view = view
    t.w.tick_once()
    s = t.state()
    assert ran == [1] and s.fails == 1 and s.last_run_at == NOW
    ran.clear()
    t.w._view = look
    t.clock[0] = NOW + 60
    t.w.tick_once()
    assert ran == []


# --- asking on the board (Task 7) ------------------------------------------

from pc.usage_refresh import (ASK_AT_MOST, ASK_EVERY_S, IDLE_LIMIT_S,  # noqa: E402
                              AskState, load_ask, save_ask, should_ask)

_YES = dict(setting_absent=True, native=True, claude_found=True,
            work_login=False, board_caps=frozenset({"svc", "ask"}),
            idle_s=60.0, ask_state=AskState())


def test_should_ask_when_everything_says_yes():
    assert should_ask(NOW, **_YES) is True


@pytest.mark.parametrize("key,value", [
    ("setting_absent", False),          # already answered, either way
    ("native", False),                  # a WSL home: never
    ("claude_found", False),            # nothing to run
    ("work_login", True),               # asked only in the terminal
    ("board_caps", frozenset({"svc"})),  # an older board: terminal only
    ("board_caps", frozenset()),
    ("idle_s", float(IDLE_LIMIT_S)),    # nobody at the computer
    ("ask_state", AskState(times=ASK_AT_MOST, last_at=0.0)),
    ("ask_state", AskState(declined=True)),
    ("ask_state", AskState(times=1, last_at=NOW - ASK_EVERY_S + 1)),
])
def test_each_condition_alone_stops_the_ask(key, value):
    assert should_ask(NOW, **dict(_YES, **{key: value})) is False


def test_an_os_that_cannot_say_idle_counts_as_in_use():
    assert should_ask(NOW, **dict(_YES, idle_s=None)) is True


def test_a_day_after_the_last_ask_it_asks_again():
    st = AskState(times=1, last_at=NOW - ASK_EVERY_S)
    assert should_ask(NOW, **dict(_YES, ask_state=st)) is True


def test_three_laters_then_never():
    st = AskState()
    t = NOW
    for _ in range(ASK_AT_MOST):
        assert should_ask(t, **dict(_YES, ask_state=st)) is True
        st = AskState(times=st.times + 1, last_at=t)   # sent; "later"
        t += ASK_EVERY_S
    for _ in range(5):
        assert should_ask(t, **dict(_YES, ask_state=st)) is False
        t += ASK_EVERY_S


def test_ask_state_lives_beside_the_run_state(tmp_path):
    home = str(tmp_path)
    save_state(home, State(last_run_at=5.0))
    save_ask(home, AskState(times=2, last_at=7.0, declined=False))
    assert load_state(home).last_run_at == 5.0     # "run" left alone
    assert load_ask(home) == AskState(2, 7.0, False)
    save_state(home, State(last_run_at=9.0))       # and the other way round
    assert load_ask(home) == AskState(2, 7.0, False)
    with open(os.path.join(home, ur.STATE_FILE), encoding="utf-8") as f:
        doc = json.load(f)
    assert set(doc) == {"run", "ask"}


@pytest.mark.parametrize("ask", [
    None, "x", [], {"times": "2"}, {"times": True}, {"last_at": "y"},
    {"declined": 1}, {"times": -1}])
def test_a_bad_ask_record_is_a_fresh_one(tmp_path, ask):
    home = str(tmp_path)
    with open(os.path.join(home, ur.STATE_FILE), "w", encoding="utf-8") as f:
        json.dump({"ask": ask}, f)
    assert load_ask(home) == AskState()


class _Asker:
    """A BoardAsk over tmp_path with every input a test can turn."""

    def __init__(self, tmp_path, *, kind=None, idle_s=60.0, work=False,
                 claude=True, clock=NOW):
        import dataclasses

        from pc import claude_home, claude_plan
        self.blink = tmp_path / ".blink"
        self.blink.mkdir(exist_ok=True)
        self.clock = [clock]
        self.idle_s = [idle_s]
        self.idle_asked = 0
        self.views = 0
        rec = dataclasses.replace(claude_home.from_environ({}),
                                  kind=kind or claude_home.NATIVE)
        if claude:
            b = pathlib.Path(os.environ["HOME"]) / ".local" / "bin"
            b.mkdir(parents=True, exist_ok=True)
            exe = b / ("claude.exe" if sys.platform == "win32" else "claude")
            exe.write_text("")
        view = dataclasses.replace(claude_plan.EMPTY_VIEW, spend_seat=work)

        def look(now):
            self.views += 1
            return view

        def idle():
            self.idle_asked += 1
            return self.idle_s[0]

        self.a = ur.BoardAsk(str(self.blink), lambda: rec, look,
                             now=lambda: self.clock[0], idle=idle)

    def ask(self):
        return load_ask(str(self.blink))

    def setting(self):
        from pc import user_config
        return user_config.load(str(self.blink)).get(ur.SETTING)


CAPS = frozenset({"svc", "ask"})


def test_board_ask_due_and_spaced_a_day_when_sent(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)
    assert k.a.due(CAPS) is True
    k.a.asked()
    # Sent, not counted: only an answer shows the person saw it.
    assert k.ask() == AskState(0, NOW, False)
    assert k.a.due(CAPS) is False                  # not again today
    k.clock[0] += ASK_EVERY_S
    assert k.a.due(CAPS) is True


def test_board_ask_never_for_a_board_without_the_cap(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)
    assert k.a.due(frozenset({"svc"})) is False
    assert k.idle_asked == 0 and k.views == 0      # nothing costly asked


def test_board_ask_never_on_a_work_login(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert _Asker(tmp_path, work=True).a.due(CAPS) is False


def test_board_ask_never_on_a_wsl_home(tmp_path, monkeypatch):
    from pc import claude_home
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path, kind=claude_home.WSL)
    assert k.a.due(CAPS) is False
    assert k.views == 0                            # no UNC config read


def test_board_ask_never_without_claude_code(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    assert _Asker(tmp_path, claude=False).a.due(CAPS) is False


def test_board_ask_never_to_an_empty_room(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path, idle_s=IDLE_LIMIT_S + 1)
    assert k.a.due(CAPS) is False


def test_board_ask_never_once_the_setting_exists(tmp_path, monkeypatch):
    from pc import user_config
    monkeypatch.setenv("PATH", "")
    for value in (True, False):
        k = _Asker(tmp_path)
        user_config.put(str(k.blink), ur.SETTING, value)
        assert k.a.due(CAPS) is False
        assert k.idle_asked == 0


def test_board_ask_never_when_config_cannot_be_read(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)
    (k.blink / "config.json").write_text("{not json")
    assert k.a.due(CAPS) is False


def test_the_idle_probe_is_asked_last(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path, work=True)
    k.a.due(CAPS)
    assert k.idle_asked == 0


def test_answer_on_turns_it_on(tmp_path, monkeypatch):
    k = _Asker(tmp_path)
    k.a.asked()
    k.a.answer("on")
    assert k.setting() is True
    assert k.ask().declined is False


def test_answer_no_turns_it_off_for_good(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)
    k.a.asked()
    k.a.answer("no")
    assert k.setting() is False
    assert k.ask().declined is True
    # Even were the setting removed by hand, a no stays a no on the board.
    os.remove(k.blink / "config.json")
    k.clock[0] += 10 * ASK_EVERY_S
    assert k.a.due(CAPS) is False


def test_answer_later_counts_one_ask_and_writes_nothing_else(tmp_path,
                                                            monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)
    for _ in range(ASK_AT_MOST):
        assert k.a.due(CAPS) is True
        k.a.asked()
        k.a.answer("later")
        assert k.setting() is None
        k.clock[0] += ASK_EVERY_S
    assert k.ask().times == ASK_AT_MOST
    assert k.a.due(CAPS) is False


@pytest.mark.parametrize("junk", ["yes", "", None, 1, "ON"])
def test_an_unknown_answer_changes_nothing(tmp_path, junk):
    k = _Asker(tmp_path)
    k.a.answer(junk)
    assert k.setting() is None and k.ask() == AskState()


def test_a_failed_save_still_holds_the_day(tmp_path, monkeypatch):
    """save_ask is one try; the copy held in memory is the floor, so a disk
    that refuses the write does not put the box up at every heartbeat."""
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)

    def refuse(*a, **kw):
        raise OSError("read-only")
    monkeypatch.setattr(ur, "save_ask", refuse)
    k.a.asked()
    assert k.a.due(CAPS) is False


def test_board_ask_never_raises(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)

    def boom():
        raise RuntimeError("x")
    k.a._idle = boom
    assert k.a.due(CAPS) is False
    monkeypatch.setattr(ur.user_config, "put", lambda *a: boom())
    k.a.answer("on")                               # swallowed, not raised


def test_the_daemon_builds_one_board_ask_and_hands_it_to_the_bridge():
    import ast

    import claude_usage_bridge as cub
    src = pathlib.Path(cub.__file__).resolve().with_name(
        "claude_usage_bridge.py")
    tree = ast.parse(src.read_text(encoding="utf-8"))
    main = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)
                and n.name == "main")
    parents = {c: p for p in ast.walk(main) for c in ast.iter_child_nodes(p)}
    built = [n for n in ast.walk(main) if isinstance(n, ast.Call)
             and getattr(n.func, "id", None) == "board_ask"]
    assert len(built) == 1
    node = built[0]
    while node in parents:              # once, not per connection
        node = parents[node]
        assert not isinstance(node, (ast.While, ast.For)), "in a loop"
    bridges = [n for n in ast.walk(main) if isinstance(n, ast.Call)
               and getattr(n.func, "id", None) == "Bridge"]
    assert bridges and all(any(k.arg == "refresh_ask" for k in c.keywords)
                           for c in bridges)


def test_no_board_ask_under_a_scenario(monkeypatch, tmp_path):
    import claude_usage_bridge as cub
    monkeypatch.setenv("BLINK_SCENARIO", "x.json")
    assert cub.board_ask(str(tmp_path)) is None
    monkeypatch.delenv("BLINK_SCENARIO")
    assert isinstance(cub.board_ask(str(tmp_path)), ur.BoardAsk)


# --- final review, Important 1: turning it back on is a fresh start --------

def _failing_watch(tmp_path, monkeypatch):
    """A watch one failure from its day of failures, the setting on, and a
    runner that always fails; returns (watch, runs)."""
    from pc import user_config
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    runs = []
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               runs.append(1) or subprocess.CompletedProcess(argv, 2, b"",
                                                            None))
    ur.save_state(str(t.blink), State(last_run_at=NOW - 7200, fails=26,
                                      failing_since=NOW - ur.GIVE_UP_S))
    user_config.put(str(t.blink), ur.SETTING, True)
    t.w.tick_once()
    assert t.state().off_reason == "failing"
    assert user_config.load(str(t.blink))[ur.SETTING] is False
    return t, runs


def _turn_on(t, at):
    """What `blink config usage-refresh on` does."""
    from pc import user_config
    user_config.put(str(t.blink), ur.SETTING, True)
    ur.clear_off(str(t.blink), now=at)


def test_reenabled_after_failing_one_failure_does_not_turn_it_off(
        tmp_path, monkeypatch, capsys):
    # The review's reproduction: the watch sees the setting off first, then
    # the person turns it on, then one run fails (a network blip). Before,
    # the watch's memory kept fails=26 and a day-old failing_since, and that
    # one failure turned it off again at once.
    from pc import user_config
    t, runs = _failing_watch(tmp_path, monkeypatch)
    t.clock[0] = NOW + 60
    t.w.tick_once()                      # sees it off
    _turn_on(t, NOW + 120)
    t.clock[0] = NOW + ur.REFRESH_AFTER_S   # spacing from the last run
    t.w.tick_once()
    assert len(runs) == 2
    s = t.state()
    assert s.off_reason == "" and s.fails == 1
    assert s.failing_since == NOW + ur.REFRESH_AFTER_S
    assert user_config.load(str(t.blink))[ur.SETTING] is True


def test_reenabled_within_one_tick_runs_again(tmp_path, monkeypatch):
    # The other order (Task 6 note): turned on before the watch ever saw it
    # off. Before, the reason held in memory kept it off until a restart
    # while `blink status` said "on".
    from pc import user_config
    t, runs = _failing_watch(tmp_path, monkeypatch)
    _turn_on(t, NOW + 30)
    t.clock[0] = NOW + 60
    t.w.tick_once()
    assert len(runs) == 1                # the throttle still holds
    t.clock[0] = NOW + ur.REFRESH_AFTER_S
    t.w.tick_once()
    assert len(runs) == 2
    assert t.state().off_reason == "" and t.state().fails == 1
    assert user_config.load(str(t.blink))[ur.SETTING] is True


@pytest.mark.allow_claude_launch
def test_a_cost_off_holds_until_turned_on_with_blink_config(tmp_path,
                                                            monkeypatch):
    from pc import user_config
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    costly = json.loads(ZERO)
    costly.update(num_turns=1, total_cost_usd=0.02)
    exe, rec = _moving_claude(tmp_path, json.dumps(costly), NOW)
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    user_config.put(str(t.blink), ur.SETTING, True)
    t.w.tick_once()
    assert t.state().off_reason == "cost"
    rec.unlink()
    t.clock[0] = NOW + 60
    t.w.tick_once()                      # sees it off
    # The key flipped by hand, not `blink config`: the reason on disk holds.
    user_config.put(str(t.blink), ur.SETTING, True)
    t.clock[0] = NOW + 3600
    t.w.tick_once()
    assert not rec.exists()
    _turn_on(t, NOW + 3600)
    t.clock[0] = NOW + 7200
    t.w.tick_once()
    assert rec.exists()


def test_a_state_file_that_reads_as_nothing_never_lifts_a_held_cost(
        tmp_path, monkeypatch):
    # Only a newer cleared_at lifts what the watch holds; a corrupt file
    # loads as all zeros and must not.
    from pc import user_config
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    runs = []
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               runs.append(1))
    t.w._mem = State(last_run_at=NOW - 7200, off_reason="cost",
                     cleared_at=5.0)
    user_config.put(str(t.blink), ur.SETTING, True)
    (t.blink / ur.STATE_FILE).write_text("{broken")
    t.w.tick_once()
    assert runs == []


def test_clear_off_always_writes_a_stamp(tmp_path):
    home = str(tmp_path)
    ur.clear_off(home, now=42.0)
    assert load_state(home).cleared_at == 42.0
    save_state(home, State(last_run_at=7.0, fails=3, failing_since=1.0,
                           off_reason="failing"))
    ur.clear_off(home, now=50.0)
    assert load_state(home) == State(last_run_at=7.0, cleared_at=50.0)


@pytest.mark.parametrize("run", [{"cleared_at": "x"}, {"cleared_at": True},
                                 {"missing": 1}])
def test_a_bad_new_field_is_a_fresh_state(tmp_path, run):
    with open(os.path.join(str(tmp_path), ur.STATE_FILE), "w",
              encoding="utf-8") as f:
        json.dump({"run": run}, f)
    assert load_state(str(tmp_path)) == State()


# --- final review, Important 2: finding claude without the shell's PATH ----

def _exe_at(d, name="claude"):
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text("")
    return str(p)


@pytest.mark.parametrize("where", [
    (".claude", "local"), (".npm-global", "bin")])
def test_find_claude_in_the_other_installers_folders(tmp_path, monkeypatch,
                                                     where):
    home = tmp_path / "h"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    exe = _exe_at(home.joinpath(*where))
    assert find_claude(str(home)) == exe


def test_find_claude_in_homebrew(tmp_path, monkeypatch):
    home = tmp_path / "h"
    brew = tmp_path / "opt" / "homebrew" / "bin"
    monkeypatch.setattr(ur, "SYSTEM_BIN_DIRS", (str(brew),))
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    exe = _exe_at(brew)
    assert find_claude(str(home)) == exe
    # The official installer's folder comes first.
    local = _exe_at(home / ".local" / "bin")
    assert find_claude(str(home)) == local


def test_find_claude_takes_the_newest_nvm_node(tmp_path, monkeypatch):
    home = tmp_path / "h"
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    node = home / ".nvm" / "versions" / "node"
    _exe_at(node / "v9.11.2" / "bin")
    new = _exe_at(node / "v20.11.1" / "bin")   # newer, though "v2" < "v9"
    assert find_claude(str(home)) == new
    # Only the newest Node is looked in: the one nvm's default most likely
    # is, rather than a claude left in a version nobody runs.
    (node / "v22.1.0" / "bin").mkdir(parents=True)
    assert find_claude(str(home)) is None


def test_the_recorded_path_comes_first_and_a_gone_one_falls_back(
        tmp_path, monkeypatch):
    home = tmp_path / "h"
    blink = tmp_path / ".blink"
    blink.mkdir()
    local = _exe_at(home / ".local" / "bin")
    rec = _exe_at(tmp_path / "opt" / "homebrew" / "bin")
    exe, _ = _fake_claude(tmp_path, ZERO)
    _on_path(monkeypatch, exe)            # even ahead of PATH
    ur.remember_claude(str(blink), rec)
    assert ur.recorded_claude(str(blink)) == rec
    assert ur.locate_claude(str(blink), str(home)) == rec
    os.remove(rec)
    assert ur.locate_claude(str(blink), str(home)) == exe
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert ur.locate_claude(str(blink), str(home)) == local


def test_remember_claude_keeps_the_rest_of_the_file(tmp_path):
    home = str(tmp_path)
    save_state(home, State(fails=2))
    ur.save_ask(home, ur.AskState(times=1))
    ur.remember_claude(home, "/opt/homebrew/bin/claude")
    assert load_state(home).fails == 2 and ur.load_ask(home).times == 1
    save_state(home, State(fails=3))        # and the other way round
    assert ur.recorded_claude(home) == "/opt/homebrew/bin/claude"


@pytest.mark.parametrize("junk", [3, "", "relative/claude", None])
def test_a_bad_recorded_path_is_none(tmp_path, junk):
    with open(os.path.join(str(tmp_path), ur.STATE_FILE), "w",
              encoding="utf-8") as f:
        json.dump({"exe": junk}, f)
    assert ur.recorded_claude(str(tmp_path)) is None


@pytest.mark.allow_claude_launch
def test_the_daemon_runs_the_recorded_claude_with_no_path(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    # launchd's PATH holds no claude; the CLI recorded where it was.
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    exe, rec = _moving_claude(tmp_path, ZERO, NOW + 2)
    t = _Watch(tmp_path, monkeypatch)
    # launchd's own PATH: the shell tools the fake needs, and no claude.
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    assert ur.find_claude(os.environ["HOME"]) is None
    ur.remember_claude(str(t.blink), exe)
    t.w.tick_once()
    assert rec.exists()
    assert capsys.readouterr().err.splitlines() == ["usage refresh ran"]


def test_a_daemon_that_finds_no_claude_says_so_in_the_state(tmp_path,
                                                            monkeypatch):
    _write_config(pathlib.Path(os.environ["HOME"]), NOW - 3600)
    found = [None]
    monkeypatch.setattr(ur, "find_claude", lambda home: found[0])
    t = _Watch(tmp_path, monkeypatch, runner=lambda argv, **kw:
               subprocess.CompletedProcess(argv, 3, b"", None))
    t.w.tick_once()
    assert t.state().missing is True
    found[0] = "/x/claude"
    t.w.tick_once()
    assert t.state().missing is False and t.state().fails == 1


# --- final review, Important 3: a cache with no figures still moves --------

def _null_cache_doc(fetched_at):
    return {"oauthAccount": {"accountUuid": ACCOUNT},
            "cachedUsageUtilization": {
                "accountUuid": ACCOUNT,
                "fetchedAtMs": int(fetched_at * 1000),
                "utilization": {"five_hour": None, "seven_day": None}}}


def test_the_view_carries_the_caches_own_fetch_time():
    from pc import claude_plan
    v = claude_plan._view(_null_cache_doc(NOW - 60), 0.0)
    assert v.windows is None and v.anchor is None
    assert v.cache_fetched_at == NOW - 60
    assert ur._cache_at(v) == NOW - 60
    other = _null_cache_doc(NOW)
    other["cachedUsageUtilization"]["accountUuid"] = "someone-else"
    assert claude_plan._view(other, 0.0).cache_fetched_at is None


@pytest.mark.allow_claude_launch
def test_a_run_that_moves_an_all_null_cache_is_ok_and_spaced(tmp_path,
                                                            monkeypatch,
                                                            capsys):
    home = pathlib.Path(os.environ["HOME"])
    (home / ".claude.json").write_text(
        json.dumps(_null_cache_doc(NOW - 3600)))
    exe, rec = _fake_claude(tmp_path, ZERO)
    body = json.dumps(_null_cache_doc(NOW + 2))
    text = pathlib.Path(exe).read_text()
    pathlib.Path(exe).write_text(text.replace(
        "cat <<'EOF'",
        f"cat > '{home / '.claude.json'}' <<'CFG'\n{body}\nCFG\ncat <<'EOF'",
        1))
    _on_path(monkeypatch, exe)
    t = _Watch(tmp_path, monkeypatch)
    t.w.tick_once()
    assert t.state().fails == 0 and t.state().last_run_at == NOW
    assert capsys.readouterr().err.splitlines() == ["usage refresh ran"]
    rec.unlink()
    t.clock[0] = NOW + ur.REFRESH_AFTER_S - 60
    t.w.tick_once()                      # fresh: 25 min from the cache
    assert not rec.exists()
    t.clock[0] = NOW + ur.REFRESH_AFTER_S + 5
    t.w.tick_once()
    assert rec.exists()


# --- final review, Important 4: an ask counts only when it was seen --------

def test_an_ask_never_answered_is_asked_again_daily_past_three(tmp_path,
                                                             monkeypatch):
    # A board that dozed through every ask (withdrawn, or dropped after ten
    # minutes) sends no answer; none of them was seen, so none counts.
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)
    for _ in range(ASK_AT_MOST + 2):
        assert k.a.due(CAPS) is True
        k.a.asked()
        assert k.a.due(CAPS) is False        # a day apart all the same
        k.clock[0] += ASK_EVERY_S
    assert k.ask().times == 0


def test_three_seen_laters_then_never(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", "")
    k = _Asker(tmp_path)
    for i in range(ASK_AT_MOST):
        assert k.a.due(CAPS) is True
        k.a.asked()
        k.a.answer("later")
        assert k.ask().times == i + 1
        k.clock[0] += ASK_EVERY_S
    assert k.a.due(CAPS) is False


@pytest.mark.parametrize("have", [True, False])
@pytest.mark.parametrize("a", ["on", "no"])
def test_an_answer_after_the_choice_was_made_changes_nothing(tmp_path,
                                                             have, a):
    # A box answered after `blink install` or `blink config` chose must not
    # overturn that choice.
    from pc import user_config
    k = _Asker(tmp_path)
    k.a.asked()
    user_config.put(str(k.blink), ur.SETTING, have)
    k.a.answer(a)
    assert k.setting() is have
    assert k.ask().declined is False


def test_an_answer_when_config_cannot_be_read_changes_nothing(tmp_path):
    k = _Asker(tmp_path)
    (k.blink / "config.json").write_text("{not json")
    k.a.answer("on")
    assert (k.blink / "config.json").read_text() == "{not json"


# --- final review, Important 5: CLAUDE_CONFIG_DIR goes with the run --------

def _env_runner(seen):
    def runner(argv, **kw):
        seen.update(kw["env"])
        return subprocess.CompletedProcess(argv, 0, ZERO.encode(), None)
    return runner


def test_run_once_passes_the_recorded_config_dir(tmp_path, monkeypatch):
    from pc import claude_home
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    h = claude_home.from_environ({"CLAUDE_CONFIG_DIR": str(tmp_path / "cc")})
    assert h.config_dir_set
    seen = {}
    run_once("c", str(tmp_path), _env_runner(seen), home=h)
    assert seen["CLAUDE_CONFIG_DIR"] == h.config_dir
    assert seen["BLINK_REFRESH"] == "1"


def test_run_once_drops_a_config_dir_the_record_did_not_choose(tmp_path,
                                                               monkeypatch):
    from pc import claude_home
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "elsewhere"))
    h = claude_home.from_environ({})
    assert not h.config_dir_set
    seen = {}
    run_once("c", str(tmp_path), _env_runner(seen), home=h)
    assert "CLAUDE_CONFIG_DIR" not in seen


def test_the_watch_hands_its_home_to_the_run(tmp_path, monkeypatch):
    import dataclasses

    from pc import claude_home
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    cc = pathlib.Path(os.environ["HOME"]) / "cc"
    cc.mkdir()
    (cc / ".claude.json").write_text(json.dumps(_config_doc(NOW - 3600)))
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")
    seen = {}
    t = _Watch(tmp_path, monkeypatch, runner=_env_runner(seen))
    rec = dataclasses.replace(
        claude_home.from_environ({"CLAUDE_CONFIG_DIR": str(cc)}),
        kind=claude_home.NATIVE)
    t.w._home = lambda: rec
    t.w._view = ur.config_view(lambda: rec)
    t.w.tick_once()
    assert seen["CLAUDE_CONFIG_DIR"] == str(cc)


def test_usage_refresh_does_not_import_ota():
    assert not hasattr(ur, "ota")
