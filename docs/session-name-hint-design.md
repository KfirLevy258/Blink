# The hint line says what is happening, and to which project

**Date:** 2026-09-02
**Status:** design, not yet planned
**Scope:** `tools/blink-hook.sh`, `pc/providers/claude_state.py`, `pc/providers/base.py`, `pc/protocol.py`, `firmware/src/proto.c`, `firmware/src/usage_view.c`

---

## The problem

The top-right dot is painted from two inputs — data health and execution
state — and `refresh_dot()` takes the worse of them. The hint line beneath
it was supposed to say which of the two fired. It never did: `usage_view_set_status()`
switches on `data_health` alone, so every execution-state condition paints the
dot and writes nothing.

The result is that four of the seven conditions the panel can be in show a
coloured dot over a blank line:

| Dot | Hint | Condition |
|---|---|---|
| Amber, steady | *(empty)* | a turn finished and is waiting on you |
| Amber, pulsing | *(empty)* | a permission prompt is open |
| **Red, steady** | *(empty)* | **a session failed** |
| Green, pulsing | *(empty)* | working — fine, nothing to say |

A red dot with a blank line under it is the worst of these. It is
indistinguishable from the red that means "the host is gone", which *does*
get words. The commit that merged the two dots into one (`6540287`,
2026-08-26) named the hint line as the thing that would disambiguate them:

> They collapse into one indicator coloured by the worse of them, with the
> hint line — already there, already empty when all is well — saying which
> fired.

That half was never built. This design builds it, and carries a project name
so the line answers *which* session as well as *what*.

## What it will say

```
Working
Waiting for you - LiveClaudeUi
Waiting for you - 3 sessions
Session failed - Blink
Reading is old - showing last known      (unchanged)
Error - showing last known               (unchanged)
HOST LOST - numbers are frozen           (unchanged)
```

Data health keeps precedence. When the reading itself is in doubt the hint
says so and the execution state is not mentioned, for the same reason
`refresh_dot()` ranks them that way: a reading we cannot vouch for makes the
execution state moot.

Statuses are sentence case, and none of them contains a dash, because ` - `
is the separator.

| State | Text |
|---|---|
| `running` | `Working` |
| `waiting` | `Waiting for you` |
| `idle` | `Finished` |
| `failed` | `Session failed` |
| `stuck` | `Session is wedged` |
| `none` | *(empty)* |

`stuck` is included for completeness. The protocol still names it and no
provider produces it any more — see the `claude_state.py` docstring on why
inference from silence was removed.

## Which session gets named

**Named when exactly one session holds the winning state; a count when
several do.**

This is the rule, and it is a deliberate refusal to pick. The context row was
cut from this screen for precisely this failure:

> It showed one context window, and with several agents running there are
> several … "88% of 4" was an attempt to qualify one number into honesty and
> it did not earn its line.

A name presented while two other sessions share the state is that same
dishonesty. So: one session waiting gets a name, three sessions waiting get
`3 sessions`, and the panel never implies it knows which of them you meant.

> **Assumption, not a decision from the owner.** This rule was chosen by the
> author from the context-row precedent. If the preference is "always name the
> most recent", only `poll()` changes.

## Two constraints that shaped this

### 1. The usage frame has no room for a name

`protocol.py:331` records the measurement:

> this message was measured at 506 of `MAX_LINE_BYTES=512` once `age_s` and
> `p2_age_s` joined it. `proto.c` DROPS an over-long line, so those six bytes
> were the difference between a panel that updates and one that silently
> freezes.

`"proj":"LiveClaudeUi",` is 22 bytes. Adding it to the usage frame would push
a fully-loaded line past the limit and freeze the panel on exactly the busy
two-provider desks that need it most.

**So the name travels as its own message type.** `proto.c:609` ignores unknown
types, which makes a new one free on every board already in the field — an
older firmware sees an unrecognised `t` and drops it, and its hint line keeps
behaving exactly as it does today.

This also fits the data: the numbers change every poll, the project name
changes when you switch terminals.

### 2. The hint label will wrap into the gauges

`usage_view.c:625-630` creates the hint with a fixed width and **no long
mode**, so an over-long string wraps. `STATUS_Y` is 24 and `FONT_LINE_H` is
16, which puts a second line at y=40 — and `GAUGE_ARC_Y` is 44. The hint has
never exceeded one line because every string it could hold was a fixed
literal; a project name removes that guarantee.

