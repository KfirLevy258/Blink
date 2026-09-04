# Codex hook contract, as of codex-cli 0.150.0

Everything here is what `blink install` writes into another vendor's
configuration, so each line says how it was established. Re-run the probes in
`docs/plans/codex-hook-shim.md` Task 1 after any Codex upgrade.

Evidence tags: **VERIFIED** = observed on this machine (binary probes on the
installed `codex-cli 0.150.0` at `~/.codex/packages/standalone/releases/0.150.0-x86_64-apple-darwin/bin/codex`,
or a hook actually executed in a sandboxed `CODEX_HOME` — see "The smoke test").
**READ** = upstream source only (openai/codex at tag `rust-v0.150.0`, cross-checked
against `main` where noted; files: `codex-rs/config/src/hook_config.rs`,
`codex-rs/hooks/src/engine/discovery.rs`, `codex-rs/hooks/src/lib.rs`,
`codex-rs/hooks/src/config_rules.rs`, `codex-rs/config/src/fingerprint.rs`,
`codex-rs/config/src/state.rs`, `codex-rs/hooks/src/schema.rs` from `main`).

## F1 — Where the hooks file lives

**`$CODEX_HOME/hooks.json`** (so `~/.codex/hooks.json` by default) — **not**
`~/.codex/hooks/hooks.json`. **VERIFIED**, both directions: in the sandbox smoke
test a `SessionStart` hook registered in `$CODEX_HOME/hooks.json` fired, and the
same file moved to `$CODEX_HOME/hooks/hooks.json` was silently ignored (no
warning, no execution). The `hooks/hooks.json` string in the binary belongs to
the **plugin** loader (`codex-rs/core-plugins/src/loader.rs` — a plugin ships its
hooks at `<plugin>/hooks/hooks.json`); it is not the user-level path — READ.

Source confirms (READ): `discovery.rs::load_hooks_json` does
`config_folder.join("hooks.json")`, and the user layer's config folder is the
parent of the user `config.toml`, i.e. `$CODEX_HOME` itself
(`state.rs::config_folder`). A project layer reads `<project>/.codex/hooks.json`
the same way.

Codex does **not** create the file — on this machine no `hooks.json` exists in
the real `~/.codex/` (VERIFIED) and `load_hooks_json` returns `None` for a
missing file without complaint (READ). We create it.

## F2 — The top-level JSON shape

The wrapped form: **`{"hooks": {"<Event>": [ ...matcher groups... ]}}`**, with an
optional top-level `"description"` string. **VERIFIED** by execution (a file of
exactly that shape fired); struct is `HooksFile { description: Option<String>, hooks: HookEventsToml }`
with `deny_unknown_fields` (READ — so a bare `{"SessionStart": [...]}` at the top
level would be rejected as an unknown field).

