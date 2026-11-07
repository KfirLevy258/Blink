# Usage Refresh Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When Claude Code is not keeping the figures fresh, BLINK runs the person's own `claude -p /usage` (opt-in, zero cost) and shows the 5-hour, weekly and spend figures it caches.

**Architecture:** A read side, a run side and an asking side. The read side teaches `claude_plan` to read `five_hour`/`seven_day` from `cachedUsageUtilization` and the Claude provider to emit one more frame from it. The run side is a new `pc/usage_refresh.py` (find Claude Code, run it, judge the answer, keep state), a stdlib idle probe `pc/idle.py`, and a `RefreshWatch` thread in the daemon modelled on `status_page.StatusWatch`. The CLI asks once at install or update and exposes `blink config usage-refresh on|off`; people updated automatically are asked once on the board's touch screen (new `ask` capability).

**Tech Stack:** Python 3.11 standard library only (`subprocess`, `ctypes` on Windows, `json`, `threading`), pytest.

**Spec:** `docs/plans/usage-refresh.md` (read it first: the measurements, the guards, the legal reasoning).

## Global Constraints

- `pc/` is standard library only. No new dependency.
- Never log the `/usage` answer, any figure, or any part of stdout/stderr. Log lines name the event only: `usage refresh ran`, `usage refresh skipped`, `usage refresh failed (exit N)`, `usage refresh failed (timeout)`, `usage refresh off (cost)`, `usage refresh off (failing)`.
- BLINK never reads, stores or sends a login token or cookie, and never calls Anthropic itself.
- The command is exactly: `[exe, "-p", "/usage", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}']`, cwd `<blink_home>/refresh`, stdin `subprocess.DEVNULL`, stderr `subprocess.DEVNULL`, env = os.environ plus `BLINK_REFRESH=1`, `**ota.NO_WINDOW` on Windows, through `wsl._bounded_run(..., timeout=60)`.
- Never `--bare` (it drops the subscription login).
- Timing constants (in `pc/usage_refresh.py`): `FRESH_SOURCE_S = 15 * 60`, `REFRESH_AFTER_S = 25 * 60`, `ASK_EVERY_S = 24 * 3600`, `ASK_AT_MOST = 3`, `IDLE_LIMIT_S = 30 * 60`, `RUN_TIMEOUT_S = 60`, `BACKOFF_AFTER = 3`, `BACKOFF_S = 3600`, `GIVE_UP_S = 24 * 3600`, `TICK_S = 60`.
- Never for a WSL Claude home in this version (`claude_home` record kind `wsl`).
- Install copy, verbatim: `BLINK can keep your figures current when Claude Code isn't running, by asking Claude Code's /usage now and then. It sends no messages and uses no quota. Turn on? [Y/n] ` (work login: `[y/N] `). On-screen and CLI copy is sentence case.
- Paths resolve at call time, never at import (`tests/test_sandbox.py` enforces it). Importing any new module does nothing.
- Tests that start a fake `claude` carry `@pytest.mark.allow_claude_launch`; the fake is a script the test writes. No captured fixtures.
- Commit messages: plain subject, prose body, no trailers.

## Review Focus

