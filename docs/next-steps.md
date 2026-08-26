# Next steps

Two pieces of work, both extensions of what landed on `desk-hud-universal`.
Written against the code as it stands at `c1146a8`.

---

## A. The claude.ai web plugin

### Where it actually is

Built and tested, **never run against the real site**. That distinction is the
whole of this section.

| Piece | State |
|---|---|
| `extension/manifest.json`, `background.js` | written, loads unpacked |
| `pc/webbridge.py` receiver | 19 tests, verified live with `curl` |
| `ClaudeWebProvider` | merges like any other source, tested |
| **Header matching against real traffic** | **unverified** |

The receiver half is done. The extension observes response headers on requests
the page already makes and matches them **by shape** — `RE_LIMIT`,
`RE_REMAINING`, `RE_RESET`, `RE_USED_PCT` in `background.js` — because the real
header names are not a documented contract. If nothing matches, it reports
nothing and the panel falls back to the CLI and desktop sources. That is the
designed failure, and it is also indistinguishable from the extension being
broken.

### Step 1 — find out what claude.ai actually sends (~2 min, gates everything)

**A page-context probe cannot answer this, and it is worth knowing why before
someone tries it again.** Wrapping `window.fetch` on claude.ai and reading
`response.headers` was attempted (2026-08-26). It returned three header names —
`cache-control`, `content-length`, `content-type` — which is *exactly* the
CORS-safelisted set, so the result proved nothing. A control fetch of a
same-origin static asset returned 22 headers, confirming page JS can see them
same-origin; the three-header responses were cross-origin telemetry.

More to the point: even a clean negative from page JS would not settle it.
`chrome.webRequest.onHeadersReceived` sees response headers that `fetch()`
hides, so the extension has strictly more visibility than any probe run inside
the page. **The extension is the measuring instrument.** There is no shortcut
around loading it.

**This is now a one-command answer.** The extension reports what it observed
every 30 seconds whether or not it found anything, and `clauge status` prints
the verdict — no DevTools, no temporary logging.

1. `chrome://extensions` → Developer mode → Load unpacked → `extension/`.
2. Use claude.ai for a turn or two.
3. `clauge status`, and read the `Browser` line.

It says one of: not seen; running but no responses carried rate-limit headers;
headers seen but none usable; or working, with a count.

(An earlier version of this plan asked for a DevTools session. That was
replaced because the thing being diagnosed — a silent extension — is exactly
the thing a user cannot tell apart from a broken one, and making them open a
service-worker console to find out is a poor answer to a question the software
can answer itself.)

Three possible outcomes, and they lead to different work:

**(a) Rate-limit headers are present.** Best case. Replace the shape regexes
with the real names, keep the shape ones as a fallback, and add a fixture test
built from the captured header set. Half a day, and the feature is done.

**(b) Headers exist but express usage differently** — a reset epoch with no
limit, a token count instead of a percentage, per-model rows. The parser in
`buildPayload()` needs a real mapping, and `usedPct()`'s assumption that
`(limit - remaining) / limit` is the answer stops holding. One to two days,
mostly deciding what the numbers mean.

**(c) Nothing numeric comes back on any response.** Then header observation is
the wrong mechanism and there are two fallbacks, both worse:
  - **A content script reading the rendered usage UI.** Honest — it reads what
    the user is already being shown — but it breaks on any markup change and
    only sees numbers while the usage panel is open.
  - **Observing the response body of whatever endpoint the usage panel calls.**
    More reliable and more invasive, and it moves this from "reads headers" to
    "reads an undocumented API's payload", which is a live concern on this
    project's list. **Do not take this route without deciding that
    deliberately.**

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

## Rough order

1. **Web plugin Step 1.** Fifteen minutes, and it decides whether A is a
   half-day or a two-day job. Nothing else should start first.
2. **Flash the current branch and boot-verify it.** Still outstanding, and it
   is the only thing standing between this work and being real.
3. **The metadata decision** for B. It gates every part of B and is a judgement
   call, not an implementation task.
4. B Steps 1–3 (daemon side, ~1.5 days), then B Step 4 v1 (a few hours).
5. B Step 4 v2 and the web plugin's packaging, in whichever order matters more.
