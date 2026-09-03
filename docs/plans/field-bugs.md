# Three field bugs — implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the panel answer "when did I last use Claude Code", let it doze when the reading has stopped moving, and stop it cold-rebooting when no daemon ever appears.

**Architecture:** Three faults, three layers. **Bug B** is daemon-only: `ClaudeCliProvider` gains one piece of memory — the last statusline payload whose five-hour window was readable — and re-maps it through the existing `statusline_source` code when the live payload has no session percentage. Nothing in the merge rule changes; the remembered reading competes by recency like every other frame and therefore wins only when it is the freshest session reading anybody has. **Bug A** is firmware: a new host-testable module remembers the age and activity the daemon last reported, `sleep_gate` gains a second rule keyed on that age, and `ui_sleep_run` gains a caller-supplied wake predicate so a board can doze through a chatty daemon. **Bug C** is firmware: the `sys_reboot` on "no daemon in 60 s" becomes the same sleep, reusing the predicate machinery Bug A introduces.

**Tech Stack:** Python 3.10+ (`pc/`, pytest), C99 Zephyr firmware (`firmware/src/`, plain-`cc` host tests under `tests/`), LVGL for the panel.

**Spec:** `/private/tmp/claude-502/-Users-KfirLevy-Projects-LiveClaudeUi/aeb3001d-3255-41ed-b053-ecf8e0cdec4c/scratchpad/field-bugs.md` (root-cause investigation, with the log evidence and the option analysis; the decisions taken from it are restated below).

## Global Constraints

- **No wire change without proving the budget.** The `usage` message measures ~510 of `protocol.MAX_LINE_BYTES = 512`, and `proto.c` drops an over-long line WHOLE — a silent panel freeze with no error. No task in this plan adds a wire field; if one is proposed, it must measure the worst case first.
- Firmware is C99, kernel style: tabs, `/* */` comments, braces on every `if` body. Every Y in `usage_layout.h` sits on a 4 px rhythm.
- **`usage_view.c` cannot be compiled on a laptop** (needs LVGL, no automated coverage). `tools/panel_render/render.sh` compiles it unchanged against real LVGL and writes a framebuffer; scenes live in `tools/panel_render/render_main.c`. Pure logic belongs in `fmt.c`, `usage_state.c`, `sleep_gate.c` or a new sibling — those are host-tested.
- **Firmware is not done until flashed and boot-verified.** The board is at `/dev/cu.usbserial-14240`; the port re-enumerates on reset. The production daemon holds it under launchd as `com.blink.bridge`. Freeing it: `launchctl bootout gui/502/com.blink.bridge`. Restoring it needs **both** `launchctl bootstrap` AND `launchctl kickstart` — bootstrap registers without starting.
- Python 3.10+, matching `pc/` style. `pytest tests -q` passes 530 today; `sh tests/ci/check_host_tests.sh` runs 14 suites. Both green at every commit.
- Comments explain WHY, in prose, at the density of the surrounding file.
- **A test that cannot fail is worse than no test.** This branch has found five. Every task below carries an explicit "prove it bites" step: build the broken variant, watch the test reject it, put the code back.
- UI copy is sentence case: every on-screen sentence starts with a capital letter.
- Do not touch anything under `~/.blink` except by the launchctl commands above. The daemon and board are live on this machine.

---

## The three decisions this plan implements

**Bug B — "I used Claude Code 6 hours ago but it says 56 hours."**
Decision: **remember the last CLI reading.** `pc/normalizer.py::merge` is strict field-by-field recency and desktop is not preferred; desktop won only because the CLI reading *ceased to exist* as a candidate (`session_pct == -1` once the five-hour window expired and Claude Code stopped rendering), leaving `age_s` describing the desktop sample. The daemon re-reads statelessly (`ClaudeCliProvider.poll`) and remembers nothing. Give it memory.

**Bug A — "the computer slept but BLINK stayed awake all night showing 'Reading is old'."**
Decision: **firmware sleeps on a stale reading too.** `sleep_gate.c` requires `host_lost && had_usage && !ota_busy`, and `proto.c:262-265` clears `host_lost` on every protocol line, pings included. The daemon pinged all night. Extend the gate with an age-based rule; keep the daemon dumb so boards fix themselves against any daemon version.

**Bug C — "no daemon running: it sits on 1/2, then resets after a few minutes."**
Decision: **make never-connected match connected-then-gone.** `main.c:1498-1503` is a deliberate `sys_reboot(SYS_REBOOT_COLD)` into standalone. Replace it with sleep, preserving the `!proto_host_seen()` guard.

### Why the module docstring's "understating vs inventing" reasoning survives Bug B

`pc/normalizer.py`'s docstring rejects "prefer the HIGHER percentage" because across a window reset that rule invents usage that has already been forgiven. The fix here does not touch preference at all. The remembered frame carries its original `observed_at` (the payload's mtime) and enters `_pick` as an ordinary candidate, so it wins the session dial **only when it is the freshest session reading in the set**. Concretely: it beat a 57-hour desktop sample in the field because it was 51 hours fresher, and it will lose to a five-minute-old desktop sample tomorrow. We never show an older number than the code shows today.

The residual invention risk — a remembered 27% outliving its own five-hour window — is already handled twice by code we deliberately re-enter rather than duplicate:

1. Inside `STALE_AFTER_S` (1800 s) of the mtime, `statusline_source._rolled_over` turns a window whose `resets_at` has passed into a hard `0.0` and records `session_rolled_at`, which then evicts any *other* source's pre-reset reading through `_survives_rollover`.
2. Past 1800 s, `map_statusline_frame` sets `stale=True` and deliberately skips `_rolled_over` ("on an old one the same reasoning inverts... 0% would be the lie"). The percentage travels with `stale=True`, `secs_until` refuses the past reset time so no countdown is drawn, and the panel says `"Reading is old - showing last known"`.

That is why the fix caches the **raw payload and its mtime** rather than the mapped frame: re-mapping at the current `now` runs all of that reasoning at the right time, for free. Caching a `NormalizedUsageFrame` instead would freeze `stale=False` at capture and put a green dot over a six-hour-old number — the exact confident-wrong-number failure the docstring exists to prevent.

### Sequencing: how A and C relate

They **share one task** (Task 6, the `ui_sleep_run` signature) and are otherwise independent. Both need a board to doze while something is still notionally present, and today `ui_sleep_run` hard-codes its wake condition as `proto_host_seen()` — which is permanently true in Bug A's scenario (the daemon never stopped pinging), so a stale-sleep built on today's function would close and open its eyes in a loop. Task 6 makes the wake condition a parameter; A passes an age-based predicate, C passes the host-based one that is today's default. A is done first because it defines the predicate vocabulary C then reuses in its simplest form. C also *depends on* a fix that falls out of Task 6: `ui_sleep_run` currently ends with an unconditional `usage_view_set_status(USAGE_STATUS_STALE)`, which would stamp "reading is old" over a board that has no reading at all (C) or over the fresh frame that just woke it (A).

---

## File Structure

| File | Status | Responsibility |
|---|---|---|
| `pc/providers/claude_cli.py` | Modify | Gains one field: the last payload whose five-hour window was readable, re-mapped when the live payload has none. |
| `tests/pc/test_claude_cli.py` | Create | The provider's memory: what is remembered, when it is emitted, when it is replaced, that its age grows. |
| `tests/pc/test_normalizer.py` | Modify | Pins the merge invariant the fix leans on: a stale CLI frame with a known percentage beats an older desktop frame. |
| `tests/pc/test_ingest.py` | Modify | End-to-end at the seam: bus + real `ClaudeCliProvider` + a desktop frame produces `src: "cli"` with a growing `age_s`. |
| `firmware/src/usage_freshness.h/.c` | Create | Remembers the age and activity of the last `usage` message and grows the age with uptime. Pure C, no Zephyr, host-testable. |
| `tests/usage_freshness/host_test.c` | Create | Host test for the above. |
| `firmware/src/sleep_gate.h/.c` | Modify | Gains the stale-reading rule and its exact complement, the wake rule. Still pure. |
| `tests/sleep_gate/host_test.c` | Modify | Grid test that start and wake are complements, and that the threshold is where it says it is. |
| `firmware/src/ui_sleep.h/.c` | Modify | Wake condition and peek note become parameters; the wake-time STALE stamp becomes conditional. |
| `firmware/src/proto.c` | Modify | Two lines: hand the parsed `age_s` and activity to `usage_freshness`. |
| `firmware/src/main.c` | Modify | `run_usb` gains the stale-sleep branch (A) and trades its `sys_reboot` for a sleep (C). |
| `firmware/CMakeLists.txt` | Modify | Adds `src/usage_freshness.c`. |
| `tests/ci/check_host_tests.sh` | Modify | Adds the `usage_freshness` row; `sleep_gate` gains a source. |
| `docs/sleep-mode-design.md` | Modify | Documents the second way to enter sleep and the threshold. |