`LV_LABEL_LONG_DOT` on the hint, with precedent at `usage_view.c:698` where
`provider_lbl` already does it.

## The pieces

### `tools/blink-hook.sh` — capture the directory name

The shim gains a second `sed` that extracts **only the final path segment** of
`cwd`. The full path is never materialised, never written, and never sent:
`~/Projects/AcmeCorp-Merger` yields `AcmeCorp-Merger` and the parent path is
discarded inside the pipeline.

Sanitised **in the pattern**, exactly as `_ident` already does for the session
id, and for the same stated reason — "there is no separate validation step
that can be forgotten or reordered". The value lands in a JSON string rather
than a filename, so the class must exclude `"` and `\` as well; a name that
fails to match is omitted entirely, and an absent key already means unknown on
both sides.

The state file becomes:

```json
{"event":"PreToolUse","t":1788000000,"name":"LiveClaudeUi"}
```

**The header promise is rewritten in the same commit.** The current text says
the cwd is not read, and after this it is. The replacement states what is
captured — event name, session id, agent id, project directory name, clock
reading — and keeps the existing list of what is still never read: the prompt,
the tool arguments, the transcript, the assistant's message. The comment's own
note that this is policy rather than structure stays; it is now carrying more
weight, and the honest thing is to say so rather than quietly widen it.

### `pc/providers/claude_state.py` — carry the name to the frame

- `_read_state()` returns the name alongside state and age. A `.state` file
  written by an older shim has no `name` key; absent is normal and reads as
  unknown, which is the convention the rest of this module already uses.
- `scan()` currently returns `{state: count}` and discards which session held
  which state. It gains a parallel `{state: [names]}` so `poll()` can tell one
  from several.
- `poll()` sets the frame's label only when the winning state is held by
  exactly one session **and** that session reported a name. Everything else
  leaves it unset, and the count already on the frame carries the meaning.

### `pc/providers/base.py` — a field on the frame

`NormalizedUsageFrame` gains an optional label. It defaults to unset so every
other provider — `claude_desktop`, `codex_cli`, `scripted` — is unchanged and
keeps working without knowing this field exists.

### `pc/protocol.py` — a new message

A `session()` builder emitting:

```json
{"t":"session","v":1,"label":"LiveClaudeUi","n":1}
```

`label` is omitted when there is no name — absent means unknown, the same
convention the usage frame uses for every optional key. `n` is the number of
sessions holding the winning state.

**Sent on change, and on reconnect.** The bridge compares the composed
`(label, n)` pair against the last one it sent and emits the message only when
it differs — but a board that has just booted holds nothing, so this must also
be re-sent on `hello`/`welcome`, the same way firmware currency is re-offered
on every connect rather than once per daemon lifetime. Without that, a board
replugged mid-session shows a bare status until the next time you happen to
switch projects.

The label is capped daemon-side at **24 bytes**. `STATUS_MAX_W` is 300 px and the sizing case
recorded in `usage_layout.h` is `"Reading is old - showing last known"` at 35
characters, so `"Waiting for you - "` leaves roughly 17 characters of budget.
The cap is a byte bound on the wire; `LV_LABEL_LONG_DOT` is what handles the
visual overflow, because character counts and pixel widths are not the same
question and only one of them can be answered on the daemon side.

### `firmware/src/proto.c` — parse it

One more branch in the type dispatch, storing into a bounded buffer and
calling the view. Bounds come from the buffer, not from trust in the daemon.

### `firmware/src/usage_view.c` — compose the line

Three changes:

1. **`usage_view_set_status()` gains an activity fallback.** Where the
   data-health switch currently yields `text = ""`, it composes from the
   execution state instead. Data health keeps precedence; this only fills the
   silence.
2. **`usage_view_set_activity()` refreshes the hint.** It calls only
   `refresh_dot()` today, so the label cannot track an activity change even
   once it has something to say. This is a real defect independent of the
   feature — with it unfixed, a session going from running to failed repaints
   the dot red and leaves whatever the hint last said.
3. **`LV_LABEL_LONG_DOT` on the hint**, per the wrap hazard above.

The suffix is composed on the firmware side from the facts the daemon sends —
a name, or a count — rather than the daemon sending finished prose. Every
other string on this screen is a firmware literal and they should not start
living in two places.

## Testing

- **`tests/ci/check_hook_shim.sh`** — already exists. Extend with the
  sanitiser cases the session-id extractor taught: a name containing a quote,
  a backslash, a slash, a space, a newline; `.` and `..`; an over-long name;
  a payload where a tool argument also carries a `cwd`, which is the exact
  shape of the first-occurrence bug already fixed for `session_id`.
- **`tests/pc/`** — `_read_state` with and without a name; `scan()` grouping
  names by state; `poll()` naming on exactly one and refusing on two; an old
  `.state` file with no name.
- **Protocol** — the new message round-trips; the label is capped; **the usage
  frame's byte count is unchanged**, which is the regression that would
  otherwise freeze panels.
- **`tests/usage_layout/host_test.c`** — the hint's long mode, so a future
  edit cannot silently reintroduce the wrap into the arcs.
- **On hardware** — per the standing rule that firmware work is not done until
  it is flashed and boot-verified. The case to watch is a long project name on
  a real panel at desk distance, which is the one thing none of the above can
  answer.

## Deliberately not in scope

- **Codex and desktop parity.** Only the hook path can produce a name. A
  Codex page will show a status with no name, and a desktop-sourced page shows
  neither. That asymmetry is real and is accepted here rather than papered
  over; `codex_cli` has a `cwd` in its rollout records and could grow the same
  field later, as its own piece of work.
- **Naming the most recent of several.** See the assumption above.
- **Anything about the dot itself.** Colour and pulse are unchanged. This
  design only fills the line under it.

---

# Addendum, 2026-09-02: the header regroups and the corner speaks

Requested after the design above was approved and Task 1 was already in
flight. It does not invalidate anything above — the hint line's behaviour is
unchanged — but it moves where that line sits and adds a second indicator.

## What changes

1. The clock moves out of the top-left and sits **under the brand**, centred.
2. Both gauges move **down**.
3. The freed top-left corner takes a **session status indicator**, in addition
   to the hint text, not instead of it.

## Why the corner is available now, when it was not

`6540287` removed the top-left execution pip and gave two reasons: two
unlabelled circles in one colour vocabulary, and a corner already occupied.
The second reason dissolves here. The clock vacates the corner, and the
first reason is answered by the hint line this design already builds — the
circles are no longer unlabelled, because the words underneath name the
condition.

So this is not a revert of `6540287`. It is the arrangement that commit could
not have, because the hint line did not yet speak for execution state.

## The vertical budget, and why the ring pays for it

Measured before designing. Below the arcs, nothing can move:

| Element | Now | After | Note |
|---|---|---|---|
| Rail | 226–232 | 226–232 | fixed by `RAIL_BOTTOM_OFF` |
| Pill | 204–224 | 204–224 | must clear the rail by 2 |
| Countdown | 186–202 | 186–202 | must clear the pill by 2 |
| Caption | 168–184 | 168–184 | must clear the countdown by 2 |

That stack is already at its stated minimum — the header records an earlier
pill attempt at 3 px of padding that "put it through the countdowns". So the
caption's top at 168 is a hard ceiling for everything above it, and the entire
cost of the new header row comes out of the arc.

**New arrangement, every seam on the 4 px rhythm the header mandates:**

| Element | Y | Height |
|---|---|---|
| Session indicator (top-left), age + dot (top-right) | 8 | 12 |
| Brand | 4 | 16 |
| Clock, centred under the brand | 24 | 16 |
| Hint line | 44 | 16 |
| **Arcs** | **64** | **100** |
| Caption | 168 | unchanged |

`GAUGE_ARC_SZ` goes **120 → 100**, and `GAUGE_ARC_Y` **44 → 64**.

> **Ruling: 100, not the 104 that was approved.** 104 lands the arcs at
> 64–168, leaving a 0 px seam against the caption, or forces a 2 px seam
> somewhere else in the header. 100 keeps every gap at 4 px and preserves the
> rhythm doctrine stated at the top of `usage_layout.h`. The difference is
> 4 px of ring diameter. Cost if wrong: a marginally smaller gauge than
> intended — reversible by taking 4 px back from the hint-to-arc gap.

`GAUGE_PCT_Y` moves with the ring, keeping its offset from the arc's top
(90 − 44 = 46, so 64 + 46 = **110**).

## The two indicators, and what each one means

| | Position | Says |
|---|---|---|
| Session indicator | top-left, `x=10, y=8` | **execution state** — running, waiting, finished, failed |
| Status dot | top-right, `x=−12, y=8` | **data health** — ok, stale, error, host lost |

`refresh_dot()` stops taking the worse of the two and each indicator reports
its own axis. The hint line's precedence is **unchanged**: data health still
speaks first and execution state only fills its silence, because a reading we
cannot vouch for still makes the execution state moot.

The colour vocabulary is shared, which was `6540287`'s objection. What answers
it is that the words are now there: a red top-left with "Session failed"
underneath cannot be read as a lost cable, and a red top-right with "HOST LOST
— numbers are frozen" cannot be read as a wedged session.

## Not in scope

- The rail, pill, countdown and caption. All four are pinned by the budget
  above and none of them moves.
- The dot's colours and pulse rules. Unchanged from `activity_color()`.

---

# Addendum 2, 2026-09-02: the pip row, and the header goes back

Requested after the branch was flashed and wire-verified. It reverses part of
Addendum 1 and replaces the single execution-state dot with one mark per
session.

## What changes

1. **The rings go back to 120**, and with them the whole header regroup:
   the clock returns to the top-left corner and the hint line to `STATUS_Y 24`.
2. **The execution-state dot becomes a pip row** — one mark per live session,
   sitting in the gap between the clock and the brand.
3. **The hint line stops carrying a session count.** `Working - 3 sessions`
   goes away; the pips say that now. The project name stays.

## Why the pips fit without a new row

Reverting the ring to 120 puts the arcs back at 44–164, which leaves room for
exactly two header rows — the old arrangement, where the clock owns the
top-left corner. That is where Addendum 1 put the execution dot, so the two
requests collide.

They stop colliding once the row is measured rather than assumed. On the old
row: the clock `12:04` at montserrat_14 runs from x=10 to about x=47, and the
brand `BLINK` is centred at 160 and about 47 px wide, so it starts near x=136.
**The 89 px between them is empty and always has been.** The pips go there.

    10        47              56 ────── 130      136        184    270  296
    |─clock──-|               |── pip row ──|    |── BLINK ──|     age  dot

With an 8 px gap either side the row has **75 px**, which at an 8 px pip on an
11 px pitch holds **7 pips** — one more than the threshold below needs.

## The rule

| Sessions | Shows |
|---|---|
| 0 | nothing. An empty corner is true; there is no session to describe. |
| 1–6 | one pip per session, grouped by state |
| 7+ | one pip per non-empty state, with its count |

Six is not a geometric limit — seven fit. It is where a row stops being read
and starts being counted, which is the opposite of glanceable.

**Order is fixed: failed, waiting, running, finished.** The eye lands
top-left first, so the leftmost mark is always the most likely to need you,
and colours never reshuffle as sessions change state.

**Counts mode holds three groups, not four.** A group is a pip, a 2 px gap and
its digits, with 7 px between groups — about 94 px for four, against a 75 px
budget. So the overflow rule is load-bearing rather than a safety net: **drop
`finished` first, then `running`**, so the worst case still shows the two
states that actually need you.

## Colours: three, and no new meanings

Red is `failed`, amber is `needs you`, green is `working`. Each pip reads
exactly like the old execution dot, because it *is* that dot, once per
session.

Waiting and finished share amber deliberately. The panel has already
established it cannot separate them at this size — `activity_color()` records
that steady-versus-pulsing "was not a difference anyone caught from across a
desk" — and a fill or shape distinction on an 8 px pip is a finer channel than
the one that already failed. If those two need telling apart, that is what a
sessions page would be for.

> **Ruling: `GAUGE_PCT_Y` stays an expression.** Addendum 1 made it
> centre-derived after the original offset-from-the-top proved wrong. Reverting
> the ring to 120 must reuse that expression rather than restoring the literal
> 90 — the expression yields 90 at `GAUGE_ARC_SZ 120` on its own, and keeps
> following any future ring change.

## Not in scope

- A sessions page. It remains the right answer for the full breakdown with
  words, and this is not it.
- Any wire change. `n_run`, `n_wait` and `n_stuck` already reach the board and
  are discarded; finished is `n_sess` minus the other three.
- Separating `waiting` from `finished` visually. See above.
