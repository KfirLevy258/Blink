# Codex research: project name, waiting, failure

Date: 2026-09-03. Ground truth: 4 real rollout files under `~/.codex/sessions/2026/**` (cli_version 0.150.0),
plus upstream `openai/codex@main` sources fetched to this scratchpad (`protocol.rs`, `policy.rs`, `hooks_schema.rs`,
`hooks_registry.rs`, `config.md`), plus strings of the installed binary `/Users/KfirLevy/.local/bin/codex`.
Throughout: **SEEN** = observed in real files on this machine; **READ** = upstream source only.

## Q1 — project name: YES

**SEEN** in all 4 real files:
- Line 1 of every rollout is `{"type":"session_meta", "payload":{..., "cwd":"/Users/KfirLevy/Projects", ...}}`.
  Always ordinal 0, always first, always carries `cwd` (also `originator`: `codex-tui` vs `codex_exec`,
  `cli_version`, `session_id`).
- `cwd` is also REPEATED per turn in `{"type":"turn_context","payload":{"turn_id":..., "cwd":...}}` — one per
  turn, so it tracks a mid-session `cd`/workspace change. Also present in `thread_settings_applied` payloads and
  in `world_state.state.environments.environments.local.cwd`.
- **READ**: policy.rs:19,24 — `SessionMeta` and `TurnContext` are unconditionally persisted. Stable.

Not in the filename (only timestamp + uuid), but it doesn't need to be — it's in the content.

Implementation note: `codex_cli.py` reads only the last `TAIL_BYTES = 256KB` (codex_cli.py:86,117). The
session_meta line is at the HEAD, and a real file here is 51 MB (the 2026-08-27 one), so the tail will usually
NOT contain it. A name reader needs one extra cheap read: open the file, read the first few KB, parse line 1.
One head-read per rollout per poll (or cached per path — the first line never changes). The tail often contains
a recent `turn_context` with cwd too, but that is not guaranteed inside 256KB of a long turn, so head-read is
the robust path.

Cost: small — ~30 lines in `codex_cli.py` (head-read + basename of cwd, same sanitising the Claude shim does),
plus plumbing a `label` into the state frame it already emits. The per-file loop in `poll()` already exists;
label policy should mirror claude_state.py:273-283 (name only when exactly one session holds the winning state).
Risks: `codex_exec` (non-interactive `codex exec`) sessions also write rollouts with cwd — SEEN (`/private/tmp`,
`/Users/KfirLevy`); they'd get pips/names identical to interactive ones. Resumed sessions may write a fresh file
whose meta echoes the original (READ upstream has resume paths; not verified — no resumed file on this machine).

## Q2 — "waiting for you": NO from the rollout file; YES via Codex's new hooks

**Rollout: flatly no.** Upstream policy.rs:176-180 puts `ExecApprovalRequest`, `ApplyPatchApprovalRequest`,
`RequestPermissions`, `RequestUserInput`, `ElicitationRequest` in the never-persisted arm of
`should_persist_event_msg` ("Transient, non-durable events" — READ). Consistently, zero approval events appear
in any real file here, including two TUI sessions running with `approval_policy: "on-request"` (SEEN). The
approval *answer* is a client→server op, also never in the file. So no waiting signal, and no clear signal
either — the rollout path is dead for Q2, exactly as the codex_cli.py:255-257 comment guessed.

**But Codex now has a lifecycle hooks system — the thing the brief said it lacks.** READ + verified shipped:
- `codex-rs/hooks/` crate; event names (hooks_schema.rs:102-125): `PreToolUse`, `PermissionRequest`,
  `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`, `SessionEnd`, `UserPromptSubmit`,
  `SubagentStart`, `SubagentStop`, `Stop`, `Interrupt`. Deliberately Claude-Code-shaped naming.
- Command-hook stdin JSON carries `session_id`, `cwd`, `hook_event_name`, `tool_name`, `turn_id`
  (hooks_schema.rs `PreToolUseCommandInput` / `PermissionRequestCommandInput` / `PostToolUseCommandInput`) —
  the exact fields `tools/blink-hook.sh` consumes from Claude.
- **Shipped in the installed 0.150.0**: the binary's strings contain the full event-name list, a top-level
  `hooks` config key (next to `plugins`, `skills` in the config-key table), `HookHandlerConfig::Command`
  (fields incl. command, timeout), handler types `command|prompt|mcp_tool|agent`, `sync|async`, and a
  `--dangerously-bypass-hook-trust` CLI flag ("persisted hook trust"). Verified by `strings`, not by running one.
- `PermissionRequest` fires when Codex asks for approval; the clear comes from the following `PostToolUse`
  (approved+ran) or `Stop`/`Interrupt`/`UserPromptSubmit` (denied/aborted/next turn) — same clearing structure
  the Claude shim already relies on, and claude_state.py:102 ALREADY lists `"PermissionRequest"` in
  `_WAITING_EVENTS`, so a Codex shim writing the same `~/.blink/state/<sid>.state` slots would light `waiting`
  with zero daemon changes.

Cost: a `hooks` entry in `~/.codex/config.toml` pointing at blink-hook.sh (or a thin port), installed by
`blink` the way the Claude hook is. What could go wrong (all unverified — I did not execute a hook):
(1) hook *trust*: Codex requires persisted trust for hook sources; a programmatic install may need a one-time
user confirmation in the TUI — needs a live test. (2) exact TOML shape (`HookEventsToml`, matcher groups) is in
the config crate; I did not pin the syntax — read `codex-rs/config` or the developers.openai.com config
reference before writing it. (3) feature is new; wire shapes could still move — but the schema.rs JSON fixtures
suggest they're now contract-tested upstream. (4) the two `.state` namespaces would collide if a Claude and a
Codex session share a session_id — UUIDs, so effectively no; but the provider attribution (claude vs codex pip
colour/count) WOULD be wrong: the Claude state dir is read by claude_state.py and would count Codex sessions as
Claude. A separate dir (`~/.blink/state-codex/`) plus a second provider instance is the honest shape.

