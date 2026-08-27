# Next steps

What is left, and what has been closed off. Written against the code as it
stands on `desk-hud-universal`.

---

## A. Browser usage: measured, and not available (CLOSED 2026-08-28)

The one place usage happens that neither the CLI hook nor the desktop cache
can see is a claude.ai tab. A browser extension and a matching localhost
receiver were built to close that gap, and then run against the real site on
2026-08-27: a page load, three reloads and one complete message turn, the
completion request included.

**178 responses from `https://claude.ai/*`, zero carrying a rate-limit
header.** Not a partial match, not an unrecognised name — nothing shaped like
a limit, a remaining count, a reset time or a used percentage.

That is the whole finding, and it is recorded here so nobody rebuilds the
apparatus to ask the question again. Two details it is worth keeping with it:

  - **`extraHeaders` would not have changed the result.** That flag exists for
    `Set-Cookie` and the CORS-restricted set; `onHeadersReceived` already sees
    every ordinary response header. There is no hidden header to go and find.
  - **The remaining mechanisms are worse, not merely untried.** Reading the
    response body or injecting a content script both mean touching page
    content rather than observing metadata, and the second is already on the
    project's concerns list. Neither should be started without deciding that
    deliberately.

The extension, the `pc/webbridge.py` receiver, `ClaudeWebProvider`, their
tests and the `Browser` line in `clauge status` were all removed on
2026-08-28. Three things paid for a source that returns nothing: a listening
socket open on loopback for the daemon's whole life — the only one this
product had — an install that asks a buyer to grant a browser extension read
access to their Claude traffic, and about a thousand lines to keep working.
The code is in git history if claude.ai ever grows the headers; reviving it is
a revert, not a rewrite.

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

**The "main source" setting is gone (2026-08-27).** It chose which provider
owned the outer ring back when both shared one gauge. They do not: each has a
page, and "which one is in front" is answered by the page you are looking at.
`cfg_get/set_main_src` stay, because the value still goes to the host on every
`hello` where the daemon uses it to break ties when merging sources — that is
a host-side meaning and not something to settle from across the room with a
fingertip.

---

## D. The Codex edition (DONE 2026-08-27/28)

A second SKU: same board, same firmware image, same wordmark, **different boot
clip and nothing else**. Which one a unit plays lives on the UNIT — an
`edition` byte in the sealed config record, written once over USB with
`clauge provision --edition codex|claude`, read once at boot by
`bootclip_active()`.

A build-time flag was rejected because it forks OTA: the manifest names one
firmware and `hello` carries no edition, so a Codex unit would be offered the
Claude image and revert silently in the field, months later. Both clips are
compiled in instead; the second costs 17 KB on an image using 601 KB of 4 MB.

**It is WRITE ONCE, and that took two locks, not one (2026-08-28).**

  - `cfg_set_edition()` latches: the first successful write sets
    `edition_locked` and every later one returns `-EPERM`. "Not reachable from
    the settings screen" was being treated as the whole enforcement and is not
    — the message arrives over USB from whatever is on the other end of the
    cable, and `clauge provision` is the same binary the customer installs.
  - **The edition survives `cfg_reset()`.** Factory reset used to wipe the
    whole record, which made the settings menu a second route to the same
    change: reset, re-provision, and a Codex box plays the Claude clip. Two
    taps and a cable, no CLI. A reset wipes what the USER put on the device;
    the edition is a property of the enclosure the board is screwed into.

What remains is erasing the config partition with esptool over USB with the
board held in bootloader mode — a factory operation by construction, which is
the boundary that was wanted. That applies to dev boards too.

`proto.c` had a matching hole: it skipped the write whenever the stored
edition already equalled the requested one. `0` means both "Claude" and "never
stamped", so provisioning a blank board as claude reported success, wrote
nothing, and left it stampable as codex by anyone with the cable. The latch
decides now, not the value.

**The clip is DRAWN, not filmed.** Four iterations built it out of the shipped
Claude clip's own frames and the panel showed "glitters and jitters" around
every shape. That clip is h264: its edges are antialiased and motion-blurred,
and hard-thresholding them to two colours leaves 13-30 stray pixels per frame
that MOVE every frame — invisible on the original's busy ground at speed,
boiling on a flat held box. `tools/make_bootanim_codex.py` draws everything at
4x and thresholds, so identical geometry gives identical pixels. Blob halved to
7,571 B. **Generalise it: never threshold filmed material into a two-colour
panel asset.**

Open: white on the clip's `#76B1DB` ground measures **2.31:1**, under the 3:1
graphic floor, and production panels are the bright ones. `#538EB8` is 3.54:1,
`#4C82A8` is 4.15:1, black on the current blue is 9.08:1. Raise it once before
launch.

---

## Rough order

1. ~~**Browser usage.**~~ Closed — see A. Measured, unavailable, removed.
2. ~~**Flash the current branch and boot-verify it.**~~ Done 2026-08-27:
   built, flashed over USB, board came up clean, `hello` at 0.6.0.
3. **The metadata decision** for B. It gates every part of B and is a judgement
   call, not an implementation task.
4. B Steps 1–3 (daemon side, ~1.5 days), then B Step 4 v1 (a few hours).
5. B Step 4 v2.

Two things noticed while testing that are not in either section:

- **`clauge status` says nothing about which providers are reporting.** With
  two of them on the wire and a preference living on the board, "Usage data
  fresh (1s old)" is now less than it could say.
- **The wire carries float noise** — `session_pct: 14.000000000000002` was
  observed. Harmless to the panel, but those are bytes inside a 512-byte line
  limit that section B is already budgeting against.
