# Claude Usage Display — ESP32-C6 firmware (Stage 1)

Prints your Claude Code session (5h) and weekly (7d) usage to the serial console.

> **Hardware:** ESP32-C6-WROOM-1 (RISC-V, WiFi 6, native USB-Serial-JTAG).
> The original brief assumed a classic ESP32-WROOM; the actual board is a C6, so
> the board target is `esp32c6_devkitc/esp32c6/hpcore`.

## Toolchain / workspace (this machine, an Intel Mac)

Stage 0 settled several environment constraints — use exactly this setup:

- **Zephyr v4.3.0** in an isolated workspace at `~/zephyr-v4.4.0` (dir name kept
  for venv-path stability; the zephyr repo inside is checked out at v4.3.0).
  Why not 4.4.0: it requires Zephyr SDK ≥1.0.1, which is **not published for
  macOS x86_64**. 4.3.0 works with the installed SDK 0.17.4.
- **Python 3.12 venv** at `~/zephyr-v4.4.0/.venv` (Zephyr 4.3 needs Python ≥3.12).
- **Zephyr SDK 0.17.4** (`~/zephyr-sdk-0.17.4`, riscv64 toolchain for the C6).
- **ccache disabled** — the system Homebrew `ccache` is broken (linked against a
  missing `libfmt.11`). Always pass `-DUSE_CCACHE=0`. (Optional fix you can run
  separately: `brew reinstall ccache`.)
- WiFi blobs already fetched (`west blobs fetch hal_espressif`).

Activate once per shell:
```bash
source ~/zephyr-v4.4.0/.venv/bin/activate
source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh
```

## Configure secrets

```bash
cp secrets.h.example src/secrets.h
# edit src/secrets.h: WIFI_SSID, WIFI_PSK, CLAUDE_TOKEN
```

Get a fresh token (~8 h lifetime) and the ground-truth numbers to compare against:
```bash
python3 ../claude_usage_test.py
```
Paste the printed `#define CLAUDE_TOKEN "..."` into `src/secrets.h`.

## Build

```bash
west build -p auto -b esp32c6_devkitc/esp32c6/hpcore . -- -DUSE_CCACHE=0
```

## Flash

The C6's native USB-Serial-JTAG enumerates as `/dev/cu.usbmodem*`. Auto-flashing
over native USB can fail with checksum/format errors at the data-transfer stage —
if so, force download mode (hold **BOOT**/GPIO9, tap **RESET/EN**, release BOOT)
and/or use a known-good data cable directly into the Mac (not a hub):

```bash
west flash --esp-device /dev/cu.usbmodemXXXX
# or directly:
esptool --port /dev/cu.usbmodemXXXX --baud 115200 --before default-reset \
  --after hard-reset write-flash --flash-mode dio --flash-freq 80m \
  --flash-size 8MB 0x0 build/zephyr/zephyr.bin
```

## Monitor

```bash
west espressif monitor          # Ctrl-] to exit
# or: screen /dev/cu.usbmodemXXXX 115200
```

Expected output once configured:
```
[usage] HTTP 200
[usage] Session (5h):  61.0%   resets 2026-06-08T21:50:01Z
[usage] Weekly  (7d):  26.0%   resets 2026-06-10T06:00:01Z
[usage] Weekly Sonnet:  2.0%
[usage] next poll in 300s
```

## Notes

- Polls every 300 s; backs off to 600 s on HTTP 429. **Do not poll faster** —
  the endpoint rate-limits aggressively.
- HTTP 401 → the token expired; paste a fresh one and reflash.
- CA verification is **on** (`src/certs.h` = GTS Root R4, the api.anthropic.com
  trust anchor). If TLS ever fails on a chain change, re-capture with
  `openssl s_client -connect api.anthropic.com:443 -servername api.anthropic.com -showcerts`.
- The JSON parser is a small manual scanner (`src/usage_parse.c`); host tests in
  `../tests/usage_parse/host_test.c` (`cc -I src ... && ./a.out`).

## Status

Built and compile-verified for the C6 (FLASH ~8.6%). **On-device run is not yet
verified** — flashing was deferred pending a stable USB connection / download-mode
step. Stage 0 sample bring-up (WiFi association, TLS handshake) likewise pending
a flashing session.
