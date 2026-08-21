<div align="center">

<img src="docs/img/logo.png" alt="Clauge" width="560">

**A little desk gauge for your Claude Code usage.**
Your 5-hour session and 7-day week, as two live dials you can glance at all day.

<p>
  <img src="https://img.shields.io/badge/firmware-v0.5.0-D97757?style=flat-square&labelColor=171B22">
  <img src="https://img.shields.io/badge/board-ESP32%20%C2%B7%20CYD-8A8F98?style=flat-square&labelColor=171B22">
  <img src="https://img.shields.io/badge/RTOS-Zephyr-6FBF8B?style=flat-square&labelColor=171B22">
  <img src="https://img.shields.io/badge/updates-over--the--air-E8A05C?style=flat-square&labelColor=171B22">
</p>

<img src="docs/img/enclosure-placeholder.png" alt="Clauge on a desk" width="580">

</div>

<table>
  <tr>
    <td width="33%"><img src="docs/img/screen-usage.png" alt="Live usage screen"><br><sub><b>Live usage</b> - session &amp; weekly, per model</sub></td>
    <td width="33%"><img src="docs/img/screen-settings.png" alt="Settings screen"><br><sub><b>Settings</b> - brightness, Wi-Fi, updates</sub></td>
    <td width="33%"><img src="docs/img/screen-update.png" alt="Software update screen"><br><sub><b>Updates</b> - over the air, and safe</sub></td>
  </tr>
</table>

## What is Clauge?

Clauge is a small desk display that shows how much of your Claude Code usage you've spent - the same numbers as the `/usage` command, but always in view. It reads your **5-hour session** limit and your **7-day weekly** limit and draws each as a dial, green while you have room, amber as you get close, red when it's nearly gone. Glance over, know where you stand, keep working.

It runs on a cheap (~$12) ESP32 touchscreen. Plug it in, give it your Wi-Fi, and it just sits on your desk and keeps itself up to date.

## What's in here

| Path | What's there |
|------|--------------|
| `firmware/` | The device firmware (Zephyr, C) - this is the product |
| `firmware/src/ota.c` | The update engine: signed install + automatic rollback |
| `pc/`, `claude_usage_bridge.py` | Optional USB bridge - feeds the board over a cable instead of Wi-Fi |
| `tools/` | Build, flash, and release helpers |
| `docs/img/` | Logo, icons, and the screen renders above |

## Hardware

Clauge runs on the **ESP32-2432S028 "Cheap Yellow Display" (CYD)** - a popular all-in-one board with a 2.8" 320×240 touchscreen, for around $12. The common variants all work.

