# Desk HUD handoff doc vs. the code — gap analysis

> **Status, 2026-08-26: acted on.** Every gap below was implemented on branch
> `desk-hud-universal`, with four deliberate departures (rows 9, 11, 13, 14 —
> each argued in place). The as-built architecture is documented in
> [multi-provider.md](multi-provider.md); this file is kept as the record of
> what was found and why each decision went the way it did.
>
> See §5 at the end for the resolution table.
>
> **Superseded in one place, 2026-08-28.** Row 11's web extension bridge was
> built, measured against the real site, found to have nothing to forward, and
> removed along with its localhost listener. This file is a dated audit and is
> left as it was written; `next-steps.md` section A carries the finding.

Audit of the "Desk HUD — Universal Architecture, Multi-Provider Design & Technical
Handoff" document against this repository.

- Audited at: `42acc9e` on `post-release-cleanup`, 2026-08-26.
- Method: read of `pc/`, `tools/blink-statusline.sh`, `claude_usage_bridge.py`,
  `firmware/src/`, `firmware/Kconfig`, `firmware/CMakeLists.txt`, `firmware/wifi.conf`,
  `tests/fixtures/statusline_payload.json`. Generated build trees (`firmware/build*`)
  excluded.
- Status vocabulary: **Holds** (doc matches code) · **Partial** (some of it exists) ·
  **Absent** (nothing in the tree) · **Conflicts** (code does something incompatible
  with what the doc specifies).
- Cost is rough and relative: **Doc** (edit the document, no code) · **S** (under a day) ·
  **M** (a few days) · **L** (a new subsystem, a week or more, and a design decision first).

---

## 1. Summary

| # | Doc section | Claim | Status | Cost to close |
|---|---|---|---|---|
| 1 | §1.1 | Zero-credential / zero-trust footprint | Holds (default build) | Doc |
| 2 | §1.2 | Zero outbound AI polling | Holds | Doc |
| 3 | §1.3 | Pluggable multi-provider architecture | Absent | L |
| 4 | §1.4 | Zero-touch install | Holds | — |
| 4b | §1.4, §4A | Self-healing anti-drift watchdog | Absent | M |
| 5 | §1.5, §4B | Schema resilience, graceful degradation | Partial | M |
| 5b | §4B | Versioned parser adapters (`DesktopCacheV1/V2`) | Absent | M |
| 6 | §1.6 | Zero content inspection (metadata only) | Partial | S |
| 7 | §3A1 | CLI hook: `rate_limits` extraction | Holds | — |
| 8 | §3A1 | CLI hook: `context_window`, `model.display_name` | Absent | S |
| 9 | §3A1 | Transport: loopback UDP `127.0.0.1:9876` | Conflicts | Doc |
| 10 | §3A2 | Desktop app cache (`plan-usage-history.json`) | Absent | M |
| 11 | §3A3 | Web extension over `ws://127.0.0.1:9877` | Absent | L |
| 12 | §3B | Codex / future provider onboarding | Absent | L |
| 13 | §5 | Live state machine (idle/running/waiting/stuck) | Absent | L |
| 14 | §6 | Serial protocol frame | Conflicts | Doc or L |
| 15 | §7 | Local-first boundary, no telemetry | Holds | Doc |
| 16 | §7 | Trademark-compliant naming | Open elsewhere | — |

Nine of the sixteen rows are **Doc** or **S**. The document is closer to the code than
it first reads; most of the distance is in three places — the protocol frame (§6), the
provider abstraction (§1.3, §3B), and everything downstream of having more than one
data source.

---

## 2. Item by item

### 1. Zero-credential / zero-trust footprint — Holds, with an omission

The shipped build handles no Anthropic credential, and this is enforced at compile
time rather than by convention.

`CONFIG_BLINK_WIFI_MODE` is `default n` (`firmware/Kconfig:16`), and
`firmware/CMakeLists.txt:32` gates the network sources behind it with
`target_sources_ifdef`. With it off, `oauth.c`, `usage_client.c`, `portal.c`,
`net_wifi.c` and friends are never handed to the compiler — the client id, the
User-Agent and the token writer are *absent from the image*, not merely unreachable
in it. A URL sweep of tracked source finds `api.anthropic.com` and `claude.ai` only
in `firmware/src/usage_client.c:2` and `firmware/src/oauth.c:25-26`, both WiFi-mode-only.

