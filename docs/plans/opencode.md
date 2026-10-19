# OpenCode support

Status: built on branch `opencode-support`, 2026-10-19. This is the design
the code follows, and the research it rests on.

Most of the team runs OpenCode (the open-source terminal agent,
`github.com/anomalyco/opencode`, which `sst/opencode` now redirects to) with
GLM 5.3 on Z.ai's GLM Coding Plan. BLINK showed them nothing. This adds
OpenCode as a third provider, `opencode`, beside `claude` and `codex`: the
same gauges (5-hour window and week) and the same activity light (running,
waiting on you, your turn, failed), with no firmware change.

## What was measured

All of it on OpenCode 1.19.3 (npm `opencode-ai`, tag `v1.19.3`, 2026-10-16),
installed into a scratch directory with HOME and every XDG variable pointed
inside it. It ran against a local stdlib mock of an OpenAI-compatible
endpoint, so no real model was called, nothing was logged in, and no quota
was spent.

**Storage.** One SQLite file, `opencode.db`, in the data directory: since
1.2.0 (2026-02-14). The `storage/` JSON tree the troubleshooting docs still
describe is no longer written. The data directory is
`$XDG_DATA_HOME/opencode`, else `~/.local/share/opencode` -- on macOS too, not
`~/Library`, and on Windows `%USERPROFILE%\.local\share\opencode`, because
OpenCode's path library (xdg-basedir 5.1.0) has no Windows case. Other release
channels write `opencode-<channel>.db`, and `OPENCODE_DB` moves it. The DB is
WAL mode: most of the data sits in `opencode.db-wal`, so an mtime check has
to look at both files.

**What the DB cannot say.** Session status (`busy`/`idle`/`retry`) is held
in memory only. A permission prompt looks exactly like a running tool. A
retry on a rate limit leaves no trace until all five attempts fail -- and the
wait honours `Retry-After` with no practical cap, so a quota error can hold a
session in `retry` for hours. There is no pid, port or lock file, and a
killed OpenCode leaves its session marked as running forever. So the DB is
not the source of live state.

**Plugins are.** OpenCode loads every `*.js` / `*.ts` file in
`<config>/opencode/plugin/` (or `plugins/`), where config is
`$XDG_CONFIG_HOME`, else `~/.config` -- also on Windows. A plain `.js` file
that imports only `node:` builtins loads with no npm install. Its `event`
hook receives every bus event. These were captured from a real run:

