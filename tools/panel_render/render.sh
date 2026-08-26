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
LVGL_DIR="${LVGL_DIR:-$HOME/zephyr-v4.4.0/modules/lib/gui/lvgl}"
CC="${CC:-cc}"

[ -d "$LVGL_DIR/src" ] || {
	echo "LVGL sources not found at $LVGL_DIR" >&2
	echo "set LVGL_DIR to a Zephyr LVGL checkout" >&2
	exit 1
}

BUILD="${TMPDIR:-/tmp}/clauge-panel-render"
mkdir -p "$BUILD" "$OUT"
find "$LVGL_DIR/src" -name '*.c' > "$BUILD/srcs.txt"

# -w: LVGL's own sources are not warning-clean under a host compiler and that
# is not this script's business. The firmware build is where warnings matter.
$CC -O1 -w -DLV_CONF_INCLUDE_SIMPLE=1 \
	-I"$HERE" -I"$LVGL_DIR" -I"$LVGL_DIR/src" -I"$ROOT/firmware/src" \
	"$HERE/render_main.c" \
	"$ROOT/firmware/src/usage_view.c" \
	"$ROOT/firmware/src/fmt.c" \
	@"$BUILD/srcs.txt" \
	-o "$BUILD/render"

for scene in 0 1 2 3; do
	"$BUILD/render" "$OUT/panel-$scene.ppm" "$scene"
done
echo "PPMs in $OUT (any image viewer opens them; convert with your tool of choice)"
