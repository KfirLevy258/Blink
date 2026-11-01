# Claude Ultra has no five-hour window

Research from 2026-10-31, kept as the reason behind the no-window rule and the
burn view. What was built is in `docs/plans/burn-5h.md` and the code it names;
where this page and the code disagree, the code is right.

## What changed

On 2026-10-30 Anthropic renamed its plans: Max 20x became **Ultra** ($200) and
Max 5x became **Max** ($100); Pro is unchanged. For *verified* Ultra accounts
the five-hour limit is removed. New Ultra accounts keep a temporary five-hour
limit until Anthropic verifies them from account and payment history, which
happens automatically. The weekly cap is unchanged on every plan. Sources are
secondary coverage so far; the Anthropic post itself has not been read:

- https://windowsreport.com/anthropic-renames-claude-max-5x-and-20x-plans-removes-ultras-5-hour-limit/
- https://cryptobriefing.com/anthropic-removes-five-hour-limit-ultra/
- https://www.datastudios.org/post/claude-max-20x-renamed-ultra-removes-5-hour-limit-verified-accounts

## What the data looks like (measured 2026-10-31, Claude Code 2.1.316)

On a desk with an Ultra account, the status line payload Claude Code writes
to `~/.blink/statusline.json` carries `rate_limits.seven_day`
(`used_percentage`, `resets_at`) and **no `five_hour` key at all**. It was
rendering as the session replied, so this is not the old "window expired
while nobody rendered" absence. (Claude Code renders the status line on
events -- token usage, a new assistant message, a mode change, the earliest
reset, prompt-cache expiry -- not on a timer: read from 2.1.316's bundle in
the re-review of 2026-11-01.)

## The plan cannot be read from local files

| Where | Field | Value on that Ultra desk |
|---|---|---|
| Keychain `Claude Code-credentials` | `claudeAiOauth.subscriptionType` | `max` |
| same | `claudeAiOauth.rateLimitTier` | `default_claude_max_20x` |
| `~/.claude.json` | `oauthAccount.organizationType` | `claude_max` |
| same | `oauthAccount.organizationRateLimitTier` | `default_claude_max_20x` |

None of these says "ultra", and even after a rename a tier string cannot tell
a verified Ultra account (no window) from an unverified one (temporary
window). Reading the keychain also means asking for a secret we do not need.

**So detect the window, not the plan.** The rule now in the code
(`pc/session_verdict`) marks the account on positive evidence only, and
quiet data proves nothing. Proof of no window is either of these:

- **Claude Code.** Sustained absence: replies (a session's cost rose
  against a render of that session at most half an hour old) with a usable
  `seven_day` and no `five_hour`, each at most an hour after the one
  before, the first and the latest at least 5 h 15 min apart, with no
  `five_hour` seen from any session in between. A short gap in the polling
  does not break it (the third review of 2026-11-01); one of about an hour,
  less two reply intervals, does, because replies rendered while nothing
  polls are not seen (the fourth).
- **Claude Desktop.** Two consecutive samples in `plan-usage-history.json`,
  less than `DESKTOP_RISE_MAX_GAP_S` (1000 s, one idle poll) apart, where
  `sd` rose strictly and neither sample has an `fh` key.

Proof of a window clears the mark: a status line with `five_hour` rendered
after the mark, a Desktop sample whose `fh` is a number (0 included), or a
Local Storage reading or reset stamp. The newest evidence wins.

The Claude Code half was two replies 25 minutes to 4 h 35 min apart until the
re-review of 2026-11-01, which read Claude Code 2.1.316's bundle and found
three premises false. The status line renders on events, so a ten-minute
silence was a pause, not a gap, and an Ultra user who read between turns was
never marked. `API_TIMEOUT_MS` bounds only the wait for headers, so one
stream across a Pro or Max reset rendered weekly-only rises half an hour
apart. And `five_hour` leaves the line when its `resets_at` passes the
*local* clock, so a desk running 30 minutes fast rendered weekly-only replies
in the last half hour of every window, and was marked every window. On Pro
and Max those stretches are the only place a weekly-only reply happens, and
a chain inside one spans less than five hours; the one-hour cap keeps two
from joining. That rests on the replies alone, not on seeing anything
between them, so a gap in the polling costs the safety nothing. A Pro or Max desk is
still marked if its clock is hours fast (about three and a half less its
longest stream, with unusual use; five or more in any case). A verdict saved under the rejected rules is read as none, because the
evidence kinds were renamed.

The Desktop half was first a single sample with `sd` and no `fh`, which is
what the measured Ultra desk wrote. But that was measured on Ultra only. A
Pro or Max account idle for over five hours has no window open and may write
the same sample, and it would have been shown the burn view, which is the
worst outcome. So after the 2026-11-01 review the owner required a rise:
usage that recent sits inside an open window on any plan that has one, so
the sample would carry `fh`. The gap was proposed as five hours. It is one
poll, because usage can also land at the end of a window that was already
open and then closes. Take a Pro window that opened at T-4h. Desktop takes a
sample at T-0.1h, the user chats at T and closes the app, and the window
closes at T+1h. When the app reopens at T+4h, `sd` has risen within five
hours, and there is no window. With the samples one poll apart, the only case
left is a window that closes within about 16 minutes of the usage. The cost
falls on the safe side: a Desktop-only Ultra desk proves it only while it is
being used.

The measured owner desk had `{fh: 1, sd: 23}` on 10-29 at 21:29 and `{sd: 19}`
on 10-31 at 18:29. That pair is 45 hours apart and falling, so it is not
Desktop evidence. Claude Code's sustained replies are what mark that desk.

