#!/bin/bash
# Flash the Clauge CYD. MANDATORY since 2026-07-17: the chip's flash-encryption
# eFuses are burned, so it only boots images encrypted with the device key.
# A plain `west flash` writes plaintext the ROM cannot read -- the board then
# sits dead until re-flashed with THIS script. (The board still works; only
# the flashing path changed.)
#
# Key: ~/.clauge/flash_key.bin (override with CLAUGE_FLASH_KEY). The eFuse
# copy is sealed inside the chip and unreadable -- the file is the only usable
# copy in the world. KEEP A BACKUP OFF THIS DISK. No key file = no future
# updates, ever.
#
# Build first (sysbuild + MCUboot, also mandatory since the same date):
#   west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
#     -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0
#
# Usage: tools/flash_encrypted.sh [port]   (port defaults to the first
# /dev/cu.usbserial*; opening it resets the board -- stop any logger first)
set -euo pipefail

PORT="${1:-$(ls /dev/cu.usbserial* 2>/dev/null | head -1)}"
KEY="${CLAUGE_FLASH_KEY:-$HOME/.clauge/flash_key.bin}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BUILD="$HERE/../firmware/build-sb"
ETOOLS="/Library/Frameworks/Python.framework/Versions/3.10/bin"

[ -f "$KEY" ] || { echo "FATAL: flash key missing at $KEY -- no key, no flashing (see firmware/README security section)"; exit 1; }
[ -n "${PORT}" ] || { echo "FATAL: no /dev/cu.usbserial* port found"; exit 1; }
[ -f "$BUILD/mcuboot/zephyr/zephyr.bin" ] || { echo "FATAL: no sysbuild output in $BUILD -- build first (see header)"; exit 1; }

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

enc() { # address, plaintext-in, encrypted-out
	"$ETOOLS/espsecure.py" encrypt_flash_data --keyfile "$KEY" \
		--flash_crypt_conf 0xf --address "$1" -o "$3" "$2"
}

# Offsets match the sysbuild runners: MCUboot at 0x1000, signed app in slot0
# at 0x20000. The address is part of the cipher tweak -- never shuffle these.
enc 0x1000  "$BUILD/mcuboot/zephyr/zephyr.bin"         "$TMP/mcuboot.enc"
enc 0x20000 "$BUILD/firmware/zephyr/zephyr.signed.bin" "$TMP/app.enc"

# 115200: 921600 fails on this CH340.
"$ETOOLS/esptool.py" --port "$PORT" --baud 115200 write_flash \
	0x1000 "$TMP/mcuboot.enc" 0x20000 "$TMP/app.enc"

echo "Flashed encrypted. NVS at 0x3b0000 untouched (settings survive)."
