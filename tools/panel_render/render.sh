#!/bin/sh
# Render the gauge screen on this machine, with no board attached.
#
#   tools/panel_render/render.sh [outdir]
#
# Compiles firmware/src/usage_view.c UNCHANGED against real LVGL and drives the
# same public functions proto.c calls. Only two things are replaced: the panel
# (the flush callback writes a plain framebuffer instead of an ILI9341) and the
# handful of Zephyr macros usage_view.c reaches for.
#
# Why this exists: tests/usage_layout/host_test.c proves the boxes do not
# overlap, and that is not the same as the screen reading well. The first run
# of this found the context bar butting flush against its own "100%" readout --
# legal by the overlap test, because touching is not overlapping, and visible
# only at 100%, which is exactly when someone is staring at that meter.
#
# Needs the Zephyr LVGL checkout. Set LVGL_DIR to override.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
HERE="$ROOT/tools/panel_render"
OUT="${1:-$ROOT/dist/panel}"
. "$ROOT/tools/lib_zephyr.sh"
LVGL_DIR="${LVGL_DIR:-$(blink_zephyr_ws 2>/dev/null)/modules/lib/gui/lvgl}"
CC="${CC:-cc}"

[ -d "$LVGL_DIR/src" ] || {
	echo "LVGL sources not found at $LVGL_DIR" >&2
	echo "set LVGL_DIR to a Zephyr LVGL checkout" >&2
	exit 1
}

BUILD="${TMPDIR:-/tmp}/blink-panel-render"
mkdir -p "$BUILD" "$OUT"
find "$LVGL_DIR/src" -name '*.c' > "$BUILD/srcs.txt"

# Derive lv_conf.h from whatever LVGL is checked out, rather than vendoring a
# copy. The template's defaults already match this panel -- 16-bit colour, the
# three Montserrat sizes, arc, bar, label -- so the only edit needed is to turn
# the file on: it guards its entire body behind `#if 0` so an unconfigured
# project fails loudly instead of silently building with defaults.
#
# A vendored copy would be 1400 lines of third-party config drifting quietly
# out of step with the LVGL the firmware actually builds against, which is the
# opposite of what this harness is for.
#
# Two edits, both mirroring what firmware/prj.conf sets. The fonts are the
# ones that would otherwise fail to link: the template ships Montserrat 14 on
# and 16 and 20 off, and the gauge screen uses 20 for the big percentages.
sed -e 's|#if 0 /\* Set this to "1" to enable content \*/|#if 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_16 .*|#define LV_FONT_MONTSERRAT_16 1|' \
    -e 's|^ *#define LV_FONT_MONTSERRAT_20 .*|#define LV_FONT_MONTSERRAT_20 1|' \
	"$LVGL_DIR/lv_conf_template.h" > "$BUILD/lv_conf.h"

for f in 14 16 20; do
	grep -qE "^ *#define LV_FONT_MONTSERRAT_$f +1" "$BUILD/lv_conf.h" || {
		echo "lv_conf.h: MONTSERRAT_$f not enabled -- the template's" >&2
		echo "  layout changed and the sed above needs updating" >&2
		exit 1
	}
done

# -w: LVGL's own sources are not warning-clean under a host compiler and that
# is not this script's business. The firmware build is where warnings matter.
$CC -O1 -w -DLV_CONF_INCLUDE_SIMPLE=1 \
	-I"$BUILD" -I"$HERE" -I"$LVGL_DIR" -I"$LVGL_DIR/src" -I"$ROOT/firmware/src" \
	"$HERE/render_main.c" \
	"$ROOT/firmware/src/usage_view.c" \
	"$ROOT/firmware/src/fmt.c" \
	"$ROOT/firmware/src/usage_state.c" \
	"$ROOT/firmware/src/svc_state.c" \
	"$ROOT/firmware/src/theme.c" \
	"$ROOT/firmware/src/msg_parse.c" \
	"$ROOT/firmware/src/burn5h.c" \
	"$ROOT/firmware/src/font_focus_40.c" \
	@"$BUILD/srcs.txt" \
	-lm -o "$BUILD/render"

for scene in 0 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24; do
	"$BUILD/render" "$OUT/panel-$scene.ppm" "$scene"
done

# The five-hour burn view (35..44, render_main.c says what each is), and
# its four directions in every theme: Dark's colours are the contract's
# bytes, every other theme's are worked out from its own roles, and those
# are worth seeing rather than trusting.
for scene in 35 36 37 38 39 40 41 42 43 44; do
	"$BUILD/render" "$OUT/panel-$scene.ppm" "$scene"
done
for theme in 0 1 2 3 4 5 6 7 8 9 10 11 12; do
	for scene in 35 36 37 38; do
		"$BUILD/render" "$OUT/theme-$theme-$scene.ppm" "$scene" "$theme"
	done
done

# Every theme, on the two scenes that between them show all three severity
# levels -- 27/42% (both low) and 78/91% (mid and high) -- and on the two
# status lines that wear a colour: degraded (amber, 13) and a major outage
# (red, 15). Theme 12 is a sample custom theme in per-gauge mode, built in
# render_main.c.
for theme in 0 1 2 3 4 5 6 7 8 9 10 11 12; do
	for scene in 0 1 13 15; do
		"$BUILD/render" "$OUT/theme-$theme-$scene.ppm" "$scene" "$theme"
	done
done

# Focus mode, on the session window (1) and the weekly one (2), in Dark (0),
# Light (9), a thick theme (Bold, 2) and Colour-blind safe (8): low, mid and
# high on both pages (25..30), the amber and red status lines on each page
# (13, 15, 17, 19), the CONNECTING takeover (31), HOST LOST (32) and an old
# reading's caption (33). And the burn desk (35), which Focus draws exactly
# as it always has: the view is laid out for a left half Focus does not have.
for theme in 0 9 2 8; do
	for focus in 1 2; do
		for scene in 25 26 27 28 29 30 13 15 17 19 31 32 33 35; do
			"$BUILD/render" \
				"$OUT/focus-$focus-theme-$theme-$scene.ppm" \
				"$scene" "$theme" "$focus"
		done
	done
done
# The widest Focus figure, "100%", in every theme and the custom one, on both
# windows (34): the spacing between it and its caption, and its clearance of
# the ring, are worth seeing at the thickest arc and in every colour.
for theme in 0 1 2 3 4 5 6 7 8 9 10 11 12; do
	for focus in 1 2; do
		"$BUILD/render" "$OUT/focus-$focus-theme-$theme-34.ppm" 34 \
			"$theme" "$focus"
	done
done
echo "PPMs in $OUT (any image viewer opens them; convert with your tool of choice)"
