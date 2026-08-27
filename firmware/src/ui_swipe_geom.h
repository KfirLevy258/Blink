#ifndef UI_SWIPE_GEOM_H
#define UI_SWIPE_GEOM_H

#include "ui_swipe.h"

/*
 * The rules that turn a displacement into a swipe, as pure functions.
 *
 * Split out of ui_swipe.c for the same reason ui_slide_geom.h was split out of
 * ui_slide.c: this is the part that decides whether a real stroke measured on
 * real hardware counts, and getting it wrong sends the user to the wrong
 * screen. tests/ui_swipe_geom/host_test.c checks it against displacements
 * taken from the touch trace, so the thresholds are pinned to strokes that
 * actually happened rather than to numbers that seemed reasonable.
 *
 * No includes beyond ui_swipe.h on purpose -- not lvgl.h, not zephyr.h -- so a
 * host test can compile it.
 */

/*
 * How far a stroke must travel along its main axis.
 *
 * The vertical strokes in the 2026-08-27 trace measured 110, 120, 144, 174,
 * 177 and 178 px, so anything up to about 100 would admit all of them. 36 px
 * is 6.4 mm on this panel: far enough clear of a tap's jitter (a fingertip
 * wanders 5-10 px on a press) that a stationary press cannot reach it, and
 * short enough that a hurried half-stroke still counts. LVGL wanted 50 on top
 * of an accumulator that kept resetting.
 */
#define UI_SWIPE_MIN_PX		36

/*
 * How far a stroke must travel before a CLICK on it is disowned.
 *
 * Smaller than a swipe on purpose: this is the tap/drag line, not the
 * tap/swipe one. A press that wandered 16 px was not aimed at a button, and
 * the edge zones would rather miss a sloppy tap than open the settings panel
 * at the end of a failed swipe.
 */
#define UI_SWIPE_DRAG_PX	16

/*
 * How decisively one axis must beat the other, in eighths.
 *
 * 12/8 = 1.5x. The stroke that opened settings during a vertical swipe
 * measured dx -154, dy +53 -- 2.9x, so it was genuinely a horizontal drag and
 * this would not have caught it. What this catches is the near-diagonal, where
 * the winning axis wins by a hair and the direction is a coin toss. Refusing
 * costs one repeated swipe; guessing costs a screen change nobody asked for.
 *
 * Eighths rather than a float: this runs on a stroke, not in a loop, but the
 * host test compares it against integer displacements and exact arithmetic is
 * what makes those assertions mean anything.
 */
#define UI_SWIPE_DOMINANCE_8	12

static inline int ui_swipe_abs(int v)
{
	return v < 0 ? -v : v;
}

/* Whether a press that moved this far should still count as a tap. */
static inline bool ui_swipe_is_drag(int dx, int dy)
{
	return ui_swipe_abs(dx) >= UI_SWIPE_DRAG_PX ||
	       ui_swipe_abs(dy) >= UI_SWIPE_DRAG_PX;
}

/*
 * The direction of a completed stroke, or UI_SWIPE_NONE.
 *
 * Screen coordinates: x grows right, y grows DOWN. So a negative dy is a
 * finger moving up the panel, which is UI_SWIPE_UP -- the sign flip lives
 * here, once, rather than at each caller.
 */
static inline enum ui_swipe_dir ui_swipe_classify(int dx, int dy)
{
	const int ax = ui_swipe_abs(dx);
	const int ay = ui_swipe_abs(dy);
	const int major = ax > ay ? ax : ay;
	const int minor = ax > ay ? ay : ax;

	if (major < UI_SWIPE_MIN_PX) {
		return UI_SWIPE_NONE;		/* a tap, or a short drag */
	}
	if (major * 8 < minor * UI_SWIPE_DOMINANCE_8) {
		return UI_SWIPE_NONE;		/* too diagonal to call */
	}
	if (ax > ay) {
		return dx > 0 ? UI_SWIPE_RIGHT : UI_SWIPE_LEFT;
	}
	return dy > 0 ? UI_SWIPE_DOWN : UI_SWIPE_UP;
}

/*
 * How long a release has to last before it is believed.
 *
 * The gaps this bridges are contact bounce under a moving finger. The trace's
 * press fragments ran 17-140 ms with the dropouts between them shorter still,
 * and a deliberate second swipe is nowhere near that quick -- a finger has to
 * leave the panel, travel back and land again. 120 ms is comfortably above the
 * bounce and comfortably below anything a person does on purpose.
 *
 * This is NOT latency. The swipe fires the moment it is unambiguous, with the
 * finger still down; this only decides when the stroke has ended and a new one
 * may begin, which is what stops the tail of a long drag from firing twice.
 */
#define UI_SWIPE_STITCH_MS	120

#endif /* UI_SWIPE_GEOM_H */
