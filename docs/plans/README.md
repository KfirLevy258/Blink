# The plans, and the order they go in

Three plans, sequenced. Each produces working, testable software on its own —
that is why they are three documents and not one. Read this page before any of
them; it carries the reasons the order is what it is, which none of them can
state from the inside.

**Start at [`STATE.md`](STATE.md)** — it says what has actually landed. This
page says only why the order is what it is.

| # | Plan | Tasks | What it is for |
|---|---|---|---|
| 1 | [`shim-self-repair.md`](shim-self-repair.md) | 6 | Make the session-name feature actually reach the people who already own a board |
| 0 | [`field-bugs.md`](field-bugs.md) | — | The three faults the owner is living with: a 56-hour reading, a panel awake all night, a board that reboots when the PC is off |
| 2 | [`codex-naming-and-failure.md`](codex-naming-and-failure.md) | 10 | Give Codex sessions a project name, and let a failed Codex turn show as failed |
| 3 | [`codex-hook-shim.md`](codex-hook-shim.md) | 14 | Let a Codex session say it is waiting on a person |

## Why this order

**Plan 0 was added after the others and runs second, not first.** Plan 1 was
already most of the way through execution when these three bugs arrived, and
its Task 3 repaired the owner's own install in the process — stopping it
mid-flight would have thrown that away. Everything after Plan 1 goes to the
field bugs, because those are the ones being lived with daily. The Codex work
waits.

**Plan 1 first, because everything already built is invisible without it.**
Nothing rewrites `~/.blink/blink-hook.sh` after the first install. `blink update`
swaps the program directory and restarts the service; it has never touched a
shim. So every customer who upgrades the documented way runs a new daemon that
reads a `name` field against an old shim that never writes one. The board is
never named, and `blink status` reports "hooks installed (10/10 events)"
throughout, because the path exists and still runs — only its contents are
stale. Building more on top of a feature no customer can see is building on a
floor that is not there.

Plan 1 also deletes three assertions that cannot fail. That is not tidying: on a
branch where the green suite is the main evidence, an assertion that cannot fail
is worse than a missing one, because it reads as coverage.

**Plan 2 before Plan 3, because it is smaller and needs nothing new.**
Everything it wants is already in files Codex writes today: `cwd` on line 1 of
every rollout, and an `error` field on `task_complete`. No new component, no
config to register, no prompt for the user to answer.

**Plan 3 last, because it is the only one that can fail in a way we cannot
predict.** It depends on a Codex hooks system that ships in the installed binary
but that **nobody has ever executed** — not us, not anyone. Its first task is a
discovery gate and its last is a desk test, and nothing between them can be
called done until that test runs.

## What crosses between them

**Plan 1 owns the shim watchdog. Plan 3 adds to it.** Plan 1 adds a content
check that rewrites any shim under `~/.blink/` whose bytes have fallen behind
the daemon. When Plan 3 introduces a Codex shim in that directory, it must be
added to the same `shims=` tuple. Plan 3 argues *against* a watchdog over
Codex's own config, and that is a different thing: writing into `~/.codex`
unattended invalidates Codex's trust hash and re-prompts the user. Our files in
our directory, yes; another vendor's config on a timer, no.

**Plan 2 fixes a bug Plan 3 would otherwise inherit.** `protocol.encode` leaves
`json.dumps` at `ensure_ascii=True`, so a non-ASCII label reaches the panel as
literal `\uXXXX` text, truncated mid-escape — the firmware unescapes nothing
(`msg_parse.c:47`). It is unreachable today only because the Claude shim refuses
non-ASCII outright. It becomes reachable the moment a `cwd` becomes a label,
which is Plan 2's Task 1. Measured before accepting the fix: `ensure_ascii=False`
is never longer — worst case zero delta on pure ASCII, and a Hebrew label drops
from 67 wire bytes (which overran the firmware's 28-byte buffer) to 23.

**Both Codex plans count sessions, and must not double-count them.** Plan 2
counts from rollout files, Plan 3 from hook-written state. They de-duplicate on
the session id. Measured on four real rollouts: `payload.session_id` is on
line 1 of every one and matches the filename's UUID exactly, so the rollout side
needs no parsing at all. Whether the *hook's* id is spelled the same is still
desk-only — but half the comparison is now pinned, so a mismatch will say which
side moved. Watch for `context_window.window_id` on the same line: it shares the
first three segments of the session id and differs only in the last two.

## Decisions already taken, so no plan re-opens them

- **`codex exec` batch runs count as sessions** and get named.
- **The Codex hook installs automatically when Codex is present**, as Claude's
  hooks already do — not behind a flag. `blink install` must say plainly, before
  it happens, that Codex will ask the user to trust it once.
- **`turn_aborted` stays mapped to idle.** It carries a `reason`, and pressing
  Esc is `interrupted`. The current mapping is correct; the gap is elsewhere.

## What is still unknown

Listed here because it is the honest headline for the whole programme, and
because it all sits in Plan 3:

- Whether a Codex hook executes at all. Never done, by anyone.
- The trust prompt's wording and timing, and whether a `blink update` that moves
  the shim re-prompts.
- The hooks file's path and shape, and whether `config.toml` needs a pointer.
- Whether the hook's `session_id` matches the rollout's.
- Which event each of approve / deny / Esc produces — the waiting-clear hangs on
  it, and a waiting state that cannot be cleared is worse than none.

Plan 2 has one of its own: no `task_complete` carrying an `error` exists in any
rollout on this machine, because every session here succeeded. That path is
pinned to upstream's schema and the contract script, never to a captured file.
