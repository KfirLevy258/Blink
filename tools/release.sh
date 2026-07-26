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
KEY="${OTA_SIGNING_KEY:-$HOME/.clauge/ota_signing_key_p256.pem}"
[ -f "$KEY" ] || { echo "FATAL: signing key missing at $KEY (set OTA_SIGNING_KEY)"; exit 1; }
# Stamp the real version into the MCUboot image header. Zephyr's default for
# CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION is "0.0.0+0" (it only auto-fills from an
# app VERSION file, which this app does not use -- version.h is the one source
# of truth), so every image ever released before 2026-07-26 carried 0.0.0.
#
# It changes nothing today: swap-using-move is told which image to boot by the
# flag ota.c sets via boot_request_upgrade(), and MCUboot never reads the
# version field in that mode. It matters for DIRECT-XIP, which has no copy and
# therefore no flag, and picks a slot by COMPARING these versions -- with both
# slots claiming 0.0.0 the tie-break is arbitrary and an update can silently
# no-op forever. Stamping it now means the field is already correct whenever
# direct-XIP lands, instead of being a trap waiting at the end of that work.
#
# "firmware" is the sysbuild image name (see build-sb/domains.yaml), not a path.
# Note this is a RELEASE-path fix: a plain `west build` still produces 0.0.0+0,
# which is fine and even desirable -- a dev image should never out-rank a
# released one under direct-XIP.
( cd "$ROOT/firmware" && west build --sysbuild -d build-sb -b esp32_devkitc/esp32/procpu . \
	-- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
	-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$KEY\"" \
	-Dfirmware_CONFIG_MCUBOOT_IMGTOOL_SIGN_VERSION="\"$VER\"" )   # inner quotes: Kconfig strings

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
