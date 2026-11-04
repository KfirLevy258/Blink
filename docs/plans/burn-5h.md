# Five-hour burn view

Decided 2026-10-31, after Anthropic removed the five-hour limit for verified
Ultra accounts (research: `docs/research/claude-ultra-no-session-window.md` on
the research branch, and the artifact linked from it).

## When it shows

Only when the account has no session window. The daemon already decides that:
`NormalizedUsageFrame.no_session_window` on the merged claude frame, set by
`pc/session_verdict` on positive evidence only, never because data is quiet.
Proof of no window is one of three things. Claude Desktop: two consecutive
history samples less than one idle poll apart (`DESKTOP_RISE_MAX_GAP_S`,
1000 s) where `sd` rose strictly and NEITHER sample has an `fh` key. Claude
Code: sustained absence, replies (a session's `cost.total_cost_usd` rose
against a render of that session at most half an hour old) whose status
lines carried a usable `seven_day` and no `five_hour`, each at most an hour
after the one before, the first and the latest at least 5 h 15 min apart,
with no `five_hour` seen from any session in that span
(`claude_cli._reply_without_window`). A session's cost falling, an mtime or
the daemon's clock going backwards starts the chain over. A short gap in
the polling does not: until the third review of 2026-11-01 one over five
minutes did, and an Ultra desk with its lid closed for ten minutes was not
marked that day. A long one still does (the fourth review): replies
rendered while nothing polls are not seen, and the first rise after the gap
is against a baseline older than `BASELINE_MAX_S` and not counted, so the
next counted reply comes over an hour after the last. A desk replying every
two minutes was proved on time after 57 minutes unplugged and three hours
late after 58; replying every ten, the edge is about 50. Nor is the hour counted from the last reply a tally kept
any more (there were three replies, 25 minutes apart, which the span
already implied): a 36-minute silence broke the chain. The chain is in
memory, so a restarted daemon starts it again. Claude Code on the Ultra
plan (owner's request, 2026-11-01: Ultra desks switch on the first reply
again): when Claude Code's `.claude.json` names the plan -- `oauthAccount.
planDisplayName` "Ultra", or with no `planDisplayName` an
`organizationRateLimitTier` ending in `max_20x`, the plan's name before the
rename -- ONE such reply is proof (`statusline_ultra`, `pc/claude_plan`).
Only those two fields are taken from the file, at most once a minute and
only when it has changed. Pro, Max 5x, any other plan, and a file missing,
unreadable, corrupt or over 8 MB keep the sustained rule.

That replaced, on 2026-11-01, two replies 25 minutes to 4 h 35 min apart,
after a re-review read Claude Code 2.1.316's bundle: the status line renders
on events, not every few seconds; `API_TIMEOUT_MS` bounds only the wait for
headers, so one stream across a reset can render weekly-only rises half an
hour apart; and `five_hour` leaves the line by the local clock, so a desk
running fast renders weekly-only replies at the end of every window. On Pro
and Max a weekly-only reply happens only in those stretches: within S before
a reset on a clock S fast, and the longest stream plus the half-hour
baseline after it. The proof rests on the replies themselves, not on seeing
the `five_hour` renders between them, which is why a poll gap costs the
safety nothing.
A chain inside one stretch spans less than 5 h 15 min, and the one-hour cap
keeps two stretches from joining. What is left marks a Pro or Max desk only
with its clock fast by hours: by three and a half less its longest stream
if its Claude Code use mid-window goes unseen (used only late in windows
opened elsewhere, or the board unplugged whenever it is) and some reply's
own render is written over before a poll, four less it if every reply's
render is seen; four and three-quarters less it to span one stretch; five
or more in any case. The cost is on Ultra where the plan cannot be read: a
desk is marked after about 5 h 15 min of Claude Code use, later if it
pauses for over an hour. Where it reads Ultra, the first reply marks it,
and the price is an unverified Ultra account (temporary window) marked by
a reply streaming across its reset until its next reply lifts it. The
earlier Desktop rule checked only the later sample, and a window closing
between two polls passed it. A single Desktop sample with no `fh` proved it
until the owner's first decision of 2026-11-01: it had been measured on Ultra
only, and a Pro or Max account idle for over five hours may write the same
sample. The samples must be one poll apart, not five hours, because usage can
land at the end of a window that then closes (`claude_desktop._no_window_rise`
has the example). The measured owner desk's pair (`{fh: 1, sd: 23}` on 10-29,
`{sd: 19}` on 10-31) is not Desktop proof under this rule; its Claude Code
replies are. Proof of a window, which clears the mark: a status line with
`five_hour`, dated when it was rendered, so any `five_hour` rendered after the
mark lifts it, even an old terminal's repaint (the safe direction); a Desktop
sample whose `fh` is a number; a Local Storage reading or reset stamp. The
evidence kinds were renamed with the rules (`statusline_sustained`,
`desktop_rise`; `statusline_ultra` is the fast path's own), so a verdict saved under the old ones is read as none and
decided again. The newest evidence wins. With none either way there is no mark, so Pro and Max never see this
view at a reset, a laptop asleep across one, or a fresh process. The verdict
is persisted in `~/.blink/session-window.json`, so a restarted daemon shows
the view at once and `blink status` agrees with the daemon. A Desktop-only
Ultra desk gets the view too, from its history alone, once a rise is seen;
its Code tab writes transcripts, and with none there is no burn line. On that page the usage
frame carries `session_pct` -1. If a window comes back, the mark goes and the
board goes back to the session gauge.

## What it measures

**Output tokens** (`message.usage.output_tokens`) from Claude Code
transcripts, over the trailing five hours, in 20 buckets of 15 minutes.
Output tokens, not the raw sum: on the dev desk cache reads were hundreds of
millions against a couple of million output tokens, and a number that size
means nothing to anyone. Claude Code only. Desktop and claude.ai chats leave
no usage on disk.

Buckets sit on epoch quarter-hours (`floor(ts / 900) * 900`). That is a
wall-clock quarter-hour in every time zone whose offset is a multiple of 15
minutes, which is all of them, including +5:30 and +5:45. So every full local
hour falls on a bucket's left edge, and the board can label hours.

## Wire contract: the `burn` line

```
{"t":"burn","v":2,"p":"claude","q":1793430000,"b":[0,0,4,12,...20 ints]}
```

- `v`: `PROTO_VERSION`, like every other line. The board reads `v` only on
  `welcome`.

- `q`: epoch seconds of the **newest** bucket's start (the quarter-hour the
  daemon's clock is in now). Bucket `i` (0 = oldest) starts at
  `q - (19 - i) * 900`.
- `b`: exactly 20 non-negative integers, oldest first: output **tokens**,
  exact, each capped at 99,999,999. They were thousands rounded up until the
  2026-10-31 review showed the rounding deciding the arrow at light use:
  2001 then 1999 tokens per quarter read "easing", and 1000 then 4 read
  "steady". The widest line is about 240 bytes.
- `p`: the provider the page belongs to: `"claude"`, or `"codex"` for a
  Codex account with no five-hour window (see "Codex" below). Lowercase letters, digits, `_` and `-`, at most 15
  characters: the board names a page by the first 15 of `usage`'s
  `provider` (`USAGE_TAG_MAX` in `firmware/src/usage_view.h`) and refuses a
  longer `p` whole rather than cutting it, so a longer id gets the session
  gauge, never another page's view. Until 2026-11-01 the board kept 11 for
  `p` while naming the page by 11 of 15, so an id of 12 to 15 characters had
  every line refused.
- Written through `protocol.encode_checked()`. It is its own line, not part of
  `usage`, which has no room for 20 more numbers (512-byte budget).

**Sent only to a board whose caps list `"burn"`** (`protocol.board_caps`,
the same gate `svc` and `theme` use). A 1.3.4 board never lists it, so it never
gets the line. The daemon sends it for each page whose merged frame has
`no_session_window`, claude and codex each on a cadence of its own
(`Bridge._burn_sent`, per provider), so one page's line never holds the
other's back. It stays out of the hello burst. It goes out on the first
tick after the 2 s quiet window, the way themes do, then whenever
`q` or `b` changes, and at least every 60 seconds as a heartbeat, at most once
every 15 seconds. When the flag clears it simply stops sending. There is no
"off" message.

## Board behaviour

- Add `"burn"` to `BOARD_CAPS` (append-only). `tests/pc/test_board_caps.py`
  measures the hello and pref lengths and must be updated with it.
- Parse `burn`. Keep `q`, `b[20]`, `p`, and the uptime it arrived at.
- On the page whose provider matches `p`: when that page's `s_pct < 0`
  **and** a burn line arrived within the last 180 seconds, the left half shows
  the burn view instead of the session gauge. Otherwise it shows exactly what
  it does today (`--%` for an unknown session). A `usage` with `s_pct >= 0`
  restores the gauge at once. The weekly gauge, header, pips, pill and rail do
  not change.
- Layout (320×240; left half centred on x = 80; the board's own fonts and
  colours):
  - total of `b`, formatted `0` / `412k` / `2.1M`: Montserrat 20, `COL_TEXT`,
    centred at about (70, 56);
  - direction glyph just right of it, at about (116, 57): `LV_SYMBOL_UP`,
    `LV_SYMBOL_DOWN` or `LV_SYMBOL_RIGHT`, Montserrat 16, `0x4387DF` when
    rising or easing, `COL_DIM` when steady; an en dash when idle;
  - `tokens · 5h`: Montserrat 14, `COL_DIM`, centred at (80, 76);
  - 20 bars, 5 px wide at a 6 px pitch, x from 20, baseline y = 148, tallest
    bar 56 px high, scaled to the largest bucket. A non-zero bucket is at least
    2 px. The newest 4 bars are `0x7FB2F0` and the rest `0x2D5C99`. There is a
    1 px baseline in `0x3A414B` from x 20 to 140;
  - a hour axis: for each bucket whose start is a full local hour (board clock
    with its UTC offset), a 3 px tick in `0x5C636B` at that bucket's left edge,
    and the hour number (24-hour, no leading zero, `9`, `10`, `14`) in
    Montserrat 14, `0x6B7280`, centred under the tick at y ≈ 160;
  - caption `BURN 5h` where `SESSION 5h` sits (y = `GAUGE_NAME_Y`, 168), and
    under it, where the countdown sits (`GAUGE_CD_Y`, 186):
    `<last hour>/h · <word>`, for example `1.1M/h · rising`, in `COL_DIM`.
  - Every bar reads at least 3:1 on the ground as the panel draws it (RGB565),
    the 3:1 every graphic on the screen is held to (`tests/theme`), and the
    newest 4 stay at least 1.5:1 stronger than the rest. On Dark that moves
    the older bars from `0x2D5C99` (2.88:1) to `0x2F609F`, towards g1; on
    every other theme the bars are mixed from its own roles and walked up
    the same way (`theme_burn_inks()` in `firmware/src/theme.c`, which says
    how). Added 2026-11-01, after the review measured 2.14 to 2.88:1 on five
    built-ins and no floor at all on a custom theme.
- Direction and the `/h` rate use **complete quarter-hours only**. The newest
  bucket (19) is the one the clock is in, still filling, and is drawn but not
  compared. The last hour is buckets 15-18 and the hour before is 11-14. Using
  bucket 19 as if complete made a steady workload read "easing" for the first
  9 minutes of every quarter-hour (review, 2026-10-31). Both zero is `idle`.
  Previous zero with the last hour above zero is `rising`. Otherwise more than
  +10% is `rising`, less than -10% is `easing`, and anything between is
  `steady`. The board computes it from `b`, so it costs no wire bytes.
- Every object is created once and hidden or shown. Nothing is allocated
  per frame. Measure flash and RAM before and after (the LVGL pool is
  32 KiB).
- Swipes, page morphs and the focus window must not leave a half-drawn mix of
  gauge and burn view. The session gauge's morph is the part most likely to
  fight this.

## Codex

Added 2026-11-01, for the owner's Codex Pro account, which has no
five-hour window either. Daemon only: the board already keeps a slot per
`p` (`firmware/src/burn5h.c`) and draws the view on the page whose provider
matches.

**The evidence is Codex's own, on every reading.** Each `token_count` event
in a rollout carries `rate_limits`. Measured over the owner's 195 rollouts
(counts only): 7969 readings had a `primary` of `window_minutes` 10080 and a
null `secondary`; the 122 before them, from when the account had a window,
had a 300-minute primary beside a 10080 secondary. `individual_limit` was
null in all of them, and the only `limit_id` other than `"codex"` was on
the null envelopes of an exhausted limit. `codex_cli._no_short_window`
marks a reading that has a usable 10080-minute window and no window of
`SESSION_WINDOW_MAX_MIN` (a day) or less, every window declaring its
length; any short window clears it; a reading with no `rate_limits` or only
nulls says nothing. It judges the reading the gauges show (the newest
usable one, `parse_rollout_tail`), and the word of the newest reading that
said True or False is the word of every Codex frame sent; a newer reading
that says nothing leaves it as it was, and a NaN or infinite window length
says nothing. The word is held in the provider's memory and never persisted:
every reading carries the evidence, and a restarted daemon reads the same
rollout.

**What it measures.** Output tokens per turn from those same `token_count`
events: each event's own `payload.info.last_token_usage.output_tokens`,
counted once per content key -- every figure of `total_token_usage` beside
every figure of `last_token_usage`. A copy of an event carries all twelve
unchanged, so a repeat in the same file (228 measured: 159 figure for
figure, 69 with another figure moved and a last output of 0), a file read
again from its start, and the same event in another rollout (2 measured,
the same session in two files) each count once, in the earliest
quarter-hour it is stamped in. The first cut took differences of the
running total, keyed by (path, total); the 2026-11-01 review found it
counted cross-file copies twice, made phantom rises of two interleaved
counters, and dropped a turn whose total came back to an earlier figure
after a reset. Reasoning is counted once: in all 8061 events with usage,
`total_tokens` was `input_tokens + output_tokens` exactly and
`reasoning_output_tokens` never exceeded `output_tokens`, so reasoning is
already inside `output_tokens`. Bucketed on the same 20 epoch
quarter-hours.

**Forks.** 27 of the owner's 195 rollouts were forks (`forked_from_id` on
the first `session_meta`, the parent's `session_meta` after it), and none
carried a `token_count` from before the fork or shared a content key with
its parent: a fork copies no usage on this desk. If a Codex version does,
the copies are recognised while the parent's events are inside the window;
a fork of a conversation older than the window would count its re-stamped
copies once. That, and two turns coinciding in all twelve figures, are the
residuals.

**How it reads.** `burn_reader.CodexBurnReader`, the Claude reader's
machinery with another root (`codex_cli.sessions_root()`, honouring
`CODEX_HOME`), needle (`"token_count"`), bridge.log sentences and `_entry`:
offsets, held-back partial lines, the 5 h 15 min cold-file cutoff,
anchors, `READ_TRIES`, the budget -- one budget per pass shared by both
readers -- and the de-dup. Of each line it reads `type`, `timestamp`,
`payload.type` and the token figures of `payload.info.total_token_usage`
and `payload.info.last_token_usage`, nothing else.
`tests/ci/check_codex_contract.sh` checks those fields upstream.

`blink status` prints `Session     no five-hour window on Codex (Codex
reported a weekly limit only)` when the merged Codex page is marked
(`blink status --wire`: the bus's own Codex page, so not for a Codex
reading the board was not given a page for).

## Privacy and safety (CLAUDE.md)

- The transcript reader parses `type`, `timestamp`, `message.id`, `requestId`,
  `message.model` and `message.usage` only. It never logs a line, a slice of
  one, or any value from one, at any level. Fixtures are generated by code.
- Resolve `~/.claude` and `CLAUDE_CONFIG_DIR` inside functions, never at
  import.
- Read only what's new: keep a byte offset per file, start over on truncation,
  hold back a partial last line, and ignore files untouched for more than
  5 h 15 min. De-duplicate on `(message.id, requestId)`, because streaming
  writes the same message more than once. Skip `<synthetic>` models.

## Done means

The daemon: tests green and the full suite run. The board: built, rendered
through `tools/panel_render` for review, then flashed with `tools/burn.sh` and
boot-verified on the bench board, showing the view on this desk's real Ultra
account.
