# Shim Self-Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `~/.blink/blink-hook.sh` repair itself when it is older than the daemon that reads it, so the session-name feature stops being a silent no-op on every install that arrives by `blink update`.

**Architecture:** The daemon already runs `install_statusline.DriftWatchdog` on a 300-second tick to put the statusline hook back when something removes it. That watchdog answers "is the *entry in settings.json* still ours?". The hook shim has a different fault: its settings entry is perfectly correct and its *contents* are stale. So this adds a second, content-based check beside the existing one, sharing the same tick, the same install-marker gate, and the same never-raise discipline. Both shims are then covered by one repair pass.

**Tech Stack:** Python 3.10+, pytest. No new dependencies.

**Spec:** No separate spec. The defect is recorded in the branch review and reproduced in Task 1's failing test; this plan is the argument for the fix.

## Global Constraints

- **Never override a deliberate uninstall.** `install_statusline.drift_check` gates every repair on `_read_marker()` returning truthy, with the comment "a missing marker means the user uninstalled, and that is never overridden". Any repair added here obeys the same rule.
- **Never raise inside the poll loop.** `drift_check`'s docstring: "a daemon that dies because settings.json was briefly unreadable is a worse outcome than a hook that stays missing for another sixty seconds." Repair functions return a description or `None`; they do not propagate.
- **`BLINK_NO_WATCHDOG`** (`install_statusline.py:485`, `WATCHDOG_DISABLE_ENV`) disables self-healing entirely. New repairs honour it.
- **Shim files are written with `newline="\n"`.** `cli.py:170` carries a comment explaining that Windows CRLF broke the shim on every status line render and every tool call, and that the CI CRLF check never looked at the installed copy. Any code that writes or compares a shim preserves this.
- Python 3.10+, matching the style in `pc/`.
- Comments explain WHY, in prose, at the density of the surrounding file.
- `pytest tests -q` passes (515 today) and `sh tests/ci/check_hook_shim.sh sh` passes at every commit.

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `pc/cli.py` | shim paths, shim source, install/update commands | add `shim_is_current()`; use it in `cmd_status` |
| `pc/install_statusline.py` | drift detection and the watchdog | add `shim_content_check()`; call it from `DriftWatchdog.tick` |
| `claude_usage_bridge.py` | the daemon loop (REPO ROOT, not `pc/`) | hand the watchdog the hook shim as well |
| `tests/pc/test_shim_repair.py` | new | the whole repair path |
| `tests/pc/test_ingest.py` | existing | delete a test that cannot fail |
| `tests/ci/check_hook_shim.sh` | existing | repair an assertion that cannot fail |
| `tests/usage_layout/host_test.c` | existing | assert the clock, which currently has none |

Tasks 1–3 are the repair and are strictly ordered. Tasks 4–6 are independent of them and of each other: each removes a test that cannot fail, which is what makes the green suite mean something after this plan.

---

### Task 1: A shim knows whether it is current

**Files:**
- Modify: `pc/cli.py`
- Test: `tests/pc/test_shim_repair.py` (create)

**Interfaces:**
- Consumes: `_shim_source(name)` (`cli.py:154`), `hook_shim_path()` (`cli.py:62`), `shim_path()` (`cli.py:58`).
- Produces: `def shim_is_current(path: str, name: str) -> bool` — Task 2 and Task 3 both call it.

- [ ] **Step 1: Write the failing test**