On the host side, `pc/statusline_source.py` reads a file Claude Code wrote; there is
nothing to authenticate with.

**Omission:** the doc describes one architecture, but the tree has two build modes.
The opt-in `wifi.conf` build (`firmware/wifi.conf`) does perform on-device OAuth and
fetches usage from Anthropic directly. The doc never acknowledges it exists.

**Cost — Doc.** Add a paragraph naming the standalone mode, its default-off status,
and the fact that pillars 1 and 2 are scoped to the default build. (The security
questions attached to that mode are tracked separately and are not re-opened here.)

### 2. Zero outbound AI polling — Holds, but §7 overstates it

No component polls an AI provider. The daemon does make outbound HTTPS calls, to
exactly one host: `https://github.com/KfirLevy258/Blink/releases/latest/download/`
(`pc/ota.py:30-32`) for the firmware manifest, and `pc/update.py:155`
`fetch_signed_manifest` for the daemon's own signed release manifest. First check is
deferred until a board is attached, then every 24 h (`claude_usage_bridge.py`,
`UPDATE_FIRST_CHECK_S` / `UPDATE_INTERVAL_S`).

This is consistent with §1.2 as literally written ("to AI cloud providers"), but §7's
"must never phone home" reads as a stronger claim than the code makes.

**Cost — Doc.** Distinguish the update feed from telemetry in §7. The distinction that
matters is direction of data: the release check sends no user data, it only asks what
the latest version is.

### 3. Pluggable multi-provider architecture — Absent

There is no `ProviderParser`, no `NormalizedUsageFrame`, no `get_provider_id()`, and
no `provider` field on the wire (confirmed by grep across `pc/`, `tools/`, `tests/`,
`firmware/src/`). The pipeline is Claude-only end to end: one source module, one
message builder, one firmware handler.

Closing this is not one change. It needs, in order:

1. A normalized frame type and a parser interface in `pc/`.
2. A `provider` field on the `usage` message, plus a firmware decision about what to
   *do* with it — today `usage_view` renders one session dial and one weekly dial with
   no notion of whose they are.
3. A UI answer for multiple simultaneous providers, which is a product question, not
   a refactor.

**Cost — L**, and step 3 should be settled before steps 1–2 are written. Building the
abstraction first risks an interface shaped by guesses about a second provider that
does not exist yet.

### 4. Zero-touch install — Holds. Self-healing watchdog — Absent

Installation is genuinely one step and genuinely non-destructive. `pc/install_statusline.py`
preserves an existing `statusLine.command` into `~/.blink/statusline-chain`, and
`tools/blink-statusline.sh:49-104` delegates to it on every render. The
self-reference guard (lines 56–94) reconstructs `shlex.quote`'s escaping byte for byte
so a shim path containing a space is not mistaken for a foreign command, and strips
both the `sh ` and `bash ` prefixes so the guard works on Windows too. Service
installation covers launchd, schtasks and systemd (`pc/cli.py:274-430`).

What does not exist is §4A's **Active Configuration Watchdog**. `install()` runs once,
at install time. There is no file watcher on `~/.claude/settings.json`, and no
periodic re-check — a grep for `watch`, `inotify`, `FSEvents`, `reinstate` across `pc/`
and `claude_usage_bridge.py` returns nothing. If a Claude Code update or a manual edit
drops the `statusLine` key, Blink goes quiet until the user reinstalls. The daemon
does notice the *symptom* — `poll_once` prints "no usage data yet" once
(`pc/bridge.py:319`) — but it neither diagnoses nor repairs the cause.

**Cost — M.** The cheap version is a re-check inside the existing 60 s poll: if
`fetch()` has returned `None` for N consecutive polls, re-read settings and reinstate
if our command is gone. That reuses `_is_ours()` (`pc/install_statusline.py:164`) and
needs no new dependency or watcher. True millisecond-latency file watching as the doc
describes would need a watcher library, which is a heavier ask for a daemon that
currently ships with only `pyserial`.

One design caveat worth recording: silently reinstating a config the user just removed
is a defensible product choice but not an obviously correct one. The existing installer
is careful to *announce* what it changes (`_announce`, `pc/install_statusline.py:314`).
A watchdog that restores without saying so would be the first place this product edits
user config invisibly.

### 5. Schema resilience — Partial, and the code is ahead of the doc

§4B's "strict type checking" is present and, on the point that matters most, better
reasoned than the doc:

- `_window()` (`pc/statusline_source.py:40-63`) returns `-1.0` for an absent or
  non-numeric window rather than `0.0`, precisely because `0.0` reads as a confident
  "0% used" — a stronger claim than "we don't have this number".
- `read_payload()` (line 150) turns every `OSError`/`ValueError` into `(None, None)`.
- `_window_has_reset()` / `_rolled_over()` (lines 86-121) carry a window across its own
  reset instead of disowning the reading, which is a case §4B does not contemplate at all.
- The freshness bound is 1800 s with a documented argument for why age is the wrong
  primary signal (lines 18-37).

What is absent is the *plural* in "versioned parsing adapters". There is one parser for
one source. §4B's fallback ladder ("mark the cache source temporarily invalid, fall
back to the active CLI hook") has no meaning with a single source — there is nothing to
fall back to.

**Cost — M**, but not independently useful. This becomes real work only once item 10
(desktop cache) or item 12 (second provider) lands. Until then, the right move is to
amend §4B to describe the recency/reset logic the code actually implements, which is
the more valuable half and is currently undocumented.

### 6. Zero content inspection — Partial

True of what is *transmitted*: `map_statusline` reads only `rate_limits`, and the
`usage` message carries percentages, countdowns and a stale flag.

Not true of what is *written to disk*. `tools/blink-statusline.sh:11,28` captures the
entire payload verbatim to `~/.blink/statusline.json`. Per the redaction note in
`tests/fixtures/statusline_payload.json`, that payload includes `session_id`,
`transcript_path`, `cwd`, workspace paths, `session_name` and `cost`. The file stays in
the user's own home directory and none of it leaves the machine — but "the daemon
strictly ignores prompt texts, source code, diffs" is a claim about the pipeline, and
the first stage of the pipeline stores more than the last stage reads.

**Cost — S**, with a real constraint. Narrowing the capture means parsing JSON in the
shim, and the shim is deliberately POSIX `sh` with no forks on the every-render path —
it uses `read` instead of `cat` specifically to avoid one (line 51-55). Options, cheapest
first: (a) amend the doc to say the capture is whole-payload and local-only; (b) have the
daemon rewrite the file down to `rate_limits` after each read; (c) `chmod 600` the
`~/.blink` directory at install time. (b) and (c) are compatible and neither touches
the render path.

### 7. CLI hook, `rate_limits` — Holds

`five_hour.used_percentage`, `five_hour.resets_at`, and the `seven_day` equivalents are
read at `pc/statusline_source.py:127-128`, with `_secs_until()` converting the absolute
timestamps to remaining seconds because the board has no wall clock over USB.

### 8. CLI hook, `context_window` and `model.display_name` — Absent

Both fields are listed in §3A1 as extracted attributes. Both are present in the real
captured payload (`tests/fixtures/statusline_payload.json`: `context_window.used_percentage: 55`,
`model.display_name: "Opus 5 (1M context)"`). Neither is read anywhere — a grep for
`context_window` and `display_name` across `pc/`, `tools/` and `firmware/src/` returns
nothing.

**Cost — S for the daemon, M with the display.** Adding two keys to the `usage` message
is a few lines each side. The real cost is on the panel: `usage_view` currently renders
two dials, and there is no slot for a context meter or a model name. `firmware/src/usage_view.c`
is 845 lines and its layout was recently re-proportioned (`42acc9e` gave the reset row's
height to the rest), so this is a deliberate design change, not a spare-corner addition.

This is the highest value-per-effort item in the audit: the data is already on disk,
already fresh, and `ctx_pct` is arguably the metric a developer glances at most.

### 9. Transport, loopback UDP `127.0.0.1:9876` — Conflicts

§3A1 specifies the hook relaying telemetry over UDP to port 9876. The code does not
bind a socket anywhere; the shim writes `~/.blink/statusline.json.tmp` and `mv -f`s it
into place (`tools/blink-statusline.sh:28-29`), and the daemon polls that file every
60 s (`claude_usage_bridge.py`, `POLL_INTERVAL_S = 60`).

The file approach is the better design and should not be changed to match the doc.
UDP would lose every render that arrives while the daemon is restarting or updating
itself; the file survives, and its mtime is what the staleness logic is built on
(`map_statusline(payload, now, mtime)`). The atomic rename exists specifically so a
half-written file never parses as malformed.

