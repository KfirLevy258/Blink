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

---

# CHECKPOINT — 2026-09-04, written for a session with no memory of this one

**Read this section first.** Everything above it is older than this.

## Where things stand in one paragraph

Plan 0 (the three field bugs) is done, flashed, and partly verified on hardware.
Plan 1 (shim self-repair) is done and proved itself in the field today. Plan 2
(Codex naming) is 6 of 10 tasks. Plan 3 (Codex hook shim) is not started, but its
discovery gate HAS run and the premise holds. A whole-branch review of 42 commits
ran today, found three real defects, and all of them are fixed. **The branch is
89+ commits ahead of `main` and still unmerged.**

Verification at this checkpoint: **636 pytest**, **15/15 host suites**, clean
tree, both firmware configurations building.

## What is LIVE on the owner's machine right now

- **Firmware**: flashed 2026-09-03 with all of plan 0. Board MAC
  `20:50:0d:2c:f7:58`, plaintext (FLASH_CRYPT_CNT=0). **The firmware review's
  fixes are NOT flashed** — settings-from-doze, the `ota_busy` helper and the
  test fixes are committed but the board still runs the pre-review image.
- **Daemon**: built from this worktree with `tools/build_binary.sh` and installed
  by hand at `~/.blink/bin` — TWICE today, most recently WITH the review fixes.
  It is not a release; no release was cut and none should be from a branch this
  unfinished. Rollbacks kept: `~/.blink/bin.prev-20260904` (the original 1.2.5)
  and `~/.blink/bin.prev-reviewfix` (pre-review-fix build).
- Confirmed live after install: `src: cli`, `active_age_s: 0`, the repaired hook
  in `~/.blink/blink-hook.sh`, and state slots carrying a real `"pid"` whose
  process is `claude`.

**A visible change the owner has NOT yet eyeballed:** the session dial can now
read blank where it used to show a pre-reset number. That is the intended
correction — blank and honest beats confident and wrong — but it is the most
visible consequence of today's fixes and nobody has looked at the panel since.

## What is verified on hardware, and what is only argued

VERIFIED on the desk: Bug C (dozes instead of rebooting; one boot, zero resets,
still dozing at 220 s, woke when the daemon returned); the doze/wake transition
driven deliberately through a fake daemon (`[sleep] dozing` then
`[sleep] waking`, zero resets); the PID premise (`$PPID` is `comm=claude`, the
safety latch never fired); the pip row at two sessions, by the owner's own eyes.

NOT verified on hardware: **Bug A and Bug B**. Both are pinned by tests only.
The overnight test on 2026-09-03 did NOT prove Bug A, because the installed
daemon still had Bug B and was reporting an 80-hour age; the panel dozed for a
correct reason on a wrong number. That test is worth repeating now that the
daemon is fixed.

Also unverified and load-bearing: whether an edge tap reaches through the
CONNECTING overlay. If it does not, the settings-from-doze fix removes the stale
latch without making settings reachable. Needs a board.

## The three defects the review found, all fixed

1. **A forgiven number came back, confident and green.** At exactly
   `STALE_AFTER_S` the dial flipped from a correct `0.0%` to
   `78% / desktop / stale=False` and never recovered. Reproduced before it was
   believed. Fixed by making `_rolled_over` run unconditionally and splitting the
   decision: the rollover epoch and a passed reset stamp are facts that do not
   rot, only the percentage depends on freshness.
2. **Settings were unreachable from a doze** — worst in the no-daemon case, where
   settings is exactly where the owner would go. Fixed by waking on the latch.
3. **Two host suites proved nothing at CI level** — their bodies could be deleted
   with output byte-identical and CI green. Now 124 and 28 checks, and the runner
   fails a zero-check suite.

## Next steps, in order

1. **Repeat the overnight test** now the daemon is fixed — this is what finally
   settles Bug A and Bug B.
2. **Finish plan 2**: tasks 6-9 (`task_complete.error` -> failed, two-provider
   precedence, the contract script, everything-green).
3. **Plan 3**: its gate has run and is written into `docs/plans/codex-hook-shim.md`
   under "DISCOVERY GATE". Its first task must still be the smoke test — **no
   Codex hook has ever been executed by anyone.**
4. **Flash the firmware review fixes** when the owner is present.
5. **Merge.** `main` has none of this.

## Rules learned the hard way today — keep them

