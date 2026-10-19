# The activity light under OpenCode 2.0

Status: design, 2026-10-19. Not built. Read `docs/plans/opencode.md` first;
this changes only how OpenCode's live state reaches `~/.blink/state-opencode`,
and nothing downstream of the slot files.

## Why this exists

OpenCode 2.0 is not a future release. `@opencode/cli` 2.0.0 was published to
npm on 2026-09-11 and 2.0.32 is current; the README's primary install paths
(`opencode.ai/v2/install`, `brew install anomalyco/tap/opencode-v2`) install
it. The legacy package `opencode-ai@latest` is still 1.19.3, and GitHub's
release list still shows only 1.x -- which is why nothing here noticed.

On 2.0 the light is dark, silently:

- `blink.js` exports a named v1 factory (`export const BlinkStatePlugin`).
  2.0 wants a default export of `{id, setup}` or `{id, effect}`
  (`core/src/plugin/module.ts:78-91` at `dev` 6bac3b9), and a module without
  one fails to load with a log warning only (`core/src/plugin/supervisor.ts:63`).
  The person sees nothing, and neither does BLINK.
- Even ported, today's mapping would never fire: `session.status` and
  `session.idle` are still defined in 2.0's schema but nothing publishes them
  (`schema/src/session-status-event.ts:35-51`, no publisher in core or
  server). The TUI itself derives "running" from `session.execution.*`.

And the CI that was meant to catch exactly this could not:
`check_opencode_contract.sh`'s non-experimental cell resolves "the latest
release" from GitHub's `releases/latest`, which is 1.19.3. The `dev` cell did
fail, and is experimental by design. So a shipping major version broke the
light while every required check stayed green.

An earlier note (`docs/research/opencode-contract.md` §6) said the plugin
loader was gone in 2.0. It is not: discovery is unchanged
(`core/src/plugin/source-directory.ts:7,23-24` still loads `plugin/` and
`plugins/` `*.js|*.ts` from every config directory, the global one
included). Only the module's shape and the events changed.

## What 2.0 is, as far as the light cares

- **One background service per user**, shared by every TUI, `opencode run`
  and the Desktop app: `opencode serve --service` on 127.0.0.1 (port 49374;
  45278 on Windows). Plugins run INSIDE it, not in the TUI.
- **It registers itself** in `<state>/opencode/service.json`
  (`{id, version, url, pid, password}`, mode 0600), removed on a clean stop
  and left behind with a dead pid after a kill.
- **Plugins load once per active project location**, and each copy sees the
  whole global bus (`core/src/plugin/host.ts:258-267`, supervisor per
  location at `supervisor.ts:268`). A location unloads after 60 minutes idle.
- **Live run state is now in the database**, written in the same transaction
  as each event: `session_v2.time_suspended` while a turn runs,
  `time_idle` + `idle_outcome` (`succeeded` / `failed` / `interrupted`) when
  it ends, and `data.retry = {attempt, at, error}` on the last assistant row
  of `session_message` while a retry is pending.
- **Pending permissions and questions are memory only**
  (`core/src/permission.ts:130`, `core/src/form.ts:109-116`).

## Options considered

| | Setup for the person | States covered | Risk |
|---|---|---|---|
| **A. Dual-shape plugin** | none beyond today | all, live | 2.0's plugin API is young and documented as evolving |
| **B. Daemon reads the DB** | none | running, your turn, failed, interrupted, long retry, agents, folder. NOT waiting on a permission; questions only by heuristic | reads an internal table's columns |
| **C. Daemon subscribes to `/api/event`** | none on the default service | all | the daemon holds a password that drives OpenCode, shell included; a long-lived SSE client in stdlib; misses standalone servers; the stream is volatile by contract |

C is out. The password in `service.json` is a credential that runs commands
on the person's machine, and a frozen customer binary that reads and keeps
one is a liability for a light. A, then B as a fallback, covers the same
states without it.

## Design: A, with B underneath

### A. One plugin file, both shapes

`tools/blink-opencode.js` gains a default export alongside the v1 factory:

```js
export default { id: "blink", server: BlinkStatePlugin, setup: blinkV2 }
```

v1 (from 1.18.29) calls `server`, v2 calls `setup` -- the form OpenCode's own
docs give for a plugin that runs on both (`services/www/src/docs/content/
build/plugins/index.mdx:1792-1826`; v1.19.3's `readV1Plugin` accepts it,
`v1/packages/opencode/src/plugin/shared.ts:272-300`). The named export stays,
so nothing about v1 changes for a version that predates the object form.

`setup` subscribes with `ctx.event.subscribe()` and writes the same slots
through the same helpers as today. The 2.0 mapping:

| OpenCode 2.0 event | written as | state |
|---|---|---|
| `session.execution.started` | `UserPromptSubmit` | running |
| `session.execution.succeeded` | `Stop` | your turn |
| `session.execution.failed` | `StopFailure` | failed |
| `session.execution.interrupted`, reason `user` / `inactivity` | `Interrupt` | your turn |
| `session.execution.interrupted`, reason `shutdown` / `superseded` | nothing: the turn resumes or is replaced | -- |
| `session.retry.scheduled`, `at - now` > 60 s | `StopFailure` | failed |
| `session.retry.scheduled`, sooner | `PreToolUse` | running |
| `permission.asked` / `form.created` | `PermissionRequest` + `<sid>.waiting` | waiting |
| `permission.replied` / `form.replied` / `form.cancelled` | `PostToolUse`, marker removed once none is open | running |
| `session.created` with `parentID` | `SubagentStart` under the parent | -- |
| `session.deleted` | `SessionEnd` | -- |

Three rules that 2.0 forces and 1.x did not:

- **Filter on location.** Every location's copy of the plugin sees every
  event. Each copy writes only events whose `location.directory` is its own
  `ctx.location.directory`, or every slot is written N times by N writers
  racing on the same rename.
- **The pid is the service's.** `process.pid` inside the plugin is the
  background service, which outlives every TUI. Closing a TUI fires nothing,
  so 1.x's "the process exiting writes `SessionEnd`" means nothing here. A 2.0
  slot is dropped when the service pid dies (`claude_state`'s pid rule,
  unchanged), on `session.deleted`, or by the existing one-hour abandonment
  sweep -- the same outcome 1.x already has for a TUI killed with ctrl-c.
- **No error-then-idle latch.** 1.x sent `idle` in the same millisecond as
  every error, and the plugin had to hold the failure until the next `busy`.
  2.0's execution emits exactly one terminal event, so the v2 path has no
  latch, and must not inherit one.

### B. The database, when the plugin is not there

The daemon already opens `opencode.db` read-only at fetch time (the
credential table, and the token budget). When no slot for a session has been
written in the last minute AND the database shows that session with
`time_suspended` set, the daemon infers running; `time_idle` with
`idle_outcome` gives your turn, failed or interrupted; a pending
`data.retry.at` more than 60 s away gives failed. Liveness is the pid in
`<state>/opencode/service.json` -- read for that one field and nothing else,
since the file also holds the service password: parse it, take `pid`, drop
the rest, never log any of it.

B never claims waiting. A permission prompt looks like a running tool in the
database, and the light that says "running" during one is wrong in the
quieter direction -- which is why B is the fallback and not the design.

B is also what keeps the light honest across 2.0's next churn: if a release
changes the plugin API again, the light degrades to "no waiting" instead of
going dark.

## CI

- **Resolve "latest" from npm, both lines.** The contract job's required
  cells become `opencode-ai@latest` (1.x) and `@opencode/cli@latest` (2.x),
  read from the npm registry's dist-tags, mapped to the git tag. `dev` stays
  experimental. The cell that read GitHub's `releases/latest` is the reason
  2.0 shipped past this repo for five weeks.
- **2.x contract assertions**: the default-export shape in `module.ts`,
  discovery in `source-directory.ts`, the event names in the table above in
  `schema/src`, the `session_v2` columns B reads, and `service.json`'s `pid`
  field. The nine checks that fail on `dev` today (global.ts moved, the
  database path in `database-path.ts`, `v1/question.ts`) are rewritten for
  2.x paths rather than marked experimental.
- **The plugin driver** (`tests/ci/opencode_plugin_driver.mjs`) gains a v2
  harness: call `setup` with a fake context, publish 2.0-shaped events, and
  assert the slots -- including the location filter and the absence of the
  latch.

## Proving it

Not done until it has run inside real OpenCode, both lines, on this Mac and
on Windows, with no real quota spent: the isolated lab (mock OpenAI server,
XDG redirected into a scratch directory) gets `@opencode/cli@2.0.32` beside
1.19.3. For each: a prompt (running, then your turn), a `bash` call with
`permission: ask` (waiting, then running), a mock 429 with a long
`Retry-After` (failed), an abort (your turn), a subagent (agent count), and
then the plugin removed to watch B take over. Before writing any of it,
confirm two things the research could only read, not run: that 2.0's Effect
Schema decode ignores the extra `server` key, and that subagent sessions emit
their own `execution.*` events.

## Not in this design

- **Standalone 2.0** (`--standalone`, `service set disabled true`, a
  foreground `opencode serve`). Plugins still load there, so A works; B does
  not, because no `service.json` names a pid.
- **The v1 compatibility flag** (`experimental.legacy_plugins`, issue
  #48138). Open, unmerged, no maintainer commitment. Plan without it.
- **v1 releases before 1.18.29**, which do not know the object form. They
  keep loading the named export, as today. Dual-shape files have an open bug
  on 1.18.31 (#50172); the lab run on 1.19.3 decides whether the named export
  needs to move to a second file for that range.