**Cost — Doc.** Replace the UDP description with the atomic-file-plus-poll design and
state why: durability across daemon restarts, and mtime as the freshness signal.

### 10. Desktop app cache — Absent

`plan-usage-history.json` appears nowhere in the tree. No file watcher, no
`samples[-1].u.fh` parsing.

**Cost — M.** The parsing itself is small. The work is the conflict-resolution the doc
names but does not specify (§2, "Conflict & Recency Resolution"): when the CLI hook says
25% and the desktop cache says 31%, which wins, and how does the panel express that it
is showing a merged figure? Note that `statusline_source.py:32-36` already identifies
this as the known blind spot — "usage happens somewhere we cannot see — claude.ai, the
phone app" — so this item is the direct answer to a limitation the code already admits.

### 11. Web extension bridge — Absent

No MV3 manifest, no WebSocket server, nothing on 9877.

**Cost — L.** A browser extension is a separate distributable with its own review and
update cycle, plus a WebSocket server in a daemon that currently opens no listening
sockets at all. This is the largest single item in the document and the doc already
scopes it to "Phase 2".

### 12. Codex and future providers — Absent

Blocked on item 3. **Cost — L**, after item 3.

### 13. Live state machine — Absent

§5's `idle` / `running` / `waiting` / `stuck` is a different axis from anything the
firmware tracks. `usage_view.h:9-12` defines four statuses —
`DISCONNECTED` / `OK` / `STALE` / `ERROR` — and these describe *data health*, not
*execution state*. The protocol's `status` message carries a `state` string, but
`proto.c:350` collapses it to a single test: `rate_limited` maps to amber, everything
else to red.

Nothing in the pipeline observes subprocess CPU, stdin blocking, or file-activity
timing, and the statusline payload carries none of it.

**Cost — L.** This needs a new telemetry source (the statusline payload cannot answer
"is a tool call hung"), a new protocol message, a new firmware widget, and a definition
of "stuck" that does not fire on a legitimately long build. It is correctly filed as
"V3 Backlog" in the doc.

### 14. Serial protocol frame — Conflicts

This is the sharpest discrepancy in the document. §6 specifies a single flat frame
pushed on state change or a 2 s heartbeat. What ships is NDJSON with typed messages,
one JSON object per line, each carrying `t` and `v` (`pc/protocol.py:1-6`).

Field mapping:

| §6 field | Shipped equivalent | Note |
|---|---|---|
| `v: 1` | `v: 2` | `PROTO_VERSION = 2` (`pc/version.py`) |
| `provider` | — | does not exist |
| `h5_pct` | `session_pct` | |
| `d7_pct` | `weekly_pct` | |
| `reset_s` | `session_resets_in_s` **and** `weekly_resets_in_s` | two countdowns, not one |
| `ctx_pct` | — | see item 8 |
| `state` | separate `status` message | different message, different lifecycle |
| `model` | — | see item 8 |
| `src` | — | does not exist |
| — | `stale` | read by `proto.c` via `msg_get_bool` |
| — | `session_resets_at`, `weekly_resets_at` | absolute timestamps kept alongside |
| — | `models[]` + flat `fable_pct` / `sonnet_pct` / `opus_pct` | flattened for the board's scalar-only scanner |
| — | `welcome`, `pong`, `time`, `ota_*` | six message types the doc does not mention |

Three specific incompatibilities beyond naming:

- **Null vs. sentinel.** §6 types `d7_pct` and `ctx_pct` as nullable. The codebase uses
  `-1` throughout and does so deliberately — `pc/statusline_source.py:44-48` argues the
  case, and `proto.c:217,227` defaults to `-1` on an absent key. Adopting `null` would
  mean teaching the firmware's `msg_get_double` a JSON null.
- **Heartbeat rate.** §6 says 2 s. Actual: usage every 60 s, board ping every 10 s
  (`proto.c:21`, `PING_INTERVAL_MS`), host declared gone after 35 s (`HOST_TIMEOUT_MS`).
  A 2 s push is 30× the current rate for a source that only updates when Claude Code
  renders its status line — it would transmit the same numbers thirty times over.
- **`reset_s` naming.** Described as "Epoch seconds remaining", which conflates a
  duration with a timestamp. The shipped `*_resets_in_s` / `*_resets_at` pair keeps
  those separate, and the reason is load-bearing: the board has no RTC over USB, so the
  daemon does the subtraction and the board ticks the result down locally.

