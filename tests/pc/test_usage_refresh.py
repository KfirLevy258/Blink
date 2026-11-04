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
    assert t.state() == State()
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
