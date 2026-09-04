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