**Cost — Doc, or L.** Rewriting §6 to document the shipped v2 protocol is a
half-day and loses nothing. Migrating the wire format to §6 as written would touch
`pc/protocol.py`, `pc/bridge.py`, `firmware/src/proto.c`, `firmware/src/msg_parse.c`
and `usage_view.c`, break every deployed unit, and force a `PROTO_VERSION` bump — which
`pc/version.py` is explicit should be "close to never", since a board whose firmware
needs a protocol the daemon does not speak gets no update offered at all. That last
point makes a breaking protocol change genuinely dangerous on a fleet that updates over
the very link being changed.

**Recommendation: amend the doc.** The shipped protocol is strictly more capable, and
§6 appears to have been written without reference to it.

### 15. Local-first boundary, no telemetry — Holds

All local IPC is filesystem-based. The only outbound destinations in tracked source
are `github.com` (releases) and, in the non-default WiFi build, the two Anthropic
endpoints. No analytics host, no crash reporter.

**Cost — Doc**, per item 2: reconcile "never phone home" with the release check.

### 16. Trademark-compliant naming — Open elsewhere

§7 directs descriptive compatibility naming; `README.md:5` leads with "Blink" as the
product title. This is an existing tracked concern with a decision attached to it
elsewhere, and is neither re-derived nor re-argued here.

---

## 3. What the code has that the doc omits

The handoff document is not a superset of the product. These ship today and appear
nowhere in it:

- **OTA over the serial link.** Five protocol messages (`ota_avail`, `ota_begin`,
  `ota_resume`, `ota_none`, `ota_error`), a signed release manifest verified against a
  pinned P-256 key (`pc/update.py:109`), and a pair-update path where the daemon
  replaces itself mid-sequence — with `ota_begin` separated from consent specifically
  because opening the serial port from the new process resets the board
  (`pc/protocol.py:145-156`).
- **Clock provisioning.** The `time` message (`pc/protocol.py:68`). The board has no
  RTC; it anchors an epoch to its own uptime and re-anchors on every usage push,
  bounding drift to one poll interval.
- **Bidirectional liveness.** Board pings every 10 s, daemon answers `pong`. Without
  it a 60 s poll interval makes "between polls" and "host died" indistinguishable, and
  the panel would show a green dot over frozen numbers (`pc/protocol.py:57-65`).
- **The `stale` contract and reset carry-over.** Item 5 above. This is the most
  carefully reasoned logic in the daemon and §4B does not describe it.
- **Service management.** launchd / schtasks / systemd backends, PID tracking,
  uninstall symmetry (`pc/cli.py:230-476`). §4A says "single step" without saying what
  that step installs.
- **Hardware realities.** Flash encryption and eFuse gating, pilot vs. production panel
  compensation (`firmware/Kconfig`), the CH340 USB-serial VID:PID table that autodetect
  depends on (`claude_usage_bridge.py:39`).

An incidental finding, adjacent but not a doc gap: `README.md:21` advertises the usage
screen as "session & weekly, per model", while `pc/statusline_source.py:141-143` passes
an empty models list — the statusline payload has no per-model breakdown, and `5808dd6`
dropped the per-model gauge from the build that has no per-model data. The README claim
is now ahead of the shipped USB product. Worth a separate fix.

---

## 4. Recommendation

Treat the document as a roadmap that was written without the current tree in front of
it, and reconcile in three passes:

**Pass 1 — doc only, no code (half a day).** Items 1, 2, 9, 14, 15. Rewrite §6 against
`pc/protocol.py`, replace the UDP transport description, scope pillars 1–2 to the
default build and name the WiFi mode, soften §7's telemetry claim. Fold in §3 above so
the document stops being a subset of the product. This is where nearly all the
discrepancy lives, and none of it requires touching code.

**Pass 2 — cheap and real (a few days).** Item 8 (`ctx_pct` and `model`, already on disk
and unread) and the poll-based half of item 4b (reinstate the hook when the payload has
been missing for N polls). Item 6's option (b)/(c) if the disk-capture nuance matters.

**Pass 3 — decide before building.** Items 3, 10, 11, 12, 13 are each gated on a product
question the document assumes away: what the panel shows when there is more than one
provider, or more than one source for the same number. That answer should precede the
`ProviderParser` interface, not follow it.


---

## 5. Resolution

What was built, and where the implementation deliberately differs from the
document. Commits are on `desk-hud-universal`.

