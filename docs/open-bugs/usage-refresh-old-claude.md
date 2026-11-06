# The usage refresh reads an old Claude Code's "Unknown skill" as cost

Open, waiting on a decision. Found 2026-11-06 by the new `/usage` check in
`tests/ci/check_claude_version.sh`, on the `old` cell of the claude-versions
matrix.

## What was measured

In the throwaway Ubuntu VM (`test-machines/ubuntu-vm`), logged out, HOME a
fresh empty folder, the exact `pc/usage_refresh.command()`:

| Claude Code | exit | `result` | `num_turns` | `total_cost_usd` | `modelUsage` | `judge()` |
|---|---|---|---|---|---|---|
| 2.1.318 | 0 | (the usage answer) | 0 | 0 | `{}` | OK |
| 2.1.100 | 0 | `Unknown skill: usage` | **1** | 0 | `{}` | **COST** |

2.1.100 is the floor the README and the claude-versions job claim. It has no
`/usage`; every flag in `command()` is accepted. All token counts in its
`usage` object are 0 and `duration_api_ms` is 0, so nothing reached the
model.

## What a desk on such a version gets

Turned on (at install, or by the board's ask), the first run is judged
COST and the refresh turns itself off for good with `off_reason` "cost":
`blink status` then says the refresh was stopped for using quota, which it
did not. No quota is spent; the fault is the wrong reason, and a feature
that can never work being offered at all.

## The decision

`judge()` treats any turn as cost on purpose (the guard must never let a
real model call through). Options, not chosen:

1. Leave `judge()` alone and stop offering the refresh below the first
   version that has `/usage` (not yet bisected), from `claude --version`.
2. Read `num_turns` 1 with zero cost, zero tokens, empty `modelUsage` and
   `duration_api_ms` 0 as FAILED rather than COST. Loosens the guard.
3. Keep both as they are and only reword the "cost" reason.

Until then the `old` cell prints this as a note and passes; stable, latest
and the other platforms still fail on it.