```python
"""The installed shims repair themselves when they fall behind the daemon.

`blink update` swaps the program directory and restarts the service. It has
never rewritten ~/.blink/blink-hook.sh, and nothing else did either, so a
customer upgrading the documented way ran a new daemon that reads `name` out
of the state files against an old shim that never writes one. The board was
never named, `blink status` said "hooks installed (10/10 events)" because the
path existed and still ran, and nothing anywhere said why.
"""
import os

from pc import cli


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def test_a_shim_matching_the_bundle_is_current(tmp_path):
    p = str(tmp_path / "blink-hook.sh")
    _write(p, cli._shim_source("blink-hook.sh"))

    assert cli.shim_is_current(p, "blink-hook.sh") is True


def test_an_older_shim_is_not_current(tmp_path):
    """The real defect: same path, same name, older contents."""
    p = str(tmp_path / "blink-hook.sh")
    _write(p, '#!/bin/sh\n# a shim from before this feature existed\n')

    assert cli.shim_is_current(p, "blink-hook.sh") is False


def test_a_missing_shim_is_not_current(tmp_path):
    p = str(tmp_path / "does-not-exist.sh")

    assert cli.shim_is_current(p, "blink-hook.sh") is False


def test_an_unreadable_shim_is_not_current(tmp_path):
    """Answer the question rather than raising it.

    This runs inside the daemon's poll loop. "I cannot read it" and "it is
    stale" lead to the same action -- rewrite it -- and a raise here would
    take the daemon down over a file permission.
    """
    p = str(tmp_path / "blink-hook.sh")
    _write(p, cli._shim_source("blink-hook.sh"))
    os.chmod(p, 0o000)
    try:
        assert cli.shim_is_current(p, "blink-hook.sh") is False
    finally:
        os.chmod(p, 0o644)


def test_line_endings_alone_make_a_shim_stale(tmp_path):
    """CRLF is not a cosmetic difference on this file.

    cli._write_shim writes newline="\\n" because Windows wrote the shim with
    CRLF and Git Bash then failed on `case ... in\\r` at every status line
    render and every tool call. A shim that differs ONLY by line ending is
    the exact shape of that bug, so it must read as stale and be rewritten.
    """
    p = str(tmp_path / "blink-hook.sh")
    with open(p, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(cli._shim_source("blink-hook.sh"))

    assert cli.shim_is_current(p, "blink-hook.sh") is False
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/pc/test_shim_repair.py -q`
Expected: FAIL — `AttributeError: module 'pc.cli' has no attribute 'shim_is_current'`.

- [ ] **Step 3: Implement it**

In `pc/cli.py`, immediately after `_write_shim` (which ends at :178):

```python
def shim_is_current(path: str, name: str) -> bool:
    """Is the shim at `path` byte-identical to the one we would install?

    Byte-identical, not "close enough": the one difference that has actually
    bitten was line endings, and a comparison that normalised them would have
    called the broken file healthy. Read in binary and compare exactly.

    False on any failure to read. The caller's response to "stale" and to "I
    cannot tell" is the same -- write it again -- and this runs inside the
    daemon's poll loop, where raising is the one outcome worse than a shim
    that stays stale for another five minutes.
    """
    try:
        with open(path, "rb") as f:
            installed = f.read()
    except OSError:
        return False

    try:
        expected = _shim_source(name).encode("utf-8")
    except OSError:
        # The bundle itself is unreadable. Claiming the installed shim is
        # stale would start a rewrite that cannot succeed and would log the
        # failure every tick; claiming it is current changes nothing. Say
        # current and let the louder failure surface elsewhere.
        return True

    return installed == expected
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/pc/test_shim_repair.py -q`
Expected: 5 passed.

- [ ] **Step 5: Commit**

```bash
git add pc/cli.py tests/pc/test_shim_repair.py
git commit -m "feat: a shim can tell whether it is the one we would install"
```

---

### Task 2: The watchdog repairs stale shim contents

**Files:**
- Modify: `pc/install_statusline.py`
- Test: `tests/pc/test_shim_repair.py`

**Interfaces:**
- Consumes: `shim_is_current()` (Task 1); `_read_marker()` (`install_statusline.py:98`); `WATCHDOG_DISABLE_ENV` (`:485`).
- Produces: `def shim_content_check(shims) -> str | None`, where `shims` is a sequence of `(path, name)` pairs. Task 3 passes the two real shims to it.

Note the deliberate difference from `drift_check`: that function asks whether *settings.json still points at us*. This one asks whether *the file we point at is still ours*. Both faults are real, they are independent, and the second is the one that has shipped.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_shim_repair.py`:

```python
from pc import install_statusline


