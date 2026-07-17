# Clauge — CYD firmware (ESP32-2432S028 "Cheap Yellow Display")

A desk display for Claude Code usage: two 270° gauges (5-hour session, 7-day week)
with live reset countdowns, a wall clock, and a status dot. Firmware version lives
in `src/version.h` (currently **0.3.0**) and is reported in the serial `hello`.

Two data modes, detected at boot — never asked:

- **USB bridge** — a PC daemon (`tools/dev.sh` / `pc/`) answers the boot `hello`
  over serial and pushes usage as NDJSON. No WiFi or token on the device.
- **Standalone WiFi** — the board holds WiFi credentials + an OAuth refresh token
  (provisioned by phone) and fetches `/api/oauth/usage` itself over TLS every 60 s.

First match wins: a talking daemon, then a reachable stored network, then the
provisioning flow (boarding-pass screen + phone captive portal). The setup AP is
**WPA2** with a random per-device password carried inside the join QR; the phone
types nothing. See the living screen map artifact for every screen and state.

## Build (Intel Mac)

Zephyr **v4.3.0** in `~/zephyr-v4.4.0` (dir name kept), Python 3.12 venv, SDK 0.17.4.
ccache is broken on this host → `-DUSE_CCACHE=0` on full builds.

```bash
source ~/zephyr-v4.4.0/.venv/bin/activate && source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh
west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
  -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0
```

Sysbuild + MCUboot is **mandatory** since 2026-07-17 (flash encryption requires
the MCUboot boot chain). The old single-image `west build` in `build/` produces
a simple-boot image the encrypted chip can no longer boot.

The board target is **`esp32_devkitc/esp32/procpu`** (no upstream CYD board exists;
`boards/esp32_devkitc_esp32_procpu.overlay` describes the wiring). Passing any other
`-b` poisons `build/` — recover with `-p always` and the right board. Incremental
builds: plain `west build` inside the configured `build/`.

## Flash — ENCRYPTED CHIP, use the script

**This board's flash-encryption eFuses are burned (2026-07-17).** The chip only
boots images encrypted with its device key; a plain `west flash` writes
plaintext the ROM cannot read and the board sits dead until re-flashed
properly. The one true flashing path:

```bash
tools/flash_encrypted.sh            # picks the first /dev/cu.usbserial*
```

The key lives at `~/.clauge/flash_key.bin` (override: `CLAUGE_FLASH_KEY`).
The eFuse copy is sealed and unreadable — **that file is the only usable copy
in the world; keep a backup off this disk.** No key file = no future updates.

Port facts unchanged: single CH340 (`/dev/cu.usbserial-14XX0`, digits change
with the socket — glob first), opening it resets the board, 115200 only.

- **Observe without disturbing:** `tools/passive_log.py <port>` (venv python; resets
  once on open, then read-only). Only one process may own the port.
- **Do not** use a flow that starts the USB daemon when testing provisioning — its
  hello puts the board into USB-bridge mode.
- **Fresh-provisioning test:** erase stored creds/token/AP-password:
  `esptool.py --port <port> erase_region 0x3b0000 0x30000` (NVS partition).
- **Display SPI is pinned at 25 MHz** — the common CYD 40 MHz overclock white-screens
  this unit (panel never latches init). See the overlay comment.

## Hardware quirks worth knowing (all learned on this unit)

- The radio boots blind (all scans empty) roughly every other soft reset; the
  firmware self-reboots to re-roll it, bounded by a `.noinit` counter.
- Touch (XPT2046) needs `reads = <8>` averaging — and DTS properties are
  last-one-wins, so a stray duplicate silently degrades it.
- The backlight (GPIO 21) runs on LEDC PWM; night mode dims to 25% between 23:00
  and 07:00 local. Transitions log `[ui] backlight N%`.
- A station join only completes from a clean boot on this driver — which is why the
  provisioning flow reboots on purpose (invisibly: dark skip-splash, CONNECTING
  spans the reset).

## Security posture

- **In transit:** the setup AP is WPA2 (random NVS-persisted password, rotated by
  factory reset); the OAuth login code is PKCE-bound (useless without the verifier,
  which never leaves the device); the refresh token travels only device↔Anthropic
  over verified TLS. The phone browser still shows an HTTP warning on the portal —
  inherent to plain HTTP; the link beneath it is encrypted.
- **At rest (DONE 2026-07-17):** flash encryption is live — key in eFuse BLK1
  (read-protected), `FLASH_CRYPT_CONFIG 0xF`, `FLASH_CRYPT_CNT 1`. A USB flash
  dump returns ciphertext (verified: app region differs from the binary, and
  the config region greps clean of the SSID/token). Two design consequences:
  the boot chain is MCUboot via sysbuild, and `cfg_store.c` keeps its own A/B
  CRC-sealed record instead of settings/NVS — NVS needs erased flash to read
  as 0xFF, which encrypted flash never does (mount failed -EDEADLK on
  hardware; ESP-IDF exempts NVS for the same reason). `FLASH_CRYPT_CNT` has a
  few remaining flips as an emergency off switch; MCUboot signs with the
  public dev key, which is fine because we rely on encryption, not signatures.

## Source map

- `main.c` — boot decision, provisioning orchestration, standalone worker, USB loop,
  backlight/night logic.
- `ui_boot.c` / `bootanim_dec.c` — eyes-clip splash (skip variant on intentional
  reboots), streamed past LVGL.
- `ui_setup.c` — boarding-pass screen, 8 states, failure popups.
- `portal.c` / `dns_hijack.c` — phone captive portal (WiFi form → ack → sign-in →
  working → done) on a hand-rolled HTTP/1.1 server.
- `usage_view.c` — gauges, unified CONNECTING boot bar, near-limit warnings, model
  card (All models / Fable, persisted selection), edge chevrons.
- `ui_settings.c` — edge tap/swipe navigation, settings panel (Reset WiFi /
  Re-sign-in / Factory reset), version/SSID/IP footer.
- `ui_anim.c` — the boot clip on loop (left chevron).
- `net_wifi.c` / `net_time.c` / `oauth.c` / `usage_client.c` / `usage_parse.c` /
  `tz_fetch.c` — radio, SNTP, PKCE OAuth, TLS usage fetch, response parsing, timezone.
- `cfg_store.c` — NVS-backed settings (mode, WiFi, token, tz, AP password, weekly
  selection).
- `proto.c` / `msg_parse.c` — serial NDJSON protocol (host-tested in `tests/`).
