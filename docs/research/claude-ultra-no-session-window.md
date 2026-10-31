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
rendering every few seconds at the time, so this is not the old "window
expired while nobody rendered" absence.

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

**So detect the window, not the plan.** The rule that shipped
(`ClaudeCliProvider._has_no_window`): the newest payload carries a usable
`seven_day` and no five-hour window, and no five-hour window has been seen
within `SESSION_WINDOW_S` plus slack before it. Ultra never carries one. Pro
and Max carried one within the last five hours, the window that just reset,
so the post-reset render Claude Code writes a second after every reset does
not qualify. Two earlier attempts got this wrong and review caught both:

- deciding per render, from fresh payloads only, brought the old frame back
  whenever Claude Code went quiet and flipped it on renders with no
  `rate_limits`;
- a sticky latch fixed that but held `--%` on every Pro and Max desk after
  every reset, until the next reply.

The one remaining ambiguity is a freshly started process on a Pro or Max desk
right after a reset, which has no memory of the window that just ended. If
`five_hour` comes back, the gauge comes back.

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
