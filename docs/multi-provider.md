# The ingestion bus, as built

How usage gets from four places into two dials, and why each piece is shaped
the way it is. Read this before adding a provider, a source, or a field.

Implemented on branch `desk-hud-universal`. Companion to
`desk-hud-doc-gap-analysis.md`, which records what the handoff document asked
for and where this departs from it.

---

## 1. The shape

```
  Claude Code            Claude Desktop         claude.ai tab
  status line            plan-usage-history     (browser extension)
       |                        |                      |
  ~/.clauge/              app's own cache        POST 127.0.0.1:9877
  statusline.json               |                      |
       |                        |                      |
  ClaudeCliProvider   ClaudeDesktopProvider    ClaudeWebProvider
  ClaudeStateProvider           |                      |
       |                        |                      |
       +------------------------+----------------------+
                                |
                          IngestionBus            pc/ingest.py
                                |
                          normalizer.select()     pc/normalizer.py
                          - group by provider
                          - merge field by field
                          - one frame wins
                                |
                     protocol.frame_to_usage()    pc/protocol.py
                                |
                        NDJSON over USB-CDC
                                |
                          proto.c -> usage_view.c
```

Four sources, three of them for Claude. Adding a provider means writing one
class and appending it to `default_providers()`; the normalizer, the protocol
and the firmware learn nothing new.

## 2. NormalizedUsageFrame

The only type that crosses from provider code into the normalizer.

| field | meaning | unknown is |
|---|---|---|
| `provider` | `"claude"`, `"codex"`, … | — (required) |
| `src` | `"cli"`, `"desktop"`, `"web"` | — (required) |
| `observed_at` | when the DATA was written, not when we read it | — (required) |
| `session_pct` / `weekly_pct` | rolling-window usage | `-1.0` |
| `session_resets_at` / `weekly_resets_at` | absolute epoch | `None` |
| `ctx_pct` | context window fullness | `-1.0` |
| `model` | display name | `""` |
| `state` | `idle` / `running` / `waiting` / `stuck` | `""` |
| `stale` | can we vouch for this reading | `False` |

`-1.0` and not `0.0`, everywhere. `0.0` renders as a confident "0% used",
which is a stronger claim than "we don't have this number". The same sentinel
survives all the way to `msg_get_double`'s default in the firmware, so a value
crosses every layer without being re-encoded.

`observed_at` is the field most easily got wrong. Reading a two-day-old file at
noon must not make it look like a noon reading; every freshness and conflict
decision below rests on this being honest.

## 3. What each source can and cannot tell you

|  | session % | weekly % | resets | context | model | state |
|---|---|---|---|---|---|---|
| CLI status line | ✓ | ✓ | ✓ | ✓ | ✓ | — |
| Desktop cache | ✓ | ✓ | **✗** | — | — | — |
| Codex rollout log | ✓ | ✓ | ✓ | — | — | — |
| Browser extension | **✗** | **✗** | **✗** | — | — | — |
| Hook state file | — | — | — | — | — | ✓ |

No source is authoritative for everything. That table is the entire argument
for merging field by field rather than picking a winning source per poll — the
desktop cache has no reset timestamps at all, so letting it "win" because it is
newest would blank the only reset time anybody has.

Two source-specific traps, both discovered against real files and both absent
from the specification:

- **`samples[].t` in the desktop cache is MILLISECONDS.** Read as seconds it
  lands in the year 56649, every freshness check passes trivially, and any
  reading in history presents as current.
- **The desktop cache carries no reset timestamps.** Not "sometimes missing" —
  never present.
- **Codex's `resets_at` is SECONDS,** in the same daemon that has to read the
  desktop cache's milliseconds. Both are range-checked at the parser, which is
  the only place the unit is known.
- **`primary` / `secondary` in Codex's `rate_limits` are positions, not
  windows.** They are matched by their declared `window_minutes` (300 → the
  five-hour dial, 10080 → the seven-day one) and only fall back to position
  when that field is absent. Trusting the order would swap the two dials
  silently, which is the failure this whole document is written against.
- **The browser extension reports nothing, and that is a measurement, not a
  bug.** Driven against the real site on 2026-08-27 it observed 178 responses
  from `https://claude.ai/*` — a full message turn included — and none of them
  carried a rate-limit header of any spelling. It says so through
  `clauge status` rather than going quiet. See `docs/next-steps.md` section A.

## 4. The merge rule

For each field independently: **among the sources that actually have this
field, the freshest wins.**

**Context is the one exception, and it is not on this bus at all.** Several
agents mean several context windows and no single number is all of them, so
`ClaudeCliProvider` combines the sessions itself and takes the **worst** — the
fullest context is the one about to end somebody's turn. The count travels with
it as `n_ctx`, so the panel says "88% of 4" rather than letting one number pass
as the only one. That rule lives in the provider rather than the normalizer
precisely because it is not recency, and putting it here would have made the
normalizer's one rule into two.

