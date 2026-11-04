import json
import os
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
