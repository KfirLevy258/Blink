# Next steps

Two pieces of work, both extensions of what landed on `desk-hud-universal`.
Written against the code as it stands at `c1146a8`.

---

## A. The claude.ai web plugin

### Where it actually is

**Run against the real site, 2026-08-27. The question this section existed to
ask has an answer, and it is the bad one.**

| Piece | State |
|---|---|
| `extension/manifest.json`, `background.js` | loads unpacked in Chrome 152 |
| `pc/webbridge.py` receiver | 20 tests, and verified live from the extension |
| `ClaudeWebProvider` | merges like any other source, tested |
| Extension → daemon → `clauge status` | **works end to end** |
| **Rate-limit headers on claude.ai** | **none. 178 responses, 0 matches** |

### Step 1 — what claude.ai actually sends: ANSWERED (outcome c)

The extension was loaded unpacked, the daemon run from this branch, and
claude.ai driven through a page load, three reloads and one complete message
turn — the completion request included. The extension's own diagnostic
reported:

```
{"t": ..., "responses": 178, "matched": 0, "usage_reports": 0}
clauge status → Browser  extension running, but none of 178 responses
                         carried rate-limit headers
```

178 responses observed on `https://claude.ai/*`, **zero** carrying anything
matching `RE_LIMIT`, `RE_REMAINING`, `RE_RESET` or `RE_USED_PCT`.

Two things that measurement does establish, and they are worth separating from
the negative result:

  - **The plumbing works.** The extension woke on real traffic, reached
    `127.0.0.1:9877`, and the daemon's crumb turned into an accurate line in
    `clauge status` without anyone opening a service-worker console. That was
    the other thing this step was for.
  - **`extraHeaders` would not change it.** That flag exists for `Set-Cookie`
    and the CORS-restricted set; `onHeadersReceived` already sees every
    ordinary response header, which is why the extension was the measuring
    instrument in the first place. There is no hidden header to go and find.

So this is **outcome (c)** as written below: header observation is the wrong
mechanism for claude.ai, and the two fallbacks are the content script and the
response body. Both were already judged worse, and the second is on the
project's concerns list. **Nothing further should be built here without
deciding that deliberately.**

What the extension is worth as it stands: it is an honest, silent no-op that
says so out loud. It costs the user nothing and it tells the truth when asked.
That is a defensible thing to ship as an optional extra and a poor thing to
put in front of anyone as a feature.

### Step 2 — decide whether it ships at all

Currently load-unpacked only. Shipping it properly means a Chrome Web Store
developer account, review, and a privacy disclosure describing exactly what
leaves the browser (nothing — it posts to loopback). Given (c) above is a live
possibility, the honest options are:

- ship it as an advanced, documented, load-unpacked extra; or
- put it in the Web Store once (a) or (b) is confirmed working.

Not worth store submission before Step 1 answers the question.

### Step 3 — Firefox (manifest done, UNTESTED)

`browser_specific_settings.gecko.id` and a dual `background` block are in the
manifest now — Chrome reads `service_worker` and ignores `scripts`, Firefox does
the reverse, so one manifest loads in both. `moz-extension://` was already in
the receiver's origin allow-list.

**Nothing about this has been run in Firefox.** Firefox's MV3 handles
`webRequest` differently from Chrome's and `strict_min_version` is a guess at
where `background.scripts` plus MV3 settled. Treat the manifest as a starting
point, not as support; the diagnostic in Step 1 will say immediately whether it
works.

### Step 4 — before it is on by default

The listener already binds loopback, allows one path and one method, caps the
body before reading it and checks Origin against an allow-list. Before it ships
enabled by default it deserves one pass from someone who did not write it —
that is the first listening socket this product has ever had.

---

## B. Session and agent status

### Where it actually is

`pc/providers/claude_state.py` answers **one** question: is Claude Code
running, waiting, idle or stuck. It answers it for **one** session.

