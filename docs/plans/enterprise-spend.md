# Enterprise spend seats, and Claude Code inside WSL

**Status:** design, revision 3 (2026-11-04), after two review rounds.
Nothing is built. Implementation plans:
`docs/plans/2026-11-04-spend-page-plan.md` (A) and
`docs/plans/2026-11-04-wsl-plan.md` (B, which builds on A's Tasks 1-2).

## The case

A Claude seat billed by usage (for example a usage-based Enterprise seat)
has no five-hour or weekly window. It has a dollar cap per calendar month,
set by the organisation, which Claude Code's `/usage` shows as
"$X / $Y spent". BLINK shows nothing useful for it today.

The first desk with such a seat also runs Claude Code inside WSL on Windows,
with the board plugged into Windows. The board's job is to warn before a
limit is hit, and for this seat the only limit is dollars.

## Scope

- **This design:** the spend page, fed only by files Claude Code already
  writes on the user's computer, and `blink install --wsl <distro>`. BLINK
  never starts Claude Code and never contacts Anthropic.
- **Later, not designed:** a way to refresh the figure automatically may be
  considered, only through a documented interface and after Anthropic's
  terms are re-checked.
- **The official route:** if Claude Code's status line starts carrying the
  spend for these seats (public request: anthropics/claude-code #105765),
  BLINK reads it there.

## Goals, non-goals, anti-goals

Goals:
- A spend page: "$X of $Y • 27d 5h" (the reset countdown), exact right after `/usage`,
  estimated in between, never frozen.
- Amber and red as the cap nears, red when it is reached.
- The five-hour burn view in dollars on a spend page.
- Claude Code inside one named WSL distribution, board on Windows.

Non-goals:
- Seat-based Team/Enterprise usage credits on top of an allowance (their
  status line already carries windows).
- Codex company plans (credits, not dollars).

Anti-goals:
- BLINK reading, storing or sending a login token or browser cookie, or
  calling Anthropic. Ever.
- Spending the user's quota, in the product or in its tests.
- Touching a WSL distribution the user did not name; starting WSL, or
  keeping it running, when the user is not using it.
- A 1.3.4 or 1.4.1 board drawing dollars as a percentage or cents as tokens.
- Logging spend figures, seat fields, account ids, paths inside WSL, or any
  text read from Claude Code's files (customers send `bridge.log` to
  support).
- Keeping raw account ids anywhere. Only a fingerprint (SHA-256 of the
  account and organisation ids, as `fp/enterprise` already does).

## What is on the user's disk

Measured on a real usage-based seat on 2026-11-03, and consistent with the
field descriptions Anthropic publishes for the same data
(Agent SDK reference, `SDKUsageReport.extra_usage`):

- **Claude Code's config** (`~/.claude.json`, or its configured location)
  holds `oauthAccount.seatTier` (`"enterprise_usage_based"` on such a seat)
  and `oauthAccount.billingType`.
- **After the user runs `/usage` in Claude Code** (in a terminal), the same
  file holds `cachedUsageUtilization: {fetchedAtMs, accountUuid,
  utilization}`, where `utilization.extra_usage` has `is_enabled`,
  `monthly_limit`, `used_credits`, `utilization` and `currency`. Amounts are
  minor units (cents for USD). Normal use does not refresh it; a `/usage`
  does. `accountUuid` may be absent.
  - Anthropic's descriptions: `is_enabled` is false "while usage credits
    can't pay for requests", which includes a reached cap; a null
    `monthly_limit` on Team and Enterprise must not be shown as unlimited.
- **The status line** gives the current session's estimated cost,
  `cost.total_cost_usd`, at each render. It carries no spend limit on such
  a seat (Anthropic's status-line docs: `spend_limit` appears only behind a
  Claude apps gateway).
- **Claude Desktop:** BLINK has no source for the figure from Desktop;
  `/usage` typed in its Code tab does not update the saved figure above.
- **The period** is the calendar month. The Claude apps gateway's
  spend-limit documentation resets monthly caps at 00:00 UTC on the 1st;
  whether a seat's usage credits use the same boundary is an owed
  measurement.

## Design

### 1. Recognising a spend seat

The page is a spend page while Claude Code's config says
`seatTier == "enterprise_usage_based"` or `billingType == "usage_based"`.
Only those seat fields decide entering and leaving spend mode. The cache
decides the figure, never the mode: `is_enabled: false` with a limit means
the cap is reached (draw it red, at or over 100%), not "not a spend seat".

- One parse of the config gives the Ultra plan, the seat, the account
  fingerprint and the cache (`pc/claude_plan.py`, extended). Every other
  reader, including `fp/enterprise`'s `AccountWatch`, takes its fields from
  that parse. Only these fields are kept; the rest of the file is not.
- A read that fails in a way worth retrying (file locked, half written)
  keeps the last good view; it never counts as "not a spend seat".
- On a spend seat the config is authoritative for the Claude page: windows
  readings from any Claude source (status line, Desktop, Local Storage) are
  not shown on it, and the no-five-hour-window rules (status line, Desktop,
  a saved verdict) never mark it.
- A cache whose `accountUuid` is present and differs from the current login
  is not used.

### 2. The figure

1. **The anchor:** `used_credits` and `monthly_limit` from the cache, at
   `fetchedAtMs`. Exact at that moment. If it was fetched before the current
   period began, it is treated as no anchor (count since the 1st, no
   limit, the hint). This is applied when the figure is built, so a daemon
   running across the 1st rolls over. A currency other than USD is no
   anchor; an absent currency is taken as USD (Anthropic's type allows it
   to be null), which is an owed measurement.
2. **The running count:** spend since the anchor, from the status line,
   kept only on a spend seat.
   - A session's first sighting sets its high-water mark; nothing is added
     (a resumed session restores an earlier total).
   - Later, only a rise above the session's high-water mark is added (two
     processes on one session alternate their totals). Since round 2 of
     the 2026-11-04 review (K1) a session keeps a mark per cost ledger,
     up to eight: a figure below every mark (a /login, which restarts the
     cost from zero under the same session id, or a process resumed at an
     older total) opens a new mark and adds nothing, and each later figure
     is counted against the highest mark at or below it.
     `pc/spend_count.py` gives the argument and its residuals.
   - Each addition is stamped with the previous sighting's time and the
     current one; an addition counts toward an anchor only if the previous
     sighting was at or after the anchor. A rise that straddles a `/usage`
     is dropped rather than counted twice.
   - Marks are kept until the period ends. The count and the marks are
     saved in `~/.blink/spend-count.json` with the account fingerprint; a
     different fingerprint at load starts from zero.
   - Rounding is in whole cents.
