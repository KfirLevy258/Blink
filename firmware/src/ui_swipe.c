/*
 * Stroke detection over a panel that keeps letting go. See ui_swipe.h for the
 * measurements this is built on.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>

#include "ui_swipe.h"
#include "ui_swipe_geom.h"

/*
 * Poll period.
 *
 * 20 ms is faster than the panel's median report gap (13 ms is the median but
 * the top fifth run 67-90 ms) and faster than LVGL's own refresh, so a stroke
 * is sampled at least as well as anything downstream could sample it. It does
 * not need to catch every report: what is wanted is where the stroke STARTED
 * and where it ENDED, and both survive a missed sample in between.
 */
#define POLL_MS		20

static void (*emit)(enum ui_swipe_dir dir);

static bool active;		/* a stroke is being tracked */
static bool down;		/* the pointer was pressed at the last poll */
static int16_t x0, y0;		/* where it started */
static int16_t x1, y1;		/* where it has reached */
static int64_t last_down_ms;	/* when contact was last seen */

static lv_indev_t *pointer(void)
{
	lv_indev_t *in = NULL;

	while ((in = lv_indev_get_next(in)) != NULL) {
		if (lv_indev_get_type(in) == LV_INDEV_TYPE_POINTER) {
			return in;
		}
	}
	return NULL;
}

bool ui_swipe_dragging(void)
{
	return active && ui_swipe_is_drag(x1 - x0, y1 - y0);
}

static void finish(void)
{
	int dx = x1 - x0;
	int dy = y1 - y0;
	enum ui_swipe_dir d = ui_swipe_classify(dx, dy);

	active = false;

	/*
	 * One line per stroke, with the displacement that produced it.
	 *
	 * Every number in ui_swipe.h came off this board through a diagnostic
	 * build that had to be flashed, captured and flashed back. A stroke
	 * that is refused should be able to say why -- "moved 140 by 90, too
	 * diagonal" is a different problem from "moved 12 by 4, that was a
	 * tap" -- without anyone rebuilding the firmware to find out. Strokes
	 * are user-initiated, so this is quiet.
	 */
	if (dx != 0 || dy != 0) {
		printk("[swipe] dx %d dy %d -> %d\n", dx, dy, (int)d);
	}

	if (d != UI_SWIPE_NONE && emit != NULL) {
		emit(d);
	}
}

static void poll_cb(lv_timer_t *t)
{
	ARG_UNUSED(t);
	lv_indev_t *in = pointer();
	lv_point_t p;
	int64_t now = k_uptime_get();

	if (in == NULL) {
		return;
	}

	down = lv_indev_get_state(in) == LV_INDEV_STATE_PRESSED;
	lv_indev_get_point(in, &p);

	if (down) {
		if (!active) {
			active = true;
			x0 = p.x;
			y0 = p.y;
		}
		/*
		 * A press arriving while a stroke is still open EXTENDS it --
		 * this is the stitch, and it is the whole point of the module.
		 * The gap was contact bounce under a moving finger, so the
		 * travel on either side of it belongs to one gesture.
		 */
		x1 = p.x;
		y1 = p.y;
		last_down_ms = now;
		return;
	}

	/*
	 * Not pressed. Hold the stroke open for the stitch window before
	 * deciding anything: releases on this panel are mostly not releases.
	 */
	if (active && now - last_down_ms >= UI_SWIPE_STITCH_MS) {
		finish();
	}
}

void ui_swipe_init(void (*cb)(enum ui_swipe_dir dir))
{
	emit = cb;
	lv_timer_create(poll_cb, POLL_MS, NULL);
}