The shipped design is a single slot: `tools/clauge-hook.sh` writes
`~/.clauge/state.json` as `{"event": ..., "t": ...}`, newest write wins. That is
correct for one terminal and **wrong for two** — two concurrent sessions
overwrite each other, so the panel shows whichever fired most recently and
silently misreports the other.

Sub-agents are not tracked at all. `SubagentStop` currently maps to `running`,
which is true but says nothing about how many agents are in flight or whether
any finished.

### The decision that comes first

Every option below requires the hook shim to capture **more than an event name
and a timestamp**. Today it captures nothing else, which is why the
metadata-only promise is structural rather than a matter of restraint — there
is literally nothing there to leak, and `check_hook_shim.sh` asserts it against
a payload carrying a session id, a transcript path and a tool name.

Tracking per-session status means capturing `session_id`. Showing *which
project* means capturing `cwd`. Counting agents means capturing `tool_name`.
All three are metadata, not content — but the promise stops being structural
and becomes a policy that has to be maintained. **Decide that explicitly before
writing any of it**, and if it goes ahead, update the install disclosure, the
README and `check_hook_shim.sh` in the same change.

### Step 1 — one file per session (half a day)

Shim writes `~/.clauge/state/<session_id>.json` instead of one `state.json`.
Per-file rather than one JSON object because the shim is POSIX `sh` with no
parser and no lock, and concurrent read-modify-write from several sessions is
exactly the corruption the atomic single write currently avoids.

Cleanup, since these accumulate:
- `SessionEnd` deletes its own file.
- The provider sweeps anything older than `ABANDONED_AFTER_S` (1 h) on poll.
- A crashed session leaves a file; the sweep is what collects it.

`ClaudeStateProvider.poll()` reads the directory and derives per-session states
with the existing `derive_state()`, unchanged.

### Step 2 — aggregate, do not enumerate (half a day)

The wire has **~210 bytes free** of the 512-byte line limit, and the board
drops an over-long line whole. A per-session array would blow that budget at
around four sessions and take the panel dark with no error.

So send counts, not a list:

```json
"n_run": 2, "n_wait": 1, "n_stuck": 0
```

~40 bytes. And keep the existing scalar `state` field, computed as **worst-of**
(`stuck` > `waiting` > `running` > `idle`), so firmware that predates this
change keeps working exactly as it does now. Additive, no `PROTO_VERSION`
move — the same rule the rest of this branch followed.

### Step 3 — agents (half a day, after Step 1)

Claude Code signals sub-agents through the hooks already installed:
- `PreToolUse` with `tool_name == "Task"` → an agent started
- `SubagentStop` → one finished

Counting them needs `tool_name` captured (see the decision above). With it, a
per-session file can carry `agents_running`, and the aggregate gains `n_agents`.

Note "finished" is **not** a state a pip can hold. It is an event — an agent
that finished five minutes ago is just `idle` now. If the goal is "tell me when
my agent is done", that is a **notification**, not a status, and it is a
different feature: a brief flash or colour change on transition, decaying after
some seconds. Worth separating in the design, because the two get conflated and
they need different mechanisms.

### Step 4 — the panel (one to two days, the real cost)

The pip already shows worst-of and needs no change.

For more than that, **the long-press gesture is free in the shipped build.**
The peek card (`#if HAVE_PER_MODEL`, `usage_view.c`) is compiled out whenever
`CONFIG_CLAUGE_WIFI_MODE` is off, which is every USB unit — so the card, its
rows and its dismiss logic exist, are known to work on hardware, and are
currently dead weight in the WiFi build only. Repurposing that scaffolding for
a session list is far cheaper than inventing a new surface.

Two increments:
- **v1, cheap:** a small count beside the pip when more than one session is
  live. Fits the existing header row; a few hours.
