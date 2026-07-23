<div align="center">

<img src="docs/img/logo.png" alt="Clauge" width="560">

**A desk gauge for your Claude Code usage.**
Two dials on a tiny touchscreen — 5-hour session and 7-day week — updating live, all day.

<p>
  <img src="https://img.shields.io/badge/firmware-v0.4.0-D97757?style=flat-square&labelColor=171B22">
  <img src="https://img.shields.io/badge/platform-ESP32%20%C2%B7%20CYD-8A8F98?style=flat-square&labelColor=171B22">
  <img src="https://img.shields.io/badge/RTOS-Zephyr-6FBF8B?style=flat-square&labelColor=171B22">
  <img src="https://img.shields.io/badge/OTA-signed%20%C2%B7%20MCUboot-E8A05C?style=flat-square&labelColor=171B22">
  <img src="https://img.shields.io/badge/updates-over--the--air-D97757?style=flat-square&labelColor=171B22">
</p>

</div>

<table>
  <tr>
    <td width="33%"><img src="docs/img/screen-usage.png" alt="Live usage screen"><br><sub><b>Live usage</b> — current session &amp; weekly, per model</sub></td>
    <td width="33%"><img src="docs/img/screen-settings.png" alt="Settings screen"><br><sub><b>Settings</b> — brightness, Wi-Fi, updates</sub></td>
    <td width="33%"><img src="docs/img/screen-update.png" alt="Software update screen"><br><sub><b>Software update</b> — over the air, signed</sub></td>
  </tr>
</table>

> The screens above are rendered UI mockups in the device's real theme. Swap in
> photos of your own board any time — drop them in `docs/img/`.

## What it is

Clauge is a small physical dashboard for [Claude Code](https://claude.com/claude-code)
usage. It shows the same numbers as `/usage` — your **5-hour session limit** and
**7-day weekly limit** — as two 270° gauges with live reset countdowns, a wall
clock, and a per-model breakdown, on an **ESP32 "Cheap Yellow Display."** Glance
at it instead of interrupting your flow to ask the terminal.

The name is *Claude + gauge*, which is exactly what the mark is.

## How it works

The board picks a data source at boot — you're never asked. First match wins:

1. **USB bridge** — plug it into your computer and a small daemon answers the
   board's `hello` over serial and streams usage as NDJSON. No Wi-Fi or token
   ever touches the device.
2. **Standalone Wi-Fi** — the board holds Wi-Fi credentials and an OAuth refresh
   token (provisioned once from your phone) and fetches `/api/oauth/usage` itself
   over TLS every 60 s.
3. **Provisioning** — if neither is available, it shows a boarding-pass screen
   and opens a phone captive portal. The setup AP is WPA2 with a random
   per-device password carried inside the join QR, so the phone types nothing.

## Hardware

- **Board:** ESP32-2432S028 — the "Cheap Yellow Display" (CYD). A ~$12 ESP32 with
  a 320×240 ILI9341 screen and XPT2046 resistive touch, all on one board.
- **Firmware:** [Zephyr RTOS](https://zephyrproject.org/) + LVGL, booting through
  **MCUboot** with **flash encryption** on. Built as a sysbuild image.

## Build &amp; flash

Zephyr toolchain in `~/zephyr-v4.4.0` (Python venv + SDK). Sysbuild + MCUboot is
mandatory — the encrypted chip can't boot a single-image build.

```bash
source ~/zephyr-v4.4.0/.venv/bin/activate
source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh

cd firmware
west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
  -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
  -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.clauge/ota_signing_key_p256.pem\""

# flash the encrypted chip (wraps esptool with the right offsets):
../tools/flash_encrypted.sh
```

Full build, flash, and monitor notes live in [`firmware/README.md`](firmware/README.md).

## Over-the-air updates

Clauge updates itself over Wi-Fi from this repo's GitHub Releases — no cables.

- **Release:** bump `CLAUGE_FW_VERSION` in `firmware/src/version.h`, commit, then
  run [`tools/release.sh`](tools/release.sh). It rebuilds from the committed tree,
  signs the image, and attaches `clauge-fw.bin` + `manifest.json` to the
  `v<version>` release here on `KfirLevy258/Clauge`.
- **Device:** *Settings → Software update → Install x.y.z.* The board streams the
  image into slot 1 over TLS, verifies its SHA-256, reboots, and MCUboot swaps it
  in. A daily background check flags the tile when something newer is waiting.
- **Safe by design:** a new image boots in **test mode** and must prove itself
  within 90 s (first usage fetched) before it self-confirms. A hung or broken
  build never confirms — the watchdog reboots and MCUboot restores the previous
  version, which then reports *"Update failed, previous version restored."*
  Every image is signed (ECDSA P-256); an unsigned or tampered image is rejected
  at boot regardless of what the download layer accepted.

## The PC bridge (optional)

Prefer a cable, or don't want a token on the device? The repo also ships the
original zero-dependency Node bridge — the same one that can render usage as a
local web page.

```bash
npm start          # → http://127.0.0.1:4317   (set PORT to change)
```

Log in with Anthropic once; the bridge stores its own access + refresh token at
`~/.config/live-claude-ui/tokens.json` (`0600`, never logged or committed) and
refreshes it automatically. It polls `GET /api/oauth/usage` every **180 s** and
either serves the web page or feeds a connected Clauge board over USB.

## Repo layout

| Path | What's there |
|------|--------------|
| `firmware/` | Zephyr firmware for the CYD (the device) |
| `firmware/src/ota.c` | OTA engine — signed streaming install + auto-revert |
| `tools/` | `release.sh`, `flash_encrypted.sh`, dev + logging helpers |
| `pc/`, `server.js`, `usage.js` | PC bridge + web page |
| `docs/superpowers/` | Design specs and implementation plans |

## Security &amp; caveats

- Uses an **undocumented** Anthropic OAuth endpoint and reuses Claude Code's
  public OAuth client. Anthropic may change or revoke this at any time; the UI
  falls back to a "sign in again" prompt rather than breaking.
- The usage endpoint is **aggressively rate-limited** — don't lower the poll
  intervals or you'll get persistent `429`s.
- Personal, single-account tooling. Treat the stored token like a password.

## Status dot

| Dot | Meaning |
|-----|---------|
| 🟢 green | Connected — fresh data |
| 🟠 amber | Rate-limited or stale — showing last good values |
| 🔴 red | Error |
| ⚪ grey | Signed out |