3. **Shown:** anchor + count. Exact when the anchor is under 15 minutes old
   and nothing has been added; estimated otherwise. No anchor this month:
   the count alone, estimated, with no limit (never "unlimited") and a hint
   to run `/usage` in Claude Code. The hint also shows when the anchor is more than a
   day old.

Known gaps, stated in the install note and the README:
- The status line is one file shared by all sessions and sampled every 2 s;
  a render overwritten before it is read can lose a session's last rise.
- Claude Desktop's Code tab, `claude -p` and Agent SDK use never run the
  status line; their spend appears only at the next `/usage`.
- Spend in Desktop chat, Cowork, the browser and other computers appears
  only at the next `/usage`.
- Claude Code's cost is its own list-price estimate and may differ from the
  bill.

### 3. On the board

Built on `fp/enterprise`'s firmware, announced by a `plan` capability.

- **Right half:** the month: a ring with the share, the dollars in the ring
  ("$314", in montserrat_20 when it fits the 76 px hollow, else
  montserrat_16), and "of $500 • 27d 5h" under it in a label widened to
  148 px on a spend page (bullet U+2022, which the fonts have). Estimated
  figures carry a "~" before the dollars. The ring uses
  the theme's arc colour; the rail uses severity.
- **Left half:** the dollar burn view: twenty quarter-hour bars of dollars,
  a total, an arrow, and the rate over the last hour. It travels on its own
  message (`sburn`), gated on a `spendburn` capability, never on the `burn`
  line.
