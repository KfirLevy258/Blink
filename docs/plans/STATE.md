# Where this programme stands

Written to disk deliberately: this is the handoff, and it must be readable by
someone — or some session — with no memory of how it got here. Update it as
things land.

Last updated: 2026-09-03, immediately before a context compaction.

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
| 1 | [`shim-self-repair.md`](shim-self-repair.md) | 6 | **all 6 committed**; one fix round in flight |
<<<<<<< Updated upstream
| 0 | [`field-bugs.md`](field-bugs.md) | 9 | written, not started |
=======
| 0 | [`field-bugs.md`](field-bugs.md) | — | being written |
>>>>>>> Stashed changes
| 2 | [`codex-naming-and-failure.md`](codex-naming-and-failure.md) | 10 | written, not started |
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
<<<<<<< Updated upstream
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
=======
  the connected-then-gone case. Keep the `!proto_host_seen()` guard — it is
  what prevents reboot loops against a live-but-slow daemon. Accepted cost: a
  board that could self-serve over WiFi will sleep instead.
>>>>>>> Stashed changes

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