## Q3 — failure distinct from finish: YES (schema), with a correction to today's mapping

- **`turn_aborted` is NOT an error.** READ protocol.rs:4151-4175: `TurnAbortedEvent{turn_id, reason, ...}`,
  `TurnAbortReason` = `interrupted` | `replaced` | `review_ended` | `budget_limited` (snake_case; upstream test
  at protocol.rs:5964 shows the wire shape `{"type":"turn_aborted","reason":"interrupted"}`). `interrupted` =
  user pressed Esc; `replaced` = user sent a new message over it. Both are user actions → idle is the right
  colour; the current mapping (codex_cli.py:265 → idle) is already correct and NOT painting red. All four
  reason strings are in the installed binary. Never SEEN in a real file (no abort ever recorded here).
- **The real failure signal is `task_complete` with an `error` field.** READ protocol.rs:2141-2147:
  `TurnCompleteEvent.error: Option<ErrorEvent>` — "Terminal error details when the turn completed
  unsuccessfully" — with `ErrorEvent{message, codex_error_info}` (protocol.rs:2063). `codex_error_info`
  variants in the binary include `UsageLimitExceeded`, `ContextWindowExceeded`, `SessionBudgetExceeded`,
  `HttpConnectionFailed`, `InternalServerError`, `Unauthorized`, etc. — `UsageLimitExceeded` is BLINK's
  headline case, mirroring Claude's `StopFailure error:"rate_limit"`. `task_complete` is persisted
  (policy.rs:118) and the field is skip-if-none, so today's files (all successes, SEEN) simply lack it.
  The standalone `EventMsg::Error` is NOT persisted (policy.rs:142) — do not wait for one.
- Wire names pinned upstream: protocol.rs:1405 `#[serde(rename = "task_started", alias = "turn_started")]`,
  :1414 `task_complete`/`turn_complete` — the aliases hint at a future rename; worth adding to
  check_codex_contract.sh.
- Mapping: `task_complete` + `error` present → failed; `turn_aborted` reason `interrupted`/`replaced`/
  `review_ended` → idle; `budget_limited` → arguably failed/attention (a stopped-for-money turn is the
  product's headline). Cost: ~10 lines in `parse_rollout_state` (it already parses the payload dict; just look
  at `payload.get("error")` / `payload.get("reason")`). Risk: `error`-carrying task_complete has never been
  observed in a real file — schema-only until one is captured; guard it as "error is a dict → failed" so a
  shape change degrades to idle, not red.

## Extras that change the picture

1. **Codex hooks exist and are installed** (Q2 above). This also means Q1 and Q3 could ride the SAME shim as
   Claude (SessionStart/UserPromptSubmit carry cwd; there is a Stop-failure story via hook outputs), making the
   rollout reader needed only for the rate-limit dials. Two viable architectures now, not one.
2. **Contract script gaps**: tests/ci/check_codex_contract.sh pins rate_limits + paths but nothing about
   task_started/task_complete/turn_aborted names, TurnAbortReason, or TurnComplete.error. If Q3 ships on the
   rollout reader, extend the script (the rename aliases at protocol.rs:1405/1414 are a live warning).
3. **51 MB rollout file** (SEEN): the 779-line 08-27 file is 51 MB — `world_state`/`turn_context` lines are
   enormous. Anything added must stay head-line + tail reads; never full-file parses.
4. **`event_msg` vs state reader**: `parse_rollout_state` (codex_cli.py:277) cheap-rejects on `"task_"`/
   `"turn_aborted"` substrings; a `task_complete` carrying a large error message still matches — no change
   needed there.

## Recommendation (single)

Q1 and Q3 on the rollout reader now (head-read session_meta for the name; error/reason fields for failed —
small, no user-side install, no new trust surface). Q2 only via the new Codex hooks, as a separate
`~/.blink/state-codex/` shim + provider — prototype the config.toml hook on the desk first to settle the trust
prompt and exact TOML syntax before building on it.
# Codex session ids and cwd — measured on four real rollouts, 2026-09-03

Checked directly because Plan C de-duplicates hook-written state against
rollout-derived state on the session id, and a mismatch double-counts every
live Codex session on the panel.

| Fact | Result |
|---|---|
| `payload.session_id` on line 1 | present in **4 of 4** |
| Does it match the filename's UUID? | **yes, exactly, 4 of 4** |
| `payload.cwd` on line 1 | present in **4 of 4** — confirms the naming finding on real data |
| Largest rollout | **53.3 MB** — confirms the 256 KB tail can never see line 1 |

## Consequences

**The rollout side of the id is now pinned, and needs no parsing.**
`rollout-<ISO timestamp>-<uuid>.jsonl`, and that uuid IS `payload.session_id`.
A reader can take the id from the filename and never open a 53 MB file to get
it. Only the *name* requires a head-read.

**A distractor to avoid.** Line 1 also carries `payload.context_window.window_id`,
which shares the session id's first three segments and differs only in the
last two — `01a0401b-b9ac-7ad3-a92b-fb9d4d69ead1` vs
`…-a92b-fba8ff2a529a`. An implementer grabbing "the id that looks right" from
a pretty-printed line 1 can take the wrong one and it will look plausible in
every log. Match on the key, never on the shape.

**Still desk-only.** Whether the *hook's* stdin `session_id` is spelled the
same as this remains unverified — nobody has executed a Codex hook. But the
rollout half of the comparison is no longer a guess, which halves that risk:
if the desk test shows a mismatch, we now know which side moved.
