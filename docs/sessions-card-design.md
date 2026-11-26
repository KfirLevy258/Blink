# The sessions card: one row per open session

**Date:** 2026-11-24
**Status:** design, written before the code (issue #18)
**Scope:** `tools/blink-hook.sh`, `pc/providers/claude_state.py`,
`pc/providers/codex_state.py`, `pc/providers/claude_cli.py`,
`pc/session_rows.py`, `pc/protocol.py`, `pc/ingest.py`, `pc/bridge.py`,
`firmware/src/proto.c`, `firmware/src/msg_parse.c`, `firmware/src/fmt.c`,
`firmware/src/usage_layout.h`, `firmware/src/usage_view.c`,
`firmware/src/ui_settings.c`

---

## Why this reopens a decision

`docs/session-name-hint-design.md` (2026-09-02) put a sessions page out of
scope and called it "the right answer for the full breakdown with words".
The hint line it built names a session only when exactly one holds the
winning state. With three terminals open and one waiting, the board says
`Waiting for you` and a row of dots, and the owner still has to go round the
windows to find which one. The hint line was always going to stop there; a
list is the next step, not a reversal.

## What the code says today

- **The dots take no tap.** Every pip in `usage_view.c` is created with
  `LV_OBJ_FLAG_GESTURE_BUBBLE` and `lv_obj_clear_flag(..., CLICKABLE)`, and
  nothing on the usage screen handles `LV_EVENT_CLICKED`. A tap there today
  does nothing, so giving it a meaning takes nothing away.
- **Context is not kept per session.** `tools/blink-statusline.sh` keeps ONE
  file, the last render from whichever terminal painted last. Its header
  records why: the per-session file and the context meter were both taken
  off when "several agents means several contexts and no single number is
  any of them". `pc/providers/claude_cli.py` already remembers one thing per
  session id out of that file (its cost, `_costs`), so a second per-session
  memory has a precedent and a bound to copy.
- **The hook slots already know most of a row.** Each
  `~/.blink/state/<session_id>.state` holds the last event, its clock and the
  project directory's final segment (`name`), and the waiting marker says
  when a person was asked. The Codex slots in `~/.blink/state-codex` are the
  same format.

## Decisions

### A card, opened from the dots

A tap on the top-left corner where the dots sit opens a full-screen card;
the settings back button (`mk_back`) closes it. Not a third swipe page: the
provider pages are one layout whose rings travel between two readings
(`usage_view_page_step`), and a single-provider desk has no second page to
swipe to. The tap area is transparent, keeps `GESTURE_BUBBLE` so a swipe
that starts on it still reaches the screen, and is live even with no dots,
in which case the card says so rather than the corner going dead.

### What a row says

Name, what it is doing, how long, and context fullness as a small bar with
the exact percentage beside it.

| Kind | Words | From |
|---|---|---|
| `wait` | Needs you | waiting state (marker or prompt event) |
| `fail` | Session failed | failed state |
| `edit` | Editing | running, last tool Edit / Write / MultiEdit / NotebookEdit / apply_patch |
| `tool` | Running a tool | running, last event PreToolUse with any other tool |
| `work` | Working | running, no tool in flight |
| `idle` | Idle | the turn finished |

"How long" is the time since the session's last hook event, or since the
waiting marker for a session that needs you. The board counts it up between
updates from when the row arrived.

Rows sort needs-you first, then failed, then the running kinds, then idle,
newest first inside each. Four rows fit on 320x240 (a 40 px bar, four 44 px
rows, a 24 px page strip). **There is no cap:** the card shows four at a
time and pages through the rest, with a page indicator.

### The tool name

Telling editing from running a tool needs the tool. The hook shim reads
the TOP-LEVEL `tool_name` of a PreToolUse event, sanitised in the pattern
exactly as the project name is, and never reads `tool_input`. It does not
write the name: the slot's `tool` is a class, `"edit"` for Edit, Write,
MultiEdit, NotebookEdit and apply_patch and `"other"` for anything else, and
no other word can reach the state directory. The shim's header and the
install disclosure say so; until 2026-11-25 (review of PR #34) the
disclosure said the name itself was recorded ("Edit, Bash"), which the shim
never did. A slot from an older shim has no `tool` and reads as `Working`.

### Context per session

Claude Code puts `context_window.used_percentage` and `session_id` on every
render of its status line. The daemon keeps `{session_id: percentage}` in
memory (`ClaudeCliProvider._ctx`), bounded like `_costs`, never logged and
never saved. An unknown value is absent, never zero: a session whose render
the daemon has not yet seen shows no bar.

**How the daemon gets each session's figure.** The status-line shim
(`tools/blink-statusline.sh`) writes every render to ONE shared file,
`~/.blink/statusline.json`, by rename, so each terminal's render replaces
the last one, whichever session it came from. Two places read it:

- the poll, on the fast tick: every `FAST_POLL_INTERVAL_S` = 2 s while the
  board answers (`claude_usage_bridge.py`; `POLL_INTERVAL_S` = 60 s is the
  heartbeat). It reads the whole file for the usage figures and notes the
  render's context on the way;
- the sampler, `ClaudeCliProvider.sample_context`, which the Bridge calls on
  every pass of the serial loop (`Bridge.pump`, through
  `IngestionBus.sample_context`). A pass waits at most 0.2 s on its read,
  over USB (`serial.Serial(timeout=0.2)`) and over WiFi
  (`wifi_link.SecureLink.read`), so the sampler runs about five times a
  second. It stats the file and reads it only when its signature (inode,
  mtime, size) moved. It takes the context figure and nothing else, so a
  sample changes no usage figure, and it refuses the old login's render
  after a `/login` switch just as the poll does.

With polling alone, session A rendering 31 % and session B rendering 64 %
inside one 2 s tick left only B's render for the poll to read. The sampler
narrows that window from 2 s to about 0.2 s (a little longer on a pass that
also runs a poll). The rows still go out from the fast tick, built from the
map the sampler filled, so a new figure reaches the board within 2 s of
being sampled.

**What is left.** Two renders landing inside ONE sampling pass, about 0.2
s, still lose the first one. That session's row keeps its previous figure
(or has none) until its next render. How often Claude Code renders, and
how often two terminals land that close together, is (not yet measured on a
desk). What cannot happen is one session's figure appearing on another's
row, because each value is keyed by the id in the payload that carried it.
The complete fix is a file per session written by the status-line script.
That changes the shim and its installer and is out of this change's scope.
The shim records why the single file was chosen. The figure's behaviour on a
desk is (not yet measured on a desk).

Codex rows carry name, state and age, and no context bar in this version.

### The wire

Its own message type, never the usage frame (454 of 512 bytes, dropped
whole when over). One flat line per row, built with
`protocol.encode_checked()`:

```json
{"t":"srow","v":2,"i":0,"n":4,"tot":6,"pg":0,"pv":"c","k":"wait","age":123,"nm":"Blink","cx":42}
```

`v` is the protocol version every line carries; the board does not insist
on it, since a version is a floor, not a match. `i` is the row on this page (0..3), `n` the rows on the page (0 is an
empty list), `tot` all live sessions, `pg` the page, `pv` the provider
(`c` Claude, `x` Codex). `nm` is at most 24 bytes and absent when unknown;
`cx` is absent when unknown. No working directory is ever on the line.

The board asks for a page with `{"t":"spage","p":k}` when the card opens
(page 0) and when the owner pages, so the board holds four rows however
many sessions are open. The daemon sends the asked page when its content
changes, at once on `spage`, after every hello and with the heartbeat, and
only to a board whose caps list `sessions`. Logs name the type only.
