# Blink firmware

The firmware that runs on the **ESP32-2432S028 "Cheap Yellow Display" (CYD)**: two
270° gauges for your Claude Code **session (5 h)** and **weekly (7 d)** usage, a wall
clock, and over-the-air updates. Built on **Zephyr** + LVGL,
booting through **MCUboot**, with flash encryption on units whose eFuses are burned.

The firmware version lives in [`src/version.h`](src/version.h) and is reported over
serial in the boot `hello` message.

## How it gets your usage

Only from the daemon on your computer, over the USB cable. At boot the board sends a
`hello` on the serial port, and the daemon replies with NDJSON lines: usage, the time,
and firmware updates when there are any. The device has no Wi-Fi and stores no token.

Older source trees also had a standalone Wi-Fi mode (captive-portal setup, an
on-device sign-in, a usage fetch of its own). No release ever shipped it. It signed
the board in to Anthropic using Claude Code's OAuth client id, so it was deleted on
2026-10-27, following gapietro's fork, which had dropped it first; git history
keeps it.

## Prerequisites

- **Zephyr v4.3.x** with its Python venv, and **Zephyr SDK 0.17.4**. The build
  scripts find the workspace themselves (`tools/lib_zephyr.sh` looks under
  `~/zephyrproject`, `~/zephyr-v*` and the same two under `~/Projects`); set
  `BLINK_ZEPHYR` to override.

  **Not v4.4.0.** Measured 2026-09-09: its mbedTLS is 4.1.0, which removed the
  legacy `mbedtls/sha256.h` that [`src/ota.c`](src/ota.c) includes, and the
  LVGL it pins moved `gesture_limit` out of the public `lv_indev` headers,
  which [`src/ui_settings.c`](src/ui_settings.c) reads. 4.3.1 has both
  (mbedTLS 3.6.6, `gesture_limit` in `lv_indev_private.h`) and wants SDK
  0.17.4 rather than the 1.0.1 that 4.4.0 asks for. Do not register both SDKs
  at once -- 1.0.1's CMake config breaks 4.3.1's configure even when 0.17.4 is
  registered alongside it.

  **esptool in the workspace venv, at this repo's pins.** Zephyr's own
  `west packages pip --install` asks only for `esptool>=5.0.2`, and esptool
  does not bound `bitstring`, so it installs bitstring 5.0.0 -- with which
  `espefuse.py` dies on import if the eFuse check before a flash uses this
  venv's copy (2026-10-12). From the repo root, after that command:

  ```sh
  <workspace>/.venv/bin/python -m pip install -c pc/requirements.txt esptool bitstring
  ```

  `blink_zephyr_build_ready` checks that esptool is on PATH before
  `release.sh`, `burn.sh` and `dev.sh flash` build, and prints this line
  when it is not. A broken bitstring shows up instead in the eFuse check,
  which reports an espefuse.py that does not start.
