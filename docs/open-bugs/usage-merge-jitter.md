# The five-hour dial stepped backwards by a rounding difference

**Status:** FIXED for the five-hour and seven-day jitter between the Claude
Code status line and Claude Desktop's cache. Reported, with the trace below,
in DevOceanIT's fork, where it was first fixed; the code here is our own,
written from a description of the rule and not from theirs. One weekly
symptom in the same report is NOT explained by this and is kept open at the
bottom.

## What was measured

Two sources report the same Claude five-hour window, and they disagree by a
fraction of a point:

- the status line (`pc/providers/claude_cli.py`, reading
  `~/.blink/statusline.json`), whose percentages are Claude Code's own;
- Claude Desktop's cache (`pc/providers/claude_desktop.py`), which is
  sampled every five minutes while the app is open.

Why they differ by a fraction is not established -- different rounding, or
the two fetches landing either side of a small amount of use -- and the rule
below does not depend on which.

The trace, condensed from a longer log to the four polls that show the
fault, one source per poll in arrival order:

    cli 39   desktop 40   cli 39   cli 41

and the panel drew

    39       40           39       41

for a window whose usage only ever rose. The third reading is the whole bug:
it was not a new figure. Claude Code rewrites `statusline.json` every time it
REDRAWS its status line, and a redraw writes the `rate_limits` block it
already had. The file's mtime -- the frame's `observed_at` -- said "Claude
Code is running", and `pc/normalizer._pick` ranked by it, so a repaint of
numbers fetched before the Desktop sample took the dial back from it.

Two things are therefore wrong at once, and either alone produces the
trace's shape:

1. **Recency was measured on the wrong clock.** The mtime dates the redraw,
   not the fetch.
2. **A disagreement was drawn as a fall.** Even ranked correctly, a genuine
   new fetch of 39.4 after Desktop's 40 is the same window seen by two
   sources, and within a window usage does not fall. The band is a whole
   point because that is the widest gap the trace showed.

## The rule

- Every frame has a `first_seen_at`: when its numbers were first seen. It is
  `observed_at` for every source except the status line, whose provider
  remembers the earliest mtime of each distinct `rate_limits` block. A redraw
  keeps the earliest time; an mtime EARLIER than the remembered one replaces
  it (a clock step, a restored file), so `first_seen_at` is never after
  `observed_at`. The memory holds at most 256 blocks, dropping blocks whose
  window has ended first and then the oldest, and dies with the process.
- For each of the two percentages separately, fresh frames rank before stale
  ones, and among them the newest `first_seen_at` wins.
- A fresh frame may keep the dial against that winner when it is at most
  `HOLD_BAND` (1.0 point) above it. Never a stale frame, never a frame the
  rollover rule already turned away, and never a frame from a different
  window: reset stamps more than five minutes apart, or a stampless Desktop
  sample taken before the stamped frame's window opened (its reset minus 5 h
  for the session, 7 d for the week). The last case was a hole found in
  review: Claude Code can fetch a new window without having seen the old one
  end, so no rollover is reported, and a Desktop 0.8 sampled a minute before
  the reset came back over the status line's 0.3. If stamps are rounded to
  the hour, a sample in the hour before the real reset still slips through;
  only rollover evidence closes that. A reset to 0, or any fall of 2 or
  more, is drawn at once.
- The caption is dated and sourced by the freshest fresh frame whose figure
  agrees with the number drawn to within the band. With Claude Code redrawing
  an unchanged 39 every minute and Desktop having sampled 39 once, Desktop
  wins the ranking and the status line still names and dates the dial: age 0,
  no "5 min ago" growing toward a stale mark.

`observed_at` and the wire's `age_s` keep their meaning, and nothing on the
wire changed. `tests/pc/test_usage_hold.py` replays the trace (39, 40, 40,
41) and the cases above.

## What the hold does not do

It delays a step back; it does not abolish one. A held frame stops holding
the moment it goes stale, and the dial then reads the winner. With Desktop's
40 sampled at 30 s and a live status line at 39, the dial reads 40 until the
sample is past the half-hour bound and then 39, at the first poll after
1830 s (1860 s with per-minute polls). That is one step back, half an hour
late, where the old merge drew one every time the two sources took turns.
The test pins it.

One judgement call worth knowing about. The panel's 600 s age caption and
1800 s stale mark never appear sooner than before while the number shown is
one the old merge would also have accepted, which is every case the trace
contains. When the new ranking shows a DIFFERENT number -- Desktop sampled 40
at 60 s after Claude Code fetched 38, and Claude Code has only redrawn since
-- the caption is dated by Desktop's sample, because that is the reading on
the dial. Dating it by the redraw would put a live caption over a number the
status line never reported. The same limit applies as above: once that
sample goes stale the dial falls to the redrawn 38, at 1920 s with
per-minute polls -- a real step back of two points, half an hour after
Desktop last spoke.

## Still open: a weekly swing of 73, 68, 76

The same report shows the seven-day dial reading 73, then 68, then 76, within
a short span. That is a 5-point fall and an 8-point rise, far outside any
rounding, so the hold correctly leaves it alone and this change does not
explain it.

What is known: both Claude sources report a seven-day percentage, and the
merge takes them field by field, so the weekly dial can come from a different
source than the session dial on the same frame. Candidates not yet measured:

- the two sources reading two different weekly windows. Desktop has no reset
  stamps, so `_same_window` can only place a Desktop sample by its time
  against the status line's window, and a merge without a status-line weekly
  stamp cannot tell them apart at all;
- a weekly figure that covers a different scope in each source -- all models
  in one, a single model's limit in the other;
- `pc/providers/weekly_anchor.py` or `pc/weekly_anchor.py` substituting an
  anchored figure on one of the polls.

Next step: a log, on the desk that shows it, naming the source of each field
on each poll -- the merged frame's `src` names only the session dial's
source. Any test made from it is built from a fixture generated by code,
never from a captured status line.
