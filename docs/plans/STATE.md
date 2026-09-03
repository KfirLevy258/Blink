# Where this programme stands

Written to disk deliberately: this is the handoff, and it must be readable by
someone — or some session — with no memory of how it got here. Update it as
things land.

Last updated: 2026-09-03, after plan 0 was flashed and Bug C verified on
hardware. NEXT UP: plans 2 and 3 (Codex).

## Branch

`worktree-hint-line`, off `main`. **Not merged.** Worktree at
`.claude/worktrees/hint-line`. The daemon and board on this machine are LIVE
and in use: board at `/dev/cu.usbserial-14240`, service `com.blink.bridge`
under launchd. Freeing the port means `launchctl bootout gui/502/com.blink.bridge`;
restoring it needs **both** `bootstrap` AND `kickstart` — bootstrap registers
without starting.

## The four plans, in execution order

| # | Plan | Tasks | State |
|---|---|---|---|
| 1 | [`shim-self-repair.md`](shim-self-repair.md) | 6 | **done**, reviewed |
| 0 | [`field-bugs.md`](field-bugs.md) | 9 | **done and FLASHED.** Bug C verified on hardware; Bug A and B await a morning check |
| 2 | [`codex-naming-and-failure.md`](codex-naming-and-failure.md) | 10 | **START HERE.** Written, not started. Daemon-only Python -- nothing to flash |
| 3 | [`codex-hook-shim.md`](codex-hook-shim.md) | 14 | written, not started |

Plan 1 ran first and is effectively done. Plan 0 was added later but runs
**next**, ahead of 2 and 3, because those are the three faults the owner is
actually living with. [`README.md`](README.md) carries the sequencing argument.

## What Plan 1 actually fixed, and why it mattered

Nothing had ever rewritten `~/.blink/blink-hook.sh` after the first install.
`blink update` swaps the program directory and restarts the service; it has
never touched a shim. So every customer who upgraded the documented way ran a
new daemon reading a `name` field that an old shim never wrote — session
naming was silently dead — while `blink status` reported "hooks installed
(10/10 events)" because the path existed and still ran.

**This was live on the owner's own machine.** Task 3's end-to-end step found
`~/.blink/blink-hook.sh` at 5076 bytes (the pre-naming version), repaired it to
9093, and state files began carrying `"name"` for the first time. Verified
independently.

Commits: `4dd5fca` detection, `1dbe4fd` repair, `1972601` wiring, `c9f567b`,
`34a1bbf`, `1dab41d` the three vacuous-test removals.

## The decisions already taken — do not re-open

**Plan 0 (field bugs):**
- Bug B (56-hour reading): **remember the last CLI reading.** Not a re-rank —
  the CLI reading ceases to exist when the five-hour window expires, so this
  needs new daemon state.
- Bug A (stayed awake all night): **firmware sleeps on a stale reading too**,
  not just on host silence. `proto.c:262` clears `host_lost` on every line,
  pings included, which is why the gate never armed.
- Bug C (reboots with no daemon): **replace the reboot with sleep**, matching
  the connected-then-gone case. Accepted cost: a board that could self-serve
  over WiFi will sleep instead.
  - **Amended during planning: the `can_fall_back` gate is removed too.**
    Keeping it would leave a board *without* stored WiFi awake forever on
    "1/2" — which is Bug A wearing a different hat. Both cases now doze.
  - **Correction to a claim made while briefing this plan.** I described
    `!proto_host_seen()` as "only when the host has NEVER been seen this
    boot". It is not: `proto.c:730` clears `host_seen` after
    `HOST_TIMEOUT_MS` (30 s) of silence, so it means "the host is not here
    *now*". The code's own comment at `main.c:1490` is accurate — "requires
    the host to be *gone*, not merely slow" — and the reboot is scoped to
    the window before the first usage push, so it is narrower than the
    paraphrase implied. Trust `main.c:1490`, not the paraphrase.

**Plans 2 and 3 (Codex):**
- `codex exec` batch runs count as sessions and get named.
- The Codex hook installs automatically when Codex is detected, not behind a
  flag — but `blink install` must say plainly that Codex will ask the user to
  trust it once.