1. **`five_hour` null or missing** (seen on the owner's Max login): the session dial stays unknown (`--`), never 0%, and the weekly dial still shows. Test in Task 1.
2. **Existing sources first:** a terminal or Desktop reading in the last 15 minutes means no run, and a Desktop source that later goes quiet lets the refresh take over within one tick. Test in Task 4.
3. **Cache written by another login** (`accountUuid` differs from `oauthAccount.accountUuid`): no frame. Test in Task 1.
4. **Claude Code hangs or the computer sleeps mid-run:** the run is killed at 60 s, the thread keeps ticking, the serial loop never waits on it. Test in Task 4.
5. **A Claude Code release makes `/usage` reach the model:** first such answer turns the refresh off with reason `cost`, and `blink status` says so. Test in Task 2 and Task 6.
6. **Windows:** no console window flashes (`NO_WINDOW` passed) and `claude.exe` under `%USERPROFILE%\.local\bin` is found. Test in Task 2.

---

### Task 0: Measurements (owner present, no code)

Done 2026-11-05 by the controller: Step 1 (logged out, recorded in the spec). Owed to Task 9 with the owner: Steps 2 and 3. Step 4 dropped: the refresh only runs on a cache older than 25 minutes, far outside the 1-5 minute reuse window.

These decide strings and one default; record each result in the spec's "What was measured" section and commit the spec.

- [ ] **Step 1:** Logged-out answer. On a machine or account where Claude Code is logged out, run the Global Constraints command from an empty folder. Record: exit code, whether stdout is JSON, `is_error`, `num_turns`, `total_cost_usd` (never the `result` text if it holds anything personal).
- [ ] **Step 2:** Windows. In the Windows VM with a logged-in Claude Code, run it once in PowerShell. Record `num_turns`/`total_cost_usd` and whether `cachedUsageUtilization.fetchedAtMs` moved.
- [ ] **Step 3:** A login that has a 5-hour window (Pro, or the work seat). Run `/usage` interactively and record whether `utilization.five_hour` is an object with `utilization` and `resets_at`.
- [ ] **Step 4:** Reuse window. Run twice 2 minutes apart, then 4 minutes apart; record whether `fetchedAtMs` moved each time.
- [ ] **Step 5:** Commit: `git add docs/plans/usage-refresh.md && git commit` with subject "usage refresh: measurements before building".

If Step 1 shows `num_turns` above 0 when logged out, stop and tell the owner: the cost guard would trip on every logged-out desk.

---

### Task 1: Read the 5-hour and weekly figures from the /usage cache

**Files:**
- Modify: `pc/claude_plan.py` (`ConfigView` :192, `EMPTY_VIEW` :206, `_view` :300; add `CacheWindows` and `_cache_windows`)
- Modify: `pc/providers/base.py` (add `USAGE_SRC_ID` next to `SPEND_SRC_ID` :63)
- Modify: `pc/providers/claude_cli.py` (`poll` :686; add `_usage_frame`)
- Test: `tests/pc/test_claude_plan.py`, `tests/pc/test_claude_cli.py` (or the existing files that test `_anchor` and `poll`; use them)

**Interfaces:**
- Produces: `claude_plan.CacheWindows(five_pct: float|None, five_resets_at: float|None, week_pct: float|None, week_resets_at: float|None, fetched_at: float)` (frozen dataclass); `ConfigView.windows: CacheWindows|None` (new last field, default None so existing constructors keep working; `EMPTY_VIEW` gets None); `base.USAGE_SRC_ID = "claude_usage"`.
- Produces: `claude_cli._usage_frame(windows: CacheWindows, now_epoch: float) -> NormalizedUsageFrame|None`.

- [ ] **Step 1: Write the failing tests** (doc built in the test as a dict, never captured):

```python
def _doc(five=None, week=None, uuid="u1", cache_uuid="u1", fetched_ms=1_793_828_166_919):
    util = {"five_hour": five, "seven_day": week}
    return {"oauthAccount": {"accountUuid": uuid},
            "cachedUsageUtilization": {"accountUuid": cache_uuid,
                                       "fetchedAtMs": fetched_ms,
                                       "utilization": util}}

def test_cache_windows_reads_both_windows():
    w = claude_plan._cache_windows(_doc(
        five={"utilization": 42, "resets_at": "2026-11-05T10:00:00+00:00"},
        week={"utilization": 15, "resets_at": "2026-11-11T05:59:59.801815+00:00"}))
    assert (w.five_pct, w.week_pct) == (42.0, 15.0)
    assert w.five_resets_at == 1793872800.0
    assert w.week_resets_at == pytest.approx(1794376799.8018, abs=1e-3)
    assert w.fetched_at == pytest.approx(1793828166.919)

def test_cache_windows_null_five_hour_keeps_week():
    w = claude_plan._cache_windows(_doc(five=None, week={"utilization": 15, "resets_at": "2026-11-11T05:59:59+00:00"}))
    assert w.five_pct is None and w.five_resets_at is None and w.week_pct == 15.0

def test_cache_windows_other_login_is_ignored():
    assert claude_plan._cache_windows(_doc(week={"utilization": 15, "resets_at": "2026-11-11T05:59:59+00:00"}, cache_uuid="someone-else")) is None

@pytest.mark.parametrize("bad", [{"utilization": "x"}, {"utilization": -1}, {"utilization": 1e309}, {"utilization": 5, "resets_at": "not a date"}, [], "15"])
def test_cache_windows_bad_values_never_raise(bad):
    w = claude_plan._cache_windows(_doc(week=bad))
    assert w is None or w.week_pct is None or 0 <= w.week_pct <= 1000

def test_cache_windows_none_without_any_window():
    assert claude_plan._cache_windows(_doc()) is None
```

and in the provider tests:

```python
def test_usage_frame_from_cache():
    w = claude_plan.CacheWindows(None, None, 15.0, 1794376799.0, 1793828166.0)
    f = claude_cli._usage_frame(w, now_epoch=1793828200.0)
    assert f.src == base.USAGE_SRC_ID and f.plan == base.PLAN_WINDOWS
    assert f.session_pct == base.UNKNOWN and f.weekly_pct == 15.0
    assert f.observed_at == 1793828166.0 and f.weekly_resets_at == 1794376799.0

def test_poll_adds_usage_frame_on_a_windows_login(tmp_path): ...
    # config with seven_day 15%, no status line: poll() returns a frame with src USAGE_SRC_ID

def test_poll_adds_no_usage_frame_on_a_spend_seat(tmp_path): ...
    # seatTier enterprise_usage_based + seven_day present: no USAGE_SRC_ID frame (the spend frame owns that page)
```

- [ ] **Step 2: Run them, expect FAIL** (`AttributeError: _cache_windows`): `.venv-test/bin/python -m pytest tests/pc/test_claude_plan.py tests/pc/test_claude_cli.py -q -k "cache_windows or usage_frame"`
- [ ] **Step 3: Implement.**
  - `_cache_windows(doc) -> CacheWindows|None` in `claude_plan.py`, beside `_anchor`. Reuse `_anchor`'s account check and `fetchedAtMs` bounds (factor the shared part into `_cache(doc) -> (dict, float)|None` so both use it). Percent through `_number`, accepted in 0..1000. `resets_at` parsed with `datetime.fromisoformat` inside a try; out-of-range or unparseable gives None for that field. Returns None when neither window has a percent. Never raises (catch `ValueError`, `TypeError`, `OverflowError`).
  - `_view` sets `windows=_cache_windows(doc)`.
  - `_usage_frame` builds `NormalizedUsageFrame(provider="claude", src=USAGE_SRC_ID, observed_at=w.fetched_at, active_at=None, session_pct=..., session_resets_at=..., weekly_pct=..., weekly_resets_at=...)`, passing each window through `base.settle_window(pct, resets_at, now_epoch, stale)` as `statusline_source.map_statusline_frame` does, with `stale = now_epoch - w.fetched_at > statusline_source.STALE_AFTER_S`. Missing percent stays `base.UNKNOWN`.
  - In `poll`, after the spend step: when `view.windows` is set and `not view.spend_seat`, append `_usage_frame(view.windows, now)`.
  - Check `claude_account._speaks_for_account` treats `USAGE_SRC_ID` as speaking for the account (it is account-wide); add it if the function keys on `src`.
- [ ] **Step 4: Run the new tests and the whole suite, expect PASS:** `.venv-test/bin/python -m pytest tests/ -q`
- [ ] **Step 5: Commit** "Read the 5-hour and weekly figures from Claude Code's /usage cache" with a body saying the cache was only read for spend before, and that a null `five_hour` (seen on a Max login 2026-11-04) leaves the session dial unknown.

---

### Task 2: `pc/usage_refresh.py` — find, run, judge, remember

**Files:**
- Create: `pc/usage_refresh.py`
- Test: `tests/pc/test_usage_refresh.py`

**Interfaces:**
- Consumes: `wsl._bounded_run(argv, *, input=None, timeout=None, **kwargs)`, `ota.NO_WINDOW`, `theme._write_json(path, obj, tries=1)`, `user_config.load/put`.
- Produces:
  - `SETTING = "usage-refresh"` (the `config.json` key; value `True`/`False`; absent means never asked).
  - `find_claude(home: str) -> str|None`: `shutil.which("claude")`, then `<home>/.local/bin/claude` (`claude.exe` on Windows) when it is a file.
  - `command(exe: str) -> list[str]`: the Global Constraints argv.
  - `Verdict` enum: `OK`, `COST`, `FAILED`, `TIMEOUT`.
  - `judge(returncode: int, stdout: bytes) -> Verdict`: `COST` when the JSON shows `num_turns > 0`, `total_cost_usd > 0` or a non-empty `modelUsage` (regardless of exit code); `OK` when exit 0, `type == "result"`, `is_error` false, `num_turns == 0`, `total_cost_usd == 0`; otherwise `FAILED`. Never raises.
  - `run_once(exe: str, blink_home: str, runner=wsl._bounded_run) -> Verdict`: creates `<blink_home>/refresh` (mode 0o700), runs with `timeout=RUN_TIMEOUT_S`, maps `TimeoutExpired` to `TIMEOUT` and `OSError` to `FAILED`.
  - `State` (dataclass): `last_run_at: float = 0.0`, `fails: int = 0`, `failing_since: float|None = None`, `off_reason: str = ""` (`""`, `"cost"`, `"failing"`). `load_state(blink_home) -> State` (missing/corrupt file gives `State()`), `save_state(blink_home, state)` to `<blink_home>/usage-refresh.json` via `theme._write_json(..., tries=1)`.
  - `record(state: State, verdict: Verdict, now: float) -> State`: OK resets `fails`/`failing_since`; COST sets `off_reason="cost"`; FAILED/TIMEOUT bump `fails`, set `failing_since` if None, and set `off_reason="failing"` once `now - failing_since >= GIVE_UP_S`. Always sets `last_run_at = now`.
  - `spacing(state: State) -> float`: `BACKOFF_S` when `fails >= BACKOFF_AFTER`, else `REFRESH_AFTER_S`.

- [ ] **Step 1: Write the failing tests.** A helper `_fake_claude(tmp_path, stdout: str, exit_code=0, sleep=0)` writes an executable `claude` script (sh on POSIX) that prints `stdout`, optionally sleeps, and records its argv, cwd and `BLINK_REFRESH` into a file. Tests (all `@pytest.mark.allow_claude_launch` where they launch):

```python
ZERO = json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 0, "total_cost_usd": 0, "modelUsage": {}, "result": "x"})

def test_judge_zero_cost_is_ok(): assert judge(0, ZERO.encode()) is Verdict.OK
@pytest.mark.parametrize("field,value", [("num_turns", 1), ("total_cost_usd", 0.0001), ("modelUsage", {"claude-x": {}})])
def test_judge_any_model_use_is_cost(field, value): ...  # also with returncode 1
@pytest.mark.parametrize("out", [b"", b"not json", b"[]", json.dumps({"type": "result", "is_error": True, "num_turns": 0, "total_cost_usd": 0}).encode()])
def test_judge_anything_else_is_failed(out): assert judge(0, out) is Verdict.FAILED
def test_judge_nonzero_exit_without_model_use_is_failed(): assert judge(1, ZERO.encode()) is Verdict.FAILED
def test_command_is_exact(): assert command("/x/claude") == ["/x/claude", "-p", "/usage", "--output-format", "json", "--no-session-persistence", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}', "--settings", '{"disableAllHooks":true}']
def test_command_never_bare(): assert "--bare" not in command("c")
def test_run_once_runs_in_its_own_folder_with_the_marker(tmp_path): ...  # cwd == <home>/.blink/refresh, BLINK_REFRESH == "1", verdict OK
def test_run_once_timeout(tmp_path): ...  # runner raising subprocess.TimeoutExpired -> Verdict.TIMEOUT (inject runner, no real sleep)
def test_run_once_passes_no_window_on_windows(monkeypatch): ...  # fake runner records kwargs; with sys.platform "win32" creationflags == 0x08000000
def test_run_once_logs_nothing(tmp_path, capsys): ...  # out/err empty after an OK and a FAILED run
def test_find_claude_on_path_then_local_bin(tmp_path, monkeypatch): ...
def test_find_claude_windows_exe(tmp_path, monkeypatch): ...  # <home>\.local\bin\claude.exe found when sys.platform == "win32" and PATH has none
def test_record_cost_turns_off(): assert record(State(), Verdict.COST, 10).off_reason == "cost"
def test_record_backoff_after_three_failures(): ...  # spacing() == BACKOFF_S after 3 FAILED, back to REFRESH_AFTER_S after an OK
def test_record_gives_up_after_a_day_of_failures(): ...  # FAILED at t=0 and t=GIVE_UP_S -> off_reason "failing"; an OK in between resets it
def test_state_round_trip_and_corrupt_file(tmp_path): ...
```

- [ ] **Step 2: Run, expect FAIL** (module missing): `.venv-test/bin/python -m pytest tests/pc/test_usage_refresh.py -q`
- [ ] **Step 3: Implement** the interfaces above. Module docstring explains the measurements (zero turns, zero cost, 2026-11-04) and why each flag is there; no work at import.
- [ ] **Step 4: Run, expect PASS**, then `tests/test_import_is_inert.py` and `tests/test_sandbox.py`.
- [ ] **Step 5: Commit** "usage_refresh: run the person's own /usage and refuse any answer that used the model".

---

### Task 3: `pc/idle.py` — seconds since the last keyboard or mouse input

**Files:**
- Create: `pc/idle.py`
- Test: `tests/pc/test_idle.py`

**Interfaces:**
- Produces: `seconds_idle(runner=subprocess.run, platform=sys.platform) -> float|None`. None means "this OS cannot say" (Linux, or any error).
  - macOS: `ioreg -c IOHIDSystem -d 4 -k HIDIdleTime -r` (timeout 5 s); first `"HIDIdleTime" = <n>` line; n is nanoseconds; return n / 1e9.
  - Windows: `ctypes` `user32.GetLastInputInfo` with a `LASTINPUTINFO` struct and `kernel32.GetTickCount`; idle = (tick - dwTime) mod 2**32 / 1000. Wrapped so a missing `windll` returns None. (Allowed in a function only; nothing at import.)
  - Anything else: None.

- [ ] **Step 1: Failing tests:** macOS parse from generated `ioreg` text (`'    |   "HIDIdleTime" = 1500000000\n'` gives 1.5); garbage or non-zero exit gives None; runner raising `OSError`/`TimeoutExpired` gives None; `platform="linux"` gives None without calling the runner; a Windows path test with a fake `ctypes` shim object (monkeypatch a module-level `_win_idle` hook) for tick wrap-around (`tick=5, dwTime=2**32-5` gives 0.01).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** Comment why Linux returns None (no stdlib source; X11/Wayland differ) and that None counts as in use (Task 4).
- [ ] **Step 4: Run, expect PASS** plus the import and sandbox tests.
- [ ] **Step 5: Commit** "idle: how long since the person last touched the computer, where the OS will say".

---

### Task 4: `RefreshWatch` — the daemon thread and its fallback rule

**Files:**
- Modify: `pc/usage_refresh.py` (add `due` and `RefreshWatch`)
- Modify: `pc/ingest.py` (`IngestionBus.poll` :531; add `claude_fresh_at`)
- Modify: `claude_usage_bridge.py` (start it next to `status_page.StatusWatch`, :1071-1073)
- Test: `tests/pc/test_usage_refresh.py`, `tests/test_bridge_main.py` (or wherever `StatusWatch` startup is tested)

**Interfaces:**
- Consumes: Task 1 `ConfigView.windows`, `ConfigView.spend_seat`; Task 2 `State`, `run_once`, `record`, `spacing`, `find_claude`; Task 3 `seconds_idle`; `bridge.board_alive()`; `claude_home` record kind.
- Produces:
  - `due(now, *, enabled, native, board_alive, idle_s, cache_at, other_at, state) -> bool` (all keyword, pure). `other_at` is the newest time any other Claude source gave a figure (status-line mtime, or the newest `observed_at` of a Claude frame with `has_usage()` whose `src` is not `USAGE_SRC_ID`), or None. True only when: `enabled`; `native`; `board_alive`; `state.off_reason == ""`; `idle_s is None or idle_s < IDLE_LIMIT_S`; `other_at is None or now - other_at >= FRESH_SOURCE_S`; `cache_at is None or now - cache_at >= REFRESH_AFTER_S`; `now - state.last_run_at >= spacing(state)`. No spend-seat exception: the running count covers terminal use.
  - `IngestionBus.claude_fresh_at() -> float|None` in `pc/ingest.py`: set at the end of each `poll()` to the newest `observed_at` among this poll's Claude frames that have a figure and are not `USAGE_SRC_ID` (Claude Code status line, Desktop Code tab, chat, Cowork). A plain float attribute written by the poll loop and read by the refresh thread; say in a comment that a float read is atomic in CPython and a one-tick-old value is harmless.
  - `class RefreshWatch(blink_home, home, enabled, board_alive, view, other_at, now=time.time, idle=idle.seconds_idle, runner=wsl._bounded_run)` with `start()` (daemon thread named `blink-refresh`, ticks every `TICK_S`) and `tick_once()` (one decision and at most one run; saves state; logs one event line). `view(now_epoch) -> ConfigView` is the `look` of a `claude_plan.PlanReader(home=home)` that RefreshWatch owns (not the bus's shared reader: that one is used from the poll loop, and PlanReader is not thread-safe); `other_at() -> float|None` is the newer of the status-line payload's mtime and `bus.claude_fresh_at()`.
  - A run judged `OK` counts only if the cache moved: right after it, `tick_once` reads the config through its own reader with a fresh `now` and requires `view.anchor`/`view.windows` `fetched_at >= run_started_at - 5`; otherwise it records `Verdict.FAILED` instead. Measured 2026-11-05: a logged-out Claude Code answers `-p /usage` with a zero-cost `success` whose `result` is a session-cost summary and writes no cache.
  - On a run that turns it off (`off_reason` newly set), `tick_once` also writes `user_config.put(blink_home, SETTING, False)` so `blink status` and `blink config` agree.

- [ ] **Step 1: Failing tests:** a table test for `due` with one row per condition flipping it to False, and the all-true row giving True; a terminal used 14 minutes ago (status-line mtime) blocks it and 16 minutes ago does not; a Desktop chat frame 10 minutes old blocks it; a spend seat with a fresh status line is NOT due; Linux (`idle_s=None`) due. `claude_fresh_at` tests: a poll with a Desktop frame sets it, a poll with only a `USAGE_SRC_ID` frame leaves it unchanged, a frame with no figure is ignored. `tick_once` tests with injected fakes: runs once when due and records `last_run_at`; does not run when not due; an OK verdict whose cache did not move is recorded as FAILED (fake claude prints the zero-cost JSON but does not touch the config); an OK verdict whose fake claude rewrites `fetchedAtMs` is recorded as OK; a COST verdict sets the setting False and logs `usage refresh off (cost)` only; a runner that raises any exception leaves the thread alive (the next `tick_once` still runs) and logs the exception type only; `find_claude` returning None logs nothing per tick (one line per streak, like `WslWatch._tick_once`). Main-wiring test: with the setting on and a native home, `main` starts a thread named `blink-refresh`; with a WSL record it does not.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.** In `claude_usage_bridge.main`, build `RefreshWatch` with `enabled=lambda: user_config.load(blink_home).get(usage_refresh.SETTING) is True` (catching `OSError`/`user_config.Unreadable` as False), `board_alive=bridge.board_alive`, `view=claude_plan.PlanReader(home=home).look` (its own reader), and `other_at=` the newer of the provider's status-line mtime (None on `OSError`) and `bus.claude_fresh_at()`. `board_alive` only reads a timestamp, so calling it from this thread is safe; say so in a comment. Never call it from the serial thread.
- [ ] **Step 4: Run, expect PASS**, full suite.
- [ ] **Step 5: Commit** "RefreshWatch: ask /usage only when nothing fresher exists and someone is at the computer".

---

### Task 5: BLINK's own shims ignore a refresh run

**Files:**
- Modify: `tools/blink-statusline.sh` (before `input=$(cat)` :40), `tools/blink-hook.sh` (before `input=$(cat)` :66)
- Modify: whatever `cli.shim_is_current` (:450), `install_statusline.shim_content_check` (:1308) compare against, if they pin content
- Test: the existing shim tests (search `tests/` for `blink-hook.sh`)

- [ ] **Step 1: Failing tests:** running each shim with `BLINK_REFRESH=1` and a payload on stdin exits 0, writes no payload/state file and prints nothing; without it, behaviour is unchanged (existing tests).
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement:** `[ "${BLINK_REFRESH:-}" = 1 ] && { cat >/dev/null; exit 0; }` with a comment: `--settings disableAllHooks` already stops hooks; this is the second lock in case a future Claude Code ignores the setting. Drain stdin so the writer never gets SIGPIPE.
- [ ] **Step 4: Run, expect PASS**, full suite (shim content checks included).
- [ ] **Step 5: Commit** "Shims step aside for BLINK's own /usage run".

---

### Task 6: CLI — the install question, the setting, status, uninstall

**Files:**
- Modify: `pc/cli.py`: `_CONFIG` (:6107) gains `"usage-refresh"`; `cmd_update` (:5842) calls `_install_refresh_step` after a successful interactive update when the setting is absent; new `_ask_yes(question: str, default: bool) -> bool|None` and `_install_refresh_step(home) -> str`; `cmd_install` (:3716) calls it after the service step (its own `step("Usage refresh")`, so `_install_steps()` (:3497) returns 7 on win32, 6 elsewhere); `cmd_status` (:5096) gains a `Usage refresh` row; uninstall's file list (:4681-4720) gains `usage-refresh.json` and the `refresh/` folder.
- Test: `tests/test_cli.py` (or the install/status/uninstall test files already there)

**Interfaces:**
- Consumes: Task 2 `SETTING`, `find_claude`, `load_state`; `claude_plan` view for `spend_seat`/`org_kind`; `claude_home` record.
- Produces: `_ask_yes` returns None when `sys.stdin.isatty()` is false (never asks), True/False otherwise ("" means the default; y/yes/n/no, case-insensitive; anything else asks again, at most 3 times, then the default).

Rules for `_install_refresh_step(home)`, returning the line printed after `[n/N] Usage refresh ... `:
- WSL home: `"not available for WSL yet"`.
- Setting already True or False: `"on"` / `"off"` (no question; a re-install never asks twice).
- No Claude Code found: `"off (Claude Code isn't installed; see docs/README-full.md#keeping-figures-fresh)"`.
- Work login (`view.spend_seat` or `view.org_kind in ("enterprise", "team")`): default False, prompt `[y/N] `; otherwise default True, prompt `[Y/n] `. Prompt text verbatim from Global Constraints, printed on its own line before the answer.
- Non-interactive (`_ask_yes` None): `"off (turn on with: blink config usage-refresh on)"`, setting left absent so a later interactive install asks.
- Answer saved with `user_config.put(blink_home, SETTING, answer)`; prints `"on"` or `"off (turn on with: blink config usage-refresh on)"`.

Status row values: `on`, `on, last ran 12 min ago`, `off`, `off: Claude Code's /usage used the model` (`off_reason == "cost"`), `off: /usage kept failing; run claude and /login, then turn it on again` (`"failing"`), `not set up` (absent). `blink config usage-refresh on` also clears `off_reason` and `fails` in `usage-refresh.json`.

- [ ] **Step 1: Failing tests:** each rule above (fake stdin via monkeypatched `isatty` and `input`); prompt text exact; work login defaults No on Enter; personal login defaults Yes on Enter; non-interactive leaves the key absent; re-install with the key set prints without asking; `blink config usage-refresh on` after a cost trip clears `off_reason`; status rows exact; uninstall removes `usage-refresh.json`, its temp siblings and `refresh/`; `blink install --wsl` never asks.
- [ ] **Step 2: Run, expect FAIL.**
- [ ] **Step 3: Implement.**
- [ ] **Step 4: Run, expect PASS**, full suite.
- [ ] **Step 5: Commit** "Install asks once whether to keep the figures fresh with /usage".

---

### Task 7: Asking people who already have BLINK, on the board

**Files:**
- Modify: `firmware/src/proto.c` (`BOARD_CAPS` :222 gains `"ask"`; parse `{"t":"ask","id":"refresh"}`; send `{"t":"answer","id":"refresh","a":"on"|"no"|"later"}`)
- Create: `firmware/src/ask_prompt.c`, `firmware/src/ask_prompt.h` (pure decision logic, like `upd_prompt.c`) and the LVGL box in the file that draws `upd_prompt`'s box
- Modify: `tests/pc/test_board_caps.py` (welcome width with the new cap)
- Modify: `pc/usage_refresh.py` (`AskState` and `should_ask`), `pc/bridge.py` (send `ask`, receive `answer`), `claude_usage_bridge.py` (wire it)
- Test: firmware host tests for `ask_prompt.c` (`tests/ci/check_host_tests.sh`), `tests/pc/test_usage_refresh.py`, `tests/pc/test_bridge.py`

**Interfaces:**
- Produces: `should_ask(now, *, setting_absent, native, claude_found, work_login, board_caps, idle_s, ask_state) -> bool`: True only when the setting is absent, native, Claude Code found, not a work login, `"ask" in board_caps`, someone at the computer (`idle_s is None or idle_s < IDLE_LIMIT_S`), `ask_state.times < ASK_AT_MOST`, `not ask_state.declined` and `now - ask_state.last_at >= ASK_EVERY_S`.
- Produces: `AskState(times: int = 0, last_at: float = 0.0, declined: bool = False)` kept in `usage-refresh.json` beside `State`.
- Answers: `"on"` -> `user_config.put(blink_home, SETTING, True)`; `"no"` -> `put(..., False)` and `declined=True`; `"later"` or no answer before the board closes the box -> counts as one ask.
- On-screen copy (sentence case, each line within the box width): title `Keep figures fresh?`; body `Blink can ask Claude Code's /usage now and then. No messages, no quota.`; buttons `Turn on`, `No thanks`; the box closes itself after 60 s as `later`.

- [ ] **Step 1: Failing tests:** `should_ask` table (each condition flips it; a board without `ask` never asked; a work login never asked; three `later`s then never); bridge test: `ask` is sent once and `answer` writes the setting; the `ask` line goes through `protocol.encode_checked`; the log names `ask`/`answer` only. Firmware host tests: the prompt never stacks on the update offer or on any open box (`busy`), withdraws on host loss, and `later` is sent when it times out.
- [ ] **Step 2: Run, expect FAIL** (`pytest -q tests/pc/test_usage_refresh.py tests/pc/test_bridge.py tests/pc/test_board_caps.py`, `sh tests/ci/check_host_tests.sh`).
- [ ] **Step 3: Implement**, firmware first. Keep the welcome within its measured budget; `test_board_caps.py` fails if it grows past it.
- [ ] **Step 4: Run, expect PASS**; build the firmware (`west build`, Zephyr 4.3.x).
- [ ] **Step 5: Commit** "Ask people who already have BLINK, once, on the board".

Firmware is not done until Task 9 has flashed it with `tools/burn.sh` and seen it boot.

---

### Task 8: Docs and the house rules

**Files:**
- Modify: `docs/README-full.md`: a new `### Keeping figures fresh` under "Where the numbers come from" (:46) (what runs, when, the exact command, zero cost as measured, how to turn it off, that it is the person's own Claude Code and login); a row in "What the installer changes" (:220) for `usage-refresh.json` and `refresh/`; a bullet in "Security & privacy" (:446): BLINK still holds no credential; the refresh is opt-in and runs the person's own Claude Code; Anthropic's terms restrict automated use and the person decides.
- Modify: `CLAUDE.md` hard rules: keep "Never generate usage to refresh a reading" and add one paragraph: the opt-in refresh runs `claude -p /usage`, which measured zero turns and zero cost; any change to its command needs that measurement again, and `--bare` is never used.
- Modify: `docs/plans/usage-refresh.md` status line to "built".
- Modify: release notes for the next version (the file `docs/releases/` uses for the upcoming version).

- [ ] **Step 1:** Write them. Plain words, sentence case, no reverse-engineering detail beyond the command itself, no employer seat values.
- [ ] **Step 2:** `.venv-test/bin/python -m pytest tests/ -q` (doc-linked tests, if any, pass).
- [ ] **Step 3: Commit** "Docs: the opt-in /usage refresh, what it runs and how to turn it off".

---

### Task 9: Verify on the desk (owner present)

- [ ] **Step 1:** Full suite on macOS; record the count. Re-run Ubuntu (Docker) and Windows (VM) suites; record each.
- [ ] **Step 2:** `blink install` from this tree on this Mac; answer the question; `blink status` shows `Usage refresh  on`.
- [ ] **Step 3:** With no Claude Code session open for 25+ minutes and the owner at the computer, watch `bridge.log` for exactly one `usage refresh ran`, `cachedUsageUtilization.fetchedAtMs` moving, and the board's weekly dial matching `/usage`. Check `~/.claude/projects` gained no transcript.
- [ ] **Step 4:** Lock the screen for 35 minutes; no run during it. With a Claude Code terminal or a Desktop chat in use, no run either.
- [ ] **Step 4b:** Remove the setting from `config.json`, restart the service, and check the board asks once; tap "No thanks"; it never asks again. Repeat with "Turn on". Flash with `tools/burn.sh` first (company logo kept) and boot-verify.
- [ ] **Step 5:** `blink config usage-refresh off`; no further runs. Uninstall on a VM removes `usage-refresh.json` and `refresh/`.
- [ ] **Step 6:** Note results in the spec and memory. No push, no PR: ships with the next version when the owner says so.