def test_a_stale_shim_is_rewritten(tmp_path, monkeypatch):
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")

    what = install_statusline.shim_content_check([(str(p), "blink-hook.sh")])

    assert what is not None
    assert "blink-hook.sh" in what
    assert p.read_text(encoding="utf-8") == cli._shim_source("blink-hook.sh")


def test_a_current_shim_is_left_alone(tmp_path, monkeypatch):
    """Silence is the normal case -- this runs every 300 seconds forever."""
    p = tmp_path / "blink-hook.sh"
    _write(str(p), cli._shim_source("blink-hook.sh"))
    before = p.stat().st_mtime_ns
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")

    assert install_statusline.shim_content_check([(str(p), "blink-hook.sh")]) is None
    assert p.stat().st_mtime_ns == before


def test_no_marker_means_hands_off(tmp_path, monkeypatch):
    """An uninstalled machine is not a broken one.

    Same rule drift_check states: a missing marker means the user uninstalled,
    and that is never overridden. Without this, `blink uninstall` would be
    undone by the next tick of a daemon that had not exited yet.
    """
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "")

    assert install_statusline.shim_content_check([(str(p), "blink-hook.sh")]) is None
    assert p.read_text(encoding="utf-8") == "#!/bin/sh\n# old\n"


def test_the_disable_switch_is_honoured(tmp_path, monkeypatch):
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")
    monkeypatch.setenv(install_statusline.WATCHDOG_DISABLE_ENV, "1")

    assert install_statusline.shim_content_check([(str(p), "blink-hook.sh")]) is None


def test_an_unwritable_shim_reports_and_does_not_raise(tmp_path, monkeypatch):
    """The daemon survives a repair it cannot perform."""
    d = tmp_path / "ro"
    d.mkdir()
    p = d / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    os.chmod(d, 0o500)
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")
    try:
        what = install_statusline.shim_content_check([(str(p), "blink-hook.sh")])
        assert what is not None
        assert "could not" in what
    finally:
        os.chmod(d, 0o700)


