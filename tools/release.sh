#!/bin/bash
# Build, sign, and publish a Clauge firmware release the board can install
# over WiFi. The version comes from firmware/src/version.h -- bump it FIRST.
# Requires: zephyr env NOT needed here (script sources it), `gh auth status` ok.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"; ROOT="$HERE/.."
REPO="${OTA_REPO:-KfirLevy258/Clauge}"
TAG="v$(sed -n 's/#define CLAUGE_FW_VERSION "\(.*\)"/\1/p' "$ROOT/firmware/src/version.h")"

VER="${TAG#v}"
[ -n "$VER" ] || { echo "FATAL: no version in version.h"; exit 1; }
[ -z "$(git -C "$ROOT" status --porcelain)" ] || {
	echo "FATAL: working tree dirty -- releases come from committed code only"; exit 1; }
# The firmware feed lives on the same release as the source tag ($TAG). Refuse
# to overwrite an existing firmware asset -- bump version.h for a new build.
gh release view "$TAG" --repo "$REPO" --json assets \
	-q '.assets[].name' 2>/dev/null | grep -qx clauge-fw.bin && {
	echo "FATAL: $TAG already carries clauge-fw.bin -- bump version.h"; exit 1; }

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

# Attach the firmware + manifest to the version's release, creating it if the
# source tag hasn't been released yet. --clobber lets a manifest-only retry win.
if gh release view "$TAG" --repo "$REPO" >/dev/null 2>&1; then
	gh release upload "$TAG" --repo "$REPO" --clobber \
		"$TMP/clauge-fw.bin" "$TMP/manifest.json"
else
	gh release create "$TAG" --repo "$REPO" --title "Clauge $VER" \
		--notes "Firmware $VER — size $SIZE bytes, sha256 $SHA" \
		"$TMP/clauge-fw.bin" "$TMP/manifest.json"
fi
echo "Released $TAG ($SIZE bytes). Boards pick it up on their next check."
