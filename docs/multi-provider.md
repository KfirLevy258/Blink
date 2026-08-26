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

## 6. Execution state

Derived from events Claude Code already announces, not from process
inspection:

| event | state |
|---|---|
| `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `SubagentStop`, `PreCompact` | `running` |
| `Notification` | `waiting` |
| `Stop`, `SessionEnd` | `idle` |
| a `running` event, then silence past the threshold | `stuck` |
| anything unrecognised, or an hour of silence | `""` (dark) |

The hook shim records **an event name and a clock reading, and nothing else**.
Not the prompt, not the tool arguments, not the transcript path, not the
session id. The metadata-only promise is structural here rather than a matter
of restraint: there is nothing captured to leak.

`stuck` fires at 180 s, not the specified 60 s. A test suite, an npm install, a
docker build and a slow model response all routinely pass a minute while
healthy, and an alert that cries wolf on every build is one its owner learns to
ignore before the day it is right.

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
