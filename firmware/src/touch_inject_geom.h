#ifndef TOUCH_INJECT_GEOM_H
#define TOUCH_INJECT_GEOM_H

/*
 * The bench touch injector's arithmetic, as pure functions
 * (CONFIG_BLINK_TOUCH_INJECT; see touch_inject.c). Split out the way
 * ui_swipe_geom.h is, for the same reason: this is the part that decides
 * where an injected finger lands, and getting it wrong taps the wrong button
 * while every reply still says the tap happened. tests/touch_inject/host_test.c
 * checks it over every pixel of the panel.
 *
 * No includes beyond <stdbool.h> and <string.h> -- not zephyr, not lvgl -- so
 * a host test can compile it. Nothing here is compiled into a release image:
 * the only includer is touch_inject.c, which the build adds only with the
 * option on.
 */
#include <stdbool.h>
#include <string.h>

/* The screen as LVGL sees it: the ILI9341 is portrait-native and rotated 90,
 * so the display reports 320 x 240. */
#define TI_SCREEN_W 320
#define TI_SCREEN_H 240

/* What the XPT2046 driver puts on the channels: its readings scaled to
 * touchscreen-size-x/-y, which the overlay sets to the PORTRAIT 240 x 320. */
#define TI_CHAN_X_MAX (TI_SCREEN_H - 1)
#define TI_CHAN_Y_MAX (TI_SCREEN_W - 1)

/*
 * One report every 13 ms while the finger is down: the median inter-report
 * gap in the 2026-08-27 touch trace (ui_swipe.h). A stroke played faster
 * than the panel can report would test a panel this board does not have.
 */
#define TI_STEP_MS 13

/*
 * The lift arrives this long after the last report that still saw the
 * finger. That is how the driver does it -- input_xpt2046.c reports the
 * release from a delayed re-check of PENIRQ, 100 ms out -- and it matters,
 * because ui_swipe's re-arm window and the zones' tap test were both tuned
 * against a release that comes late (UI_SWIPE_REARM_MS).
 */
#define TI_RELEASE_LAG_MS 100

/* No stroke longer than this, and a press with no release lets go by itself
 * at the same mark: a bench script that dies mid-press must not leave a
 * finger on the panel. */
#define TI_MAX_MS 10000
#define TI_HOLD_MAX_MS TI_MAX_MS

enum ti_op {
	TI_OP_NONE = 0,
	TI_OP_TAP,	/* x0,y0 held for ms, then lifted */
	TI_OP_SWIPE,	/* x0,y0 -> x1,y1 over ms, then lifted */
	TI_OP_PRESS,	/* x0,y0 held until a release (or TI_HOLD_MAX_MS) */
	TI_OP_RELEASE,	/* lift the finger a press left down */
	/* Not strokes: no finger goes down. Bench housekeeping a person
	 * would do by eye -- see touch_inject.c. */
	TI_OP_DISMISS,	/* take down the notice popup */
	TI_OP_WAKE,	/* end a doze, as a wake reason would */
};

struct ti_stroke {
	enum ti_op op;
	int x0, y0;	/* screen coordinates */
	int x1, y1;	/* swipe only */
	int ms;		/* tap: hold; swipe: travel; otherwise unused */
};

struct ti_sample {
	int t_ms;	/* from the stroke's first report */
	int x, y;	/* screen coordinates */
};

static inline enum ti_op ti_op_from_name(const char *s)
{
	if (strcmp(s, "tap") == 0) {
		return TI_OP_TAP;
	}
	if (strcmp(s, "swipe") == 0) {
		return TI_OP_SWIPE;
	}
	if (strcmp(s, "press") == 0) {
		return TI_OP_PRESS;
	}
	if (strcmp(s, "release") == 0) {
		return TI_OP_RELEASE;
	}
	if (strcmp(s, "dismiss") == 0) {
		return TI_OP_DISMISS;
	}
	if (strcmp(s, "wake") == 0) {
		return TI_OP_WAKE;
	}
	return TI_OP_NONE;
}

static inline int ti_clamp(int v, int lo, int hi)
{
	return v < lo ? lo : (v > hi ? hi : v);
}

