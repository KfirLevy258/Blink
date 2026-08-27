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
static bool fired;		/* ...and it has already been acted on */
static bool down;		/* the pointer was pressed at the last poll */
static int16_t x0, y0;		/* where it started */
static int16_t x1, y1;		/* where it has reached */
static int64_t start_ms;	/* when contact began */
static int64_t last_down_ms;	/* when contact was last seen */
static int64_t last_poll_ms;	/* when this ran, whenever that was */
/*
 * The widest gap between two polls during the stroke.
 *
 * This is an LVGL timer, so its period is a REQUEST: it runs when
 * lv_timer_handler() gets round to it, and not while something expensive is
 * rendering. The poll clips a stroke's travel at both ends, so as the gap
 * grows, strokes read shorter than they were -- and a stroke that reads short
 * enough is refused. Measured rather than assumed, because assuming is what
 * cost this file two wrong diagnoses already.
 */
static int gap_max;

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

/*
 * One line per stroke: the displacement, the contact time, the worst the poll
 * was starved, and what it came to.
 *
 * Every number in ui_swipe_geom.h came off this board -- the first set through
 * a diagnostic build that had to be flashed, captured and flashed back, the
 * rest straight off this line. A stroke that is refused should be able to say
 * why: "140 by 90" is a different finding from "12 by 4 in 300 ms", and both
 * are different from "the poll was starved for 800 ms and this is what was
 * left of it". Strokes are user-initiated, so this is quiet.
 */
static void say(int dx, int dy, enum ui_swipe_dir d)
{
	printk("[swipe] dx %d dy %d in %d ms, poll gap<=%d -> %d\n", dx, dy,
	       (int)(last_down_ms - start_ms), gap_max, (int)d);
}

/*
 * Act the moment the stroke is unambiguous, rather than when the finger lifts.
 *
 * Waiting for the end of a stroke cost a swipe two delays and neither was
 * doing any work. UI_SWIPE_STITCH_MS is 120 ms of deliberate hesitation, and
 * it exists to decide when a stroke is OVER -- which is a question about
 * contact bounce, not about what the user asked for. And the last poll before
 * a release clips the stroke's travel, which is worse than it sounds: the
 * poller is an LVGL timer and runs between LVGL's frames, so during the page
 * animation it is starved to ~37 ms rather than the 20 it asks for (measured
 * on the board 2026-08-27, alongside "[morph] 6 frames in 440 ms"). At a
 * swipe's speed that clipping is tens of pixels, and a stroke read short
 * enough is refused.
 *
 * Both go away by deciding DURING the stroke. A swipe that has already
 * travelled far enough in one direction is not going to stop being that, so
 * there is nothing to wait for -- and the travel is read from the middle of
 * the stroke, where no clipping has happened.
 *
 * The stitch window still does its original job. It is what decides when the
 * stroke has really ended and a NEW one may begin, which is exactly what stops
 * the remainder of a long drag from firing a second time.
 */
static void consider(void)
{
	int dx = x1 - x0;
	int dy = y1 - y0;
	enum ui_swipe_dir d;

	if (fired) {
		return;
	}
	d = ui_swipe_classify(dx, dy);
	if (d == UI_SWIPE_NONE) {
		return;
	}
	fired = true;
	say(dx, dy, d);
	if (emit != NULL) {
		emit(d);
	}
}

static void finish(void)
{
	int dx = x1 - x0;
	int dy = y1 - y0;

	/* Only the refusals are still unreported here; anything that counted
	 * said so when it happened. */
	if (!fired && (dx != 0 || dy != 0)) {
		say(dx, dy, UI_SWIPE_NONE);
	}
	active = false;
	fired = false;
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
			start_ms = now;
			gap_max = 0;
			fired = false;
		} else if ((int)(now - last_poll_ms) > gap_max) {
			gap_max = (int)(now - last_poll_ms);
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
		last_poll_ms = now;
		consider();
		return;
	}

	/*
	 * Not pressed. Hold the stroke open for the stitch window before
	 * deciding anything: releases on this panel are mostly not releases.
	 */
	if (active && now - last_down_ms >= UI_SWIPE_STITCH_MS) {
		finish();
	}
	last_poll_ms = now;
}

void ui_swipe_init(void (*cb)(enum ui_swipe_dir dir))
{
	emit = cb;
	lv_timer_create(poll_cb, POLL_MS, NULL);
}