- **v2 is blocked by a decision already taken, and that is worth knowing before
  anyone starts it.** A card *listing sessions* needs per-session data on the
  wire, and Step 2 deliberately sends counts instead — a per-session array blows
  the 512-byte line budget at around four sessions and takes the panel dark with
  no error. So v2 as originally sketched cannot be built on the current
  protocol.

  Two honest options. Either build the card from the counts that *are* sent
  ("2 running, 1 waiting, 2 agents"), which is a modest gain over the `2s 2a`
  already in the corner — or add a second message type carrying one session per
  line, which the NDJSON protocol handles natively and the 512-byte limit then
  applies to per line rather than in total. The second is the real answer if
  per-session detail is wanted, and it is additive, so it costs no version bump.

Whatever the card shows, `usage_layout.h` and `tests/usage_layout/host_test.c`
must grow with it. The band below the gauges has 2 px and 0 px of clearance in
it, and that test is what will catch a new widget landing on the hint line
without anyone having to plug a board in.

---

## C. Codex, and the second page (DONE 2026-08-27)

The firmware has had a second provider page since `bca447f` — one provider per
screen, vertical swipe between them, a rail dot each — and the settings screen
has let you pick Codex as the main source for longer than that. **Nothing ever
reported Codex.** So `set_preferred("codex")` was refused every time the board
announced it (`board asked for provider 'codex', which is not reporting`),
`page_count()` stayed at 1, and both vertical swipes were no-ops. The feature
existed at every layer except the one that produces numbers.

`pc/providers/codex_cli.py` closes that. Codex CLI appends its own
`rate_limits` to the rollout log it keeps per session:

```
~/.codex/sessions/<YYYY>/<MM>/<DD>/rollout-<stamp>-<uuid>.jsonl
  → event_msg / token_count → rate_limits
      primary   {used_percent, window_minutes: 300,   resets_at}
      secondary {used_percent, window_minutes: 10080, resets_at}
```

Same shape as every other source here: a figure another program has already
worked out, read from a file it writes for its own reasons. No credential, no
prompt text, no network.

Three decisions in it worth not re-deriving:

  - **Windows are matched by `window_minutes`, not by `primary`/`secondary`.**
    Those are positions in a file we do not control, and getting them the
    wrong way round swaps the two dials silently. Position is the fallback,
    used only when the length is absent.
  - **`resets_at` is seconds, and is range-checked.** Claude Desktop's sample
    timestamps in the same daemon are milliseconds, so this is a live
    difference between two files, not a hypothetical one. Out of range costs
    the reset time and keeps the percentage.
  - **The newest file is not the newest reading.** A terminal left open moves
    its rollout's mtime without ever writing a `token_count`. The provider
    reads the tail of the few most recently touched files and takes the
    freshest *event*, then emits ONE frame — the percentages are account-wide,
    so six terminals are six copies of one answer.

Verified live, 2026-08-27: real rollout parsed, board flashed with this
branch, `p2: 'codex'` on the wire, and the board's stored `codex` preference
honoured for the first time (`[bridge] main source: codex`).

---

## Rough order

1. ~~**Web plugin Step 1.**~~ Done — see A. The answer is (c); the remaining
   question is a judgement call about the two fallbacks, not an
   implementation task.
2. ~~**Flash the current branch and boot-verify it.**~~ Done 2026-08-27:
   built, flashed over USB, board came up clean, `hello` at 0.6.0.
3. **The metadata decision** for B. It gates every part of B and is a judgement
   call, not an implementation task.
4. B Steps 1–3 (daemon side, ~1.5 days), then B Step 4 v1 (a few hours).
5. B Step 4 v2 and the web plugin's packaging, in whichever order matters more.

Two things noticed while testing that are not in either section:

- **`clauge status` says nothing about which providers are reporting.** With
  two of them on the wire and a preference living on the board, "Usage data
  fresh (1s old)" is now less than it could say.
- **The wire carries float noise** — `session_pct: 14.000000000000002` was
  observed. Harmless to the panel, but those are bytes inside a 512-byte line
  limit that section B is already budgeting against.