No task adds a field to the `usage` wire message. `age_s` and `state` are already sent and already parsed by `proto.c` (lines 336-341 and 418-422); the firmware simply stops throwing them away.

---

### Task 1: Pin the merge invariant the Bug B fix leans on

The fix works by handing the merge a CLI frame that is stale but carries a percentage, and trusting recency to prefer it over an older desktop frame. That behaviour is real today (the investigation's CASE1) but nothing pins it, so a future tidy-up of `_pick` could silently restore the bug. This is a characterization test written before the code that depends on it.

**Files:**
- Modify: `tests/pc/test_normalizer.py` (append; helpers `cli()` and `desktop()` already exist at the top of the file)

**Interfaces:**
- Consumes: `normalizer.merge(frames)`, `tests/pc/test_normalizer.py::cli`, `::desktop` (already defined there).
- Produces: nothing other tasks import. It guards `merge`'s recency rule for Tasks 2 and 3.

- [ ] **Step 1: Write the test**

Append to `tests/pc/test_normalizer.py`:

```python
def test_a_stale_reading_that_has_a_number_beats_a_fresher_one_that_does_not():
    """The invariant pc/providers/claude_cli's memory is built on.

    A CLI reading six hours old still carries a five-hour percentage; a
    desktop sample two days older carries one too. Staleness does not remove
    a frame from the contest -- recency ranks it -- so the CLI figure wins
    and `src` follows it. If this ever inverts, the panel goes back to
    reporting the age of a desktop sample nobody asked about (field report,
    2026-09-02).
    """
    m = normalizer.merge([
        cli(NOW - 6 * 3600, session=27.0, stale=True),
        desktop(NOW - 57 * 3600, session=0.0, stale=True),
    ])
    assert m.session_pct == 27.0
    assert m.src == "cli"
    assert m.observed_at == NOW - 6 * 3600
    assert m.stale is True
```

- [ ] **Step 2: Run it**

Run: `pytest tests/pc/test_normalizer.py::test_a_stale_reading_that_has_a_number_beats_a_fresher_one_that_does_not -v`
Expected: PASS. This one characterizes existing behaviour, so passing on the first run is correct — Step 3 is what proves it is not vacuous.

- [ ] **Step 3: Prove the test bites**

Temporarily add `and not f.stale` to the `has` lambda for `session_pct` in `pc/normalizer.py::merge`:

```python
    session_pct, session_src = _pick(
        frames,
        lambda f: (_known_pct(f.session_pct) and not f.stale
                   and _survives_rollover(f, s_rolled, "session_rolled_at")),
        lambda f: f.session_pct)
```

Run the same command. Expected: FAIL — `m.src == "desktop"`, which is exactly the field bug. Revert the edit with `git checkout -- pc/normalizer.py` and re-run to confirm PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/pc/test_normalizer.py
git commit -m "test: pin that a stale reading with a number beats a fresher blank one"
```

---

### Task 2: The CLI provider remembers its last usable reading

**Files:**
- Modify: `pc/providers/claude_cli.py`
- Create: `tests/pc/test_claude_cli.py`

**Interfaces:**
- Consumes: `pc.statusline_source.read_payload(path) -> (payload|None, mtime|None)`, `pc.statusline_source.map_statusline_frame(payload, now_epoch, mtime_epoch) -> NormalizedUsageFrame`, `pc.statusline_source.STALE_AFTER_S = 1800`, `pc.providers.base.UNKNOWN = -1.0`.
- Produces: `ClaudeCliProvider.poll(now_epoch) -> list[NormalizedUsageFrame]`, unchanged signature, now returning **up to two** frames — the live reading (when the file parses) and, when the live reading has no five-hour percentage, the remembered one. Both carry `src == "cli"`; they differ in `observed_at`. Task 3 consumes this.

- [ ] **Step 1: Write the failing tests**

Create `tests/pc/test_claude_cli.py`:

```python
"""The one thing this provider remembers, and why it may.

The status line is rewritten only when Claude Code renders. When the
five-hour window expires and nobody is rendering, the payload stops carrying
a percentage, the frame stops being a candidate for the session dial, and the
panel falls to whatever else is on the bus -- in the field, a Claude Desktop
sample 57 hours old, whose age was then drawn as "56h ago" over a machine
that had used Claude Code six hours earlier.
"""
import json

from pc import statusline_source as ss
from pc.providers.claude_cli import ClaudeCliProvider

NOW = 1_787_700_000.0


def write(path, payload, mtime):
    path.write_text(json.dumps(payload), encoding="utf-8")
    import os
    os.utime(path, (mtime, mtime))
    return path


def payload(five_hour=27.0, resets_at=None, seven_day=12.0):
    limits = {"seven_day": {"used_percentage": seven_day}}
    if five_hour is not None:
        limits["five_hour"] = {"used_percentage": five_hour,
                               "resets_at": resets_at}
    return {"rate_limits": limits}


def test_a_live_reading_is_returned_alone(tmp_path):
    p = write(tmp_path / "statusline.json",
              payload(five_hour=27.0, resets_at=NOW + 900), NOW - 60)
    prov = ClaudeCliProvider(path=str(p))

    frames = prov.poll(NOW)

    assert len(frames) == 1
    assert frames[0].session_pct == 27.0


def test_the_last_reading_with_a_percentage_is_offered_when_the_file_loses_one(tmp_path):
    """The field case: the window expired and the file was rewritten without
    it, so the only session figure left in the world is the one we read an
    hour ago."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW - 7200), NOW - 6 * 3600)
    prov.poll(NOW - 6 * 3600 + 1)

    write(p, {"rate_limits": {"seven_day": {"used_percentage": 12.0}}}, NOW - 60)
    frames = prov.poll(NOW)

    remembered = [f for f in frames if f.session_pct >= 0]
    assert len(remembered) == 1
    assert remembered[0].session_pct == 27.0
    assert remembered[0].observed_at == NOW - 6 * 3600
    assert remembered[0].src == "cli"


def test_the_remembered_reading_is_marked_stale_by_its_own_age(tmp_path):
    """Not frozen at the staleness it had when captured. A six-hour-old
    number under a green dot is the confident-wrong-number failure
    pc/normalizer's docstring exists to prevent."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW - 7200), NOW - 6 * 3600)
    prov.poll(NOW - 6 * 3600 + 1)
    assert prov.poll(NOW - 6 * 3600 + 1)[0].stale is False

    write(p, {"rate_limits": {}}, NOW - 60)
    remembered = [f for f in prov.poll(NOW) if f.session_pct >= 0][0]
    assert remembered.stale is True


def test_the_remembered_reading_ages(tmp_path):
    """The whole point: the age must answer "when did you last use Claude
    Code", so it grows with the wall clock instead of resetting each poll."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW - 7200), NOW - 3600)
    prov.poll(NOW - 3600 + 1)
    write(p, {"rate_limits": {}}, NOW - 60)

    a = [f for f in prov.poll(NOW) if f.session_pct >= 0][0]
    b = [f for f in prov.poll(NOW + 60) if f.session_pct >= 0][0]
    assert a.observed_at == b.observed_at == NOW - 3600


def test_a_newer_reading_with_a_percentage_replaces_the_remembered_one(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW + 900), NOW - 3600)
    prov.poll(NOW - 3599)
    write(p, payload(five_hour=61.0, resets_at=NOW + 900), NOW - 60)
    prov.poll(NOW)

    write(p, {"rate_limits": {}}, NOW - 30)
    remembered = [f for f in prov.poll(NOW) if f.session_pct >= 0][0]
    assert remembered.session_pct == 61.0


def test_a_vanished_file_still_leaves_the_memory(tmp_path):
    """read_payload returns nothing for an absent or malformed file, and that
    is not evidence the last reading never happened."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW + 900), NOW - 3600)
    prov.poll(NOW - 3599)
    p.unlink()

    frames = prov.poll(NOW)
    assert len(frames) == 1
    assert frames[0].session_pct == 27.0


def test_nothing_is_invented_before_the_first_good_reading(tmp_path):
    p = write(tmp_path / "statusline.json", {"rate_limits": {}}, NOW - 60)
    prov = ClaudeCliProvider(path=str(p))

    frames = prov.poll(NOW)
    assert all(f.session_pct < 0 for f in frames)


def test_the_memory_does_not_survive_a_new_daemon(tmp_path):
    """Deliberately in-memory: a fresh process starts with no history. Pinned
    so the decision is visible rather than accidental."""
    p = tmp_path / "statusline.json"
    write(p, payload(five_hour=27.0, resets_at=NOW + 900), NOW - 3600)
    ClaudeCliProvider(path=str(p)).poll(NOW - 3599)

    write(p, {"rate_limits": {}}, NOW - 60)
    frames = ClaudeCliProvider(path=str(p)).poll(NOW)
    assert all(f.session_pct < 0 for f in frames)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `pytest tests/pc/test_claude_cli.py -v`
Expected: `test_a_live_reading_is_returned_alone`, `test_nothing_is_invented_before_the_first_good_reading` and `test_the_memory_does_not_survive_a_new_daemon` PASS (they describe today's stateless behaviour). The other five FAIL with `IndexError: list index out of range` or `assert 0 == 1` — there is no memory yet.

- [ ] **Step 3: Implement the memory**

In `pc/providers/claude_cli.py`, extend the module docstring, `__init__` and `poll`:

Append to the module docstring, after the existing final paragraph:

```
One thing IS remembered, and only one: the last payload whose five-hour
window we could actually read. The file is rewritten when Claude Code
renders, and when the window expires with nobody rendering, the rewrite
drops the percentage -- so the reading does not lose a comparison, it stops
existing, and the panel falls to whatever else is on the bus. In the field
that was a Claude Desktop sample 57 hours old, whose age was drawn over a
machine that had used Claude Code six hours earlier.

The RAW payload is kept, not the frame it maps to, and it is re-mapped at
the current clock every time it is offered. That is the whole reason this is
five lines instead of a policy: staleness, a window carried across its own
reset, and the refusal to zero an old one all live in map_statusline_frame
and all depend on `now`. A cached frame would freeze `stale=False` at
capture and put a green dot over an hours-old number, which is precisely
what pc/normalizer's docstring exists to prevent.

The memory dies with the process, deliberately. It answers "since this
daemon started, when did you last use Claude Code", and a daemon restart is
almost always an app update or a login -- after which the desktop cache is
as good an answer as we have. Persisting it would put a second copy of a
file that already exists on disk into ~/.blink, with its own invalidation
and corruption paths, for a case the field report does not contain.
```

Replace `__init__` and `poll`:

```python
    def __init__(self, path=None):
        self._path = path if path is not None else ss.PAYLOAD_PATH
        # (payload, mtime) for the last reading that had a five-hour
        # percentage, or None before the first one. See the module docstring.
        self._remembered = None

    def poll(self, now_epoch):
        payload, mtime = ss.read_payload(self._path)
        frames = []
        if payload is not None:
            live = self.parse_cli_event(payload, now_epoch, mtime)
            if live is not None:
                frames.append(live)
                # base.UNKNOWN is -1.0; anything >= 0 is a real percentage,
                # including the hard 0.0 map_statusline_frame computes for a
                # window it watched roll over.
                if live.session_pct >= 0:
                    self._remembered = (payload, mtime)
                    return frames

        # The live reading has no session figure, so offer the last one that
        # did -- ALONGSIDE the live frame rather than instead of it. They are
        # merged field by field, so the live payload keeps whatever it still
        # has (a seven-day percentage outlives the five-hour window every
        # time) and the remembered one is a candidate for the session dial
        # only. The normalizer then ranks it by recency like anything else,
        # so it wins the dial only when it is genuinely the freshest session
        # reading in the set.
        if self._remembered is not None:
            payload, mtime = self._remembered
            remembered = self.parse_cli_event(payload, now_epoch, mtime)
            if remembered is not None:
                frames.append(remembered)
        return frames
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/pc/test_claude_cli.py -v`
Expected: all eight PASS.

- [ ] **Step 5: Prove the tests bite**

Change `if live.session_pct >= 0:` to `if live.session_pct > 0:` and run again. Expected: FAIL — a remembered `0.0` from a rolled-over window is no longer captured (this is the boundary `base.UNKNOWN = -1.0` sits just below, and getting it wrong loses the one reading that *is* certain). Restore it.

Then replace the re-mapping with a cached frame — `self._remembered = live` in the capture branch and `frames.append(self._remembered)` below — and run again. Expected: `test_the_remembered_reading_is_marked_stale_by_its_own_age` FAILs with `assert False is True`, which is the green-dot-over-an-old-number regression. Restore.

- [ ] **Step 6: Run the whole Python suite**

Run: `pytest tests -q`
Expected: 530 passed + the 8 new = 538 passed, 0 failed.

- [ ] **Step 7: Commit**

```bash
git add pc/providers/claude_cli.py tests/pc/test_claude_cli.py
git commit -m "fix: the panel forgot the last Claude Code reading the moment its window expired"
```

---

### Task 3: Prove it at the seam — bus in, message out

Task 2 tests the provider in isolation. The bug was reported as a number on a panel, and the path from provider to that number runs through `IngestionBus.poll` → `normalizer.select_pair` → `protocol.frame_to_usage`, where `age_s` is finally computed. This task pins the whole path.

**Files:**
- Modify: `tests/pc/test_ingest.py` (append; `Fixed`, `frame()` and `NOW` already exist at the top)

**Interfaces:**
- Consumes: `ClaudeCliProvider.poll` from Task 2, `ingest.IngestionBus(providers=..., now=...)`, `ingest.IngestionBus.poll() -> dict`, the message keys `src`, `session_pct`, `age_s`.
- Produces: nothing other tasks import.

- [ ] **Step 1: Write the failing test**

Append to `tests/pc/test_ingest.py`:

```python
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

    write({"rate_limits": {"five_hour": {"used_percentage": 27.0,
                                         "resets_at": NOW - 7200}}},
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
```

- [ ] **Step 2: Run it**

Run: `pytest tests/pc/test_ingest.py::test_the_panel_reports_the_age_of_the_last_claude_code_reading -v`
Expected: PASS, on the strength of Task 2. If Task 2 is not yet applied it fails with `assert 'desktop' == 'cli'`.

- [ ] **Step 3: Prove the test bites**

`git stash` the `pc/providers/claude_cli.py` change from Task 2 (`git stash push pc/providers/claude_cli.py`), re-run. Expected: FAIL with `assert 'desktop' == 'cli'` and `age_s == 205200` — the field bug reproduced through the real seam. `git stash pop` and re-run to confirm PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/pc/test_ingest.py
git commit -m "test: pin the reported age against the last Claude Code reading, end to end"
```

---

### Task 4: `usage_freshness` — the board remembers how old its reading is

`proto.c` already parses `age_s` and computes the activity enum; both are handed to `usage_view` and then unreachable from `main.c`, where the sleep decision is made. This module keeps them somewhere a laptop can test.

**Files:**
- Create: `firmware/src/usage_freshness.h`
- Create: `firmware/src/usage_freshness.c`
- Create: `tests/usage_freshness/host_test.c`
- Modify: `firmware/src/proto.c` (after line 422, `usage_view_set_ages(...)`)
- Modify: `firmware/CMakeLists.txt` (after `src/usage_state.c`, line 32)
- Modify: `tests/ci/check_host_tests.sh` (the `run_one` table)

**Interfaces:**
- Consumes: `enum usage_activity` from `usage_view.h`.
- Produces:
  - `void usage_freshness_note(int32_t age_s, enum usage_activity act, int64_t now_ms);`
  - `int32_t usage_freshness_age_s(int64_t now_ms);` — the noted age plus elapsed uptime, or `-1` before the first note or when the daemon sent no age.
  - `enum usage_activity usage_freshness_activity(void);` — `USAGE_ACTIVITY_NONE` before the first note.
  Tasks 5 and 7 consume all three.

- [ ] **Step 1: Write the failing host test**

Create `tests/usage_freshness/host_test.c`:

```c
/* What the board knows about the age of the figures on its own screen.
 *
 *   cc -Wall -Werror -I firmware/src tests/usage_freshness/host_test.c \
 *      firmware/src/usage_freshness.c -o /tmp/usage_freshness
 */
#include <stdio.h>
#include "usage_freshness.h"

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

int main(void)
{
	/* Nothing has arrived yet: not "brand new", unknown. */
	CHECK(usage_freshness_age_s(1000) == -1);
	CHECK(usage_freshness_activity() == USAGE_ACTIVITY_NONE);

	/* A daemon older than this firmware sends no age at all. That must
	 * stay unknown rather than becoming a very fresh zero, which would
	 * hold the panel awake forever against every such daemon. */
	usage_freshness_note(-1, USAGE_ACTIVITY_NONE, 10000);
	CHECK(usage_freshness_age_s(70000) == -1);

	/* The ordinary case, and the one the sleep gate reads: the daemon
	 * says the reading is an hour old, and a minute later it is an hour
	 * and a minute old even though nothing new arrived. */
	usage_freshness_note(3600, USAGE_ACTIVITY_IDLE, 100000);
	CHECK(usage_freshness_age_s(100000) == 3600);
	CHECK(usage_freshness_age_s(160000) == 3660);
	CHECK(usage_freshness_activity() == USAGE_ACTIVITY_IDLE);

	/* A fresh reading resets the clock it grows from. */
	usage_freshness_note(1, USAGE_ACTIVITY_RUNNING, 200000);
	CHECK(usage_freshness_age_s(260000) == 61);
	CHECK(usage_freshness_activity() == USAGE_ACTIVITY_RUNNING);

	/* k_uptime_get is monotonic, but a caller that passes a stale
	 * timestamp must not produce an age that runs backwards past the
	 * figure the daemon actually gave us. */
	CHECK(usage_freshness_age_s(199000) == 1);

	printf("%s\n", fails ? "FAIL" : "ok   usage_freshness");
	return fails ? 1 : 0;
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cc -Wall -Werror -I firmware/src tests/usage_freshness/host_test.c firmware/src/usage_freshness.c -o /tmp/uf`
Expected: BUILD FAILS — `fatal error: 'usage_freshness.h' file not found`.

- [ ] **Step 3: Write the module**

Create `firmware/src/usage_freshness.h`:

```c
#ifndef USAGE_FRESHNESS_H
#define USAGE_FRESHNESS_H

#include <stdint.h>

#include "usage_view.h"

/*
 * How old the figures on the screen are, and what the tool feeding them was
 * doing -- the two facts the sleep gate needs and the only two the board
 * used to throw away.
 *
 * proto.c parses both out of every `usage` message (`age_s`, `state`) and
 * hands them to usage_view, which draws them and keeps them behind LVGL
 * where main.c cannot ask and no laptop can test. This holds the same two
 * values in a translation unit with no Zephyr and no LVGL in it, for the
 * same reason usage_state.c exists.
 *
 * The age GROWS between messages. The daemon recomputes it every 60 s, so
 * this mostly agrees with what is drawn -- but a board dozing on a stale
 * reading is deciding whether to keep dozing in the gaps, and an age frozen
 * at whatever last arrived would answer that question with the wrong number
 * for up to a minute at a time.
 */
void usage_freshness_note(int32_t age_s, enum usage_activity act,
			  int64_t now_ms);

/* Seconds since the shown reading was taken, or -1 when we cannot say --
 * before the first message, or from a daemon too old to send an age. Unknown
 * is not zero: a caller that treats it as a fresh reading holds the panel
 * awake against every such daemon. */
int32_t usage_freshness_age_s(int64_t now_ms);

/* What the last message said the tool was doing. USAGE_ACTIVITY_NONE before
 * any message, which is the same "said nothing" the enum already means. */
enum usage_activity usage_freshness_activity(void);

#endif /* USAGE_FRESHNESS_H */
```

Create `firmware/src/usage_freshness.c`:

```c
#include "usage_freshness.h"

static int32_t noted_age_s = -1;
static int64_t noted_at_ms;
static enum usage_activity noted_act = USAGE_ACTIVITY_NONE;

void usage_freshness_note(int32_t age_s, enum usage_activity act,
			  int64_t now_ms)
{
	noted_age_s = age_s;
	noted_at_ms = now_ms;
	noted_act = act;
}

int32_t usage_freshness_age_s(int64_t now_ms)
{
	int64_t grown;

	if (noted_age_s < 0) {
		return -1;
	}
	if (now_ms < noted_at_ms) {
		/* Cannot happen with k_uptime_get, which is monotonic. If it
		 * ever does, the daemon's own figure is still true of the
		 * moment it arrived, and an age that ran backwards would read
		 * as a reading getting fresher on its own. */
		return noted_age_s;
	}
	grown = (int64_t)noted_age_s + (now_ms - noted_at_ms) / 1000;
	if (grown > INT32_MAX) {
		grown = INT32_MAX;
	}
	return (int32_t)grown;
}

enum usage_activity usage_freshness_activity(void)
{
	return noted_act;
}
```

- [ ] **Step 4: Run the host test to verify it passes**

Run: `cc -Wall -Werror -I firmware/src tests/usage_freshness/host_test.c firmware/src/usage_freshness.c -o /tmp/uf && /tmp/uf`
Expected: `ok   usage_freshness`, no FAIL lines.

- [ ] **Step 5: Prove the test bites**

Change `if (noted_age_s < 0) { return -1; }` to `if (noted_age_s < 0) { return 0; }` and re-run. Expected: `FAIL ... usage_freshness_age_s(1000) == -1`. Restore.

Then delete `noted_at_ms = now_ms;` from `usage_freshness_note` and re-run. Expected: `FAIL ... usage_freshness_age_s(260000) == 61`. Restore.

- [ ] **Step 6: Wire it into `proto.c`**

In `firmware/src/proto.c`, add to the include block near line 15:

```c
#include "usage_freshness.h"
```

and immediately after `usage_view_set_ages((int32_t)age, (int32_t)p2age);` (line 422):

```c
		/* The same two facts, kept where main.c can ask and a laptop
		 * can test the reasoning about them. See usage_freshness.h. */
		usage_freshness_note((int32_t)age, act, k_uptime_get());
```

`act` is the `enum usage_activity` declared earlier in this same `usage` branch (line 336) and is still in scope.

- [ ] **Step 7: Register the new source and the new host test**

In `firmware/CMakeLists.txt`, after `src/usage_state.c`:

```
	src/usage_freshness.c
```

In `tests/ci/check_host_tests.sh`, after the `usage_state` row:

```sh
run_one usage_freshness "usage_freshness.c" ""
```

- [ ] **Step 8: Run the host suite**

Run: `sh tests/ci/check_host_tests.sh`
Expected: `PASS [host tests]` with 15 rows, `usage_freshness ok (9 checks)` among them.

- [ ] **Step 9: Commit**

```bash
git add firmware/src/usage_freshness.h firmware/src/usage_freshness.c \
        firmware/src/proto.c firmware/CMakeLists.txt \
        tests/usage_freshness/host_test.c tests/ci/check_host_tests.sh
git commit -m "feat: the board can now say how old the figures on its screen are"
```

---

### Task 5: The sleep gate learns the second way in

**Files:**
- Modify: `firmware/src/sleep_gate.h`
- Modify: `firmware/src/sleep_gate.c`
- Modify: `tests/sleep_gate/host_test.c`
- Modify: `tests/ci/check_host_tests.sh` (the `sleep_gate` row needs no new source, but the test now includes `usage_view.h` — confirm the row still builds)
- Modify: `docs/sleep-mode-design.md`

**Interfaces:**
- Consumes: `enum usage_activity` from `usage_view.h`.
- Produces:
  - `#define SLEEP_STALE_AFTER_S 14400`
  - `bool sleep_reading_is_old(int32_t age_s);`
  - `bool sleep_stale_should_start(int32_t age_s, bool had_usage, bool ota_busy, enum usage_activity act);`
  - `bool sleep_stale_should_wake(int32_t age_s, bool ota_busy, enum usage_activity act);`
  `sleep_should_start(bool, bool, bool)` is unchanged. Tasks 6 and 7 consume these.

**The threshold, and why 14400 s:** it has to sit above every gap a person at the desk can produce, and below a night. The daemon's own `STALE_AFTER_S` is 1800 s, which marks "this reading is old" — not "nobody is here", and a Claude Code user reading code between renders crosses it routinely. `AGE_CAPTION_MIN_S` is 600 s, chosen against Claude Desktop's 300 s / 900 s refresh schedules, so a desktop-only user at the machine sits under 900 s. Four hours is 8× the daemon's staleness bound and 16× the desktop's away-schedule; it outlasts any lunch, any meeting, and any single stretch of work without a render. It is also short enough to matter: a machine that sleeps at 23:00 has the panel dozing by 03:00 rather than at dawn. 8 h would have left the field board lit until morning, which is the complaint; 1800 s would doze on a person who is sitting right there.

- [ ] **Step 1: Write the failing test**

Replace `tests/sleep_gate/host_test.c` entirely:

```c
/* The dozing rules, pinned: docs/sleep-mode-design.md. */
#include <stdio.h>
#include "sleep_gate.h"

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

int main(void)
{
	/* --- the original rule: the app went silent --- */

	/* the case it exists for: app silent, figures shown, nothing flashing */
	CHECK(sleep_should_start(true, true, false));
	/* never met the app this boot: still "connecting" */
	CHECK(!sleep_should_start(true, false, false));
	/* the app is talking, or said bye: no sleep */
	CHECK(!sleep_should_start(false, true, false));
	/* esptool has the port: silence means an update, not a nap */
	CHECK(!sleep_should_start(true, true, true));

	/* --- the second rule: the app talks, the reading does not move --- */

	/* The field case (2026-09-02): the computer slept, the daemon kept
	 * pinging all night, and the panel sat awake on a reading that had
	 * stopped moving. Silence never came, so the rule above never fired. */
	CHECK(sleep_stale_should_start(SLEEP_STALE_AFTER_S, true, false,
				       USAGE_ACTIVITY_NONE));
	/* One second under the line is not old enough. */
	CHECK(!sleep_stale_should_start(SLEEP_STALE_AFTER_S - 1, true, false,
					USAGE_ACTIVITY_NONE));
	/* An unknown age is not a very old one. A daemon too old to send an
	 * age must not put the panel to sleep. */
	CHECK(!sleep_stale_should_start(-1, true, false, USAGE_ACTIVITY_NONE));
	/* Same three refusals the first rule has. */
	CHECK(!sleep_stale_should_start(SLEEP_STALE_AFTER_S, false, false,
					USAGE_ACTIVITY_NONE));
	CHECK(!sleep_stale_should_start(SLEEP_STALE_AFTER_S, true, true,
					USAGE_ACTIVITY_NONE));
	/* Something wants a person. A wedged session or an open prompt is a
	 * claim on them, and closing the eyes over it hides the one thing
	 * this panel exists to show. */
	CHECK(!sleep_stale_should_start(SLEEP_STALE_AFTER_S, true, false,
					USAGE_ACTIVITY_WAITING));
	CHECK(!sleep_stale_should_start(SLEEP_STALE_AFTER_S, true, false,
					USAGE_ACTIVITY_STUCK));
	CHECK(!sleep_stale_should_start(SLEEP_STALE_AFTER_S, true, false,
					USAGE_ACTIVITY_FAILED));
	CHECK(!sleep_stale_should_start(SLEEP_STALE_AFTER_S, true, false,
					USAGE_ACTIVITY_RUNNING));
	/* A finished turn is amber, not a summons: the pip already carries
	 * it and it does not keep the panel lit for four hours. */
	CHECK(sleep_stale_should_start(SLEEP_STALE_AFTER_S, true, false,
				       USAGE_ACTIVITY_IDLE));

	/* --- start and wake are exact complements --- */

	/*
	 * The one property that matters more than any single case. The wake
	 * rule runs INSIDE ui_sleep_run while the start rule stays outside
	 * it, so a threshold or an activity that drifted between them would
	 * make the board close its eyes and open them again in a loop,
	 * forever, on a real desk. Pinned as a grid rather than as prose.
	 */
	{
		static const enum usage_activity acts[] = {
			USAGE_ACTIVITY_NONE, USAGE_ACTIVITY_IDLE,
			USAGE_ACTIVITY_RUNNING, USAGE_ACTIVITY_WAITING,
			USAGE_ACTIVITY_STUCK, USAGE_ACTIVITY_FAILED,
		};
		static const int32_t ages[] = {
			-1, 0, 600, 1800, SLEEP_STALE_AFTER_S - 1,
			SLEEP_STALE_AFTER_S, SLEEP_STALE_AFTER_S + 1, 205200,
		};
		unsigned int a, g, o;

		for (a = 0; a < sizeof(acts) / sizeof(acts[0]); a++) {
			for (g = 0; g < sizeof(ages) / sizeof(ages[0]); g++) {
				for (o = 0; o < 2; o++) {
					bool start = sleep_stale_should_start(
						ages[g], true, o == 1, acts[a]);
					bool wake = sleep_stale_should_wake(
						ages[g], o == 1, acts[a]);

					CHECK(start != wake);
				}
			}
		}
	}

	/* --- the age predicate the wake-time status stamp shares --- */
	CHECK(!sleep_reading_is_old(-1));
	CHECK(!sleep_reading_is_old(0));
	CHECK(!sleep_reading_is_old(SLEEP_STALE_AFTER_S - 1));
	CHECK(sleep_reading_is_old(SLEEP_STALE_AFTER_S));

	printf("%s\n", fails ? "FAIL" : "ok   sleep_gate");
	return fails ? 1 : 0;
}
```

- [ ] **Step 2: Run it to verify it fails**

Run: `cc -Wall -Werror -I firmware/src tests/sleep_gate/host_test.c firmware/src/sleep_gate.c -o /tmp/sg`
Expected: BUILD FAILS — `implicit declaration of function 'sleep_stale_should_start'`.

- [ ] **Step 3: Implement the rules**

Append to `firmware/src/sleep_gate.h`, before the `#endif`, and add `#include "usage_view.h"` under the existing `#include <stdbool.h>` plus `#include <stdint.h>`:

```c
/*
 * How old a reading has to be before the board stops waiting up for it.
 *
 * Four hours, and the number is load-bearing. It has to sit above every gap
 * a person sitting at the desk can produce and below a night:
 *
 *   - The daemon's own staleness bound is 1800 s (pc/statusline_source
 *     STALE_AFTER_S). That marks "this reading is old", not "nobody is
 *     here" -- a Claude Code user reading code between renders crosses it
 *     routinely, and dozing on them would be the opposite bug.
 *   - The age caption appears at 600 s (AGE_CAPTION_MIN_S in usage_view.c),
 *     chosen against Claude Desktop's 300 s at-the-machine and 900 s
 *     away refresh schedules -- so a desktop-only user who is present sits
 *     under 900 s.
 *   - Four hours is 8x the first and 16x the second. It outlasts a lunch, a
 *     meeting, and any single stretch of work without a render, and still
 *     has a machine that sleeps at 23:00 dozing by 03:00 rather than at
 *     dawn. Eight hours would have left the board that produced this bug
 *     lit until morning, which was the complaint.
 */
#define SLEEP_STALE_AFTER_S 14400

/* Has the shown reading stopped moving? -1 (we cannot say) is NOT old: a
 * daemon too old to send an age must not doze the panel. */
bool sleep_reading_is_old(int32_t age_s);

/*
 * The second way in (field report 2026-09-02).
 *
 * The rule above waits for silence, and silence never came: proto.c clears
 * host_lost on every protocol line including the 10 s pings, so a computer
 * that slept while its daemon kept answering left the panel awake all night
 * on a reading 57 hours old. This asks the other question -- the app is
 * talking, but is it saying anything new? -- and refuses for the same two
 * reasons the first rule does, plus one of its own: nothing on screen may be
 * asking for a person.
 */
bool sleep_stale_should_start(int32_t age_s, bool had_usage, bool ota_busy,
			      enum usage_activity act);

/*
 * And back out again. The exact complement of the rule above minus
 * had_usage, which cannot become false once true.
 *
 * It has to be a separate function because it is asked from inside
 * ui_sleep_run, where the loop is waiting on something to change, and
 * complement rather than "a fresh reading arrived" because the wake
 * condition drifting from the sleep condition by so much as a second would
 * have a board on a real desk closing and opening its eyes forever.
 */
bool sleep_stale_should_wake(int32_t age_s, bool ota_busy,
			     enum usage_activity act);
```

Append to `firmware/src/sleep_gate.c`:

```c
/* Nothing on screen is asking for a person. RUNNING is excluded as well as
 * the three alarming ones: work in flight is work somebody may want to watch
 * land, and it will be over long before four hours are out. IDLE is allowed
 * because a finished turn is an amber pip, not a summons -- and it is the
 * state a desk sits in overnight. */
static bool nothing_wants_a_person(enum usage_activity act)
{
	return act == USAGE_ACTIVITY_NONE || act == USAGE_ACTIVITY_IDLE;
}

bool sleep_reading_is_old(int32_t age_s)
{
	return age_s >= 0 && age_s >= SLEEP_STALE_AFTER_S;
}

bool sleep_stale_should_start(int32_t age_s, bool had_usage, bool ota_busy,
			      enum usage_activity act)
{
	return had_usage && !ota_busy && nothing_wants_a_person(act) &&
	       sleep_reading_is_old(age_s);
}

bool sleep_stale_should_wake(int32_t age_s, bool ota_busy,
			     enum usage_activity act)
{
	return ota_busy || !nothing_wants_a_person(act) ||
	       !sleep_reading_is_old(age_s);
}
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cc -Wall -Werror -I firmware/src tests/sleep_gate/host_test.c firmware/src/sleep_gate.c -o /tmp/sg && /tmp/sg`
Expected: `ok   sleep_gate`, no FAIL lines.

- [ ] **Step 5: Prove the test bites**

Change `sleep_reading_is_old`'s `age_s >= SLEEP_STALE_AFTER_S` to `age_s > SLEEP_STALE_AFTER_S`. Re-run. Expected: 2 `FAIL ... start != wake` lines at exactly `age_s == SLEEP_STALE_AFTER_S`, for the NONE and IDLE activities — the boundary is the only place an off-by-one can hide, and start-and-wake-both-true there is the animation looping forever. Restore.

> **Corrected after execution.** This step originally said to rewrite `!sleep_reading_is_old(age_s)` as `age_s < SLEEP_STALE_AFTER_S` and predicted 12 failures. That rewrite is an *identity*: `sleep_reading_is_old` is `age_s >= 0 && age_s >= SLEEP_STALE_AFTER_S`, and for any positive threshold the first conjunct is implied by the second, so the two spellings agree at every age including `-1`. It produced 0 failures, correctly. Task 5's implementer caught this and substituted the off-by-one above; the reviewer confirmed it independently. A mutation that cannot change behaviour proves nothing about a test — which is the whole point of this step.

Then change `#define SLEEP_STALE_AFTER_S 14400` to `1800` and re-run. Expected: PASS — the grid is written in terms of the constant, so it deliberately does not pin the number. That is what Step 6 is for. Restore.

- [ ] **Step 6: Pin the number itself**

The threshold is a product decision, not an implementation detail, so it gets its own assertion rather than travelling as a symbol. Add just before the `printf` in `tests/sleep_gate/host_test.c`:

```c
	/* The number, not just the symbol. Everything above is written in
	 * terms of SLEEP_STALE_AFTER_S and would keep passing if somebody
	 * changed it to 30 minutes -- which would doze on a person sitting
	 * at the desk between renders. Four hours is argued for in
	 * sleep_gate.h; changing it means changing that argument too. */
	CHECK(SLEEP_STALE_AFTER_S == 4 * 60 * 60);
```

Re-run: PASS. Set the constant to `1800` again: expected `FAIL ... SLEEP_STALE_AFTER_S == 4 * 60 * 60`. Restore.

- [ ] **Step 7: Document it**

In `docs/sleep-mode-design.md`, under `## How sleep is detected`, after the "Board-side, from silence." paragraph:

```markdown
There is a second way in, added after a field report (2026-09-02): **from a
reading that has stopped moving**. The first rule waits for silence, and a
computer that goes to sleep does not necessarily produce any — the daemon on
it kept answering pings all night while the figures it pushed were 57 hours
old, so `host_lost` never armed and the panel sat awake showing "Reading is
old" until morning. So a reading older than **4 h** (`SLEEP_STALE_AFTER_S`),
on a board that has shown figures this boot, with no update in flight and
with nothing on screen asking for a person (no waiting, stuck, failed or
running session), also enters SLEEP. It leaves on the first reading younger
than that — which is the next `usage` message after somebody uses Claude Code
again — or on any of those conditions changing. A tap peeks as it always did.

The threshold is argued for in `firmware/src/sleep_gate.h`: above the
daemon's own 1800 s staleness bound and Claude Desktop's 900 s away-schedule
by a wide margin, so a person at the desk is never dozed on; short enough
that a machine sleeping at 23:00 has a dark panel by 03:00.
```

And under `## Firmware`, after the `ui_sleep.c/.h` bullet:

```markdown
- `usage_freshness.c/.h`: keeps the `age_s` and `state` that `proto.c`
  already parses somewhere `main.c` can read them and a laptop can test them,
  and grows the age with uptime between messages.
- `sleep_gate.c`: `sleep_stale_should_start()` / `sleep_stale_should_wake()`
  are exact complements, pinned by a grid in `tests/sleep_gate`, because one
  lives outside `ui_sleep_run` and the other inside it.
```

- [ ] **Step 8: Run the host suite**

Run: `sh tests/ci/check_host_tests.sh`
Expected: `PASS [host tests]`, `sleep_gate ok (…)` with the new count.

- [ ] **Step 9: Commit**

```bash
git add firmware/src/sleep_gate.h firmware/src/sleep_gate.c \
        tests/sleep_gate/host_test.c docs/sleep-mode-design.md
git commit -m "feat: a reading that stopped moving is also a reason to doze"
```

---

### Task 6: `ui_sleep_run` takes its wake condition from the caller

Pure refactor plus one honesty fix. No behaviour change at the existing call site; Tasks 7 and 8 both need this.

**Files:**
- Modify: `firmware/src/ui_sleep.h`
- Modify: `firmware/src/ui_sleep.c`
- Modify: `firmware/src/main.c` (the existing call at line 1413)

**Interfaces:**
- Consumes: `sleep_reading_is_old(int32_t)` and `usage_freshness_age_s(int64_t)` from Tasks 4 and 5.
- Produces: `void ui_sleep_run(bool (*awake)(void), const char *peek_note);` — `awake` must not be NULL; it is polled between animation frames and ends the doze when it returns true. `peek_note` is the sentence shown for ten seconds on a tap. Tasks 7 and 8 call it.

- [ ] **Step 1: Change the signature and the peek note**

`firmware/src/ui_sleep.h` becomes:

```c
#ifndef UI_SLEEP_H
#define UI_SLEEP_H

#include <stdbool.h>

/*
 * Close the eyes, doze until `awake()` says otherwise, open them. Blocks for
 * the whole of it, servicing the daemon protocol between frames, and returns
 * with the previous screen restored. A tap while dozing shows the dashboard
 * with `peek_note` under it for ten seconds.
 *
 * `awake` is the caller's, not this file's, because there are two reasons to
 * doze and they end differently. A computer that went silent wakes when it
 * speaks. A computer whose daemon never stopped talking but has had nothing
 * new to say for hours cannot use that test at all -- it is true the whole
 * time it is dozing -- so it asks about the age of the reading instead. This
 * function used to hard-code the first test, which is why the second kind of
 * sleep could not be built on it.
 *
 * Must not be NULL: a doze with no way out is a bricked panel.
 */
void ui_sleep_run(bool (*awake)(void), const char *peek_note);

#endif /* UI_SLEEP_H */
```

In `firmware/src/ui_sleep.c`, add the two includes and replace the predicate helpers and `ui_sleep_run`'s head and tail:

```c
#include "sleep_gate.h"
#include "usage_freshness.h"
```

Replace `host_back()` and `host_back_or_tap()` with:

```c
static bool (*wake_when)(void);

static bool awake_now(void)
{
	return wake_when();
}

static bool awake_or_tap(void)
{
	return wake_when() || tapped;
}
```

Replace `peek()`'s signature and its two `proto_host_seen()` tests:

```c
/* A tap: the dashboard as it was, with a word about why nothing moves. Ten
 * seconds, or until there is something new to show, then back to dozing. */
static void peek(lv_obj_t *prev, lv_obj_t *sleep_scr, const char *note)
{
	int64_t until = k_uptime_get() + PEEK_MS;

	lv_scr_load(prev);
	ui_settings_notice(note);
	lv_refr_now(NULL);
	while (k_uptime_get() < until && !wake_when()) {
		service();
	}
	ui_settings_notice_dismiss();
	if (!wake_when()) {
		lv_scr_load(sleep_scr);
		lv_refr_now(NULL);
	}
}
```

`ui_sleep_run` becomes:

```c
void ui_sleep_run(bool (*awake)(void), const char *peek_note)
{
	const struct bootclip *close = sleepclip_active(SLEEP_CLOSE);
	const struct bootclip *loop = sleepclip_active(SLEEP_LOOP);
	const struct bootclip *open = sleepclip_active(SLEEP_OPEN);
	lv_obj_t *prev = lv_scr_act();
	lv_obj_t *scr = lv_obj_create(NULL);

	wake_when = awake;
	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_add_flag(scr, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_set_style_bg_color(scr, lv_color_hex(close->bg_rgb), 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_obj_add_event_cb(scr, tap_cb, LV_EVENT_CLICKED, NULL);
	lv_scr_load(scr);
	lv_refr_now(NULL);
	printk("[sleep] dozing (%s)\n", close->name);

	ui_boot_play_clip(close->blob, close->blob_len, awake_now);
	while (!wake_when()) {
		tapped = false;
		if (!ui_boot_play_clip(loop->blob, loop->blob_len,
				       awake_or_tap)) {
			/* A loop that will not decode must not spin. */
			int64_t until = k_uptime_get() + 1000;

			while (k_uptime_get() < until) {
				service();
			}
		}
		if (tapped && !wake_when()) {
			peek(prev, scr, peek_note);
		}
	}
	printk("[sleep] waking\n");
	ui_boot_play_clip(open->blob, open->blob_len, NULL);

	/*
	 * Back to the dashboard as it was -- flagged old only if it still IS.
	 *
	 * This used to stamp STALE unconditionally, which was right for the
	 * one caller that existed and wrong for both of the others. A board
	 * dozing because its reading stopped moving wakes on a FRESH reading,
	 * which has already set the dot green; stamping amber over it labels
	 * the very frame that woke us as old. And a board dozing before it
	 * ever met a daemon has no reading at all to call old.
	 */
	lv_scr_load(prev);
	lv_obj_del(scr);
	if (usage_view_have_data() &&
	    sleep_reading_is_old(usage_freshness_age_s(k_uptime_get()))) {
		usage_view_set_status(USAGE_STATUS_STALE);
	}
	lv_refr_now(NULL);
}
```

- [ ] **Step 2: Update the existing call site**

`firmware/src/main.c` line 1413 becomes:

```c
				ui_sleep_run(host_is_back,
					     "Your computer may be asleep.");
```

and, near the other file-scope helpers in `main.c`, above `run_usb`:

```c
/* The original reason to wake: the app said something. proto.c clears
 * host_seen on the timeout that armed the sleep in the first place, so this
 * is false throughout the doze and true on the first line back. */
static bool host_is_back(void)
{
	return proto_host_seen();
}
```

- [ ] **Step 3: Build the firmware**

Run the project's normal Zephyr build for this board (`west build` from `firmware/`, exactly as the branch has been building it — do not add `ZEPHYR_BASE` or a stray `-b`; see the CYD hardware notes).
Expected: compiles clean, no new warnings. The unconditional-STALE change and the function-pointer indirection are the only behavioural deltas, and neither is reachable differently at this call site — `host_is_back` is what `host_back` was.

- [ ] **Step 4: Verify no other caller was missed**

Run: `grep -rn "ui_sleep_run" firmware/src/`
Expected: exactly three hits — the declaration in `ui_sleep.h`, the definition in `ui_sleep.c`, the call in `main.c`.

- [ ] **Step 5: Commit**

```bash
git add firmware/src/ui_sleep.h firmware/src/ui_sleep.c firmware/src/main.c
git commit -m "refactor: the sleeper asks the caller when to wake up"
```

---

### Task 7: Bug A — doze on a reading that has stopped moving

**Files:**
- Modify: `firmware/src/main.c` (the sleep block at lines 1402-1416, inside `run_usb`)

**Interfaces:**
- Consumes: `sleep_should_start`, `sleep_stale_should_start`, `sleep_stale_should_wake` (Task 5); `usage_freshness_age_s`, `usage_freshness_activity` (Task 4); `ui_sleep_run(bool (*)(void), const char *)` (Task 6).
- Produces: nothing other tasks consume.

- [ ] **Step 1: Add the wake predicate**

In `firmware/src/main.c`, beside `host_is_back` from Task 6:

```c
/*
 * The other reason to wake: something new to show.
 *
 * The mirror of sleep_stale_should_start's non-had_usage terms, and it has
 * to be exactly that -- tests/sleep_gate pins the two as complements over a
 * grid, because a wake condition a second away from the sleep condition
 * would have this board closing and opening its eyes forever on a real desk.
 * ota_busy is read fresh: an update that starts while dozing should put the
 * progress screen back up.
 */
static bool reading_moved_again(void)
{
	struct ota_ui snap;

	ota_ui_get(&snap);
	return sleep_stale_should_wake(usage_freshness_age_s(k_uptime_get()),
				       snap.st == OTA_UI_DOWNLOADING ||
				       snap.st == OTA_UI_REBOOTING,
				       usage_freshness_activity());
}
```

- [ ] **Step 2: Extend the sleep block**

Replace the block at lines 1402-1416:

```c
		/* Two reasons to doze (docs/sleep-mode-design.md). The first
		 * is silence past the host timeout. The second is an app that
		 * never stopped talking and has had nothing new to say for
		 * hours -- which is what a sleeping computer with a running
		 * daemon actually looks like from here, and why the first
		 * rule alone left a panel awake all night (field report
		 * 2026-09-02). Both block until there is a reason to wake. */
		{
			struct ota_ui snap;
			bool ota_busy;

			ota_ui_get(&snap);
			ota_busy = snap.st == OTA_UI_DOWNLOADING ||
				   snap.st == OTA_UI_REBOOTING;
			if (sleep_should_start(proto_host_lost(),
					       usage_view_have_data(),
					       ota_busy)) {
				ui_sleep_run(host_is_back,
					     "Your computer may be asleep.");
				continue;
			}
			if (sleep_stale_should_start(
				    usage_freshness_age_s(k_uptime_get()),
				    usage_view_have_data(), ota_busy,
				    usage_freshness_activity())) {
				ui_sleep_run(reading_moved_again,
					     "No new readings for a while.");
				continue;
			}
		}
```

Add `#include "usage_freshness.h"` to `main.c`'s include block if Task 4 did not already put it there (`sleep_gate.h` is already included for the existing gate).

- [ ] **Step 3: Build**

Run the Zephyr build.
Expected: clean.

- [ ] **Step 4: Reason about the wire budget**

No new fields: `age_s` and `state` have been on the `usage` line since before this branch and are already parsed at `proto.c:420` and `:338`. Confirm with `grep -n '"age_s"\|"state"' pc/protocol.py firmware/src/proto.c` — expected: both keys present on both sides, nothing added. The ~510/512 measurement is unchanged and needs no re-measurement.

- [ ] **Step 5: No panel scene needed, and say why**

`tools/panel_render` renders `usage_view.c`. Nothing in this task draws: the dashboard is unchanged, and the two strings that are new (`"No new readings for a while."` and the `printk`s) belong to `ui_settings_notice` and the console. Run `sh tools/panel_render/render.sh` anyway to confirm the harness still builds against an untouched `usage_view.c` — expected: it writes its framebuffers as before. The peek note is covered by the hardware step in Task 9.

- [ ] **Step 6: Commit**

```bash
git add firmware/src/main.c
git commit -m "fix: a chatty daemon with nothing to say kept the panel awake all night"
```

---

### Task 8: Bug C — never-connected dozes instead of rebooting

**Files:**
- Modify: `firmware/src/main.c` (lines 1376-1386, the `can_fall_back` block; lines 1478-1504, the waiting-for-host block inside `run_usb`)

**Interfaces:**
- Consumes: `ui_sleep_run(bool (*)(void), const char *)` and `host_is_back` (Task 6).
- Produces: nothing other tasks consume.

**What is lost, and it is deliberate:** a board with stored WiFi and a stored token that never hears a daemon will now doze on the "Link the PC daemon" screen instead of cold-rebooting into standalone self-service. It will not fetch its own usage over WiFi in that situation. The owner asked for this explicitly — the reset is worse than the missing fallback, and the two never-heard-from-a-daemon cases behaving differently was the actual complaint. Standalone mode is still reached the normal way: `main()` (lines 1637-1676) picks it at boot when no daemon speaks during the splash and the SSID scan succeeds. This task removes the *reboot into* standalone, not standalone.

**A nuance worth knowing before editing:** the existing comment says "requires the host to be *gone*, not merely slow", and `proto_host_seen()` is not "seen at any point this boot" — `proto_service` clears it after `HOST_TIMEOUT_MS` of silence. So the current condition already fires for a daemon that connected at t=10 s and died at t=15 s. The replacement keeps `!proto_host_seen()` verbatim, and adds a timer that restarts whenever the host is seen, so the 60 s grace applies to every silence rather than only the first.

- [ ] **Step 1: Remove the fallback probe**

Replace lines 1376-1386 of `firmware/src/main.c` (the `can_fall_back` block, both arms of the `#if`) with nothing, and delete the `#include <zephyr/sys/reboot.h>`-dependent `sys_reboot` use in Step 2. The `ssid`/`psk`/`tok` buffers were declared only for this probe; removing them removes the `#if IS_ENABLED(CONFIG_BLINK_WIFI_MODE)` block from `run_usb` entirely. Leave `sys_reboot`'s include alone — `main.c` uses it elsewhere; confirm with `grep -n "sys_reboot" firmware/src/main.c` and only remove the include if that grep comes back empty.

- [ ] **Step 2: Replace the reboot with a doze**

Add, immediately after `int stage_shown = 1;` at the top of `run_usb`:

```c
	/* When the app was last heard from, for the waiting-for-host doze
	 * below. Starts at the top of the loop rather than at boot: this is
	 * about how long THIS screen has been unanswered. */
	int64_t host_quiet_since = last_tick;
```

Replace lines 1490-1504 (the comment and the `if (can_fall_back && ...)` block) with:

```c
			/*
			 * Waiting-for-host timeout.
			 *
			 * This used to cold-reboot into standalone
			 * (user request 2026-07-16), on the theory that a
			 * daemon which has not spoken is not coming back and
			 * a board with WiFi and a token can serve itself. On
			 * a desk it read as an unexplained reset, and it made
			 * the two never-heard-from-a-daemon cases behave
			 * differently for no reason a user could see: a board
			 * that once talked to the app dozes, a board that
			 * never did rebooted. Now both doze (owner's call,
			 * 2026-09-02). The cost is real and accepted: a board
			 * that could have fetched its own usage over WiFi
			 * sleeps instead. Standalone is still chosen at boot
			 * in main() when no daemon answers the splash.
			 *
			 * Still gated on the host being GONE rather than
			 * slow, which is what stops a live-but-busy daemon
			 * from dozing us mid-conversation -- and note that
			 * proto_host_seen() goes false again after
			 * HOST_TIMEOUT_MS, so the timer restarts on every
			 * silence rather than only the first.
			 */
			if (proto_host_seen()) {
				host_quiet_since = k_uptime_get();
			} else if (k_uptime_get() - host_quiet_since
				   > 60 * 1000) {
				printk("[usage] no app after 60 s; dozing until one speaks\n");
				ui_sleep_run(host_is_back,
					     "Waiting for the app on your computer.");
				host_quiet_since = k_uptime_get();
				continue;
			}
```

- [ ] **Step 3: Build**

Run the Zephyr build, and also build with WiFi mode off if the branch has a config for it, since the `#if` block was removed.
Expected: clean, no unused-variable warnings (the build runs `-Wall`; the removed `can_fall_back`, `ssid`, `psk` and `tok` were its only users in this function).

- [ ] **Step 4: Confirm nothing else depended on the reboot**

Run: `grep -n "can_fall_back\|standalone can serve" firmware/src/main.c`
Expected: no hits.
Run: `grep -n "ui_boot_mark_intentional_reboot" firmware/src/`
Expected: still called from the provisioning path; if this was its only remaining caller, leave the function in place and say so in the commit message rather than deleting it in this task.

- [ ] **Step 5: Commit**

```bash
git add firmware/src/main.c
git commit -m "fix: a board that never met a daemon reset itself instead of dozing"
```

---

### Task 9: Flash, boot-verify, and prove all three on the desk

Firmware is not done until flashed and boot-verified. The daemon and board on this machine are live and in use; every command below is reversible and the final state must be the daemon running again.

**Files:** none changed. This task produces evidence.

**Interfaces:**
- Consumes: everything above.
- Produces: the observations that let the owner close the three reports.

- [ ] **Step 1: Green on both suites first**

Run: `pytest tests -q`
Expected: 538 passed (530 + 8 from Task 2), 0 failed.
Run: `sh tests/ci/check_host_tests.sh`
Expected: `PASS [host tests]`, 15 rows, none FAILED.

- [ ] **Step 2: Take the port**

Run: `launchctl bootout gui/502/com.blink.bridge`
Expected: returns without output; `launchctl print gui/502/com.blink.bridge` then reports the service is not found. Confirm the port is free: `ls /dev/cu.usbserial-*`.

- [ ] **Step 3: Flash and watch it boot**

Flash with the branch's normal `west flash` path against `/dev/cu.usbserial-14240`, remembering that the port re-enumerates on reset — re-read `ls /dev/cu.usbserial-*` after the flash before opening a monitor. Attach the passive logger (the venv python reader, not a second daemon).
Expected: the boot clip plays, the panel reaches the CONNECTING bar, and the console shows `[usage] mode: USB bridge (no host yet)` — there is no daemon running yet.

- [ ] **Step 4: Verify Bug C on the bench**

With no daemon running, leave the board on the "Link the PC daemon" screen and watch for four minutes.
Expected: at ~60 s the console prints `[usage] no app after 60 s; dozing until one speaks`, the eyes close, and the board stays dozing. **No `sys_reboot`, no boot clip replay, no watchdog reset** — the previous behaviour was a cold boot at 60 s, so a second boot banner in the log is a failure of this task. Tap the panel: the CONNECTING screen returns for ten seconds with "Waiting for the app on your computer." under it, then the eyes close again.

- [ ] **Step 5: Restore the daemon and verify it wakes**

Run: `launchctl bootstrap gui/502 ~/Library/LaunchAgents/com.blink.bridge.plist` then `launchctl kickstart -k gui/502/com.blink.bridge`. Both are required — bootstrap registers without starting.
Expected: within seconds the eyes open, the dashboard appears with real figures, and the health dot is green.

- [ ] **Step 6: Verify Bug B against the live daemon**

Run: `blink status` and check the Reading line, then compare against the daemon's own log (read-only) for the `src` of the frames it is sending: `tail -5 ~/.blink/bridge.log`.
Expected: while Claude Code has an active five-hour window, `src: 'cli'` with a small `age_s` — unchanged from today. The remembered-reading path only shows itself once the window expires with nobody rendering, so record the current `age_s` and re-check after the next expiry; the behaviour under test is already pinned by Tasks 2 and 3, and this step is confirming the shipped bundle does what the worktree does.

- [ ] **Step 7: Verify Bug A without waiting four hours**

Temporarily rebuild with `SLEEP_STALE_AFTER_S` set to `120` (in `firmware/src/sleep_gate.h`), flash, and let the daemon run normally against a machine where Claude Code has not rendered for two minutes.
Expected: the daemon keeps pinging (visible in the log), the eyes close anyway at ~120 s of reading age, and using Claude Code — which rewrites the status line and makes the next frame's `age_s` small — opens them again within a minute. Tap while dozing: the dashboard returns for ten seconds with "No new readings for a while."
Then **restore `SLEEP_STALE_AFTER_S` to 14400, rebuild, reflash, and re-verify the board boots and shows figures.** Confirm with `grep -n "SLEEP_STALE_AFTER_S 14400" firmware/src/sleep_gate.h` and `sh tests/ci/check_host_tests.sh` (the Task 5 Step 6 assertion fails if the temporary value is still in the tree).

- [ ] **Step 8: Leave the desk as it was**

Run: `launchctl print gui/502/com.blink.bridge | head -20`
Expected: the service is loaded and running, the board shows live figures with a green dot. Nothing under `~/.blink` was written by hand.

- [ ] **Step 9: Commit the evidence**

Nothing to commit if Step 7's restore was clean. Confirm with `git status` — expected: clean tree. If the temporary threshold survived, that is the failure this step exists to catch.

---

## Self-review

**1. Spec coverage.** Bug B's decision (remember the last CLI reading) → Tasks 1-3, with the docstring argument written out in "Why the module docstring's reasoning survives Bug B". Bug A's decision (firmware sleeps on a stale reading, threshold justified) → Tasks 4, 5, 6, 7, with the justification in `sleep_gate.h` and in Task 5's preamble. Bug C's decision (never-connected matches connected-then-gone, `!proto_host_seen()` preserved, loss stated) → Task 8. The three judgement calls asked for are answered in Task 2's docstring (where the cache lives, why it dies with the process), Task 5 (the threshold), and the note below (whether the panel says the reading is remembered). Global constraints: wire budget → Task 7 Step 4 confirms no field was added; host-testability → Tasks 4 and 5 put every new decision in a `cc`-compilable module; flash-and-verify → Task 9; "prove it bites" → every test task has an explicit broken-variant step.

**Judgement call 3, stated here because it produces no task:** the panel does **not** say the reading is a remembered one. It shows it with a growing age and the caption it already has — `"Reading is old - showing last known"` (`usage_view.c:2325`), which is armed by the `stale` flag the remembered frame carries on its own age. That sentence is already exactly true of a remembered reading, and it is the sentence a user sees today for the same underlying situation. A new word would need either a new wire field (against the 510/512 budget, for a distinction the user cannot act on) or a re-purposed one, and the number that was actually wrong was the age, not the label. So: no wire change, no new copy, no new `render_main.c` scene.

**2. Placeholder scan.** No "TBD", "handle edge cases", "similar to Task N" or "add appropriate error handling". Every code step carries the code. Two steps deliberately say "run the branch's normal build" rather than inventing a `west` invocation — the CYD notes record that a wrong `-b` or a stale `build/` poisons that build, so naming a command from memory here would be worse than pointing at the one the branch already uses. Task 9 Step 6 does not assert a value it cannot produce on demand and says so.

**3. Type consistency.** `usage_freshness_note(int32_t, enum usage_activity, int64_t)` / `usage_freshness_age_s(int64_t)` / `usage_freshness_activity(void)` are declared in Task 4 and called with those exact signatures in Tasks 5's test, 6, and 7. `sleep_reading_is_old(int32_t)`, `sleep_stale_should_start(int32_t, bool, bool, enum usage_activity)` and `sleep_stale_should_wake(int32_t, bool, enum usage_activity)` are declared in Task 5 and called with the same argument order in Tasks 6 and 7 — note `should_wake` takes three arguments and drops `had_usage`, which is deliberate and stated in the header. `ui_sleep_run(bool (*)(void), const char *)` is defined in Task 6 and called with `host_is_back` (Task 6) and `reading_moved_again` (Task 7) and `host_is_back` again (Task 8); `host_is_back` is defined once, in Task 6, and Task 8 does not redefine it. On the Python side `ClaudeCliProvider.poll` keeps its `(now_epoch) -> list` shape, and Task 3's test consumes only the message keys `src`, `session_pct`, `age_s`, `stale`, all of which `protocol.usage()` already emits.

**Fixed during review:** Task 8 originally left `start` as the timer base, which would have dozed instantly on every silence after the first; it now carries `host_quiet_since`. Task 6 originally kept `ui_sleep_run`'s unconditional `usage_view_set_status(USAGE_STATUS_STALE)`, which would have labelled the fresh frame that woke a stale-sleeper as old, and would have stamped "reading is old" on a board with no reading at all in Task 8's case; it is now conditional on `usage_view_have_data() && sleep_reading_is_old(...)`.