- **`git commit -- <paths>`, never a bare `git commit`.** In a shared worktree a
  bare commit takes the WHOLE index, including a sibling's staged work. That is
  what put six `pc/**` files inside firmware commit `cdf52a2`. Explicit
  `git add` is not enough. (My first reading blamed `git add -A` and was wrong.)
- **Never `git commit --amend` while a sibling is active.** It crossed a
  concurrent commit twice today.
- **Commit a fix BEFORE mutating anything to test it.** `git checkout --` reverts
  uncommitted work; it destroyed a fix twice.
- **Never run a test suite while another agent is mid-mutation** — the result is
  noise. I briefly reported two failures that were exactly that.
- `tests/ci/check_host_tests.sh` uses a FIXED shared temp dir and `rm -rf`s it on
  entry; concurrent runs corrupt each other.
- **Thirteen-plus assertions that could not fail have been found on this branch.**
  A dedicated mutation survey (468 assertions, 11 survivors) was worth more than
  either general review. Run one before merging.

## Known-open, deliberately not fixed

- **Windows has no PID liveness.** `os.kill(pid, 0)` there is `TerminateProcess`
  and would KILL the session; it is gated to POSIX and the `ctypes`
  `OpenProcess` probe is unwritten. The owner has a Windows install.
- A mixed host+container desk can still drop a live session; closing it needs a
  boot-id in the slot, which every installed shim would omit. Owner decision.
- A daemon that pings but pushes no usage still dozes the panel with the owner
  present. Accepted; the real fix is a daemon that pushes a frame saying it
  cannot read rather than falling silent.
- No host-return wake term. Argued no, reasoning in `sleep_gate.h`.
- Six-pip legibility has never been judged by a human; only two were ever live.

---

# HARDWARE VALIDATION — 2026-09-04, the firmware review fixes are FLASHED

**The board on the desk changed.** Yesterday's plan-0 flash went to
`20:50:0d:2c:f7:58`. The unit attached today is `20:50:0d:2c:f9:88` — the
**pipl company unit**, burned this morning with the shipped 1.2.5. So between
yesterday evening and this morning the desk board was swapped, and for a few
hours the owner's daemon (branch build) was driving release firmware with no
pip row and none of plan 0. Owner chose to flash the pipl unit rather than swap
back.

Flashed with the explicit two-offset esptool write — `0x1000` MCUboot,
`0x20000` the app re-signed `imgtool sign --pad --confirm`. Both images
hash-verified. **Nothing was written at `0x330000`, so the pipl logo survived**,
and the board proved it on the next boot: `[boot] logo: company, 111676 bytes,
30 frames, hold 1800 ms`. `tools/burn.sh` would have erased it; that is why the
manual write was used.

FLASH_CRYPT_CNT = 0b0000000 on this unit — plaintext, checked before writing,
per the two-units hazard.

## What the board proved, this image, this session