The rejected alternative, kept as a test in
`tests/pc/test_normalizer.py::test_higher_does_not_beat_fresher_across_a_reset`:

> "Prefer the higher percentage, since no source can see claude.ai."

That holds within a window and inverts across a reset. A window that rolled
over a minute ago reads 0% from the fresh source and 90% from one taken just
before, and higher-wins would show 90% until the old reading aged out.
Understating a limit costs something; inventing usage that has already been
forgiven costs more.

Two consequences worth stating:

- **`src` names the source of the session percentage**, because that is the
  number on the largest dial. Labelling the panel with anything else would put
  a caption on a figure it does not describe.
- **`stale` follows that same source**, not the whole set. A fresh desktop
  percentage beside an hours-old CLI reset time is a live panel, not a stale
  one — and `secs_until()` already refuses a reset that has passed.

Providers are never merged into each other. Two providers are two accounts with
two separate limits; `select()` chooses between them (preferred first, then
freshest) rather than blending. What a genuinely two-provider panel should look
like is a hardware design question, not one the normalizer may answer by
averaging.

## 4b. Two providers, one pair of gauges

Each gauge draws a second, inner ring when a second provider reports.

**The arc is severity. A small ball at the end of the arc is the provider.**

Green under 60%, amber to 90%, red beyond — on the biggest element on the
panel, which is where the thing you read from across a desk belongs. Identity
is a second-look question, so it gets a disc the size of a fingernail at the
tip of the filled arc, in that provider's colour: Claude the brand's warm
orange, Codex a teal well clear of it, anything else a cool blue. The ball
also marks the value, since it rides the indicator's end.

This went through two worse versions first. Provider-by-ring-position was
unreadable; provider-by-arc-colour worked but spent the green/amber/red ramp,
the single most useful thing on the screen, on something a dot can carry.

LVGL draws the ball as the arc's KNOB part, which these gauges used to delete
outright on the grounds that a readout is not a control. It still is not — the
arc stays unclickable — but the knob is the only part that tracks the
indicator's end.

Colour follows the provider's **name**, not its ring position. On a machine
running only Codex the outer ring is Codex and must not wear Claude's colour —
`select_pair()` makes whichever provider is present the primary, so ring
position says nothing about identity.

Each provider's own countdown sits under the gauge, in its own colour, so "how
long has each of them got" is answerable at a glance. With one provider the
single countdown re-centres; the alignment is recomputed on every render, not
fixed at build time, because a second provider can arrive and leave while the
board is running.

Beyond two providers, `select_pair()` drops the third rather than rotating
through them. A ring that silently changes whose number it is showing is worse
than one that never shows it.

### The ring hollow

The hollow holds both percentages — primary large, secondary small — and the
inner ring eats the space they live in. The usable hollow is the inner ring's
diameter minus two walls, so the fix when something is sliced by its own gauge
is counter-intuitive: make the inner ring **bigger and thinner**. 88 across
with a 6 px wall gives 76 px of clear centre; 84 with an 8 px wall gave 68 px.
Pinned in `tests/usage_layout/host_test.c` rather than left as a number someone
will helpfully shrink.

This is also why the countdowns are no longer in there. One fitted; two did
not.

## 5. The wire

Additive fields on protocol v2. **`PROTO_VERSION` did not move, and should
not.** `pc/version.py` sets the rule: the version is a floor that refuses, so
bumping it stops every deployed board being offered updates — over the same
link the update travels on. That is not a mistake that can be corrected
remotely.

New keys: `provider`, `src`, `ctx_pct`, `model`, `state`. Unknown values are
**omitted, not sent as sentinels**, because of the budget below.

### The 512-byte cliff

`proto.c:367-371` does not truncate an over-long line. It drops it:

```c
} else if (line_len < LINE_MAX - 1) {
        line[line_len++] = (char)c;
} else {
        line_len = 0; /* overflow: drop the line */
}
```

No error, no partial parse, no sign on the panel — the board simply stops
updating while the daemon reports success. `protocol.encode_checked()` refuses
to write a line the board could not receive, and
`test_the_real_capture_fits_the_board_line_limit` pins the real payload against
the limit. A realistic message today is ~300 bytes. **Every field you add
spends this budget.**

## 6. Execution state, sessions and agents

Derived from events Claude Code already announces, not from process
inspection:

| event | state |
|---|---|
| `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStop`, `PreCompact` | `running` |
| `Notification`, `PermissionRequest` | `waiting` |
| `Stop`, `SessionEnd` | `idle` |
| `StopFailure` | `failed` |
| a `running` event, then silence past the threshold | `stuck` |
| anything unrecognised, or an hour of silence | `""` (dark) |

`failed` earns its own state because `StopFailure` runs instead of `Stop` when
a turn dies on an API error and carries `error: "rate_limit"` among its causes.
On a usage gauge that is the headline, not a detail — which is also why
`worst_of()` ranks it above `stuck`.

