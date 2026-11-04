# Usage refresh: keeping the figures fresh with Claude Code's own /usage

Status: draft, 2026-11-04. Not built. The legal review (bottom) comes before
any code.

## Why

BLINK reads usage from files Claude Code already writes. The status line only
updates while a Claude Code session is open, so a person who works in Claude
Desktop or claude.ai in Chrome sees stale or no figures. A usage-billed
Enterprise seat sees exact dollars only right after someone types `/usage`.

All of that use counts against the same account limits, and Claude Code's
`/usage` reports those limits account-wide. If BLINK runs the person's own
Claude Code `/usage` now and then, every surface is covered: 5-hour window,
weekly window and monthly dollars, each with its reset time.

## What was measured (2026-11-04, this Mac, Claude Code 2.1.318, a Max login)

- `claude -p "/usage" --output-format json` ran the built-in command, not a
  prompt: `num_turns 0`, `total_cost_usd 0`, every token count 0,
  `modelUsage {}`, `duration_api_ms 0`. It cost nothing. Twice.
- The first run rewrote `cachedUsageUtilization` in `~/.claude.json`
  (`fetchedAtMs` moved to the run), the cache BLINK already reads for the
  spend anchor.
- With `--no-session-persistence --strict-mcp-config --mcp-config
  '{"mcpServers":{}}' --settings '{"disableAllHooks":true}'` the run left no
  transcript (only an empty `memory/` folder under `~/.claude/projects`).
  Run a minute after the first, it did not rewrite the cache; run again 5
  minutes later, it did (`fetchedAtMs` moved 332 s), still `num_turns 0`,
  `total_cost_usd 0`. So Claude Code reuses a recent answer for somewhere
  between 1 and 5 minutes; the flags do not stop the write.
- `--bare` cannot be used: it reads auth only from `ANTHROPIC_API_KEY` /
  `apiKeyHelper`, so a subscription login is not seen.
- The cache on this login had `five_hour: null` and the status line had no
  `five_hour` either: this account has no 5-hour window right now. A login
  with one is still to be seen (Task 0).

## Goals

- Fresh 5-hour, weekly and spend figures for people who never open a
  Claude Code terminal, as long as Claude Code is installed and logged in.
- Off unless the person says yes at install. Easy to turn off. Says what it
  does in one plain sentence.
- Costs nothing. If a Claude Code release ever makes `/usage` reach the
  model, BLINK notices on the first run and turns the refresh off by itself.

## Non-goals

- BLINK never reads, holds or sends a login token or cookie, and never calls
  Anthropic itself. Claude Code does the asking, with its own login.
- Not a way around limits, not faster than a person would reasonably check.
- No refresh inside WSL in the first version (it would have to start or keep
  the distribution awake, which the WSL design forbids).

## Anti-goals (a failure even if it works)

- Anything that sends a message to the model.
- Company MCP servers, hooks or plugins starting because BLINK ran Claude
  Code.
- Sessions appearing in the person's history, `/resume` list or Desktop.
- The `/usage` text, or any figure, in a log line.

## Design

### 1. Install: one-time setup (`blink install`)

After the service step, on macOS, Linux and native Windows:

1. Find Claude Code: `claude` on PATH, then `~/.local/bin/claude`
   (`%USERPROFILE%\.local\bin\claude.exe`). Desktop's private copy is not
   used.
2. Missing: print the official install command for the platform and stop
   this step (BLINK does not pipe a script into a shell for the person).
   Re-running `blink install` or `blink refresh on` picks it up.
3. Found: ask

   > BLINK can keep your usage figures current by running Claude Code's
   > /usage every 15 minutes while you're at the computer. It sends no
   > messages and uses no quota. Turn on? [y/N]

   Default No. A non-interactive install leaves it off.
4. Yes: run one probe (section 3). Not logged in: say "Run `claude`, then
   `/login`, then `blink refresh on`." Logged in: save `on` and say so.

