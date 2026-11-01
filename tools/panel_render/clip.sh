#!/bin/sh
# The gauge screen's left half through the eye clip, a wipe and a doze.
#
#   tools/panel_render/clip.sh [OUTDIR]
#
# Compiles firmware/src/ui_anim.c, ui_slide.c, ui_sleep.c and usage_view.c
# UNCHANGED against real LVGL, on a simulated clock, and runs each scene in
# clip/clip_main.c: a burn line, a session reading or the burn view's 180 s
# landing while the eyes play, a burn line landing mid-wipe, and a doze
# peeked into before, across and after the view's 180 s. Prints PASS or FAIL
# per check and leaves PNGs in OUTDIR (default dist/clip). Exit status is
# nonzero if any check failed.
#
# The touch harness (touch.sh) cannot see any of this: it models the eye clip
# with a flat overlay and a wipe as the clock moving on, and these faults
# live in what that model leaves out (clip/clip_main.c has the account).
#
# Needs the Zephyr LVGL checkout, like render.sh. Set LVGL_DIR to override.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
HERE="$ROOT/tools/panel_render"
OUT="${1:-$ROOT/dist/clip}"
. "$ROOT/tools/lib_zephyr.sh"
LVGL_DIR="${LVGL_DIR:-$(blink_zephyr_ws 2>/dev/null)/modules/lib/gui/lvgl}"
CC="${CC:-cc}"

[ -d "$LVGL_DIR/src" ] || {
	echo "LVGL sources not found at $LVGL_DIR" >&2
	echo "set LVGL_DIR to a Zephyr LVGL checkout" >&2
	exit 1
}

BUILD="${TMPDIR:-/tmp}/blink-clip-render"
mkdir -p "$BUILD" "$OUT"
find "$LVGL_DIR/src" -name '*.c' > "$BUILD/srcs.txt"
# The same lv_conf.h render.sh derives, for the same reasons.
sed -e 's|#if 0 /\* Set this to "1" to enable content \*/|#if 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_16 .*|#define LV_FONT_MONTSERRAT_16 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_20 .*|#define LV_FONT_MONTSERRAT_20 1|' \
	"$LVGL_DIR/lv_conf_template.h" > "$BUILD/lv_conf.h"

# The clip stubs first on the path, so their zephyr/kernel.h (a simulated
# clock with a sleep that moves it) wins.
$CC -O1 -w -DLV_CONF_INCLUDE_SIMPLE=1 \
	-I"$BUILD" -I"$HERE/clip" -I"$LVGL_DIR" -I"$LVGL_DIR/src" \
	-I"$ROOT/firmware/src" \
	"$HERE/clip/clip_main.c" "$HERE/clip/stubs.c" "$HERE/touch/png.c" \
	"$ROOT/firmware/src/ui_anim.c" \
	"$ROOT/firmware/src/ui_slide.c" \
	"$ROOT/firmware/src/ui_sleep.c" \
	"$ROOT/firmware/src/usage_view.c" \
	"$ROOT/firmware/src/fmt.c" \
	"$ROOT/firmware/src/usage_state.c" \
	"$ROOT/firmware/src/usage_freshness.c" \
	"$ROOT/firmware/src/sleep_gate.c" \
	"$ROOT/firmware/src/svc_state.c" \
	"$ROOT/firmware/src/theme.c" \
	"$ROOT/firmware/src/msg_parse.c" \
	"$ROOT/firmware/src/burn5h.c" \
	"$ROOT/firmware/src/bootanim_dec.c" \
	"$ROOT/firmware/src/font_focus_40.c" \
	@"$BUILD/srcs.txt" \
	-lm -o "$BUILD/clip"

status=0
for scene in on off expiry wipe doze; do
	"$BUILD/clip" "$scene" "$OUT" || status=1
done
if [ "$status" -eq 0 ]; then
	echo "PASS [clip] (PNGs in $OUT)"
else
	echo "FAIL [clip] (PNGs in $OUT)" >&2
fi
exit "$status"
