#!/bin/sh
# Build the touch-target harness and run a bench script against it.
#
#   tools/panel_render/touch.sh OUTDIR [SCRIPT]
#
# SCRIPT defaults to tools/bench/full_touch.txt. Every stroke leaves two PNGs
# in OUTDIR -- the screen with the stroke marked, then the screen it led to --
# and tools/board_touch.py checks each expectation exactly as it does against
# a board (touch/harness.c explains what is modelled). Exit status is the
# script's: nonzero if any expectation failed.
#
# Needs the Zephyr LVGL checkout, like render.sh. Set LVGL_DIR to override,
# and BLINK_PYTHON for a Python other than python3.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
HERE="$ROOT/tools/panel_render"
OUT="${1:?usage: touch.sh OUTDIR [SCRIPT]}"
SCRIPT="${2:-$ROOT/tools/bench/full_touch.txt}"
. "$ROOT/tools/lib_zephyr.sh"
LVGL_DIR="${LVGL_DIR:-$(blink_zephyr_ws 2>/dev/null)/modules/lib/gui/lvgl}"
CC="${CC:-cc}"
PY="${BLINK_PYTHON:-python3}"

[ -d "$LVGL_DIR/src" ] || {
	echo "LVGL sources not found at $LVGL_DIR" >&2
	echo "set LVGL_DIR to a Zephyr LVGL checkout" >&2
	exit 1
}

BUILD="${TMPDIR:-/tmp}/blink-touch-render"
mkdir -p "$BUILD" "$OUT"
find "$LVGL_DIR/src" -name '*.c' > "$BUILD/srcs.txt"
# The same lv_conf.h render.sh derives, for the same reasons.
sed -e 's|#if 0 /\* Set this to "1" to enable content \*/|#if 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_16 .*|#define LV_FONT_MONTSERRAT_16 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_20 .*|#define LV_FONT_MONTSERRAT_20 1|' \
	"$LVGL_DIR/lv_conf_template.h" > "$BUILD/lv_conf.h"

# The touch stubs first on the path, so their zephyr/kernel.h (a simulated
# clock) wins over render.sh's wall-clock one. CONFIG_BLINK_TOUCH_INJECT, so
# ui_settings.c compiles the state getter the board's ui_state reply reads.
$CC -O1 -w -DLV_CONF_INCLUDE_SIMPLE=1 -DCONFIG_BLINK_TOUCH_INJECT=1 \
	-I"$BUILD" -I"$HERE/touch" -I"$LVGL_DIR" -I"$LVGL_DIR/src" \
	-I"$ROOT/firmware/src" \
	"$HERE/touch/harness.c" "$HERE/touch/stubs.c" "$HERE/touch/png.c" \
	"$ROOT/firmware/src/touch_widgets.c" \
	"$ROOT/firmware/src/ui_settings.c" \
	"$ROOT/firmware/src/ui_theme.c" \
	"$ROOT/firmware/src/usage_view.c" \
	"$ROOT/firmware/src/fmt.c" \
	"$ROOT/firmware/src/usage_state.c" \
	"$ROOT/firmware/src/svc_state.c" \
	"$ROOT/firmware/src/theme.c" \
	"$ROOT/firmware/src/msg_parse.c" \
	"$ROOT/firmware/src/font_focus_40.c" \
	"$ROOT/firmware/src/upd_prompt.c" \
	"$ROOT/firmware/src/upd_row.c" \
	"$ROOT/firmware/src/whatsnew.c" \
	"$ROOT/firmware/src/ota_parse.c" \
	@"$BUILD/srcs.txt" \
	-lm -o "$BUILD/touch"

PYTHONDONTWRITEBYTECODE=1 "$PY" "$ROOT/tools/board_touch.py" \
	--harness "$BUILD/touch" --renders "$OUT" script "$SCRIPT"
