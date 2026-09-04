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
tests and the `Browser` line in `blink status` were all removed on
2026-08-28. Three things paid for a source that returns nothing: a listening
socket open on loopback for the daemon's whole life — the only one this
product had — an install that asks a buyer to grant a browser extension read
access to their Claude traffic, and about a thousand lines to keep working.
The code is in git history if claude.ai ever grows the headers; reviving it is
a revert, not a rewrite.

## B. Session and agent status (DONE 2026-08-26)

**This section described unbuilt work for two days after it was built.** It is
kept, rewritten, because the decision it turned on is worth having on record --
and because a plan that describes shipped code as pending is worse than no
plan: it sends someone to build a thing twice.

### What was built

`tools/blink-hook.sh` writes one file per session (`~/.blink/state/<id>.state`)
and one per agent, so two terminals no longer overwrite each other.
`pc/providers/claude_state.py` reads the directory, derives a state per session
from the last event, sweeps what has died, and reports the worst of them plus
an exact live agent count. Ten hook events are installed:

    SessionStart  UserPromptSubmit  PreToolUse  PostToolUse  Notification
    Stop  StopFailure  SessionEnd  SubagentStart  SubagentStop

Three of those came from reading the hooks reference rather than assuming:
there is no `session_id` environment variable (only `CLAUDE_PROJECT_DIR`), so
it is parsed from the payload; `SubagentStart`/`SubagentStop` carry `agent_id`,
which is what makes an exact lock-free count possible (one file per agent); and
`StopFailure` runs INSTEAD of `Stop` when a turn dies on an API error, carrying
`error: "rate_limit"` — the headline on a usage gauge, so it earned its own
state ranked above `stuck`.

### The decision that came first, and how it went

Every option here needed the shim to capture **more than an event name and a
timestamp**, which is what made the metadata-only promise structural rather
than a matter of restraint. It was taken deliberately: the capture widened to
`session_id` and `agent_id` and **nothing else** — no prompt, no tool
arguments, no paths, no message text. The install disclosure, the README and
`check_hook_shim.sh` were updated in the same change, and the shim is asserted
under sh, bash and dash against a payload that deliberately contains all the
things it must not keep.

The session id becomes a FILENAME, which makes it the only attacker-shaped
input on a path in this product. The character class in the shim's `sed`
pattern is the sanitiser, and traversal is pinned by test.

### What is still open

`stuck` is inferred from silence at 180 s, which is a guess about how long a
tool may legitimately take. Nothing has been measured against real long-running
tools. And a subagent that outlives `ABANDONED_AFTER_S` (1 h) is swept from the
count even though it is still running, because the shim never refreshes its
file's mtime.

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
`blink provision --edition codex|claude`, read once at boot by
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
    cable, and `blink provision` is the same binary the customer installs.
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

Done 2026-08-28: the ground is now `#4C82A8` (4.15:1 against white; it was
`#76B1DB` at 2.31:1, under the 3:1 floor). `tools/make_bootanim_codex.py`
defaults to it.

---

## Rough order

Everything with a section of its own is closed. What is left is not
implementation:

1. **Decided, 2026-08-28** (the README's "Decisions" table is the public
   record): the name is **BLINK**, everywhere, repository included; a config
   record from before the edition latch migrates **latched as Claude**, since
   every fielded unit is a Claude unit and the open reading would have handed
   each owner `blink provision --edition codex`; `daemon.auto` ships **off**;
   the Codex clip's ground is now `#4C82A8` (4.15:1 against white, up from
   2.31:1).

   Still open: the Claude Desktop cache path on Windows and Linux -- an
   Electron convention, never observed; `blink status` names the path it
   looked at, and one run beside a signed-in Desktop settles it; macOS
   notarisation (needs an Apple developer account); a board
   id on the settings screen; the disclosure question on the five known
   concerns -- four of which are WiFi-build only and are not compiled into
   the shipping image, but whose code is public; and whether the Codex clip,
   which mirrors the Claude clip and lands on a `>_` glyph on Codex blue,
   trades on a second company's mark.
2. **Merge and cut a release.** The published v0.6.0 predates the second page,
   the swipe detector, Codex as a provider, the Codex edition, per-page
   freshness, the burn rate and every fix from the August review.
3. **Two things nobody has watched run**: Windows, and an M-series Mac. CI
   exercises the installer on both; no human has seen the product work there.

Smaller, still open:

- **`blink status` says nothing about which providers are reporting.** With
  two on the wire and a preference living on the board, "Usage data fresh
  (1s old)" is less than it could say.
- **The wire carries float noise** — `session_pct: 14.000000000000002` was
  observed. Harmless to the panel, but they are bytes inside a 512-byte line,
  and the fully loaded frame already measures 484 of it.
