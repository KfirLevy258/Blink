# Usage refresh: keeping the figures fresh with Claude Code's own /usage

Status: built (tasks 1-8) and desk-checked (Task 9), 2026-11-05: macOS
unattended run (tools/desk/usage-refresh-mac.sh), board ask on a bench
touch-inject image, a logged-in Windows run, suites green on all three
platforms. Only a login with a 5-hour window is unseen (Open questions). Design agreed with the owner 2026-11-05. Plan:
`docs/plans/2026-11-05-usage-refresh-plan.md`.

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

- Logged out (2026-11-05, `CLAUDE_CONFIG_DIR` pointed at an empty
  folder): exit 0, `type "result"`, `subtype "success"`, `is_error` false,
  `num_turns 0`, `total_cost_usd 0`, empty `modelUsage` -- but the `result`
  is a session-cost summary, not usage, and no cache is written. So a
  zero-cost "success" does not prove a refresh: a run counts only if
  `fetchedAtMs` moved past the run's start.

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

After the service step, on macOS, Linux and native Windows (never for a WSL
home in this version):

1. Find Claude Code: `claude` on PATH, then `~/.local/bin/claude`
   (`%USERPROFILE%\.local\bin\claude.exe`), `~/.claude/local`, and on
   macOS and Linux `/opt/homebrew/bin`, `/usr/local/bin`,
   `~/.npm-global/bin` and the newest `~/.nvm/versions/node/*/bin`.
   Desktop's private copy is not used. While the refresh is on, `blink
   install`, `blink update` and `blink config usage-refresh on` record the
   path they found in `usage-refresh.json` (`exe`), because the daemon runs
   with the service manager's PATH (launchd's is
   `/usr/bin:/bin:/usr/sbin:/sbin`) and would not find a Homebrew, npm or
   nvm install on its own. The daemon tries the recorded path first, then
   the same search. When it finds nothing, `blink status` says "on, but
   Claude Code wasn't found".
2. Missing: say how to install it (the official command for the platform)
   and leave the refresh off. BLINK does not pipe a script into a shell.
3. Found: ask once

   > BLINK can keep your figures current when Claude Code isn't running,
   > by asking Claude Code's /usage now and then. It sends no messages and
   > uses no quota. Turn on? [Y/n]

   Default Yes. On a work login (a usage-billed seat, or an Enterprise or
   Team organization) the default is No and the prompt says `[y/N]`,
   because the employer's rules apply. A non-interactive install (stdin
   not a terminal) asks nothing and leaves it off.
4. The answer is saved; `blink config usage-refresh on|off` changes it later
   and `blink status` shows it. A re-install does not ask again once there is
   an answer.

### 1b. People who already have BLINK

Someone updated automatically never sees `blink install`, and BLINK never
runs the refresh without a yes, so an update leaves it off (the setting is
absent) and asks:

- **On the board**, for a board that lists the `ask` capability: a touch
  prompt like the update offer. "Keep figures fresh?" / "Blink can ask
  Claude Code's /usage now and then. No messages, no quota." Buttons
  "Turn on" and "No thanks". "Turn on" saves yes; "No thanks" saves no and
  it never asks again. Closed without an answer ("later", after a minute),
  it asks again the next day. It is shown at most 3 times, then stops: an
  ask counts only when the board answers it, since an answer is the one
  proof the box was seen. A box the board withdraws (the computer went
  away, or the board started to doze) answers nothing and counts nothing,
  and so does an ask the board could not put up within 10 minutes of its
  arrival (dozing, or another box up), which it drops rather than show to
  an empty room later. The day before the next ask starts only from an
  answer: an ask that got none was not seen, and the next may go out an
  hour after it, still only while someone is at the computer (a board
  dozing for want of Claude activity drops every ask, and a day's wait
  after each meant the people this ask is for were never asked).
  An answer is applied only while the setting is still absent: one given
  after `blink install` or `blink config` made the choice changes nothing.
  It asks only while someone is at the computer, only when Claude Code is
  installed, never on a work login (those are asked only in the terminal,
  default No), and never on a WSL home (not asked anywhere in this
  version).
- **In the terminal**: `blink update` and `blink install` ask the install
  question when the setting is absent; `blink status` shows "not set up"
  with the command to turn it on.
- An older board without `ask` gets the terminal paths only.

### 2. Daemon: when it runs (fallback only)

BLINK's existing sources come first. If any of them has given a figure for
this Claude login in the last 15 minutes -- the Claude Code status line in a
terminal, or Claude Desktop's sources (the Code tab, chat, Cowork) -- the
refresh does not run. It runs only when all of these hold:

- the setting is on and the Claude home is native (not WSL);
- the board is answering (`board_alive()`);
- the computer is in use: input within the last 30 minutes where the OS
  reports it (macOS, Windows); Linux has no stdlib source and counts as in
  use;
- no other Claude source has a reading younger than 15 minutes (the status
  line file's mtime, or the newest `observed_at` of any other Claude frame
  with a figure);
- the `/usage` cache (`fetchedAtMs`) is older than 25 minutes, so the dial
  is refreshed before it calls the figure stale at 30;
- BLINK's own last run started more than 25 minutes ago.

A spend seat follows the same rule: while Claude Code is in use the running
count already adds what each session costs, and the refresh only makes the
figure exact again once nothing else is moving it. After 3 failures in a row
it waits an hour between tries; after 24 hours of nothing but failures, and
at least 6 of them, it turns itself off.

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
  `CLAUDE_CONFIG_DIR` is set to the folder `blink install` recorded when
  that variable chose it, and removed when it did not: the daemon runs
  without the person's shell rc, and a run against another folder would
  refresh a cache BLINK does not read, or another login's.

### 4. The cost guard and the failure guard

Cost guard: if the JSON answer shows any model use -- `num_turns` above 0,
`total_cost_usd` above 0, or a non-empty `modelUsage` -- the refresh turns
itself off at once, saves the reason `cost`, logs one line and stays off
until the person turns it on again. `blink status` says
"off: Claude Code's /usage used the model".

Failure guard: an answer that is not that JSON, a zero-cost success after which `fetchedAtMs` did not move past the run's start (what a logged-out Claude Code gives; the cache's own `fetchedAtMs`, whatever its windows hold, so a seat whose windows are both null still counts), `is_error` true, a non-zero
exit, or the 60-second limit is a failure. 3 in a row stretch the spacing to
an hour; failures only, for 24 hours and at least 6 runs (two failures a
weekend apart are not a day of trying), turn it off with the reason `failing`
("off: /usage kept failing; run claude and /login, then turn it on again").
`blink config usage-refresh on` is a fresh start after either guard: the
reason and the failure streak are cleared, on disk and in the running
daemon, and only the spacing since the last run still holds.

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

See `docs/plans/2026-11-05-usage-refresh-plan.md`.

## Open questions

- What a logged-out or blocked Claude Code answers to `-p /usage` (plan
  Task 0); the failure guard is built to those strings.
- Whether a login with a 5-hour window fills `five_hour` in the cache
  (Task 0). Until seen, a null `five_hour` leaves the session dial unknown.
  Still unseen after the desk check (2026-11-05): the owner's login has no
  5-hour window on macOS or Windows, and the owner chose not to buy a
  second account. The weekly figure, read by the same code from the same
  object, refreshed on both. Shipped as "should work, not yet seen"; a Pro
  user can confirm with a command that prints only whether `five_hour` is
  present.

## Legal review (owner discussion, 2026-11-04/05; not legal advice)

What Anthropic's pages say:

- Claude Code's legal page allows "an end user signing in to the unmodified
  Claude Code binary with their own Claude subscription", and forbids
  developers to "collect, store, or intermediate Claude.ai credentials or
  session tokens". BLINK runs the unmodified binary and never touches a
  token.
- The Consumer Terms forbid access "through automated or non-human means,
  whether through a bot, script, or otherwise" except with an API key or
  "where we otherwise explicitly permit it", and harvesting data from the
  Services. A scheduled `claude -p /usage` is automated use; nothing
  explicitly permits it. That is the open risk.
- Anthropic's 2026 enforcement (January errors, February docs, April
  statement) was against tools that used subscription logins for model
  work. No public action against read-only usage monitors was found, and
  issue #81768 asking for a usage API got no staff answer.

What other open-source monitors do: most read Claude Code's token and call
the usage endpoint directly (riskier than this); CodexBar also runs the real
`claude` and sends `/usage`, which is this design.

Who carries the risk: mostly the customer (their account could be flagged
for automated use; a work seat answers to the employer), and the seller
through them. Hence: off unless the person answers yes, default No on work
logins, fallback only, a cost guard, and turning itself off on repeated
failure. Owner to also ask Anthropic through support/sales; shipping does
not wait for the answer.
