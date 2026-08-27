/*
 * Stroke detection over a panel that keeps letting go. See ui_swipe.h for the
 * measurements this is built on.
 */
#include <zephyr/kernel.h>
#include <zephyr/input/input.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>

#include "ui_swipe.h"
#include "ui_swipe_geom.h"

/*
 * The panel's own reports, not LVGL's replay of them.
 *
 * This module used to poll lv_indev_get_point() from an LVGL timer, and that
 * had two faults with the same shape: the position it read was not where the
 * finger was.
 *
 * The first is the queue. Zephyr's lvgl_pointer_input glue pushes every report
 * into a k_msgq (96 deep here) and LVGL pops exactly ONE per indev read, once
 * per refresh. The panel reports at a median of 13 ms and LVGL drains at ~33,
 * so during a stroke the queue fills and LVGL replays the touch in slow
 * motion, falling further behind the longer the stroke runs.
 *
 * The second is the timer. An LVGL timer's period is a request: it runs
 * between LVGL's frames, and measured on the board it ran at ~37 ms against
 * the 20 it asked for -- and at ~1 s while a settings slide was blocking.
 *
 * Together they meant the stroke was seen late and coarsely. The threshold is
 * 36 px, but strokes were firing at 125, 145, 150, 162, 173 and 181 px --
 * "I should swipe the entire screen in order for it to move", which is exactly
 * what it was asking for. Nothing was wrong with the rules; the input reaching
 * them was several samples stale.
 *
 * An input callback runs on the input thread, on every report, as it arrives.
 * There is no queue in front of it and nothing renders in its way.
 */

/*
 * Channel frame to screen frame.
 *
 * Derived from lvgl_pointer_input.c against THIS board's devicetree rather
 * than guessed: the node carries invert-x, no swap-xy, no invert-y, and the
 * panel is rotation = <90>, so the glue computes
 *
 *   tmp.x    = 240 - ABS_X          (invert-x, using y_resolution when rotated)
 *   screen_x = tmp.y = ABS_Y
 *   screen_y = 240 - tmp.x = ABS_X
 *
 * Two channels, straight across, neither inverted. It also agrees with the
 * touch-trace capture of 2026-08-27, where the vertical swipes' channel-X
 * displacements (178, 177, -174, -144, -120, -110) were the screen-Y ones the
 * gauge screen saw. Two independent derivations, which is what it takes to
 * trust a transform in this tree -- the comments that disagreed about the
 * horizontal axis cost a whole diagnosis earlier today.
 *
 * Only DISPLACEMENTS are used, so the offsets and the clamping the glue
 * applies do not matter here. That is also why nothing needs revisiting if
 * min-x/max-x are ever recalibrated.
 */
#define SCREEN_DX(chan_x, chan_y)	(chan_y)
#define SCREEN_DY(chan_x, chan_y)	(chan_x)

/*
 * How often the LVGL side looks for something to do.
 *
 * It no longer measures anything -- it only hands a decided stroke to the
 * caller on the right thread. So its period is dispatch latency and nothing
 * else, and being starved to 37 ms costs 37 ms of delay rather than a stroke.
 */
#define DRAIN_MS	10

static void (*emit)(enum ui_swipe_dir dir);
static void (*show)(enum ui_swipe_dir dir, int pct);

/* Guards the stroke. Written from the input thread, read from the LVGL thread
 * -- ui_swipe_dragging() is called out of a click handler. */
static struct k_spinlock lock;

static bool active;		/* a stroke is being tracked */
static bool fired;		/* ...and it has already been acted on */
static int32_t x0, y0;		/* where it started, in SCREEN axes */
static int32_t x1, y1;		/* where it has reached */
static int64_t start_ms;
static int64_t last_down_ms;
static int samples;		/* panel reports in this stroke, for the log */

/* Decided, waiting to be handed over on the LVGL thread. */
static enum ui_swipe_dir pending;
static int pend_dx, pend_dy, pend_ms, pend_n;

/*
 * Where the stroke has got to, for the live indicator. Published separately
 * from `pending` because it is a different kind of thing: `pending` is an
 * event that must be delivered exactly once, this is a state that is simply
 * read whenever the drain gets round to it, and a missed update costs one
 * frame of a growing dot rather than a swipe.
 */
static enum ui_swipe_dir live_dir;
static int live_pct;

/* Latest field values. The driver sends ABS_X and ABS_Y with sync=0 and closes
 * the report with BTN_TOUCH sync=1, so a report is complete on the sync. */
static int32_t cur_x, cur_y, cur_down;

