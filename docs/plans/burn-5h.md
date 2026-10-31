# Five-hour burn view

Decided 2026-10-31, after Anthropic removed the five-hour limit for verified
Ultra accounts (research: `docs/research/claude-ultra-no-session-window.md` on
the research branch, and the artifact linked from it).

## When it shows

Only when the account has no session window. The daemon already decides that:
`NormalizedUsageFrame.no_session_window` on the merged claude frame. The
status line marks a payload that has a usable `seven_day` and no `five_hour`
window, when the daemon has seen no five-hour window within five hours and ten
minutes of that payload's figures being fetched
(`ClaudeCliProvider._has_no_window`); a Claude Desktop source carrying a reset
time still ahead overrides it. On that page the usage frame carries
`session_pct` -1. If `five_hour` comes back, the mark goes and the board goes
back to the session gauge. Pro and Max do not see this view at a five-hour
reset, because the window that just reset is the evidence they have one. A
freshly started daemon has no such memory, so on a Pro or Max desk whose last
render came after a reset it shows the view until Claude Code's next reply.
Neither does a Desktop-only desk, which has no status line.

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
- `b`: exactly 20 non-negative integers, oldest first: output tokens in
  **thousands**, rounded up, so any activity at all shows as at least 1.
- `p`: the provider the page belongs to. Always `"claude"` for now; it leaves
  room for Codex.
- Written through `protocol.encode_checked()`. It is its own line, not part of
  `usage`, which has no room for 20 more numbers (512-byte budget).

**Sent only to a board whose caps list `"burn"`** (`protocol.board_caps`,
the same gate `svc` and `theme` use). A 1.3.4 board never lists it, so it never
gets the line. The daemon sends it while the claude page's frame has
`no_session_window`. It stays out of the hello burst. It goes out on the first
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
- Direction: the last hour (newest 4 buckets) against the hour before (the
  4 before that). Both zero is `idle`. Previous zero with current above zero is
  `rising`. Otherwise more than +10% is `rising`, less than -10% is `easing`,
  and anything between is `steady`. The board computes it from `b`, so it costs
  no wire bytes.
- Every object is created once and hidden or shown. Nothing is allocated
  per frame. Measure flash and RAM before and after (the LVGL pool is
  32 KiB).
- Swipes, page morphs and the focus window must not leave a half-drawn mix of
  gauge and burn view. The session gauge's morph is the part most likely to
  fight this.

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