| event | properties used |
|---|---|
| `session.created` / `session.updated` | `info.id`, `info.parentID` (set on a subagent's session), `info.directory` |
| `session.status` | `sessionID`, `status.type` = `busy` / `idle` / `retry` (retry: `attempt`, `message`, `next` = epoch ms) |
| `session.error` | `sessionID`, `error.name` (`APIError`, `MessageAbortedError`, ...) |
| `session.deleted` | `info.id` |
| `permission.asked` / `permission.replied` | `id` / `requestID`, `sessionID` |
| `question.asked` / `question.replied` / `question.rejected` | the same |

The `permission.ask` hook is declared in the plugin API but never called in
1.19.3; the `permission.asked` event is what fires. There is no shell-hook
config of the kind Claude Code and Codex have.

**Usage.** OpenCode stores token counts and cost, but no limit, so there is
no percentage to read on disk. The plan's own quota endpoint has it:

    GET https://api.z.ai/api/monitor/usage/quota/limit          (global)
    GET https://open.bigmodel.cn/api/monitor/usage/quota/limit  (mainland China)
    Authorization: <the Coding Plan API key>

```json
{"code":200,"success":true,"data":{"level":"lite","limits":[
 {"type":"CREDIT_LIMIT","unit":3,"number":5,"usage":2000,"currentValue":71,
  "remaining":1929,"percentage":3,"nextResetTime":1786073946574},
 {"type":"CREDIT_LIMIT","unit":6,"number":1,"usage":10000,"currentValue":71,
  "remaining":9929,"percentage":1,"nextResetTime":1786660486998}]}}
```

- The 5-hour window is `unit 3, number 5`, and the week is `unit 6, number 1`.
  Match on the unit and number, never on position or type: both entries can
  share a type.
- Plans bought before 2026-07-30 report `TOKENS_LIMIT` instead of
  `CREDIT_LIMIT`, sometimes with only `percentage`. They also report a
  monthly tool quota, `TIME_LIMIT` with unit 5, number 1, which we ignore.
- `usage` is the allowance, not what was used. `nextResetTime` is in epoch
  milliseconds.
- **Failures come back as HTTP 200** with `success:false` (code 1001 for no
  key, 401 for a bad one). Check `success` and `code`, not the HTTP status.
- **The 5-hour reset time is sometimes about 10 hours ahead** (CodexBar
  #2712, #2871). A 5-hour reset more than 5 h + 60 s away is dropped rather
  than shown.

The key is the one OpenCode already holds, in `<data>/opencode/auth.json` as
`{"zai-coding-plan": {"type": "api", "key": "..."}}`, or `zhipuai-coding-plan`
for the mainland plan. The provider id picks the host.

Sources: the full reports are in this branch's PR description. Code read:
OpenCode `packages/core/src/{global.ts,database/database.ts,session/sql.ts}`,
`packages/schema/src/{session-status-event.ts,v1/session.ts,v1/permission.ts,v1/question.ts}`,
`packages/opencode/src/config/plugin.ts`, `packages/opencode/src/session/retry.ts`;
CodexBar `zai.js`; Z.ai's `zai-coding-plugins` `query-usage.mjs`;
opencode-glm-quota; docs.z.ai `devpack/overview` and `api-reference/api-code`.

## Design

### Live state: a plugin that writes the hook slot format

`tools/blink-opencode.js` is installed as `<config>/opencode/plugin/blink.js`.
It writes exactly the files `tools/blink-hook.sh` writes, into
`~/.blink/state-opencode`, so that `claude_state` -- its state machine, the
waiting marker, the tombstone, pid liveness and the abandonment sweep -- reads
OpenCode unchanged, as it already does for Codex:

| OpenCode | written as | state |
|---|---|---|
| `session.status` busy | `UserPromptSubmit` | running |
| `tool.execute.before` / `.after` | `PreToolUse` (refreshes the clock, never touches the marker) | running |
| `permission.asked`, `question.asked` | `PermissionRequest` + `<sid>.waiting` | waiting |
| `permission.replied`, `question.replied` / `.rejected` | `PostToolUse`, marker removed once no request is open | running, if the request is still open and the turn has not finished |
| `session.status` idle | `Stop` | idle (your turn) |
| `session.status` retry, `next` more than 60 s away | `StopFailure` | failed |
| `session.status` retry, sooner | `PreToolUse` | running |
| `session.error`, `MessageAbortedError` | `Interrupt` | idle |
| `session.error`, anything else | `StopFailure` | failed |
| `session.deleted` | `SessionEnd` (slot removed, tombstone left) | -- |
| a session with `parentID` | `SubagentStart`/`Stop` under the parent: an agent, not a session | -- |

Four rules the table above does not show, all found by running the plugin
inside real OpenCode 1.19.3:

- **OpenCode always sends `idle` straight after an error,** in the same
  millisecond. Mapped literally, `Stop` overwrote `StopFailure` before
  anything could read it. So a failure or an abort holds until the next
  `busy`.
- **`busy` fires on every model step,** not once per prompt. So unlike the
  shim's `UserPromptSubmit`, it does not clear the waiting marker. The marker
  lives exactly while a permission or question is open, and `idle` or an
  error closes them all. An abort with a prompt pending sends no `replied` at
  all.
- **The process exiting writes `SessionEnd`.** Only `opencode run` delivers
  that event; a TUI quit with ctrl-c and `serve` on SIGTERM do not. For those,
  the pid check drops the slot on the next poll, as long as the daemon saw
  that pid alive at some earlier poll -- which, polling every two seconds, it
  has for any session that lasted longer than that. Catching the signals
  ourselves would stop OpenCode exiting.

  OpenCode's directory has its own pid rule, "trusted once seen alive"
  (`claude_state.SeenAlivePids`), and never touches the process-wide
  suspension Claude and Codex share. Found end to end with real OpenCode
  1.19.3, 2026-10-19: ctrl-c one second after the answer left a slot
  seconds old whose pid was gone, which that suspension reads as "pids mean
  nothing on this machine". It switched pid liveness off for the whole
  daemon, so the closed session stayed amber "your turn" for an hour, and
  Claude's and Codex's dead sessions lingered with it. Now a pid the daemon
  has seen alive ends its session the moment it is gone, whatever the
  slot's age. A pid it has never seen alive -- a container's, under a
  bind-mounted `~/.blink` -- never ends one, and that session falls back to
  the hour. A fresh `blink status` has seen nothing alive, so it may count a
  just-closed session that the daemon has already dropped. That errs the
  safe way.

  On Windows the check is `OpenProcess` + `GetExitCodeProcess`, for this
  directory only. Claude's and Codex's directories still have no liveness
  there.
- **A session id that fails the pattern is skipped, not written as
  `unknown`.** One `serve` process carries many sessions, and folding them
  into one slot is the misreport the per-session slots exist to prevent.

A subagent's permission prompt or long retry shows on its parent session.

**An answer after its turn is over changes only the marker.** Found in
review, 2026-10-19: busy, a question, Esc (`MessageAbortedError`, then
idle), and then `question.rejected` for the question the abort tore down --
or a `permission.replied` naming a request this process never saw, after
`Stop` -- each wrote `PostToolUse` and painted a finished session as running
for up to an hour. The slot moves back to running only for a request the
plugin saw asked and still holds open, in a turn that is not in `Stop`,
`Interrupt` or `StopFailure`.

A short retry is a "high concurrency" blip that clears in seconds; painting
it red would cry wolf. A retry scheduled a minute or more out is the quota
case, and that is the headline on a usage gauge.

The plugin keeps the shim's privacy rules:
- Session ids pass the same pattern, with an alphanumeric first character
  and at most 64 characters.
- Only the project directory's last segment is kept, as the same 24-byte
  pattern, never the path.
- Files are mode 0600 in a 0700 directory, and the directory is the
  installer's: `blink install` creates `~/.blink/state-opencode`, and the
  plugin writes only while it exists. It never creates `~/.blink` or the
  state directory. Found in review, 2026-10-19: it used to `mkdir -p` on
  every write, so an OpenCode left running across `blink uninstall` -- which
  keeps the plugin loaded after its file is gone -- put the directory back
  on its next event.
- Writes go to a temp file named with the pid, then are renamed.
- `pid` is `process.pid`. OpenCode is one process, so the pid is the
  session's own process, and liveness works on POSIX and Windows (see the
  `SessionEnd` rule above).
- Every handler is wrapped so that it can never throw into OpenCode.

The first line of the file is an ownership marker. Uninstall removes the
file only when that line is ours, and refresh rewrites it only then.

### Usage: the quota endpoint, read by the daemon

`pc/providers/opencode.py` emits two frames under provider `opencode`:
- a usage frame (`src: "zai"`) from the quota endpoint;
- a state frame (`src: "state"`) from the slots.

The usage frame is the first time the daemon makes a request with a user's
credential. The rules:

- The key is read from auth.json at each fetch and held only in the request
  header. auth.json, which holds every provider's token and not only the
  plan key, is opened only when a fetch is due -- not on the five-second
  ticks between (found in review, 2026-10-19). It is never logged, never put in an exception message, and never
  written anywhere.
- The request goes only to the host that key was issued for: z.ai's
  endpoint for `zai-coding-plan`, BigModel's for `zhipuai-coding-plan`.
- **When to fetch:**
  - at most once a minute while OpenCode has been active in the last 10
    minutes;
  - once every 10 minutes otherwise;
  - not at all once OpenCode has been idle for 7 days.

  Activity means the mtime of the DB, its WAL, or the state directory.
- The fetch runs on a background thread, so the poll loop never waits on the
  network. A failure keeps the last good reading, which then ages into
  `stale` like any other -- unless the failure is the key itself: a key the
  plan rejects, or one removed from auth.json (the install text's way to
  stop the requests), drops the reading at once rather than leaving it on
  the panel for up to a week. An HTTP 403 is not about the key -- a WAF, a
  geo block or a proxy sends it without looking -- so it keeps the reading
  (found in review, 2026-10-19). Reaching the 7-day give-up drops it too,
  since no fetch will ever replace it, and a window whose reset is already
  past when the reply is read has no percentage.
- **Where OpenCode is.** The service (launchd, systemd, the Task Scheduler)
  runs without the shell's `XDG_DATA_HOME` / `XDG_CONFIG_HOME`. So where those
  move OpenCode off its defaults, `blink install` records the directories it
  saw in `~/.blink/opencode-paths.json` (0600, written atomically, removed by
  uninstall), and every lookup goes variable, then record, then default.
  `blink status` warns when this shell's answer differs from the service's.
  Found in review, 2026-10-19: install and status found OpenCode while the
  daemon looked in `~/.local/share` and showed nothing.
- `install` says all of this in its disclosure before doing it, as the Codex
  hooks step does.

A desk where OpenCode runs a model that is not on the GLM plan has no
OpenCode gauges, rather than a number from somewhere else. Its sessions
still drive the light beside Claude's or Codex's numbers. On a desk where
OpenCode is the only tool, nothing has a number, and the board gets a page
with blank rings (`--%`, `src` `none`) so the light and the project name
still have a message to ride on (`ingest.IngestionBus._blank_poll`). This
sentence used to say the board kept its waiting screen, which is what the
code did until 2026-10-19 -- and on that screen there is no light at all,
and a USB update waiting for usage data reverts at 90 seconds. Firmware
older than v0.7.0 draws a blank ring as 0%, so the daemon holds the page
back from it and says in the log that the board needs updating.

### Three providers

The panel has two rings and one light.

- **The rings** go to the preferred provider (Claude by default), then to
  whichever was used most recently. `rank` sorts on `active_at`, not
  `observed_at`. OpenCode's `observed_at` is the time of the fetch, so a just
  read number is not flagged as old. Its `active_at` is the last real
  OpenCode activity, so polling it does not take the ring from a Codex that
  is actually in use.
- **A state frame's `active_at` is its last real event.** Every provider's
  state frame used to carry `now`, so any provider with a slot under an hour
  old tied, the stable sort kept poll order, and Codex held the second ring
  for an hour after it went idle (found in review, 2026-10-19). Now it is
  the newest slot's clock (for Codex, the newer of that and the rollout's
  mtime), and `now` only while a session is running, because a long tool
  call writes nothing until it ends. Sleep is unaffected: the board never
  dozes on a running, waiting or failed light, and inside the hour this
  changes the age is under an hour against a four-hour threshold.
- **The light** folds every provider: the two on the rings, any past them,
  and any that has a state but no number. A waiting OpenCode session turns
  it amber even when OpenCode has no ring, or no GLM key and so no gauges.
  The board's `active_age_s` folds the same set, so an OpenCode turn a
  minute ago keeps the panel awake beside two quiet rings.

### Not in this change

- **Firmware.** The provider name is a free string on the wire and the
  board renders any one. It shows as `Opencode`, because `tag_cased`
  capitalises only the first letter. An OpenCode edition (boot clip, sleep
  animation) and a third page are separate decisions.
- **OpenCode 2.0.** The `dev` branch moves auth.json into a SQLite
  `credential` table. The contract check fails the day a release tag
  carries that change.
- **Pay-as-you-go `zai` keys** have no plan quota to show.
