# Sleep mode — design (2026-08-30)

Approved in conversation 2026-08-30. Ships as 1.2.0 (firmware + app).

## What the user sees

When the computer goes to sleep, the screen closes its eyes and dozes; when
the computer wakes, the eyes open and the dashboard returns.

Three clips, drawn by `tools/make_sleepanim.py`, identical for both editions
except ground/ink colour (Claude: terracotta `#cd795a` / black; Codex: steel
blue `#4c82a8` / white):

1. **Closing** (1.5 s, once) — the eyes as the boot left them, then one slow
   eased fall to two thin bars.
2. **Sleeping** (2.5 s, looped) — Zs rise from above the right eye, three in
   the air, periodic in the 2.5 s; the shut eyes breathe a pixel. Replayed end
   to end for as long as the computer sleeps.
3. **Opening** (1.8 s, once) — eyes open, one quick blink, dashboard.

Decisions: 30 s of silence before sleeping; normal backlight; no company logo
on wake; a tap while asleep shows the dashboard (last figures, flagged stale)
with the line "Your computer may be asleep" for 10 s, then the loop resumes;
after a shutdown that leaves the port powered, the face sleeps overnight.

## How sleep is detected

Board-side, from silence. The board pings every 10 s and the app answers;
a sleeping computer freezes the app. After **30 s** without an answer, on a
board that has had a good connection this boot, the board enters SLEEP. The
first message from the app (a pong, or a `welcome` from a restarted app)
ends it. No OS-specific code; works for sleep, lid close and hibernate on all
three platforms.

There is a second way in, added after a field report (2026-09-02): **from a
reading that has stopped moving**. The first rule waits for silence, and a
computer that goes to sleep does not necessarily produce any — the daemon on
it kept answering pings all night while the figures it pushed were 57 hours
old, so `host_lost` never armed and the panel sat awake showing "Reading is
old" until morning. So a reading older than **4 h** (`SLEEP_ABSENT_AFTER_S`),
on a board that has shown figures this boot, with no update in flight and
with nothing on screen asking for a person (no waiting, stuck, failed or
running session), also enters SLEEP. It leaves on the first reading younger
than that — which is the next `usage` message after somebody uses Claude Code
again — or on any of those conditions changing. A tap peeks as it always did.

The threshold is argued for in `firmware/src/sleep_gate.h`: above the
daemon's own 1800 s staleness bound and Claude Desktop's 900 s away-schedule
by a wide margin, so a person at the desk is never dozed on; short enough
that a machine sleeping at 23:00 has a dark panel by 03:00.

Edges:
- **Uninstall** sends a `bye` (new protocol message, additive) before stopping
  the service; the board shows "connecting" for that instead of sleeping.
  Restarts (updates) are shorter than 30 s and are not seen.
- **Firmware update in progress** (OTA UI in DOWNLOADING/REBOOTING) suppresses
  sleep: the port is closed for ~75 s while esptool writes.
- **Crash** reads as sleep until the OS restarts the app; the board then wakes.
- **Boards that lose USB power** in sleep simply go dark and boot on wake.
- **Standalone WiFi mode** is untouched: no host, no sleep.
- Before the first good connection, silence is still "connecting" (unchanged).

## Firmware

- `ui_sleep.c/.h`: plays closing once, loops sleeping, plays opening once; owns
  the tap-to-peek (dashboard + hint, 10 s). Uses the existing BAN1 player
  (`ui_boot.c`'s `bootanim_play`, factored to a shared helper) and the same
  `pump()`.
- `usage_freshness.c/.h`: keeps the `age_s` and `state` that `proto.c`
  already parses somewhere `main.c` can read them and a laptop can test them,
  and grows the age with uptime between messages.
- `sleep_gate.c`: `sleep_stale_should_start()` / `sleep_stale_should_wake()`
  are exact complements, pinned by a grid in `tests/sleep_gate`, because one
  lives outside `ui_sleep_run` and the other inside it.
- Clips compiled in as `sleepanim_<edition>_{close,loop,open}.h`, encoded with
  `tools/encode_bootanim.py --frames` from `make_sleepanim.py` output;
  `bootclip.c` picks the edition's set the way it picks the boot clip.
- State: `proto.c` already tracks `last_host_ms` with `HOST_TIMEOUT_MS`
  (35 s → 30 s). New: `proto_host_silent_for_ms()`; `main.c` USB loop enters
  SLEEP when silent > 30 s && host_seen && had_usage && OTA idle; leaves it on
  the next host message. `bye` sets a flag that routes the silence to
  DISCONNECTED ("connecting") instead of SLEEP.
- "Connecting 1/2" screen gains a hint line under the step name: "Connected
  and the app is running? Try another cable." (shown after 20 s on step 1).

## App

- `protocol.bye()`; `cmd_uninstall` stops the service, opens the remembered
  port and sends `bye` before removing files (best effort, 2 s timeout).
- No other change: pongs and `welcome` already exist.

## Tests

- Firmware host test for the sleep state machine (silence/bye/OTA gates).
- `tests/pc`: `bye` on uninstall, protocol shape.
- Hardware: desk board (Claude) and Lenovo board (Codex): sleep the computer,
  watch closing → loop; wake, watch opening → dashboard; tap while asleep;
  uninstall → "connecting".
