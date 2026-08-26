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
| Browser extension | ✓ | ✓ | ~ | — | — | — |
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

**Provider is encoded by geometry, severity by colour**, and that split is the
whole design. Colour is what the eye resolves from across a desk — green means
room, red means stop — so it cannot also be spent on saying which tool a number
belongs to. Ring position carries that instead, legible on a second look
without costing anything on the first. The inner ring is thinner as well as
smaller, so the primary provider stays the thing you read.

The inner ring is unlabelled; the bottom line names it once ("inner ring:
codex"). Repeating the tag on both gauges would say the same thing twice, and
the ring hollow is not wide enough for it anyway — see below.

Beyond two providers, `select_pair()` drops the third rather than rotating
through them. A ring that silently changes whose number it is showing is worse
than one that never shows it.

### The ring hollow

The countdown lives inside the ring, and the inner ring eats the space it lives
in. The usable hollow is the inner ring's diameter minus two walls, so the fix
for a countdown being sliced by its own gauge is counter-intuitive: make the
inner ring **bigger and thinner**. 88 across with a 6 px wall gives 76 px of
clear centre; 84 with an 8 px wall gave 68 px, for a ~70 px string. Pinned in
`tests/usage_layout/host_test.c` rather than left as a number someone will
helpfully shrink.

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

`n_sess`, `n_run`, `n_wait`, `n_stuck`, `n_agents` — and zeros are omitted.
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
5. Append it to `default_providers()`.

Nothing else changes — not the normalizer, not the protocol, not the firmware.
That is the property the whole structure exists to have.