- `turn_aborted` stays mapped to idle. It carries a `reason`, and Esc is
  `interrupted`; the current mapping is right. The gap is `task_complete`
  carrying an `error`.

## The one thing that keeps biting

**Five assertions on this branch could not fail.** The atomic-write check
tested for a filename the shim cannot write; `make_fetch`'s guard had no
caller; the clock lost its assertion when it moved rows; a permission test
chmod'd the wrong thing; and `EXPECT_EQ(clock.y0, STATUS_Y)` restates its own
construction. Two were authored by the session that then reviewed them.

The habit that catches them: **build the broken variant and check the test
rejects it.** A green run proves nothing on its own. Every plan here carries
that instruction; keep it.

## Open, not scheduled

- The branch is unmerged and `main` has none of this.
- Non-ASCII labels reach the panel as truncated `\uXXXX` text. Unreachable
  today because the Claude shim refuses non-ASCII; becomes reachable the moment
  a Codex `cwd` becomes a label, which is Plan 2 Task 1. Measured: switching to
  `ensure_ascii=False` is never longer on the wire.
- `tools/blink-hook.sh` refuses names over 24 chars, with spaces, or non-ASCII.
  `My Project` silently gets no label. Still the owner's decision.
- Nobody has ever executed a Codex hook — the single largest unknown in the
  programme, and all of it sits in Plan 3.

## Plan 0: what is done, and the one thing that is not

Tasks 1-8 are committed and reviewed. All three field bugs are fixed **in
code**. Task 9 — flash the board and watch the three behaviours on the desk —
has not been started, and nothing on this branch has ever run on hardware.

Evidence as it stands: `pytest tests -q` 549 passed; `sh tests/ci/check_host_tests.sh`
PASS across 15 suites, from a clean serial run; the default Zephyr build clean
with a valid image; and the `wifi.conf` build compiling and linking clean.

**Why the build mattered more than usual here.** `ui_sleep.c` and `main.c` need
LVGL and Zephyr, have no host tests, and cannot be compiled by any agent on this
laptop — so tasks 6, 7 and 8 were reviewed by reading alone until the build ran.
The default build has `CONFIG_BLINK_WIFI_MODE` unset and compiles the `#else`
arm; task 8's actual edit lives in the `#if` arm, so a second build with
`-DEXTRA_CONF_FILE=wifi.conf` was needed to compile it at all.

Two environmental facts found on the way, neither caused by this branch:
- The WiFi variant **cannot produce a signed image on this machine**: MCUboot's
  `imgtool.py` needs the `cbor2` Python module and `/usr/local/bin/python` has
  no such module. The default build, which is what ships, signs fine.
- That configuration sits at 98.8% of dram0 and 99.6% of dram1.

## Open, carried out of plan 0

- **A daemon that pings but never pushes usage will doze the panel with the
  owner present** (reinstalled, tokenless, or backed off after a 429). Escapable
  by a tap or the first real reading. Accepted deliberately: the alternative is
  Bug A verbatim. The honest fix is daemon-side and outside this plan — a daemon
  that cannot read usage should push a frame saying so rather than falling
  silent, which would keep the panel awake AND say why.
- `tests/ci/check_host_tests.sh` is **not concurrency-safe**: it builds into a
  fixed shared temp dir and `rm -rf`s it on entry, so two runs delete each
  other's files. Observed directly. Pass/fail is still sound (it comes from exit
  status) but any overlapping run's output is worthless.
- The `ok (N checks)` count greps for `^PASS`, and the `sleep_gate` and
  `usage_freshness` CHECK macros print only on failure, so both display
  `ok (0 checks)` while running real assertions. Harmless for those two;
  unhealthy as a convention, since a suite running zero assertions looks
  identical.


---

# Plan 0 is finished. Read this before starting plan 2.

## What the hardware proved, and what it did not

Flashed 2026-09-03 onto the plaintext board (MAC `20:50:0d:2c:f7:58`).