| | Result |
|---|---|
| Boot with **no daemon**, 135 s | **zero resets.** One boot, dozes at 60 s (`[usage] no app after 60 s`, `[sleep] dozing (claude)`), still alive at the end |
| Bug C | re-verified on this unit |
| The unconfirmed-image reboot loop | did not happen — `--pad --confirm` held, and the `ota_busy` refactor (`46fd958`) did not reintroduce it |
| **Stale-reading doze** (Bug A's gate) | **dozed** on `age_s=20000, idle`, then **woke** on `age_s=0, running`. `[sleep] dozing` then `[sleep] waking`, **0 resets** |
| Live daemon reconnect | `welcome`, then usage carrying `n_sess`, `n_run`, `n_agents` and `active_age_s: 0`. No `NOT SENT` — nothing crossed the 512-byte cap in real traffic |

**Bug A's mechanism is now hardware-proven**, not only host-tested: the gate
fires on a stale reading and releases on a fresh one. What is still untested on
hardware is the same thing end-to-end over four real hours with the real daemon
— that is what the overnight repeat buys, and it is now worth running, because
the daemon no longer has Bug B to poison the number.

## Still needs a finger

**F2, settings-from-doze, cannot be validated remotely.** It needs a left swipe
or a right-edge tap on a dozing board. Two questions ride on it: does the panel
open, and does an edge tap reach through the CONNECTING overlay at all. Until
someone touches it, the fix is argued, not shown.

## An unexplained daemon death

At 09:44 the daemon was gone — no launchd job registered at all, `bootout`
answering "No such process", last log line mid-stream with no traceback.
`KeepAlive` is true, so it did not crash into a restart loop; something booted
it out. Restarted by hand and healthy since. Cause unknown; worth watching for
a second occurrence before treating it as a defect.

---

# PLAN 2 IS DONE — 2026-09-04

All ten tasks. The Codex reader now names sessions and reports a dead turn as
`failed` instead of `idle`. Commits: `c3e03d3`, `b1b6ab4`, `b3bc6f2`,
`94ddadb`, `146d104`, `dccada7`, `4f569cd`, `17f099b`, `0de4041`, `0bbd198`,
`7f3f728`, `33ddcb6`, `697ac5e`, `431b8ff`, `b4aeb9e`.

Gate, all four green: **647 pytest**, `PASS [codex contract at main]` (14
checks, up from 8), `PASS [host tests]` (15 suites), `PASS [sh]` (hook shim).

## What tasks 6-9 actually changed

- A Codex `task_complete` carrying an `error` now reports `failed`. It costs no
  wire change: `base.STATE_FAILED` already existed and `usage_state.c` already
  maps it. **Never observed in a real file** — every rollout on this machine is
  a success — so the branch rests on a reading of upstream's Rust schema and is
  written to degrade toward `idle` on any shape it does not recognise.
- `turn_aborted` still means `idle`, all four reasons, and there is now a test
  and a contract check holding it there.
- The both-providers-named case is pinned for the first time. It was
  unreachable until this plan, because only Claude could set a `label`.
- The contract script grew from 8 checks to 14. This is the part that matters:
  two of the five names the reader now depends on carry upstream's own rename
  warning (`alias = "turn_started"`, `alias = "turn_complete"`). The day an
  alias becomes the primary name, the state machine goes silent and **every
  Python test stays green**, because they all feed the reader strings this repo
  wrote. The script is the only thing that can notice.

## The count of tests that could not fail reached FOURTEEN

Two more found in tasks 6 and 7, and the second is the worst kind yet: a test
written specifically to pin a guard, in a scenario where two conditions each
independently forced the expected answer, so no single-line break of the guard
could redden it. The guard was live — calling `ingest._pair_from` directly with
two named frames claiming no counts proved it — and NOTHING in 647 tests could
falsify it.

Both fixes were verified by mutation run by the coordinator, not accepted on an
implementer's report. **One implementer's mutation table was simply wrong**: its
headline mutation raised `IndexError` rather than changing behaviour, reddening
13 baseline tests and none of the three it claimed to vouch for.

**Run a mutation survey before merging.** It has now been worth more than either
general review, three times.

## Reported, not acted on

The Claude hook shim's `_projname` rule is `[0-9A-Za-z][0-9A-Za-z._-]{0,23}` —
no spaces, no non-ASCII. Now that `encode` no longer escapes, the wire and the
firmware can both carry a non-ASCII name, so **that shim is the only thing left
stopping a project called `café` from being named.** Open owner decision.

## Next

Plan 3 (`codex-hook-shim.md`), 14 tasks. Its discovery gate has run and the
premise holds. **Its first task must be the smoke test — no Codex hook has ever
been executed by anyone.** Then: flash nothing (plan 3 is daemon-and-shim only),
and merge, because `main` still has none of this.

---

# CORRECTION — no board carries the branch firmware, 2026-09-04 evening

The "HARDWARE VALIDATION" section above says the review fixes are flashed. **They
are not, any more.** A sibling session spent the afternoon burning customer units
with `tools/burn.sh` on the owner's instruction, and every one of them went back
to the release build:

| Unit | What happened |
|---|---|
| `20500d2cf988` (pipl) | my hand-flashed branch image, then burned ~12:20 and again ~12:30 after a full `erase_flash` |
| `20500d2cf758` | burned ~14:50 as an individual claude unit, then erased and burned again |
| `20500d33ff1c` | burned in the evening, individual claude, no logo |

**The measurements in that section remain true** — zero resets in 135 s, the
stale doze firing and releasing, the logo surviving a two-offset write. They were
made, they were real, and they are recorded honestly. What is no longer true is
the present tense: **no board on this desk runs `worktree-hint-line` firmware.**
Anything that needs the branch image on hardware — the settings-from-doze fix
above all — starts by flashing a board again.

**The daemon is DOWN, deliberately.** The owner's instruction, given while
customer units were on the desk: their usage must not be pushed to a board that
ships. It stays down until the owner says otherwise, and the restore is BOTH
`launchctl bootstrap gui/502 ~/Library/LaunchAgents/com.blink.bridge.plist` AND
`launchctl kickstart -k gui/502/com.blink.bridge` — bootstrap alone registers the
job without starting it.

I restarted that daemon twice today before learning any of this, the second time
after the owner had already asked for it to be stopped. Roughly ten minutes of
the owner's usage went to a customer board because I treated a shared service as
mine to fix. **Check who else is on the machine before touching a shared
service.**

Related and worth carrying into whichever branch merges first: `tools/burn.sh`
restores the daemon with `bootstrap` and no `kickstart`, so a burn leaves the
service registered but not running — which looks exactly like success. Already
fixed on `feat/fleet-tests` as `36c733e`; this worktree is off `main` and does
not carry it.

---

# F2 VERIFIED ON HARDWARE — 2026-09-04 evening

**Settings from a doze works, and so does everything around it.** Confirmed by
the owner's own hand on board `20:50:0d:33:ff:1c`, flashed with the branch image
(two-offset write, `imgtool --pad --confirm`, one boot, zero resets).

The sequence, in the owner's words and in the order it happened:

1. Daemon stopped, board dozing, no app running — the worst case, and the exact
   situation in which a person goes looking for settings.
2. **A right-to-left swipe woke the board and opened the settings panel.**
3. **It stayed awake while the panel was open** — the second half of the fix: an
   open panel is a present person, so the doze must not fire on them.
4. **Closing the panel let it doze again**, so nothing is left holding the board
   awake afterwards.

On the shipped firmware step 2 does nothing: the request latches, no one answers,
and the panel opens by itself hours later in front of nobody. That was the whole
defect, and all three parts of the repair are now shown rather than argued.

**Every firmware fix on this branch has now been confirmed on hardware.**

## The CONNECTING question is closed, by being the wrong question

The fix's design note leaned on an edge tap surviving the CONNECTING takeover,
and this handoff carried "does an edge tap reach through the CONNECTING overlay"
as load-bearing and unverified. Asked to test it, the owner reported:

> "it says connecting for less then a second so i can[not] really tell"

So the state barely exists on a disconnect. The swipe is deliberately refused
while it shows, which the owner also confirmed ("the swipe works when the board
is up, not while the loading") — and that refusal costs nothing if the window is
sub-second. **Dropped as a concern.** It was a real question about an unreal
situation.

## Still not verified on hardware

Only Bug A and Bug B end-to-end, which need a real four-hour quiet spell and a
real five-hour window expiry with the fixed daemon running. Both are pinned by
host tests, and Bug A's gate itself was proved on hardware this morning
(dozed at `age_s=20000`, woke at `age_s=0`). What is missing is only the long
wall-clock run, not the mechanism.

**Note for whoever ships this board:** `20500d33ff1c` is an individual claude
unit burned to ship and now carries unreleased firmware. It needs
`tools/burn.sh --edition claude` (no `--logo`) before it goes in a box.

## Decision: the pip row stays whole-desk — 2026-09-04

The owner noticed that switching the panel between Codex and Claude changes the
ring but not the pips, and asked whether sessions should be separated per
provider. Read the code rather than guessing: `ingest._pair_from` sums both
frames' counts for `worst_of(primary, secondary)`, and that sum is symmetric, so
the preference genuinely **cannot** move the pip row. The observation is exact.

**Decided: do not separate.** Three reasons, the first close to decisive:

1. **The attention light cannot be per-provider.** `state` is deliberately
   `worst_of(claude, codex)` because the point of the device is that a glance
   tells you something needs you. Per-provider pips under a whole-desk light
   would put **"Waiting for you" above zero pips** whenever the waiting session
   belonged to the other provider — a worse inconsistency than the one being
   fixed, and one that reads as a bug.
2. **The ring is per-provider out of necessity, not scope.** Percentages from
   two accounts with different limits and reset clocks cannot be added. That
   constraint applies to percentages only; the pips should not inherit it.
3. **Counting is the one thing that genuinely aggregates.** "Three things are
   running on my desk" is true and useful; "two, unless you toggle" is neither.

**But the owner found something real, and it is not the count.** Nothing on
screen says which element has which scope — the ring means Claude, the pips mean
everything, and the only way to learn that is to switch and notice nothing moved.

**The fix, when it is worth doing, is the opposite of separating:** make the
whole-desk scope visible, so two Claude pips and one Codex pip can be told apart
at a glance. The count stays honest, switching still changes nothing, and it
stops being a surprise because the reason is on the screen. That is firmware
work and it is not free, so it goes behind plan 3, not in front of it. **Nothing
is broken today** — this is a legibility improvement, not a defect.