**Get the board:** [search AliExpress for "ESP32-2432S028"](https://www.aliexpress.com/w/wholesale-esp32%2D2432s028.html)

### Enclosure

A 3D-printable case gives the bare board a home on your desk. **[Download the CAD files](#)** *(coming soon)*

## Connecting it

Clauge needs to know your usage numbers. It figures out the best way to get them on its own - you don't choose a mode, it just works:

- **Wi-Fi - the default.** Give the board your Wi-Fi and it fetches your usage itself, over a secure connection. First power-on walks you through it right on the screen.
- **USB cable - if Wi-Fi isn't an option.** On a network where you'd rather not put the board online, plug it into your computer instead and run the small bridge below. Your usage streams over the cable, and no Wi-Fi or login ever touches the device. The bridge also handles updates for you - see below.

  ```bash
  ./install.sh          # once; any Python 3.9+. See "What the installer changes"
  ```

  That is the whole setup. It finds the board by itself, and starts again on
  its own every time you log in - plug the cable in and the panel comes up.

  **Needs Claude Code 2.1.100 or newer.** Clauge reads the usage figures from
  the status line, and older versions do not put them there - 2.1.0 has no
  usage figures in that payload at all, so the panel would stay blank. Update
  Claude Code first if yours is older.

  ```bash
  ./install.sh status      # is the panel getting data?
  ./install.sh uninstall   # put everything back
  ```

  ### What the installer changes

  Over USB, Clauge reads the usage figures Claude Code has already worked out,
  rather than asking Anthropic for them itself. Claude Code hands those figures
  to whatever command is set as its **status line**, so that is the one setting
  Clauge has to change.

  | | |
  |---|---|
  | Changes | `statusLine.command` in `~/.claude/settings.json` |
  | Creates | `~/.clauge/` - a private Python environment for the bridge (`pyserial`, `esptool`), and a copy of the status line shim |
  | Creates | a login item, so the bridge starts with your session (`~/Library/LaunchAgents` on macOS, a user systemd unit on Linux) |
  | Leaves alone | every other key in `settings.json`, and the file's own formatting and permissions. Your system Python is not modified |
  | Reads or stores | nothing else - no credential, no token, no account data |

  **It does this without asking**, so that plugging the board in is the whole
  setup. It prints all of the above before it changes anything, and every part
  of it is reversible:

  ```bash
  ./install.sh uninstall
  ```

  **If you already have your own status line, it keeps working.** Clauge records
  your existing command, and runs it after capturing the usage figures - your bar
  renders exactly as before. Uninstalling puts your command back unchanged, and
  will not touch a status line Clauge did not install.

## Build &amp; flash

You only need this to put firmware on a board yourself (after that, it updates over the air). It uses the [Zephyr](https://zephyrproject.org/) toolchain.

```bash
source ~/zephyr-v4.4.0/.venv/bin/activate
source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh

cd firmware
west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
  -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
  -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.clauge/ota_signing_key_p256.pem\""

# Which flash path you need depends on the board -- stock CYDs are unfused,
# and the two kinds cannot boot each other's images:
espefuse.py --port <port> summary | grep FLASH_CRYPT_CNT   # odd bits set = encrypted

# Unfused board -- two commands, because west flash writes only the app at
# 0x20000 and leaves MCUboot at 0x1000 untouched:
west flash -d build-sb --esp-device <port> --esp-baud-rate 115200
esptool.py --port <port> --baud 115200 write_flash 0x1000 build-sb/mcuboot/zephyr/zephyr.bin

# Fused board (flash-encryption eFuses burned) -- the script re-checks the
# chip itself and refuses one it would leave dark:
../tools/flash_encrypted.sh
```

Full details - the signing key, the encrypted-flash setup, and the release flow - are in **[firmware/README.md](firmware/README.md)**.

## Updates

Clauge checks for a new release as soon as it starts up, and if it finds one it asks you - **Update now** or **Later** - right on the gauge screen. How the update arrives depends on how the board is connected, and you don't have to pick:

- **Over Wi-Fi.** The board fetches the release itself over a secure connection, verifies it, and restarts into it. If a new build ever misbehaves it **automatically rolls back** to the version that was working, so a bad update can't brick it. Allow several minutes - most of it is the changeover at the end, during which the screen holds its last frame.
- **Over the USB cable.** A tethered board has no network of its own, so the bridge does the work: it downloads the release and writes it over the same cable, in about a minute. The screen goes dark while that happens - it tells you first, and comes back on the new version. Because this route writes the running slot directly it has no automatic rollback, which is a fair trade when the machine that can reflash it is the one already plugged in.

Either way you get a confirmation on screen once the new version is up.

Every image is **signed with a private key only you hold**, and the board only accepts firmware carrying that signature. That means nobody else can push updates to your device - not by forking this repo, not by uploading a release - even though the repo itself is public.

## Security &amp; privacy

- **Only you can update your device.** Updates must be signed with your private key (kept off this repo, at `~/.clauge/…`); the bootloader rejects anything else. The public repo only lets people read and download the firmware, which holds no secrets.
- Clauge uses an **undocumented** Anthropic usage endpoint and reuses Claude Code's public sign-in. Anthropic may change it at any time; if that happens the screen asks you to sign in again rather than breaking.
- That endpoint is **rate-limited**, so the board polls gently (about once a minute).
- This is a personal, single-account gadget. Treat the sign-in on it like a password.

## The status dot

A small dot in the top-right corner tells you how fresh the numbers are:

| Dot | Meaning |
|-----|---------|
| 🟢 green | Connected - live data |
| 🟠 amber | Rate-limited or stale - showing the last good numbers |
| 🔴 red | Error |
| ⚪ grey | Signed out |