Event keys are the exact PascalCase names (READ, and present verbatim in the
binary's string table — VERIFIED as compiled-in): `PreToolUse`,
`PermissionRequest`, `PostToolUse`, `PreCompact`, `PostCompact`, `SessionStart`,
`SessionEnd`, `UserPromptSubmit`, `SubagentStart`, `SubagentStop`, `Stop`,
`Interrupt`. Each maps to an array of matcher groups; a missing key means no
hooks for that event (`#[serde(default)]`).

## F3 — Does config.toml need a pointer

**No.** `hooks.json` is discovered purely by location; the sandbox `config.toml`
contained no hooks-related key at all and the hook fired — **VERIFIED**. The
real `~/.codex/config.toml` today holds only a `[projects."…"]` trust table
(VERIFIED, read-only look), and that absence means "no hooks declared in TOML",
not "hooks disabled".

Two related facts:
- `config.toml` can *itself* carry hooks under a `[hooks]` table
  (`[[hooks.SessionStart]]` array-of-tables with the same shape) — **VERIFIED**
  by execution in the sandbox. If both the TOML table and `hooks.json` declare
  hooks for one layer, both load and Codex warns "prefer a single
  representation" (READ). BLINK should use `hooks.json` only.
- `config.toml` *is* where the per-hook **state** lives (`[hooks.state."<key>"]`
  — see F5), so `blink install` writes both files: the declaration into
  `hooks.json`, the trust record into `config.toml`.

## F4 — The matcher-group and handler shape

One registered command hook, exactly as the smoke test ran it (**VERIFIED**):

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          { "type": "command", "command": "sh /abs/path/to/shim.sh" }
        ]
      }
    ]
  }
}
```

Full shapes (READ, `hook_config.rs`):
- `MatcherGroup`: `{ "matcher": <string, optional>, "hooks": [ <handler>... ] }`.
- Command handler (`HookHandlerConfig`, internally tagged on `"type"`):
  `type` (`"command"`), `command` (string, the POSIX command), `commandWindows`
  (optional string, used *instead of* `command` on Windows; alias
  `command_windows` accepted), `timeout` (optional seconds; default 600 for most
  events, but SessionEnd/Interrupt default to 1 s and clamp to 3 s), `async`
  (optional bool, default false; forced sync for SessionEnd), `statusMessage`
  (optional string shown while running), `additionalContextLimit` (optional,
  only meaningful for events that can emit additionalContext).
- Other handler types exist: `mcp_tool` (`server`, `tool`, `input`, `timeout`,
  `statusMessage`); `prompt` and `agent` are declared but **rejected at load
  time** with "not supported yet" (READ).
- An empty/whitespace `command` is skipped with a warning (READ).

The hook process receives its input as **JSON on stdin** — **VERIFIED** (the
smoke-test hook was literally `cat > capture.json` and captured it).

## F5 — Trust

Where the record lives (**VERIFIED** — a hand-written record was honored):
in the **user `config.toml`**, keyed per handler:

```toml
[hooks.state."<abs path to hooks.json>:<event_key>:<group_index>:<handler_index>"]
trusted_hash = "sha256:<64 hex>"
```

- Key format is `hook_key()` (READ, confirmed working VERIFIED):
  `<source path as displayed>:<snake_case event label>:<matcher-group index>:<handler index>`,
  e.g. `/Users/x/.codex/hooks.json:session_start:0:0`. Event labels are the
  snake_case forms (`session_start`, `pre_tool_use`, `permission_request`, …).
- The same table row can carry `enabled = false` to disable a hook (READ).
- Only `User` and `SessionFlags` config layers are consulted for state (READ,
  `config_rules.rs`), so the record must go in the user `config.toml`.

What the hash covers (**VERIFIED** end-to-end): a normalized, config-derived
identity — `{event_name, matcher, hooks:[the one normalized handler]}`
serialized to TOML, converted to canonical JSON (keys sorted, compact), then
SHA-256, rendered `sha256:<hex>` (`hook_hash` + `fingerprint.rs::version_for_toml`,
READ). Normalization before hashing (READ): platform command selected
(`commandWindows` collapses into `command`, then `commandWindows` dropped),
`timeout` filled with its default (600 for SessionStart), `async` kept,
`statusMessage` kept, default `additionalContextLimit` dropped. The smoke test
reproduced the hash independently in Python for
`{"event_name":"session_start","hooks":[{"async":false,"command":"sh …/hook.sh","timeout":600,"type":"command"}]}`
and Codex accepted it — the trusted hook ran with **no** bypass flag.

What the hash does **not** cover (**VERIFIED**): the contents of the script the
command points at. Editing the script file's contents left the hook running;
appending one argument to the declared `command` string made Codex silently skip
the hook (trust status `Modified`). So: changing the shim's *path or arguments*
re-prompts / disables; shipping new *contents* at the same path does not.

When Codex prompts: the interactive TUI is where trust is granted (binary
carries "Failed to trust hooks:", "failed to write hook trust:",
"config/batchWrite failed while updating hook trust in TUI" — VERIFIED strings;
flow READ). In non-interactive `codex exec`, an untrusted or modified hook is
**silently skipped — no prompt, no warning, no output** (**VERIFIED**).
`--dangerously-bypass-hook-trust` runs enabled hooks regardless of trust for
that invocation and prints a warning banner (**VERIFIED**); it does not write a
trust record (**VERIFIED** — config.toml unchanged after the run).

Managed/system/MDM layers and allow-listed builtin plugin hooks bypass the hash
check entirely (`Managed`/builtin trust status, READ) — not our case.

## What could not be established without running one

The smoke test below settled most of what this section was reserved for. Still
open, carried to Task 14's checklist:

- **PermissionRequest / Stop / PreToolUse inputs observed live.** Their schemas
  are READ (from `schema.rs`): PermissionRequest input =
  `session_id, turn_id, agent_id?, agent_type?, transcript_path, cwd, hook_event_name, model, permission_mode, tool_name, tool_input`;
  Stop input adds `stop_hook_active, last_assistant_message`. The sandbox's dead
  model provider cannot reach a tool call, so none of these fired here.
- **Gate finding 3 (approve/deny fire no events of their own): READ only.** The
  compiled-in event list has no approval-decision event — `PermissionRequest`
  fires when permission is *requested*, and nothing in the enum corresponds to
  the human's answer. Verifying that no event sneaks out on the answer needs an
  interactive session with a real model.
- **The TUI trust prompt's exact wording and write** (we hand-wrote the record
  instead). Worth one interactive look before shipping `blink install`, since
  the installer will pre-write trust the same way.
- **`session_id` shape in interactive/app-server sessions.** In `codex exec` it
  is the rollout UUID (VERIFIED, below). `thr_…` thread ids exist in the
  app-server/desktop surface; whether hooks report those there is unobserved.
- **SessionEnd delivery timing** — the sandbox runs were killed by a watchdog
  before clean exit, so `SessionEnd` never got a clean chance to fire.

## The smoke test

First known execution of a Codex hook on this machine (2026-09-04). Everything
ran in a sandboxed `CODEX_HOME` under the session scratchpad; the real
`~/.codex` was only ever read. No credentials were copied: `config.toml`
declared a fake model provider (`base_url = "http://127.0.0.1:9/v1"`,
`wire_api = "responses"`; note `wire_api = "chat"` is refused by 0.150.0), so
every run started a real session, fired `SessionStart`, then failed at the
network layer and was reaped by a 40–90 s watchdog.

Runs (all `codex exec --skip-git-repo-check "hello"` with `CODEX_HOME` set):

1. **Bypass-trust run** — `hooks.json` as in F4, `--dangerously-bypass-hook-trust`.
   Output showed `hook: SessionStart` / `hook: SessionStart Completed`; the hook
   (`cat > capture.json`) captured this exact stdin payload (synthetic session,
   quoted in full):
   `{"session_id":"01a06c1a-9b13-7a51-9201-2da98ebd8ccf","transcript_path":"<sandbox>/home/sessions/2026/09/04/rollout-2026-09-04T14-07-58-01a06c1a-9b13-7a51-9201-2da98ebd8ccf.jsonl","cwd":"<sandbox>/work","hook_event_name":"SessionStart","model":"fake-model","permission_mode":"bypassPermissions","source":"startup"}`
   Keys: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, `model`,
   `permission_mode`, `source`.
2. **No-bypass run** — same files, no flag: hook silently skipped, nothing
   captured, no trust record written.
3. **Location run** — file moved to `$CODEX_HOME/hooks/hooks.json`: ignored.
4. **TOML run** — same hook as `[[hooks.SessionStart]]` in `config.toml`: fired.
5. **Hand-trust run** — `hook_key` + `trusted_hash` computed in Python and
   written to `config.toml`: hook fired with no bypass flag.
6. **Script-edit run** — script contents changed, trust untouched: still fired.
7. **Command-edit run** — command string changed, trust untouched: silently
   skipped.

What this upgraded from READ to VERIFIED: F1 (path, both positive and
negative), F2 (wrapped top-level shape), F3 (no pointer needed; TOML variant
works), F4 (one command hook as written; stdin JSON delivery), F5 (state key
format, hash algorithm and coverage, exec-mode silent skip, bypass-flag
behavior), gate finding 1 in full (hash covers the declared command string, not
script contents), the `transcript_path` half of gate finding 2, and gate
finding 4 (`permission_mode` is on the input).

Against the gate's finding 2, one **contradiction**: in `codex exec` the hook's
`session_id` is **not** `thr_…`-shaped — it is the same UUID that names the
rollout file (rollout line 1 carries `payload.session_id` == `payload.id` ==
the hook's `session_id`, observed in the synthetic transcript). The join to the
transcript needs no id gymnastics anyway: `transcript_path` on the hook input
is the rollout file's absolute path.

---

# Live-event probe, 2026-09-04 — real model calls, real events

Task 1's smoke test used a fake provider on a dead port, so only `SessionStart`
could ever fire. This section is a second pass with the owner's real
credentials — copied read-only into a sandbox `CODEX_HOME`, so the live
`~/.codex` was never written — asking deliberately small questions, because each
run spends real Codex usage.

**Keys and shapes only below.** `UserPromptSubmit.prompt` and
`Stop.last_assistant_message` carry text from the machine they ran on; no value
from either is reproduced here.

## The six events `codex exec` actually fires — VERIFIED

One run, one shell tool call, all twelve events registered:

| Event | Input keys |
|---|---|
| `SessionStart` | `cwd`, `hook_event_name`, `model`, `permission_mode`, `session_id`, `source`, `transcript_path` |
| `UserPromptSubmit` | + `prompt`, `turn_id` |
| `PreToolUse` | + `tool_input`, `tool_name`, `tool_use_id`, `turn_id` |
| `PostToolUse` | + `tool_input`, `tool_name`, `tool_response`, `tool_use_id`, `turn_id` |
| `Stop` | + `last_assistant_message`, `stop_hook_active`, `turn_id` |
| `SessionEnd` | `cwd`, `hook_event_name`, `reason`, `session_id`, `transcript_path` |

Three things worth having in front of you before writing the shim:

- **`transcript_path` is on every one of the six.** The join key is universal,
  not something to reconstruct per event.
- **`SessionEnd` is the odd one out** — the only event carrying neither `model`
  nor `permission_mode`, and the only one with `reason`. A shim that reads
  `permission_mode` unconditionally will get a `KeyError` exactly at teardown.
- **`turn_id` is absent on `SessionStart` and `SessionEnd`** and present on the
  other four.

## `PostToolUse` does NOT fire for a rejected tool — VERIFIED

Second run, read-only sandbox, a prompt demanding a disk write: `PreToolUse`
fired once, `PostToolUse` **not at all**. So the two are not a matched pair, and
any state machine that clears a `PreToolUse`-set flag on `PostToolUse` will
leave that flag set forever the first time a tool is refused.

## `PermissionRequest` CANNOT be observed from `codex exec` — VERIFIED

This is the finding that matters most, because `PermissionRequest` is the
WAITING signal the whole plan is built on, and **it has still never been seen.**

- With `-s read-only -c approval_policy="on-request"` and a prompt that forces a
  write, the write was refused outright and no `PermissionRequest` fired.
  Non-interactive runs do not escalate to a human; there is no human to escalate
  to.
- `approval_policy = "untrusted"`, the strictest setting the plan's research
  named, is **rejected by 0.150.0**: `Error: approval_policy = "untrusted" is no
  longer supported; remove this setting`. Any part of the plan reasoning from
  that value is reasoning about a version that has shipped past it.

**Consequence: the premise of plan 3 cannot be verified by automation.** It
needs a person at an interactive Codex session, doing something that prompts for
approval, with the shim registered. Until that happens, every task below task 1
is built on an event nobody has observed — which is precisely the position the
discovery gate warned about, one level deeper than it knew.

This does not sink the plan. `PreToolUse`, `Stop` and `SessionEnd` are all
verified and all useful, and a session that is running versus finished is
already worth showing. But **WAITING specifically is unproven**, and the plan
should not claim otherwise until a human has sat in front of one.

---

# PermissionRequest, observed at last — interactive session, 2026-09-04 17:39

The owner ran one interactive Codex session in the sandbox and did two things:
asked for a file and **approved** the write, then asked for a second file and
**refused** it. Twelve events fired. This is the first time anyone has seen a
`PermissionRequest`, and it settles the question plan 3 is built on.

Shapes and enum values only below; `tool_input`, `prompt` and
`last_assistant_message` carry the owner's own words and are not reproduced.

## The two sequences, with the human in them

**Approved:**

    17:39:51  SessionStart
    17:39:51  UserPromptSubmit
    17:39:55  PreToolUse           tool_name=apply_patch
    17:39:55  PermissionRequest    tool_name=apply_patch     <- the panel should say WAITING here
    17:39:58  PostToolUse          tool_name=apply_patch     <- 3 s later, the human said yes
    17:40:04  PreToolUse / PostToolUse   tool_name=Bash
    17:40:08  Stop

**Refused:**

    17:40:12  UserPromptSubmit
    17:40:17  PreToolUse           tool_name=apply_patch
    17:40:17  PermissionRequest    tool_name=apply_patch     <- WAITING
    17:40:20  Interrupt                                      <- 3 s later, the human said no

## What this corrects

**The gate said a refusal fires nothing, so WAITING would clear LATE. That is
wrong** — at least for how the owner refused. `Interrupt` arrived three seconds
after the request, the same latency as the approval's `PostToolUse`. WAITING is
promptly clearable on **both** paths, which removes the "how long may a stale
WAITING linger" design question the gate said had to be answered deliberately.

**One honest caveat on that.** `Interrupt` is also what Esc fires. If the owner
refused by pressing Esc rather than choosing a "no" option, then what was
observed is Esc's `Interrupt` and a menu refusal might still fire nothing. This
needs one word from the owner before the correction is relied on. Recorded as
observed, not as settled.

**`PreToolUse` fires BEFORE `PermissionRequest`, in the same second.** The plan
never says which comes first. A shim that writes `running` on `PreToolUse` and
`waiting` on `PermissionRequest` is in the right order; one that assumed the
reverse would show `running` over a session that is actually blocked on a human.

**`PermissionRequest` carries NO `tool_use_id`** — `PreToolUse` and
`PostToolUse` both do. So a request cannot be correlated to its completion by
that id. The pair that is present on all three is `turn_id` + `tool_name`.

## Gate finding 2 — the session id — SETTLED, and the gate is wrong

`session_id` in the interactive session is a **bare UUID**, it does **not** start
with `thr_`, and it **appears verbatim in the rollout filename**. Same as exec.
The gate's warning that the two ids are spelled differently, and its conclusion
that `transcript_path` is therefore a necessary join key, do not hold at 0.150.0.

`transcript_path` is still the better key — absolute, present on every event,
and verified to exist on disk — but it is now a convenience, not a rescue.

## Gate finding 4 — CONFIRMED useful

`permission_mode` is `default` in the interactive session and
`bypassPermissions` under `codex exec`. So BLINK really can suppress WAITING in
modes where an approval prompt cannot occur, as finding 4 suggested.

## `turn_id` is per turn, `session_id` per session

Two distinct `turn_id` values across the two prompts, one `session_id`
throughout. Both are UUIDs. `turn_id` is absent from `SessionStart` and
`SessionEnd`.

## Still open

- **`SessionEnd` never fired** in this interactive session, though it does under
  `codex exec`. The session was quit by hand, so this may be how the owner
  closed the terminal rather than a real difference. Do not rely on `SessionEnd`
  for cleanup in an interactive session until it has been seen.
- Whether a menu refusal (as opposed to Esc) fires `Interrupt`. See the caveat
  above.

---

# OWNER'S RULING, 2026-09-04: three states, and the input type is irrelevant

> "we should have 3 states - running, which just running, finish - finished and
> waiting for user, and stuck - waiting for user input, its not relevant what
> type of input it is"

This settles the open caveat above and shrinks the plan. **All three already
exist on the wire and on the panel** — no new state, no wire change, no firmware
change, which matters because the usage line has one byte of headroom and
`proto.c` drops an over-long line whole.

| Owner's state | Wire | `usage_view.c` draws |
|---|---|---|
| running | `running` | "Working" |
| finished, user's turn | `idle` | "Finished" |
| needs the user's input | `waiting` | "Waiting for you" |

The whole hook-to-state mapping, with no branch on what kind of input is wanted:

| Event | State |
|---|---|
| `UserPromptSubmit`, `PreToolUse` | `running` |
| `PermissionRequest` | **`waiting`** |
| `PostToolUse` | `running` |
| `Interrupt` | `idle` — see the correction below |
| `Stop` | `idle` |

**The Esc-versus-menu question is dropped, and it was never worth asking.** It
existed only to decide how long a stale `waiting` may linger. Under this model
there is nothing to decide: if a menu refusal fires no event, the session sits on
"Waiting for you" until the next event arrives — and that is still TRUE, because
a turn whose tool was refused is a turn waiting for its human. The worst case of
the thing being probed is the correct answer.

Consequences for the tasks below:
- Nothing needs `tool_name`, `tool_input`, `tool_use_id` or `tool_response`. The
  shim can ignore every one of them, which also means it never touches the
  fields carrying the owner's own text.
- The missing `tool_use_id` on `PermissionRequest`, noted above as a
  correlation problem, stops being one — there is nothing to correlate.
- `permission_mode` stays useful for one thing only: suppressing `waiting`
  in modes where an approval prompt cannot happen.

## Correction to the table above, 2026-09-04

The mapping row originally read `PostToolUse, Interrupt -> running`. **That was
wrong, and my own probe data says so.** In the captured refusal the sequence was
`UserPromptSubmit -> PreToolUse -> PermissionRequest -> Interrupt`, and then
nothing: **no `Stop` ever followed.** `Interrupt` is terminal for its turn.

Filed under `running`, that leaves the panel saying "Working" over a session that
has stopped, until `ABANDONED_AFTER_S` times it out an hour later. Filed under
`idle` it says "Finished", which is both true and exactly the owner's second
state -- "finished and waiting for user".

The plan had this right in the first place (`_IDLE_EVENTS`); the compressed table
I wrote when recording the owner's ruling lost it, and task 3's implementer
correctly followed the newer document over the older plan. The error was mine and
the fix is `_IDLE_EVENTS = ("Stop", "Interrupt")`.