- The **CYD board** on USB (a CH340 serial port, `/dev/cu.usbserial-*` on macOS).
- Your **signing keys** (see [Keys](#keys)).

## Build

```bash
. tools/lib_zephyr.sh && blink_zephyr_activate   # from the repo root

cd firmware
west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
  -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
  -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.blink/ota_signing_key_p256.pem\""
```

Notes:

- **This is the one shipped build: USB only.** It has no radio, no sign-in and no
  token store. `tools/release.sh` scans the finished artifact, not just the config,
  for the deleted Wi-Fi mode's strings and refuses to publish if it finds them -- a
  guard against that code drifting back in through a merge or an undone commit.
- **Sysbuild + MCUboot is required.** Flash encryption needs the MCUboot boot chain;
  a plain single-image build produces something the encrypted chip can't boot.
- The board target is **`esp32_devkitc/esp32/procpu`** - there's no upstream CYD board,
  so [`boards/esp32_devkitc_esp32_procpu.overlay`](boards/) describes the wiring.
  Building with a different `-b` pollutes the build dir; recover with `-p always`.
- **Display SPI is pinned at 25 MHz.** The common 40 MHz CYD overclock white-screens
  some panels (init never latches) - see the overlay comment.

## Flash

**Not every CYD is fused, so check which one you have first.** A unit whose
flash-encryption eFuses are burned boots *only* images encrypted with its device key;
an unfused unit boots plaintext and cannot read an encrypted image at all. Cross the
two and the board sits in a boot loop (`invalid header: ...`) until it is re-flashed
the other way. Nothing is bricked - flashing burns no eFuses - but a cycle is lost
finding out.

```bash
espefuse.py --port <port> summary | grep FLASH_CRYPT_CNT
```

An **odd** number of bits set means encryption is on; `0` is a plaintext board.

**Fused unit** - use the script, which re-reads the eFuse itself and refuses a
plaintext chip (override with `BLINK_SKIP_EFUSE_CHECK=1`):

```bash
tools/flash_encrypted.sh        # defaults to the first /dev/cu.usbserial* port
```

**Plaintext unit** - two commands, because `west flash` writes only the app at
`0x20000` and leaves MCUboot at `0x1000` untouched:

```bash
west flash -d build-sb --esp-device <port> --esp-baud-rate 115200
esptool.py --port <port> --baud 115200 write_flash 0x1000 build-sb/mcuboot/zephyr/zephyr.bin
```

Port notes: opening the port resets the board, and only one process can own it at a
time. To watch the console without disturbing behavior, use
`tools/passive_log.py <port>`: it resets the board once, when it opens the port, and
only reads from then on. For a clean first-run test, clear every saved setting by
erasing their region: `esptool.py --port <port> erase_region 0x3b0000 0x30000`.

Flash map (4 MB): MCUboot `0x1000`, slot 0 `0x20000`, slot 1 `0x170000`,
scratch `0x2C0000` (unused in swap-move mode), **company logo `0x330000`
(512 KB, see `src/logo_parse.h`)**, settings `0x3B0000`. The logo partition is
written only by `tools/burn.sh --logo` / `tools/flash_encrypted.sh --logo` and
read memory-mapped at boot; erase it (`erase_region 0x330000 0x80000`) to turn
a bench board back into an individual unit. `tools/flash_encrypted.sh` erases
it by default -- pass `--keep-logo` when re-flashing a dev board that should
keep its logo.

## Keys

Two secrets live **outside the repo**, in `~/.blink/`:

| Key | Purpose |
|-----|---------|
| `flash_key.bin` | Encrypts what's written to flash. Override with `BLINK_FLASH_KEY`. |
| `ota_signing_key_p256.pem` | Signs firmware images (ECDSA P-256) so the board will boot them. Override with `OTA_SIGNING_KEY`. |

**Back both up off this machine.** The eFuse copy of the flash key is sealed and
unreadable - the file is the only usable copy. Lose the signing key and you can no
longer ship updates that installed devices will accept (only a USB reflash with a new
key recovers).

## Releasing an update

```bash
# 1. bump BLINK_FW_VERSION in src/version.h, commit
# 2. from the repo root:
tools/release.sh
```

`release.sh` builds from the committed tree (it refuses a dirty tree or a reused
version), signs the image, and attaches `blink-fw.bin` + `manifest.json` to the
`v<version>` release on **`KfirLevy258/Blink`** - which is the feed every board reads.

With no network of its own, the board only approves an update; the daemon fetches the
release and writes slot0 with esptool. Roughly 75 s, and no swap, since the image goes
straight to the slot the bootloader boots. What that gives up is automatic rollback,
which is tolerable only because whatever can reflash the board is already plugged into
it. The machine running the daemon needs `pip install esptool`. (The removed Wi-Fi mode
updated over HTTPS into slot 1 instead; that code path is still in `ota.c`, but nothing
calls it.)

The board checks for an update when it comes up and once a day after that. A newer
release shows a prompt on the gauge screen, **Update now** or **Later**, and can also
be installed at any time from Settings, under **Update → Install x.y.z**.
Whether it worked is shown on the boot that follows.

## How updates stay safe

Each of three checks guards something different:

- **HTTPS** proves where the *download* came from. The daemon pulls it from GitHub
  and verifies the certificate against the OS store and certifi (`pc/ota.py`).
- **The manifest's SHA-256** fixes the *exact bytes*. Nothing is written until the
  download hashes to it.
- **MCUboot's ECDSA P-256 signature check** gates *what may boot* - only images signed
  with your private key run, so even a hash-passing forgery is rejected.

Nothing rolls back automatically: slot0 is overwritten where it sits, and no second
copy exists to return to. The daemon compensates where it can: it verifies the asset
against its manifest before writing, and it reads the chip's eFuses and refuses
outright if it cannot tell whether flash encryption is burned, since a plaintext write
to a fused board leaves it dark.

A board that has only ever run this firmware holds no secrets. **A board that once ran
a Wi-Fi build may still hold some.** On its first boot of this firmware it erases any
sign-in token it finds, but nothing erases the network name and password or the setup
hotspot's password: they stay in the settings record, and on an unfused board anyone
with the cable and esptool can read them in plain text. To remove them, erase the
settings region -- `esptool.py --port <port> erase_region 0x3b0000 0x30000`. That
clears the whole record: brightness and the other settings go back to their defaults,
and the unit's edition reads as Claude until it is stamped again with
`blink provision --edition codex`, so re-provision a Codex unit afterwards. The record
keeps those old fields, unread, so that its layout, and every setting in it, survives
an update. Published images hold no secrets either way; what lets an image install is
the signing key, not secrecy.

## Bench builds

Three builds exist only for testing a board on the bench. **None is ever
shipped.** `tools/release.sh` refuses all three, `tools/flash_encrypted.sh` refuses
the first two outright, and `tools/burn.sh` refuses those two unless you pass the
matching flag.

- **The touch injector** (`CONFIG_BLINK_TOUCH_INJECT`) lets the computer drive
  the touch panel and read back what the screen shows, so every interaction can be
  tested with nobody at the panel. Build it into its own directory, never into
  `build-sb` (the help text of `BLINK_TOUCH_INJECT` in `Kconfig` has the full
  `west build` line), then flash it with
  `BLINK_BUILD_DIR=$PWD/build-inject ../tools/burn.sh --edition claude --no-build --bench`.
- **The fault self-test** (`CONFIG_BLINK_FATAL_SELFTEST`) faults on purpose a
  set time into every boot, to prove the fatal handler. Flash it with
  `burn.sh --selftest`.
- **The touch trace** (`trace.conf` with `trace.overlay`, which set
  `CONFIG_BLINK_TOUCH_TRACE`) prints every raw touch sample on the console, to
  diagnose how a finger's stroke reaches the panel. Layer it on with
  `-Dfirmware_EXTRA_CONF_FILE=trace.conf` and the overlay; the first lines of
  `trace.conf` say how.

With an inject board on its gauges, `tools/board_touch.py` stands in for the
daemon and plays taps, swipes and clicks:

```sh
tools/board_touch.py state                                # what the screen shows
tools/board_touch.py widgets                              # every control, and where
tools/board_touch.py click "Later"
tools/board_touch.py script tools/bench/full_touch.txt    # every Settings path
tools/board_touch.py script tools/bench/popups_touch.txt  # every popup
```

It takes the daemon's lock and refuses while anything else holds the port, so stop
the installed daemon first (the `launchctl` lines are in its docstring). At the end
of a script it prints each screen's peak use of the LVGL pool, and an LVGL
allocation failure on the console (`No memory`, `Allocating layer buffer failed`
and the like) fails the run, because on a customer's panel it is a reboot.

The same scripts run without a board on the host harness, which compiles the
firmware's UI against LVGL on the laptop and saves a PNG of every step:
`tools/panel_render/touch.sh OUTDIR [SCRIPT]`.

When you are done, flash a normal image with `burn.sh` before the unit goes
anywhere but the bench.

## Hardware notes

Things worth knowing when hacking on the CYD:

- Touch (XPT2046) needs averaging (`reads = <8>`), and devicetree properties are
  last-one-wins - a stray duplicate silently degrades it.
- The backlight (GPIO 21) runs on LEDC PWM and is adjustable in Settings.

## Source map

| Area | Files |
|------|-------|
| Boot, orchestration, workers | `main.c` |
| Boot splash (streamed past LVGL) | `ui_boot.c`, `bootanim_dec.c` |
| Gauge screen, hints and toasts | `usage_view.c` |
| Settings + software-update UI | `ui_settings.c` |
| OTA engine (check, install, verify) | `ota.c`, `ota_parse.c` |
| Clock (the daemon's `time` message) | `net_time.c` |
| Persisted config (A/B, CRC-sealed) | `cfg_store.c` |
| Serial NDJSON protocol | `proto.c`, `msg_parse.c` |

Parsers and the protocol have host-side unit tests under [`../tests/`](../tests/).