- **Bug C: VERIFIED.** Confirmed image, no daemon: one boot, zero resets, dozed
  at 60 s, still dozing at 220 s, woke when the daemon returned.
- **The owner has now LOOKED at the panel** and confirmed the pip row at two
  sessions (one waiting, one running), the wordmark with the clock under it, and
  the activity dot on the right. First eyes-on confirmation of the hint-line UI.
- **Bug A and Bug B: NOT verified on hardware.** Both pinned by host tests only.
  Bug A needs an overnight sleep or a temporary `SLEEP_ABSENT_AFTER_S=120`
  rebuild-flash-restore; Bug B needs a real five-hour window expiry. The owner
  is checking both the morning of 2026-09-04.
- **Six-pip legibility is still unjudged by any human.** Only two pips were ever
  live in front of the owner, and "do six read as six" is the question the whole
  pip design turns on.

## The defect hardware found that nothing else could

Dozing during an **unconfirmed test boot** starved the 30 s watchdog that
`ota_boot_pump()` feeds from `run_usb()`'s loop, because `ui_sleep_run()` blocks
that loop. Boot, doze at 60 s, `SW_CPU_RESET` at 90 s, forever -- it reproduced
the very reset this plan removes, as a loop. Task 8 made it reachable: the old
doze required `had_usage`, which implies a daemon talked, which is what disarms
the watchdog. Fixed in `53801cc` by folding `ota_test_boot` into `ota_busy`.

**Do not repeat my two wrong conclusions.** A directly-flashed image is
UNCONFIRMED: it reverts at 90 s by design, its explanatory printk is lost to an
unflushed UART (so you see a bare `SW_CPU_RESET` that reads like a crash), and
`boot_write_img_confirmed()` does not stick, so every boot re-arms a test boot.
Bench-test from an image re-signed with `imgtool sign --pad --confirm`. Take the
exact parameters from `firmware/build-sb/firmware/build.ninja`.

Also: `imgtool.py` runs under `/usr/local/bin/python`, needs `cbor` (installed
2026-09-03; `cbor2` wants Rust and fails). Without it **no signed image builds on
this machine at all**.

## Carried into plan 2, deliberately unfixed

- **The wire is effectively full: 509 of 512 bytes worst case.** Measured through
  `frame_to_usage`, the only caller that can put a line on the wire, with codex's
  `cli-state` src, `stale=False`, fractional reset stamps and a far-out reset.
  `tests/pc/test_protocol.py::test_the_widest_line_the_daemon_can_build_still_fits`
  is the guard. **Plan 2 must not add a usage field without re-measuring.**
  Per-model percentages (`sonnet_pct` etc.) can no longer coexist with
  `active_age_s`; they are unreachable today because `protocol.py` passes a
  literal `[]` for models.
- **A daemon that pings but never pushes usage dozes the panel with the owner
  present** (reinstalled, tokenless, 429-backed-off). Escapable by a tap. The
  real fix is daemon-side and belongs near plan 2: a daemon that cannot read
  usage should PUSH A FRAME SAYING SO rather than falling silent.
- **State-only frames stamp `observed_at = now_epoch`** (`claude_state.py:285`,
  `codex_cli.py:373`), so `active_age_s` can read ~0 for up to an hour after the
  last real write, delaying a doze. Errs toward staying awake. Untested.
- `select_pair` still sorts by `-observed_at`, so a fresher Codex reading can
  take the primary dial. Relevant to plan 2, which gives Codex a name.
- `tests/ci/check_host_tests.sh` is **not concurrency-safe** (fixed shared temp
  dir, `rm -rf` on entry). Never run two at once; a concurrent run's output is
  worthless. Pass/fail is still sound.

## The count that matters

**Nine assertions on this branch could not fail**, found across plan 0. Several
were written by the session that then reviewed them, and three were mandated by
my own plan text. Two plan steps told an implementer to prove a test with a
mutation that was algebraically an identity. The habit that catches it every
time: **build the broken variant and watch the test reject it** -- and check the
mutation can actually change behaviour before trusting a red.