| # | Item | Resolution |
|---|---|---|
| 1 | Zero-credential footprint | **Confirmed, doc amended.** Enforced at compile time (`CONFIG_BLINK_WIFI_MODE` default n); the standalone WiFi build is now named as a second mode the document omitted. |
| 2 | Zero outbound AI polling | **Confirmed**, and now trivially so: the extension that observed page responses was removed 2026-08-28, so nothing reaches the network on Blink's behalf at all. |
| 3 | Pluggable multi-provider | **Built.** `pc/providers/base.py` (`ProviderParser`, `NormalizedUsageFrame`), `pc/ingest.py`, `pc/normalizer.py`. |
| 4b | Anti-drift watchdog | **Built**, polled rather than watched, and it never overrides a deliberate uninstall. |
| 5 | Schema resilience | **Built.** Versioned adapters dispatch on the cache's own `version`; an unknown version falls to a shape-driven reader. No `_parse_v1` — inventing a schema nobody has observed would mean testing against the invention. |
| 6 | Zero content inspection | **Tightened.** The state hook keeps only an event name and a timestamp, asserted by `check_hook_shim.sh`. The statusline shim's whole-payload capture is unchanged and still local-only. |
| 8 | `context_window`, `model.display_name` | **Built**, daemon through to pixels. Both were already in the payload and unread. |
| 10 | Desktop app cache | **Built.** Two traps found against the real file: `t` is milliseconds, and there are no reset timestamps at all. |
| 11 | Web extension bridge | **Built, then removed 2026-08-28.** Measured against the real site: 178 responses, no rate-limit headers. See `next-steps.md` section A. |
| 12 | Codex / future providers | **Unblocked, not written.** The interface exists and is exercised by four sources; no second provider ships, because there is nothing to test it against. |
| 13 | Live state machine | **Built, differently** — see below. |
| 14 | Serial protocol | **Capabilities delivered additively** — see below. |
| 15 | Local-first, no telemetry | **Confirmed.** The one new socket was loopback-bound and origin-checked, and went with row 11 on 2026-08-28; the daemon now listens on nothing. |
| 16 | Naming | Untouched. Tracked elsewhere. |

### The four departures

**Row 9 — transport (UDP → file).** Kept as it was. The file survives a daemon
restart and its mtime is the freshness signal the whole staleness design rests
on; UDP would lose every render arriving during a restart or self-update.

**Row 11 — WebSocket → HTTP POST.** Same host, same port, same locality, same
push-on-completion timing. RFC 6455 would have meant a hand-rolled handshake and
frame unmasker exposed to a socket inside a daemon that ships with one
dependency, to move a payload that fits in one body.

**Row 13 — state machine inputs.** The document specifies "waiting on stdin"
and "0% CPU". Both mean finding a process among several and inferring intent
from a number that is legitimately zero whenever a tool waits on the network.
Claude Code's hooks announce every one of these transitions; `Notification` is
authoritative for exactly the question "0% CPU" was guessing at. `stuck` fires
at 180 s rather than 60 s.

**Row 14 — protocol.** §6's frame was not adopted. Its capabilities were, as
additive keys on v2: `provider`, `src`, `ctx_pct`, `model`, `state`. Bumping
`PROTO_VERSION` would stop every deployed board being offered updates, over the
link the update travels on — unfixable remotely. `-1` sentinels were kept over
§6's `null`, and the 2 s heartbeat was not adopted for a source that only
changes when Claude Code renders.

### One thing the audit did not ask for

`proto.c:367-371` drops an over-long line whole rather than truncating it — no
error, no partial parse, the panel just stops updating. Adding five fields to a
512-byte budget made that worth a guard, so `protocol.encode_checked()` now
refuses to write a line the board could not receive, and the real payload is
pinned against the limit. A realistic message is ~300 bytes.

### Verification

- 318 Python tests pass (was 204).
- Both firmware configurations build with no new warnings; USB build DRAM 57.8% / 55.5%.
- `check_versions.sh`, `check_shim.sh` (sh/bash/dash) and the new
  `check_hook_shim.sh` (sh/bash/dash) pass.
- End-to-end verified against this machine's real files plus a live extension
  POST: the browser report takes the percentages, the CLI keeps supplying the
  reset time, context and model, and `src` flips to `web`.
- **Not flashed.** No board was attached; boot verification is outstanding.