- **`cfg_set_main_src()` has no callers** since the "Main source" row was
  removed from the settings screen. The board therefore always announces
  `pref: claude`, so a user running both cannot make Codex the primary dial.
  Three files still describe it as a live user choice. Either delete the field
  and its migration, or give the choice a home -- the page pill is the obvious
  candidate -- but it is a design decision, not a bug to patch.
- **A state-only frame is dropped by the normalizer.** On a machine with the
  hooks installed but no statusline payload and no desktop app, the activity
  light has nothing to ride on: `merge()` returns None for a provider group
  carrying no percentage, and sending one would blank the dials instead. The
  real answer is a `state`-only wire message that does not touch them.

**The pip row (2026-09-02).** The header's single execution-state dot became a
row of pips, one per live session, in the gap between the clock and the brand
— which was empty the whole time. The clock is back in its corner and the
rings are back to 120 as a result, and the hint line no longer counts
sessions, because the row does. `fmt_pips()` owns every decision (mode, order,
overflow); `usage_view.c` only positions and colours.

Verified live on the board, 2026-09-02, five frames driven through the real
daemon with `up_ms` climbing 10,263 → 241,128 ms across all of them and no
reset:

| sessions | on the wire | what the row must show |
|---|---|---|
| 3 | `n_run 1` | pip mode, 3 pips: 1 green, 2 amber |
| 6 | `n_run 4` | pip mode, 6 pips — the threshold |
| 7 | `n_run 4, n_stuck 1` | counts mode, an empty state skipped |
| 8 | `n_run 4, n_wait 1, n_stuck 1` | counts mode, `finished` dropped by the overflow rule |
| 16 | `n_run 12, n_wait 1, n_stuck 1` | counts mode, a two-digit tally through the measured-width path |

The daemon omits a zero, so `n_wait`/`n_stuck` are simply absent below —
absent means zero on both sides.

**Not verified: the panel itself.** Every row above is the wire and the board's
liveness, not a photograph. `usage_view.c` needs LVGL and has no automated
coverage of any kind, so whether six pips read as *six* rather than a smear —
the threshold the whole design turns on — is still unanswered, as is whether
the row visibly clears the clock and the wordmark. One session below 3 was
never driven, because reaching it would have meant deleting the owner's real
state files.

**Re-flashed 2026-09-02, and why the table above was briefly wrong.** The
frames in that table were driven at `199df2d`. Two commits later the tally
numeral moved from y=8 to y=6 — its line box had been ending exactly on
`STATUS_Y`, with no clearance to the hint line at all — and
`usage_view_set_counts()` gained the `if (!built)` guard its siblings have.
So the evidence described firmware the branch no longer had, and the one fix
whose whole purpose was a visible 2 px had never run on the board.

Re-flashed at `2ad00ec` and re-driven: 8 sessions (`n_run 4, n_wait 1,
n_stuck 1` — the overflow rule) and 16 (`n_run 12, n_wait 1, n_stuck 1` — the
two-digit tally, which is the case the numeral's Y actually matters in).
`up_ms` 10,263 → 120,346 ms, no reset. The five rows above still hold; only
the image they were taken from changed.

**The hour took the row (2026-09-02, later the same day).** The line under
BLINK read "Working" over a row of green pips — the panel spending its one
sentence to repeat what colour already said. It shows the time instead, and
yields to a sentence only while something wants a person: a failed turn, a
wedged session, an open prompt. "Finished" stays a pip on the owner's call —
not an error, and a desk that finishes a session every few minutes would
never see the clock.

The clock leaving the corner gave the pip row the whole 120 px from the bezel
to the wordmark, up from 75. Counts mode holds **four** groups now, so
`finished` is no longer dropped on an ordinary desk and the overflow rule is
the edge case it was always described as.

Verified on the board at `cecd489`: `state: idle` (clock holds the row),
`waiting` and `failed` (each takes it), then back to `idle` when the probes
cleared. `up_ms` 10,265 → 120,350, no reset.

Verified by rendering, which is the part the board cannot show: the six
scenes in `tools/panel_render/render_main.c` compile `usage_view.c` unchanged
against real LVGL. Six pips read as six. Four count groups fit. Two 9999
groups fit and the rest are dropped by the wall guard rather than crossing it.

Two numbers were wrong and are now derived rather than typed:
`BRAND_W` — "BLINK" measures 52 px at montserrat_14 with the letter_space of
2 the code sets, so the wordmark starts at 134, not the 136 a comment claimed
from a tracking value this code has never used. The clock's worst case was 49,
not 47. Both errors ran in the direction that eats the row's clearance.