/*
 * Channel frame to screen frame: what Zephyr's lvgl_pointer_input glue does
 * to a report on THIS board, step by step, so the host test checks the
 * inverse against the glue rather than against itself.
 *
 * The lvgl_pointer node has invert-x and nothing else; the display is
 * DISPLAY_ORIENTATION_ROTATED_90 and reports x_resolution 320, y_resolution
 * 240 (display_ili9xxx.c swaps them when rotated). lvgl_pointer_input.c then:
 *
 *   tmp.x = y_resolution - ABS_X	invert-x, rotated, so it uses y_res
 *   tmp.y = ABS_Y
 *   point.x = tmp.y			ROTATED_90
 *   point.y = y_resolution - tmp.x
 *   clamp both into the screen
 *
 * which is the same thing ui_swipe.c derived for displacements: two channels,
 * straight across.
 */
static inline void ti_chan_to_screen(int cx, int cy, int *sx, int *sy)
{
	int tx = TI_SCREEN_H - cx;
	int ty = cy;

	*sx = ti_clamp(ty, 0, TI_SCREEN_W - 1);
	*sy = ti_clamp(TI_SCREEN_H - tx, 0, TI_SCREEN_H - 1);
}

/*
 * ...and back, which is what the injector reports. Each step of the above
 * undone in reverse: point.y = 240 - tmp.x gives tmp.x = 240 - point.y, and
 * tmp.x = 240 - ABS_X gives ABS_X = 240 - tmp.x. So ABS_X is the screen's y
 * and ABS_Y its x. Exact for every pixel on the panel, so the clamp in the
 * glue never touches an injected point.
 */
static inline void ti_screen_to_chan(int sx, int sy, int *cx, int *cy)
{
	int tx = TI_SCREEN_H - sy;

	*cx = TI_SCREEN_H - tx;
	*cy = sx;
}

static inline bool ti_on_panel(int x, int y)
{
	return x >= 0 && x < TI_SCREEN_W && y >= 0 && y < TI_SCREEN_H;
}

static inline bool ti_valid(const struct ti_stroke *s)
{
	switch (s->op) {
	case TI_OP_TAP:
		return ti_on_panel(s->x0, s->y0) && s->ms >= 0 &&
		       s->ms <= TI_MAX_MS;
	case TI_OP_SWIPE:
		return ti_on_panel(s->x0, s->y0) && ti_on_panel(s->x1, s->y1) &&
		       s->ms >= TI_STEP_MS && s->ms <= TI_MAX_MS;
	case TI_OP_PRESS:
		return ti_on_panel(s->x0, s->y0);
	case TI_OP_RELEASE:
	case TI_OP_DISMISS:
	case TI_OP_WAKE:
		return true;
	default:
		return false;
	}
}

/* Reports with the finger down. A press is held by re-reporting at the
 * panel's pace, as a resting finger is, until its release or the cap. */
static inline int ti_samples(const struct ti_stroke *s)
{
	switch (s->op) {
	case TI_OP_TAP:
	case TI_OP_SWIPE:
		return s->ms / TI_STEP_MS + 1;
	case TI_OP_PRESS:
		return TI_HOLD_MAX_MS / TI_STEP_MS + 1;
	default:
		return 0;
	}
}

/*
 * Report `i` of the stroke. A swipe's points are spaced evenly in time and in
 * distance, ends exact; ms / (n - 1) is at least one step, so no gap is
 * shorter than the panel's and none reaches two steps.
 */
static inline void ti_sample(const struct ti_stroke *s, int i,
			     struct ti_sample *out)
{
	int n = ti_samples(s);

	if (s->op != TI_OP_SWIPE) {
		out->t_ms = i * TI_STEP_MS;
		out->x = s->x0;
		out->y = s->y0;
		return;
	}
	out->t_ms = i * s->ms / (n - 1);
	out->x = s->x0 + (s->x1 - s->x0) * i / (n - 1);
	out->y = s->y0 + (s->y1 - s->y0) * i / (n - 1);
}

/* When the lift is reported, from the stroke's first report. */
static inline int ti_release_ms(const struct ti_stroke *s)
{
	struct ti_sample last;

	ti_sample(s, ti_samples(s) - 1, &last);
	return last.t_ms + TI_RELEASE_LAG_MS;
}

#endif /* TOUCH_INJECT_GEOM_H */