static void stroke_end(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(stitch_work, stroke_end);

/*
 * Decide, if there is anything to decide, with the finger still down.
 *
 * Called with the lock held. Waiting for the release would cost the stitch
 * window -- 120 ms that exists to answer when a stroke is OVER, which is a
 * question about contact bounce and not about what was asked for -- and would
 * read the travel from the last sample before a release, which is the one most
 * likely to be short. A stroke that has already gone far enough in one
 * direction is not going to stop having done that.
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
	pend_dx = dx;
	pend_dy = dy;
	pend_ms = (int)(last_down_ms - start_ms);
	pend_n = samples;
	pending = d;
}

/*
 * Publish how far the stroke has committed, for the rail to draw.
 *
 * Called with the lock held, on every report, so the indicator tracks the
 * finger at the panel's own rate rather than LVGL's. Once the stroke has
 * FIRED there is nothing left to preview -- the page is already changing --
 * so it goes quiet and lets the real rail take over.
 */
static void publish_live(void)
{
	int dx = x1 - x0;
	int dy = y1 - y0;
	int ax = ui_swipe_abs(dx), ay = ui_swipe_abs(dy);

	if (fired) {
		live_dir = UI_SWIPE_NONE;
		live_pct = 0;
		return;
	}
	live_dir = ui_swipe_heading(dx, dy);
	live_pct = live_dir == UI_SWIPE_NONE
			   ? 0 : ui_swipe_progress(ax > ay ? ax : ay);
}

/*
 * The stitch window has passed with no contact, so the stroke really has
 * ended. Runs on the system workqueue, rescheduled by every report -- so a
 * dropout in the middle of a stroke pushes it out rather than closing it.
 */
static void stroke_end(struct k_work *work)
{
	ARG_UNUSED(work);
	k_spinlock_key_t k = k_spin_lock(&lock);
	int dx = x1 - x0, dy = y1 - y0;
	int ms = (int)(last_down_ms - start_ms), n = samples;
	bool say_refused = active && !fired && (dx != 0 || dy != 0);

	active = false;
	fired = false;
	/* Let go without committing and the rail comes back, which is the
	 * other half of what the indicator is for: it says the swipe did not
	 * happen, rather than leaving a half-grown dot behind as if it had. */
	live_dir = UI_SWIPE_NONE;
	live_pct = 0;
	k_spin_unlock(&lock, k);

	/* Only refusals are still unreported: anything that counted said so
	 * when it was handed over. */
	if (say_refused) {
		printk("[swipe] dx %d dy %d in %d ms, %d samples -> 0\n",
		       dx, dy, ms, n);
	}
}

static void report(void)
{
	int64_t now = k_uptime_get();
	/* Channel axes in, screen axes out. See the transform above. */
	int32_t sx = SCREEN_DX(cur_x, cur_y);
	int32_t sy = SCREEN_DY(cur_x, cur_y);
	k_spinlock_key_t k;

	if (!cur_down) {
		return;
	}

	k = k_spin_lock(&lock);
	if (!active) {
		active = true;
		fired = false;
		samples = 0;
		x0 = sx;
		y0 = sy;
		start_ms = now;
	}
	x1 = sx;
	y1 = sy;
	last_down_ms = now;
	samples++;
	consider();
	publish_live();
	k_spin_unlock(&lock, k);

	/*
	 * Push the end of the stroke out on every report. A release on this
	 * panel is usually contact bounce under a moving finger, and this is
	 * what absorbs it: the stroke only ends once the reports actually stop
	 * for UI_SWIPE_STITCH_MS.
	 */
	k_work_reschedule(&stitch_work, K_MSEC(UI_SWIPE_STITCH_MS));
}

static void input_cb(struct input_event *evt, void *user_data)
{
	ARG_UNUSED(user_data);

	switch (evt->code) {
	case INPUT_ABS_X:
		cur_x = evt->value;
		break;
	case INPUT_ABS_Y:
		cur_y = evt->value;
		break;
	case INPUT_BTN_TOUCH:
		cur_down = evt->value;
		break;
	default:
		break;
	}
	if (evt->sync) {
		report();
	}
}

bool ui_swipe_dragging(void)
{
	k_spinlock_key_t k = k_spin_lock(&lock);
	bool drag = active && ui_swipe_is_drag(x1 - x0, y1 - y0);

	k_spin_unlock(&lock, k);
	return drag;
}

/*
 * Hand a decided stroke to the caller, on the LVGL thread.
 *
 * The measurement happens on the input thread because that is where the data
 * is; the ACT has to happen here, because what it does is flag work for the
 * mode loop and read screen state, and neither is safe from an input callback.
 * The printk lives here too rather than in the input path, whose thread stack
 * is sized for callbacks that only forward events.
 */
static void drain_cb(lv_timer_t *t)
{
	ARG_UNUSED(t);
	k_spinlock_key_t k = k_spin_lock(&lock);
	enum ui_swipe_dir d = pending;
	int dx = pend_dx, dy = pend_dy, ms = pend_ms, n = pend_n;
	enum ui_swipe_dir ld = live_dir;
	int lp = live_pct;

	pending = UI_SWIPE_NONE;
	k_spin_unlock(&lock, k);

	/*
	 * The indicator first, and unconditionally. It is a state rather than
	 * an event, so it is pushed every tick and the consumer drops the
	 * repeats -- which keeps the "let go and it goes back" case from
	 * needing a message of its own.
	 */
	if (show != NULL) {
		show(ld, lp);
	}

	if (d == UI_SWIPE_NONE) {
		return;
	}
	/*
	 * One line per stroke: where it had got to when it was decided, how
	 * long that took, and how many panel reports it took to get there.
	 * The sample count is the one that matters now -- polling LVGL saw two
	 * or three per stroke, which is why it fired at 150 px.
	 */
	printk("[swipe] dx %d dy %d in %d ms, %d samples -> %d\n", dx, dy, ms,
	       n, (int)d);
	if (emit != NULL) {
		emit(d);
	}
}

void ui_swipe_init(void (*on_swipe)(enum ui_swipe_dir dir),
		   void (*on_progress)(enum ui_swipe_dir dir, int pct))
{
	emit = on_swipe;
	show = on_progress;
	lv_timer_create(drain_cb, DRAIN_MS, NULL);
}

#if DT_NODE_EXISTS(DT_NODELABEL(xpt2046))
INPUT_CALLBACK_DEFINE(DEVICE_DT_GET(DT_NODELABEL(xpt2046)), input_cb, NULL);
#else
#error "ui_swipe needs the xpt2046 node: it reads the panel, not LVGL's replay"
#endif
