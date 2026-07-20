#!/bin/bash
# Build, sign, and publish a Clauge firmware release the board can install
# over WiFi. The version comes from firmware/src/version.h -- bump it FIRST.
# Requires: zephyr env NOT needed here (script sources it), `gh auth status` ok.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/.."
REPO="${OTA_REPO:-KfirLevy258/clauge-releases}"

VER=$(sed -n 's/#define CLAUGE_FW_VERSION "\(.*\)"/\1/p' "$ROOT/firmware/src/version.h")
[ -n "$VER" ] || { echo "FATAL: no version in version.h"; exit 1; }
[ -z "$(git -C "$ROOT" status --porcelain)" ] || {
	echo "FATAL: working tree dirty -- releases come from committed code only"; exit 1; }
gh release view "fw-v$VER" --repo "$REPO" >/dev/null 2>&1 && {
	echo "FATAL: fw-v$VER already released -- bump version.h"; exit 1; }

source ~/zephyr-v4.4.0/.venv/bin/activate
source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh
( cd "$ROOT/firmware" && west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
	-- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 )

BIN="$ROOT/firmware/build-sb/firmware/zephyr/zephyr.signed.bin"
SIZE=$(stat -f%z "$BIN")
SHA=$(shasum -a 256 "$BIN" | cut -d' ' -f1)
SLOT=$((0x150000))
[ "$SIZE" -le "$SLOT" ] || { echo "FATAL: image $SIZE > slot $SLOT"; exit 1; }

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
cp "$BIN" "$TMP/clauge-fw.bin"
printf '{"version":"%s","size":%s,"sha256":"%s"}\n' "$VER" "$SIZE" "$SHA" \
	> "$TMP/manifest.json"

gh release create "fw-v$VER" --repo "$REPO" --title "Clauge firmware $VER" \
	--notes "size $SIZE bytes, sha256 $SHA" \
	"$TMP/clauge-fw.bin" "$TMP/manifest.json"
echo "Released fw-v$VER ($SIZE bytes). Boards pick it up on their next check."