`stuck` fires at 180 s, not the specified 60 s. A test suite, an npm install, a
docker build and a slow model response all routinely pass a minute while
healthy, and an alert that cries wolf on every build is one its owner learns to
ignore before the day it is right.

### On disk

```
~/.clauge/state/<session_id>.state      one JSON slot, newest event wins
~/.clauge/state/<session_id>/<agent_id> one empty file per live agent
```

One file per **session** because a single global slot silently misreports the
moment a second terminal exists — two sessions overwrite each other and the
panel confidently shows the wrong one.

One file per **agent** because that makes the count exact without a lock. Two
agents starting at once cannot race on a shared counter, and `SubagentStop`
carries `agent_id`, so a stop removes precisely the agent that stopped rather
than decrementing and hoping. Both are swept by mtime after an hour, for the
sessions that die without `SessionEnd` firing.

### What the shim captures

An event name, a timestamp, a session id and an agent id. **Nothing else** —
no prompt, no tool arguments, no transcript path, no cwd, no message text.
`check_hook_shim.sh` asserts the other payload fields never reach disk.

The ids are a real widening. The first version captured an event name and a
timestamp, which made the metadata-only promise *structural* — there was
nothing there to leak. It is now a policy, and the reason for accepting that is
that the single slot was wrong the moment a second session existed.

The session id becomes a **filename**, which makes it the only attacker-shaped
input on a path in this product. The character class in the shim's extraction
pattern is the sanitiser rather than a separate validation step that can be
forgotten or reordered: a value containing a slash or a quote simply fails to
match and falls through to `unknown`. Traversal and injection are both pinned
in CI under sh, bash and dash.

### Counts, not a list

`n_sess`, `n_run`, `n_wait`, `n_stuck`, `n_agents` — and zeros are omitted. The
second provider adds `p2`, `p2_session_pct`, `p2_weekly_pct`, `p2_s_in_s`,
`p2_w_in_s`, all absent until a second provider reports. Those last two are
deliberately short: the fully-loaded line is close enough to the limit that
spelling them out would cost more than they carry.

Three fields were removed rather than added. `models` never reached the
firmware usefully. `ctx_pct` and `model` went with the widgets that showed
them — see below.

### What the panel deliberately does not show

**Context window.** It showed one, and with several agents running there are
several, at different levels, belonging to conversations the panel cannot name.
`"88% of 4"` was an attempt to qualify one number into honesty and it did not
earn its line: knowing the fullest of four contexts is at 88% does not tell you
which one, and there is nothing to do about it from across the room.

**The model in use.** It answered a question nobody glances at a desk gauge to
ask.

Both removals bought space for the gauges, which are what the panel is for. A
useful consequence: the status line shim went back to capturing **no session
id at all**, because the only reason it ever needed one was per-conversation
context. The hook shim still records session and agent ids — multi-session
*state* is real and worth having — but the status line path is back to
capturing nothing but the payload Claude Code already computed.
A per-session array would blow the 512-byte budget at around four sessions,
taking the panel dark with no error on exactly the busy machine most likely to
have four. A busy machine measures 351 bytes; a typical one, 297.

The scalar `state` stays, computed as worst-of, so firmware that predates the
counts keeps working unchanged.

## 7. Self-healing

`install_statusline.drift_check()` restores the `statusLine` hook when
something wipes it. The rule that matters:

- **marker present, hook gone** → something wiped it. Put it back.
- **marker absent** → `uninstall()` removed it. That is intent, and it is never
  overridden.

A program that restores its own hook after being told to go away is not
self-healing. Three other refusals: it will not touch an unparseable
`settings.json`, it chains rather than clobbers a status line the user set
after installing, and after three restorations it says so and stops — a write
fight with another program on the machine is worse than a hook that stays
missing.

Polled on an interval, not watched. A file watcher would add a dependency the
daemon does not otherwise carry, for a fault measured in "since the last CLI
update" rather than in milliseconds.

## 8. Adding a provider

1. Subclass `base.ProviderParser` in `pc/providers/`.
2. Implement `get_provider_id()` and `poll(now_epoch)`; add `parse_cli_event`
   or `parse_cache_file` as the shape of your source warrants.
3. **Never raise.** The bus catches and disables a provider that throws, but a
   parser for an application we do not control is exactly the code that must
   not be able to stop a daemon whose job is keeping a board fed.
4. Return `-1.0` / `None` / `""` for anything you do not know. Do not guess,
   and do not return `0.0`.
5. Append it to `default_providers()`. **This step is not optional and it is
   the one that gets forgotten.** `IngestionBus.set_preferred()` refuses a
   provider that is not in that list, so a board whose settings screen offers
   "Codex" will announce that choice at every boot and have it declined, and
   `page_count()` on the firmware stays at 1 — a second page that exists in
   the firmware, in the protocol and on the settings screen, and never once
   appears. That was the state of Codex support until 2026-08-27, when
   `pc/providers/codex_cli.py` gave it something to report.

Nothing else changes — not the normalizer, not the protocol, not the firmware.
That is the property the whole structure exists to have.
