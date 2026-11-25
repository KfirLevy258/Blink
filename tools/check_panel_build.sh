#!/bin/sh
# Build the shipped firmware once, pristine, and prove the panel is a run-time
# choice in it (issue #23). Run from the repo root:
#
#   sh tools/check_panel_build.sh
#
# Why this exists. Until issue #23 the display panel was a build option, and
# a board built the wrong way came out mirrored with red and blue swapped --
# the only symptom, on a desk, after the fact. Now one image serves both
# modules and the stored record chooses (firmware/src/panel.h). What could
# quietly undo that is a stale config: a build directory whose CMake cache
# still carries the retired symbols, or an overlay that sets them, compiles
# without complaint. So this builds from nothing, into a scratch directory of
# its own, and reads the result rather than the source:
#
#   - the app's .config holds neither retired symbol;
#   - LVGL has no flush thread, because panel_apply() sends MADCTL from the
#     main thread on the promise that every window-set-then-pixels write
#     happens on that thread too (panel.h);
#   - the signed image carries the standard panel's boot line
#     ("panel: MADCTL=0x") and the live-change reply ("[cfg] panel set to"),
#     so the string a test greps is the string a board prints.
#
# Then, once those pass, it builds the bench (touch-inject) image the same
# way into a second scratch directory and checks that it compiles, has the
# option set, and draws no warning from proto.c or main.c (stage two, below).
#
# Never build-sb and never inside the repo: a developer's build-sb is what
# burn.sh flashes, and a pristine build into it would throw away their
# configuration. Both scratch directories go on exit, pass or fail.
#
# Touches no serial port and no board: it builds and reads files, nothing more.
# Plain sh: tools/lib_zephyr.sh is written to be sourced by sh, bash or zsh.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)

# The retired symbols, spelled in pieces: the repo-wide check that they are
# gone (`grep -r ... tools`) reads this file too, and must not find them here.
P1="BLINK_PANEL_""PILOT"
P2="BLINK_BL_SCALE""_PCT"

fail() {
	echo "FAIL: $*" >&2
	exit 1
}

# Without -u for the activation: the venv's activate and zephyr-env.sh are
# not ours, and an unset variable in either would end the script with an
# expansion error rather than with the message below.
set +u
# shellcheck disable=SC1091
. "$ROOT/tools/lib_zephyr.sh"
blink_zephyr_activate || fail "no Zephyr workspace; see firmware/README.md"
blink_zephyr_build_ready || fail "the workspace cannot build this firmware"
set -u

KEY="$HOME/.blink/ota_signing_key_p256.pem"
[ -r "$KEY" ] || fail "no signing key at ~/.blink/ota_signing_key_p256.pem (firmware/README.md, Keys)"

DIR=$(mktemp -d "${TMPDIR:-/tmp}/blink-panel-build.XXXXXX") ||
	fail "could not make a scratch directory"
# The bench stage's directory is made only once the release stage passes; it
# is named here so the one EXIT trap removes both, whichever stage ends it.
BENCH=""
trap 'rm -rf "$DIR"; [ -z "$BENCH" ] || rm -rf "$BENCH"' EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

echo "== pristine sysbuild into $DIR"
# The Build line of firmware/README.md, with -p always and -d the scratch dir.
(
	cd "$ROOT/firmware"
	west build -p always --sysbuild -d "$DIR" -b esp32_devkitc/esp32/procpu . \
		-- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
		-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$KEY\""
) || fail "west build failed"

# Sysbuild names the application's image after its source directory, which
# is firmware/ (Kconfig's -Dfirmware_CONFIG_... lines rely on the same name).
APP="$DIR/firmware/zephyr"
CONFIG="$APP/.config"
IMAGE="$APP/zephyr.signed.bin"
[ -f "$CONFIG" ] || fail "no app .config at $CONFIG"
[ -f "$IMAGE" ] || fail "no signed image at $IMAGE"

status=0
for sym in "$P1" "$P2"; do
	if grep -q "CONFIG_$sym" "$CONFIG"; then
		echo "FAIL: the app .config still names CONFIG_$sym" >&2
		status=1
	else
		echo "PASS: no CONFIG_$sym in the app .config"
	fi
done

if grep -q '^CONFIG_LV_Z_FLUSH_THREAD=y' "$CONFIG"; then
	echo "FAIL: LVGL flushes on its own thread; panel_apply() is no longer safe" >&2
	status=1
else
	echo "PASS: no LVGL flush thread"
fi

for s in 'panel: MADCTL=0x' '[cfg] panel set to' \
	 'panel: alternate, driver MADCTL kept'; do
	if grep -aqF "$s" "$IMAGE"; then
		echo "PASS: the signed image carries '$s'"
	else
		echo "FAIL: the signed image lacks '$s'" >&2
		status=1
	fi
done

[ "$status" -eq 0 ] || exit 1
echo "release stage passed"

# Stage two: the bench (touch-inject) image. It is what the board check's
# touch run flashes and plays tools/bench/full_touch.txt on, and its code
# lives inside #ifdef CONFIG_BLINK_TOUCH_INJECT, which the release build above
# never compiles. On 2026-11-25 bench-only code in proto.c went to that check
# having been compiled by nobody, because nothing a round may run built the
# option. So this builds it too, pristine, into a scratch directory of its own
# (never build-inject, for the same reason as never build-sb), and the second
# build costs its few minutes for that. The line is BLINK_TOUCH_INJECT's help
# in firmware/Kconfig, with -d the scratch dir and the key the one above.
BENCH=$(mktemp -d "${TMPDIR:-/tmp}/blink-panel-bench.XXXXXX") ||
	fail "could not make a scratch directory for the bench build"
LOG="$BENCH/build.log"

echo "== pristine bench (touch-inject) sysbuild into $BENCH"
# The log goes to a file rather than through tee: a pipe in sh reports tee's
# status, not west's, and the warning check below needs the whole output.
if ! (
	cd "$ROOT/firmware"
	west build -p always --sysbuild -d "$BENCH" -b esp32_devkitc/esp32/procpu . \
		-- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
		-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$KEY\"" \
		-Dfirmware_CONFIG_BLINK_TOUCH_INJECT=y
) >"$LOG" 2>&1; then
	tail -n 40 "$LOG" >&2
	fail "the bench west build failed (the last 40 lines are above)"
fi
echo "PASS: the bench build compiled"

BCONFIG="$BENCH/firmware/zephyr/.config"
[ -f "$BCONFIG" ] || fail "no bench app .config at $BCONFIG"
if grep -q '^CONFIG_BLINK_TOUCH_INJECT=y' "$BCONFIG"; then
	echo "PASS: the bench .config has CONFIG_BLINK_TOUCH_INJECT=y"
else
	fail "the bench .config lacks CONFIG_BLINK_TOUCH_INJECT=y; the -D did not reach the app"
fi

# Only our two files with bench code in them: the build has an unrelated
# '"LOGO_H" redefined' warning in logo.c that is not this check's business.
# "src/" keeps MCUboot's boot/zephyr/main.c out of the match.
WARN=$(grep -E 'src/(proto|main)\.c:[0-9]+(:[0-9]+)?: warning:' "$LOG" || true)
if [ -n "$WARN" ]; then
	echo "$WARN" >&2
	fail "the bench build warns in firmware/src/proto.c or firmware/src/main.c"
fi
echo "PASS: no warning from proto.c or main.c in the bench build"

echo "panel build check passed"