def test_a_second_stale_shim_is_also_repaired(tmp_path, monkeypatch):
    """Both shims, one pass. The statusline shim has the same failure mode."""
    a = tmp_path / "blink-hook.sh"
    b = tmp_path / "blink-statusline.sh"
    _write(str(a), "#!/bin/sh\n# old\n")
    _write(str(b), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")

    what = install_statusline.shim_content_check(
        [(str(a), "blink-hook.sh"), (str(b), "blink-statusline.sh")])

    assert what is not None
    assert a.read_text(encoding="utf-8") == cli._shim_source("blink-hook.sh")
    assert b.read_text(encoding="utf-8") == cli._shim_source("blink-statusline.sh")
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/pc/test_shim_repair.py -q`
Expected: FAIL — `module 'pc.install_statusline' has no attribute 'shim_content_check'`.

- [ ] **Step 3: Implement it**

In `pc/install_statusline.py`, after `drift_check` ends (:538, the `return what`):

```python
def shim_content_check(shims):
    """Rewrite any installed shim whose CONTENTS have fallen behind.

    A different fault from drift_check above, and the one that shipped.
    drift_check asks whether settings.json still points at us; this asks
    whether the file it points at is still the file we would write. `blink
    update` swaps the program directory and restarts the service -- it has
    never rewritten a shim -- so every install that arrived by the documented
    upgrade path ran a new daemon against whatever shim its original install
    left behind. The symptom is not an error: the hook runs, the path exists,
    `blink status` reports ten of ten events, and the only thing missing is
    the field the new daemon came to read.

    `shims` is a sequence of (path, name) pairs. Returns a description of what
    it rewrote, or None when there was nothing to do -- which is the normal
    case on every tick after the first.
    """
    if os.environ.get(WATCHDOG_DISABLE_ENV):
        return None

    # Same rule as drift_check: a missing marker means the user uninstalled,
    # and that is never overridden. Without it a daemon still winding down
    # would put back the shims `blink uninstall` had just removed.
    if not _read_marker():
        return None

    from . import cli

    repaired = []
    for path, name in shims:
        if cli.shim_is_current(path, name):
            continue
        try:
            cli._write_shim(path, name)
        except Exception as e:
            repaired.append(f"{name} is out of date and could not be replaced: {e}")
            continue
        repaired.append(f"{name} was out of date; replaced it")

    return "; ".join(repaired) if repaired else None
```

- [ ] **Step 4: Run to verify it passes**

Run: `python -m pytest tests/pc/test_shim_repair.py -q`
Expected: 11 passed.

- [ ] **Step 5: Run the whole suite**

Run: `python -m pytest tests -q`
Expected: 515 + 11 pass, nothing else changed.

- [ ] **Step 6: Commit**

```bash
git add pc/install_statusline.py tests/pc/test_shim_repair.py
git commit -m "feat: the watchdog replaces a shim that fell behind the daemon"
```

---

### Task 3: Wire it into the daemon, and say so in `blink status`

**Files:**
- Modify: `claude_usage_bridge.py` (REPO ROOT — not `pc/claude_usage_bridge.py`, which does not exist), `pc/install_statusline.py`, `pc/cli.py`
- Test: `tests/pc/test_shim_repair.py`

**Interfaces:**
- Consumes: `shim_content_check()` (Task 2), `shim_is_current()` (Task 1).
- Produces: nothing later depends on.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_shim_repair.py`:

```python
import ast


def test_the_daemon_hands_the_watchdog_both_shims():
    """A guard against the exact regression this plan exists to fix.

    The feature was dead because one call site was missing, and every test
    passed anyway. Read the daemon's source and assert the wiring, because
    the alternative is a test that constructs its own watchdog and proves
    only that the constructor works -- which is what let the original gap
    through.
    """
    src = open("claude_usage_bridge.py", encoding="utf-8").read()
    tree = ast.parse(src)

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "DriftWatchdog"]
    assert len(calls) == 1, "expected exactly one watchdog"

    kwargs = {k.arg for k in calls[0].keywords}
    assert "shims" in kwargs, (
        "the daemon builds a DriftWatchdog without shims=, so the hook shim "
        "is never checked and the naming feature is dead on every install "
        "that arrived by `blink update`")


def test_status_reports_a_stale_shim(tmp_path, monkeypatch):
    """`blink status` said "hooks installed (10/10 events)" throughout.

    It compares the command strings in settings.json against the shim path.
    The path existed and still ran; only its contents were stale, so the one
    place a user would look reported health.
    """
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(cli, "hook_shim_path", lambda: str(p))

    assert cli.hook_shim_status_note() == "the activity hook shim is out of date"


def test_status_is_quiet_when_the_shim_is_current(tmp_path, monkeypatch):
    p = tmp_path / "blink-hook.sh"
    _write(str(p), cli._shim_source("blink-hook.sh"))
    monkeypatch.setattr(cli, "hook_shim_path", lambda: str(p))

    assert cli.hook_shim_status_note() is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `python -m pytest tests/pc/test_shim_repair.py -q`
Expected: FAIL on all three — no `shims=` keyword, no `hook_shim_status_note`.

- [ ] **Step 3: Let the watchdog carry the shims**

In `pc/install_statusline.py`, `DriftWatchdog.__init__` (:550) takes a new keyword-only argument, defaulted so existing callers and tests keep working:

```python
    def __init__(self, settings_path, shim_path, interval_s=300.0,
                 now=None, check=drift_check, *, shims=()):
```

Store it as `self._shims = tuple(shims)`, and in `tick()`, after the existing `drift_check` result is computed, run the content check and join the two descriptions. Read `tick()` before editing it and preserve its reinstatement cap exactly — the cap exists so a daemon fighting another program does not rewrite settings.json forever, and the content check must not reset or bypass it.

Combine the two results so a tick that repairs both reports both. **I am deliberately not giving you the diff for this**: `tick()` (`:562-582`) threads its result through a reinstatement counter and an early `return None` on the not-yet-due branch, and a snippet written without that in front of me would name variables that do not exist — which is exactly how the last plan on this branch shipped an instruction that did not apply.

What it must do, in the file's own terms:
- The not-yet-due early return at `:567` still short-circuits BOTH checks. The content check is not free and must not run every loop iteration.
- `MAX_REINSTATEMENTS` counts settings.json reinstatements only. A shim rewrite must not increment it, must not reset it, and must not be suppressed by it — the cap exists so the daemon stops fighting another program over settings.json, and a stale file is not that fight.
- When both produce a description, return both, joined with `"; "`. When neither does, return `None`, which is the normal case on every tick after the first.

Write it, then read `tick()` back and check each of those three against what you wrote.

- [ ] **Step 4: Hand the daemon's watchdog both shims**

In `claude_usage_bridge.py` at :447, extend the construction. Keep the existing comment above it and add to it:

```python
    watchdog = install_statusline.DriftWatchdog(
        settings_path(), shim_path(),
        # The hook shim fails the other way round: its entry in settings.json
        # stays perfect while its contents fall behind, because `blink update`
        # swaps the program directory and has never rewritten a shim. Both
        # shims are listed rather than just the hook, so the statusline shim
        # gets the same protection it turned out never to have had either.
        shims=((shim_path(), "blink-statusline.sh"),
               (hook_shim_path(), "blink-hook.sh")))
```

`hook_shim_path` must be imported alongside `shim_path` and `settings_path`; check the existing import line at the top of the file and extend it rather than adding a second one.

- [ ] **Step 5: Make `blink status` tell the truth**

In `pc/cli.py`, beside the other status helpers:

```python
def hook_shim_status_note():
    """A one-line warning when the installed hook shim is out of date.

    `blink status` reports the activity hooks by comparing settings.json's
    command strings against the shim path. That check passed throughout the
    period when the feature was dead: the path existed, the hook ran, and the
    file it ran was simply older than the daemon reading its output. The
    daemon repairs this within five minutes of starting -- this line is for
    the person looking at the gap before it closes, or at a machine where the
    watchdog is disabled.
    """
    if shim_is_current(hook_shim_path(), "blink-hook.sh"):
        return None
    return "the activity hook shim is out of date"
```

Then call it from `cmd_status` where the `Activity` row is printed, and print it as an indented continuation line under that row when it returns a string. Read the surrounding rows first and match their indentation and sentence case exactly.

- [ ] **Step 6: Run the tests**

Run: `python -m pytest tests -q`
Expected: all pass.

- [ ] **Step 7: Prove the end-to-end repair by hand**

```bash
cp ~/.blink/blink-hook.sh /tmp/blink-hook.backup
printf '#!/bin/sh\n# deliberately stale\n' > ~/.blink/blink-hook.sh
~/.blink/bin/blink status | grep -A1 Activity     # must now warn
```

Then restart the service and wait for one tick:

```bash
launchctl kickstart -k gui/502/com.blink.bridge
```

Expected: within 300 s, `~/.blink/blink-hook.sh` is byte-identical to `tools/blink-hook.sh` again, and `bridge.log` carries a `[watchdog]` line naming it. If anything goes wrong, `cp /tmp/blink-hook.backup ~/.blink/blink-hook.sh` restores it.

**Do not skip this step.** Every automated test here uses a temp directory; this is the only check that the paths the daemon actually passes are the paths a real install uses.

- [ ] **Step 8: Commit**

```bash
git add claude_usage_bridge.py pc/install_statusline.py pc/cli.py tests/pc/test_shim_repair.py
git commit -m "fix: the hook shim was never rewritten by an update, so naming shipped dead"
```

---

### Task 4: Delete a test that cannot fail (`make_fetch`)

**Files:**
- Modify: `tests/pc/test_ingest.py`
- Test: the file itself

`ingest.make_fetch()` has no production caller. The daemon builds `IngestionBus()` and calls `bus.fetch()` inline (`claude_usage_bridge.py:496-502`); `cli._wire_line` builds its own. `test_make_fetch_carries_the_accessor` justifies itself as keeping `make_fetch` and the daemon in step — it cannot, because nothing calls it. This is the same shape as the `fetch = bus.poll` defect it was written in response to.

- [ ] **Step 1: Confirm there is still no caller — and mind which `make_fetch` you are looking at**

There are TWO functions with this name. `pc/statusline_source.py:183` has its own `make_fetch` and is **not** the subject of this task; deleting it would be a real regression. The one with no caller is `pc/ingest.py:205`.

Run: `grep -rn "make_fetch" pc/ claude_usage_bridge.py`
Expected: the definition at `pc/statusline_source.py:183`, the definition at `pc/ingest.py:205`, and a prose mention at `pc/ingest.py:135` — no call site for the `ingest` one.
**If a call site for `ingest.make_fetch` has appeared, stop.** The test is real, and this task should be abandoned rather than forced.

- [ ] **Step 2: Decide and act**

Delete `make_fetch` from `pc/ingest.py` and both tests that reference it (`tests/pc/test_ingest.py:110` and `:297`). Deleting the function is the honest fix: a helper with no callers and a test that guards nothing is worse than absent, because it reads as covered.

If `make_fetch` is part of a documented public surface (check `docs/` and any `__all__`), keep the function, delete only the test that claims to guard the daemon, and leave a comment on the function saying it has no in-tree caller.

- [ ] **Step 3: Run the suite**

Run: `python -m pytest tests -q`
Expected: passes with two fewer tests.

- [ ] **Step 4: Commit**

```bash
git add pc/ingest.py tests/pc/test_ingest.py
git commit -m "test: a guard with no caller guarded nothing"
```

---

### Task 5: Repair the atomic-write assertion

**Files:**
- Modify: `tests/ci/check_hook_shim.sh`

Check 4, "atomic write leaves no temp file", asserts `[ ! -e "$DIR/abc-123.state.tmp" ]`. The shim writes `$DIR/$sid.state.$$.tmp` — `abc-123.state.<pid>.tmp`. The name tested for cannot occur, so the assertion has never been capable of failing.

- [ ] **Step 1: Prove the current assertion is vacuous**

Run: `grep -n 'state.tmp\|state\.\$\$\.tmp' tests/ci/check_hook_shim.sh tools/blink-hook.sh`
Expected: the test's literal and the shim's `$$` form differ, exactly as described.

- [ ] **Step 2: Write the assertion that can fail**

Replace the literal with a glob that matches what the shim really writes:

```sh
# The shim writes "$sid.state.$$.tmp", so a test for "$sid.state.tmp" could
# never match and never fail. Match the pid form instead. A leftover temp is
# not cosmetic: nothing sweeps these -- ClaudeStateProvider.scan() ignores
# anything not ending in .state, and SessionEnd removes only $sid.state and
# $sid/ -- so one per killed hook accumulates for the life of the install.
if ls "$DIR"/abc-123.state.*.tmp >/dev/null 2>&1; then
	fail "atomic write left a temp file behind"
fi
```

- [ ] **Step 3: Prove it can fail**

Create `$DIR/abc-123.state.99.tmp` by hand before the assertion, run the script, and confirm it FAILS. Then remove the line and confirm it passes. A test you have not seen fail is not yet a test.

- [ ] **Step 4: Run it on every shell CI uses**

Run: `sh tests/ci/check_hook_shim.sh sh`
Run: `sh tests/ci/check_hook_shim.sh dash` (skip with a note in your report if `dash` is not installed)
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/ci/check_hook_shim.sh
git commit -m "test: the atomic-write check looked for a filename the shim cannot write"
```

---

### Task 6: Assert the clock's position

**Files:**
- Modify: `tests/usage_layout/host_test.c`

The clock moved to `LV_ALIGN_TOP_MID` at `STATUS_Y`, sharing that row with the hint line. The layout test lost it entirely — no box, no assertion — leaving the clock the one header element with nothing checking it, in a file whose own comment says "a rule with no assertion is exactly how the Y above went unchecked until a tally landed on the hint line". Two comments in that file still describe the clock's old corner.

- [ ] **Step 1: Write the failing assertions**

Add a box for the clock beside the existing `brand` box (~:113), using the file's real `top_mid` helper. Its width is the worst-case `HH:MM`: four of the widest digit (9 px each at montserrat_14, where LVGL rounds `(adv_w + 8) >> 4`) plus a 3 px colon.

```c
	/* The clock shares STATUS_Y with the hint -- one of them is visible at
	 * a time, so they may overlap each other and must not overlap anything
	 * else. Width is the worst case the format can produce, not "12:04":
	 * 0, 4, 6, 8 and 9 all round to 9 px, so four digits and a 3 px colon. */
	struct box clock = top_mid("clock", 0, STATUS_Y, 4 * 9 + 3, FONT_LINE_H);
```

```c
	EXPECT_EQ(clock.y0, STATUS_Y);
	CHECK(clock.y1 <= GAUGE_ARC_Y,
	      "the clock's line box clears the arcs below it");
	CHECK(clock.y0 >= brand.y1,
	      "the clock sits under the brand, not on it");
	CHECK(!overlaps(clock, pips),
	      "the clock no longer collides with the pip row it vacated");
```

If a `pips` box does not already exist in the file, build one first from `PIP_X0`, `PIP_Y`, `PIP_MAX`, `PIP_PITCH` and `PIP_SZ`, and use it for the existing X assertions too.

- [ ] **Step 2: Run and read the result carefully**

Run: `sh tests/ci/check_host_tests.sh`
These may pass on the first run, because the geometry is already correct — the defect is the *absence* of the check, not a broken layout. That is fine, but you must then prove each one can fail: temporarily change `STATUS_Y` and confirm the suite goes red. Report which assertions you saw fail.

- [ ] **Step 3: Fix the two stale comments**

`tests/usage_layout/host_test.c:170` still reads "The clock is back in the corner it briefly vacated", contradicting the corrected text below it, and the block above the pip assertions still promises the clock's width as one of two asserted edges. Rewrite both to describe the shared row.

- [ ] **Step 4: Run the full host suite**

Run: `sh tests/ci/check_host_tests.sh`
Expected: 14 suites pass.

- [ ] **Step 5: Commit**

```bash
git add tests/usage_layout/host_test.c
git commit -m "test: the clock moved rows and nothing was checking where it landed"
```

---

## Self-Review

**Coverage.** The defect is that no code path rewrites `~/.blink/blink-hook.sh` after install. Task 1 detects staleness, Task 2 repairs it under the same gates the existing watchdog uses, Task 3 wires it to the one daemon that runs and makes `blink status` stop reporting health during the gap. Tasks 4–6 remove three assertions that cannot fail, which is what makes "the suite is green" mean something on this branch.

**Type consistency.** `shim_is_current(path, name) -> bool` is defined in Task 1 and called in Tasks 2 and 3. `shim_content_check(shims) -> str | None` is defined in Task 2 and called in Task 3, with `shims` a sequence of `(path, name)` pairs in both. `DriftWatchdog(..., *, shims=())` keeps every existing caller working.

**Known soft spots, stated rather than hidden.** Task 3 Step 3 edits `DriftWatchdog.tick`, whose reinstatement cap I have described but not reproduced — the implementer must read it, because a content check that resets that cap would let a daemon fight another program forever. Task 6's assertions may all pass on first run; the step says so and requires proving each can fail, since a passing new assertion is exactly the failure mode the task exists to remove. Task 4 may find a caller for `make_fetch` and is told to stop rather than force the deletion.

**Deliberately not here.** The non-ASCII label bug (`protocol.encode` leaves `ensure_ascii` at its default, so a non-ASCII label reaches the panel as truncated `\uXXXX` text) is real but cannot currently be triggered: `blink-hook.sh` refuses non-ASCII names outright. It becomes reachable the moment a Codex `cwd` becomes a label, so it belongs to the Codex naming plan, which owns that boundary.
