# Claude Usage Display — ESP32-C6 firmware (UART bridge)

The board no longer fetches anything itself. A **PC daemon** (`../claude_usage_bridge.py`)
holds the Claude OAuth token (auto-refreshed by Claude Code), polls
`/api/oauth/usage`, and **pushes** the numbers to this board over the USB-C serial
link using a small NDJSON protocol. The board sends identity + keep-alive and
displays whatever usage the PC sends. No WiFi / TLS / SNTP / token on the device.

## Protocol (NDJSON, one JSON object per line, `t`=type, `v`=version)

- Board → PC: `hello` (on boot, with `board_id` from the chip MAC), `ping` (every 10 s).
- PC → board: `welcome` (ack), `usage` (`session_pct`/`weekly_pct`/resets + optional
  `models[]`), `status` (e.g. rate-limited).
- Unknown `t` and non-`{` lines are ignored on both sides (forward-compatible; logs and
  protocol coexist on the one serial stream).

## Toolchain / build (Intel Mac; see the prior plan for why)

- Zephyr **v4.3.0** in `~/zephyr-v4.4.0` (dir name kept; pinned to 4.3.0), Python 3.12
  venv, SDK 0.17.4 (riscv64). ccache is broken on this host → always `-DUSE_CCACHE=0`.

```bash
source ~/zephyr-v4.4.0/.venv/bin/activate && source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh
west build -p auto -b esp32c6_devkitc/esp32c6/hpcore . -- -DUSE_CCACHE=0
```

## Flash (learned the hard way)

The C6 exposes **two** USB CDC interfaces on its two USB-C ports:
- **UART bridge** (e.g. `/dev/cu.usbmodem143401`) — flash here with esptool.
- **native USB-Serial-JTAG** (e.g. `/dev/cu.usbmodem58CD…`) — the firmware **console +
  protocol** flow here (where the PC daemon connects).

Auto-reset into the bootloader is unreliable; use manual download mode:
1. Quit any serial monitor (only one process may own a port).
2. Hold **BOOT**, tap **RESET/EN**, release **BOOT** (board is now in download mode).
3. Flash:
```bash
esptool --port /dev/cu.usbmodem<UART-bridge> --before no-reset --after hard-reset \
  write-flash --flash-mode dio --flash-freq 80m --flash-size 8MB 0x0 build/zephyr/zephyr.bin
```
Expect `Hash of data verified.`

## Run the bridge (this is the whole app)

```bash
source ~/zephyr-v4.4.0/.venv/bin/activate           # has pyserial
python3 ../claude_usage_bridge.py --port /dev/cu.usbmodem<native-USB-JTAG>
```
The daemon reads your token (Keychain / `~/.claude/.credentials.json`), connects, and
on the board's `hello` replies `welcome`+`usage`, then polls every 300 s. Its log shows
**both directions** plus the board's echoed `[usage] Session…` display lines, so one
terminal shows the whole loop. (`tio` and the daemon can't both own the port.)

Important: the daemon opens the port with **DTR/RTS de-asserted** — on the C6 native
USB-Serial-JTAG the default open sequence resets the chip into ROM download mode and
silences the firmware.

## Verified

Real round trip on hardware: board `hello`(+MAC id)/`ping` → daemon `welcome` + `usage`
with live numbers (e.g. Session 38%, Weekly 14%, Sonnet 2%). FLASH ~1.6%.

## Source map
- `proto.c` — UART line reader, emits `hello`/`ping`, dispatches inbound by `t`.
- `msg_parse.c` — flat-field JSON value extractors (host-tested: `tests/msg_parse/`).
- `usage_view.c` — prints the latest usage (later: a display).
- `main.c` — init + service loop.