The Desktop rise first checked only the later sample's `fh`. A Pro window
closing between two polls passed it: `{fh: 85, sd: 40}` four minutes before
the reset, the window's last usage, then `{sd: 41}` a minute after. So the
earlier sample must have no `fh` either (re-review, 2026-11-01).

Three earlier rules got this wrong, and review caught each one:

- Deciding per render, from fresh payloads only, brought the old frame back
  whenever Claude Code went quiet, and flipped the mark on renders with no
  `rate_limits`.
- A sticky latch fixed that, but held `--%` on every Pro and Max desk after
  every reset, until the next reply.
- Inferring no window from a weekly-only status line, once five hours had
  passed without seeing one, read quiet as Ultra. That happens on a laptop
  asleep across a reset, a 40-day-old status line read at start, or a fresh
  `blink status` (review, 2026-10-31).

## What the daemon did before the fix (wrong on Ultra)

A missing `five_hour` maps to -1 (`pc/statusline_source.py`, `_window`). Then
`pc/providers/claude_cli.py` `poll()` offers the last remembered payload that
had a session figure, and the normalizer may also take a Claude Desktop sample
from `plan-usage-history.json`. On the measured desk the newest Desktop sample
was two days old. So the session dial on an Ultra desk shows a leftover number
that no longer means anything.

### It also marks live data stale

`blink status --wire` on the Ultra desk, one second after Claude Code wrote
the status line, printed `"session_pct":1.0, "src":"desktop", "stale":true,
"age_s":136136, "active_age_s":0`. The board is fed live data and told it is
37 hours old. Cause: `pc/normalizer.py` `merge()` sets
`primary = session_src or weekly_src`, and `stale` and `age_s` follow
`primary`. A session source always wins when one exists, even a
two-day-old Desktop sample. Fix direction: when the fresh status line has no
`five_hour`, borrow no session reading from memory or from Desktop, and let
weekly be the primary.

### Wi-Fi mode: not a customer bug

In the 1.3.4 tree, `firmware/src/usage_parse.c:154` returns `-ENODATA` unless
both `five_hour` and `seven_day` are present. So a Wi-Fi-mode board on Ultra
would never connect. But that mode was built only with
`CONFIG_BLINK_WIFI_MODE`, which no shipped image enabled, and 1.4.0 deletes
it (commit `1e8abe5`). No board in the field runs this path, so nothing
needs fixing.

## Replacing the gauge: tokens in the last five hours

The owner chose a five-hour histogram of output tokens with a direction cue
beside the total, and real clock hours on the axis rather than relative
labels. Direction compares the last hour with the one before, with ±10%
counting as steady. It uses LVGL's built-in symbols, so no font is added. The
contract is `docs/plans/burn-5h.md`.

Claude Code transcripts (`~/.claude/projects/**/*.jsonl`, subagent sidechains
included) have one line per assistant message with `timestamp`,
`message.id`, `requestId`, `message.model` and `message.usage`:
`input_tokens`, `output_tokens`, `cache_creation_input_tokens`,
`cache_read_input_tokens`. Streaming writes the same message more than once,
so de-duplicate on `(message.id, requestId)`. A trial summation over the
trailing five hours on the dev desk read about 110 files and 1,600 unique
messages. The result was dominated by cache reads (hundreds of millions)
against a few million output tokens. Whatever is shown has to pick a
meaningful figure, not the raw sum.

Constraints for an implementation:

- Standard library only. Reading over a hundred files every poll is too
  much, so tail each file from a saved offset and keep a rolling per-minute
  bucket in memory.
- Transcripts are full conversation content. Parse `usage`, `timestamp`,
  ids and model only. Never log a line, and never write a fixture from a real
  transcript (CLAUDE.md).
- Resolve `~/.claude` (and `CLAUDE_CONFIG_DIR`, if set) inside functions, not
  at import (`tests/test_sandbox.py`).
- It covers Claude Code only. Desktop and claude.ai chats do not leave usage
  on disk in this form.
- The usage line has a 512-byte budget, which a loaded frame nearly fills, so
  the buckets travel as a line of their own (`burn`), written through
  `protocol.encode_checked()`.
- Codex rollouts already carry `token_count` events (`pc/providers/codex_cli.py`),
  so the same statistic could exist for both providers.

## Codex (checked in the same pass)

No change needed. OpenAI has had plans without a five-hour window since July
2026: Pro currently has none (https://learn.chatgpt.com/docs/pricing). Its
2026-10-29 change doubled Pro and Business Premium limits; the size changed,
not the shape. A weekly-only account sends one `primary` window with
`window_minutes: 10080`. `codex_cli._classify` sorts windows by length, so that
already lands on the weekly gauge and the session gauge reads `--%`. Gaps:

- No test covers "primary is 10080, secondary is null". Add one with a
  generated fixture.
- `plan_type` is in every snapshot (`pro`, `prolite`, `promax`, ...) and
  nothing reads it. It could break the tie if `window_minutes` ever went
  missing too, where the position fallback would put the weekly number on the
  session gauge.
- "Limit reached, show 100" applies to the session gauge only, so a
  weekly-only account that runs out shows its last weekly reading. Unmeasured.

## What was considered for the left half

A rate dial (rejected: a full arc reads as a limit that does not exist), the
histogram (chosen), text only (rejected: slowest to read across a desk), and
an activity ring of 20 segments (rejected: brightness is harder to read than
height).