- **Focus mode:** on a spend page the big gauge is always the share
  ("62%", or "--%" with no limit); the dim line reads "Spent $314 of $500"
  ("Spent ~$314 of $500" when estimated, "Spent ~$12" with no limit).
- **Status line on the board**, one at a time, in this order: "Spend limit
  used up" / "Spend limit almost used up", then the service status, then
  "Run /usage in Claude Code".
- On a spend page the weekly figure is not sent; no token burn line is sent.
- **Older boards** (no `plan` capability): the page goes with `-1` shares and
  shows `--%`; no burn or `sburn` line.

### 4. Claude Code inside WSL

**Never automatic.** BLINK does not look for distributions, read their
files or run anything in them unless the user names one with
`blink install --wsl <name>`. `blink install --wsl` alone lists names and
starts nothing. A plain `blink install` that finds no Claude Code on Windows
prints one hint: "Using Claude Code inside WSL? Run
`blink install --wsl <name>`."

- **The daemon stays on Windows** with the board (USB passthrough into WSL
  takes the port away from Windows and keeps WSL running).
- **Install** (the user is present, so starting the distribution is fine):
  - a probe in the user's own login shell reads the environment
    (`exec env -0`, which works in bash, zsh and fish), then finds `claude`
    and `CLAUDE_CONFIG_DIR` from it; nothing secret is stored (no proxy or
    certificate variables);
  - settings are merged on Windows with the same code as a native install,
    then written inside the distribution as the user, following symlinks,
    refusing if the file changed since it was read;
  - the shims go into the distribution; they write their output into a
    Windows folder the daemon owns (`%USERPROFILE%\.blink\wsl-<guid>`),
    named in `$HOME/.blink/out` inside the distribution;
  - the record (distribution GUID and name, Linux user and home, config
    folder) is saved; the config folder is the one found at install, so the
    background service, which does not see the shell's variables, uses the
    same one.
- **Reading:** the 2 s poll reads only the Windows folder. Claude Code's
  config, the transcripts and the drift check are read from the
  distribution only while the user is using it (the Windows folder changed
  in the last ten minutes) and the distribution is running, on a worker
  thread, never on the serial loop. The drift repair also runs on an idle
  desk every five minutes, only when `wsl -l --running` lists the
  distribution: a deleted shim or out file stops the folder ever changing
  again, so a repair gated on use would never come.
- **One home per page:** with a WSL home configured, the Claude page reads
  that home only, and `blink status` says the native one is ignored.

### 5. Wording and privacy

- `blink install` on a spend seat says what is read, what is kept, that the
  figure is estimated between `/usage` runs and may differ from the bill,
  and the known gaps. Nothing is sent.
- The README's privacy text and the install disclosure that today say the
  config is read only for the Ultra plan, and that the session cost is
  never saved, are updated: the config is read on every desk for the plan
  and the seat; on a spend seat the cache's spend fields are read and
  per-session totals are saved in `~/.blink/spend-count.json`.

## Owed measurements

On a real usage-based seat (read-only, nothing spent):
1. `oauthAccount.billingType` and `organizationType`.
2. Whether the cache's `accountUuid` is present.
3. `is_enabled` once the cap is reached.
4. Whether the status line on such a seat ever carries `rate_limits`.
5. The real period boundary: the cache before and after 00:00 UTC on 1 Dec.
6. Whether `currency` is ever absent in the saved usage.

On Windows with WSL, from the real logon task: listed in Plan B, Task 7.