`blink refresh on|off|status` changes or shows it later. `blink status` shows
one line: on/off, last run, last result. Uninstall removes the setting and
the working folder.

### 2. Daemon: when it runs

- Only while on, only while the board is connected, only while the computer
  is in use: skipped after 30 minutes without keyboard or mouse input where
  the OS reports it (macOS `HIDIdleTime`, Windows `GetLastInputInfo`; Linux
  has no reliable stdlib source, so Linux runs on the timer alone).
- Every 15 minutes, and skipped when `fetchedAtMs` is already younger than
  that (the person ran `/usage`, or Claude Code refreshed it).
- One at a time, on its own thread, never the serial thread. A 60-second
  limit; a run that overruns is killed.
- After 3 failures in a row, back off to hourly until one succeeds.

### 3. Daemon: how it runs

```
claude -p /usage --output-format json --no-session-persistence
       --strict-mcp-config --mcp-config {"mcpServers":{}}
       --settings {"disableAllHooks":true}
```

- cwd: `~/.blink/refresh/`, an empty folder BLINK owns, so no project
  CLAUDE.md, settings or trust prompt is picked up.
- stdin `/dev/null`; stdout read in full, stderr discarded; on Windows
  `CREATE_NO_WINDOW`.
- Environment: the person's, plus `BLINK_REFRESH=1` so BLINK's own
  status-line script and hooks ignore the run even if a setting slips.

### 4. The cost guard

The JSON answer must say `type "result"`, `num_turns 0`,
`total_cost_usd 0` and an empty `modelUsage`. Anything else, including an
answer that does not parse, counts as "may have reached the model":

- the refresh turns itself off and saves why;
- `blink status` and the board's error line say
  "Usage refresh turned off: Claude Code changed";
- it stays off until the person turns it on again.

The `result` text is never read for figures and never logged.

### 5. Reading the figures

A new source in `pc/claude_plan.py` / the Claude provider reads
`cachedUsageUtilization.utilization`:

- `five_hour` and `seven_day`: `utilization` (percent) and `resets_at`.
- the spend anchor, as today (`extra_usage`).
- `observed_at` = `fetchedAtMs`. The cache must carry the current login's
  `accountUuid`, as the spend anchor already requires; otherwise it is
  ignored.

It competes with the status line's frames by `observed_at`: newest wins.
No firmware change: the frames are the ones the board already draws.

### 6. Logging

Log lines name events only: `refresh ran`, `refresh skipped (fresh)`,
`refresh failed (exit N)`, `refresh off (cost guard)`. No figures, no text
from the answer, no paths with the person's name beyond what the log already
carries.

## Tasks

0. Measure (no code): a login with a 5-hour window fills `five_hour`;
   Windows and Linux give the same zero-cost answer; how long the reuse
   window is.
1. `pc/usage_refresh.py`: finding Claude Code, building the command, the
   cost guard, the saved setting. Unit tests with a scripted fake `claude`
   generated by the test.
2. The scheduler in the daemon: activity check per OS, interval, skip when
   fresh, back-off, timeout, thread.
3. The cache source: five_hour / seven_day frames from
   `cachedUsageUtilization`, account check, precedence by time.
4. CLI: the install question, `blink refresh`, `blink status` line,
   uninstall cleanup.
5. BLINK's own hooks and status-line script ignore `BLINK_REFRESH=1`.
6. Docs: README (what it does, how to turn it off), privacy section,
   CLAUDE.md rules (the "never call Anthropic" rule reworded to "BLINK never
   holds a token or calls Anthropic itself; the opt-in refresh runs the
   person's own Claude Code"), release notes.
7. Fleet scenario with the scripted fake: on/off, cost guard trips, stale vs
   fresh cache.

## Open questions

- Interval: 15 minutes is a guess; it trades freshness for load.
- Whether to offer it on usage-billed Enterprise seats by default
  differently (their admin may have rules about automation).

## Legal review

To be written with the owner before Task 1. See the discussion of
2026-11-04.
