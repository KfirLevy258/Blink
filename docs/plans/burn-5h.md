# Five-hour burn view

Decided 2026-10-31, after Anthropic removed the five-hour limit for verified
Ultra accounts (research: `docs/research/claude-ultra-no-session-window.md` on
the research branch, and the artifact linked from it).

## When it shows

Only when the account has no session window. The daemon already decides that:
`NormalizedUsageFrame.no_session_window` on the merged claude frame, set by
`pc/session_verdict` on positive evidence only, never because data is quiet.
Proof of no window: a Claude Desktop history sample with a usable `sd` and
no `fh` key (measured on a verified Ultra account, 2026-10-31), or a Claude
Code reply that came back without `five_hour` (the same session's
`cost.total_cost_usd` rose between two weekly-only status lines). Proof of a window,
which clears the mark: a status line with `five_hour`, a Desktop sample whose
`fh` is a number, a Local Storage reading or reset stamp. The newest evidence
wins. With none either way there is no mark, so Pro and Max never see this
view at a reset, a laptop asleep across one, or a fresh process. The verdict
is persisted in `~/.blink/session-window.json`, so a restarted daemon shows
the view at once and `blink status` agrees with the daemon. A Desktop-only
Ultra desk gets the view too, from its history alone; its Code tab writes
transcripts, and with none there is no burn line. On that page the usage
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
