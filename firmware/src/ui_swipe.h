#ifndef UI_SWIPE_H
#define UI_SWIPE_H

#include <stdbool.h>

/*
 * Swipe detection for a resistive panel, done here instead of by LVGL.
 *
 * LVGL's own detector cannot work on this hardware, and the reason is not a
 * threshold that needs tuning. Measured on the board 2026-08-27 with the
 * touch tracer (CONFIG_CLAUGE_TOUCH_TRACE), five deliberate vertical swipes
 * produced THIRTY separate press-release cycles:
 *
 *   press durations, ms: 17 26 29 38 62 64 91 98 100 104 105 128 138 139
 *                        540 643 856 865
 *   12 of 30 traces held fewer than two samples
 *   inter-report gaps: median 13 ms, but the top fifth ran 67-90 ms
 *
 * A finger SLIDING on a resistive panel keeps losing enough contact pressure
 * for PENIRQ to let go, and the xpt2046 driver reports a release the first
 * time it reads the pin deasserted (input_xpt2046.c, xpt2046_release_handler).
 * So one physical stroke arrives as five or six short presses. LVGL resets its
 * gesture accumulator on every press boundary, so most strokes never reach a
 * threshold at all -- and the fragments that do get classified on whatever
 * partial displacement they happened to carry, which is how a swipe UP opens
 * the settings panel.
 *
 * The second failure is LVGL's gesture_min_velocity, which does not mean what
 * it sounds like: a sample that moved less than it in BOTH axes ZEROES the
 * accumulated total rather than being skipped. LVGL samples at its refresh
 * period and the panel reports more slowly than that, so a large share of
 * ticks see no new point, and each one throws the stroke away. The floor
 * cannot be set below 1, and at 1 a repeated identical point still trips it.
 *
 * What this does instead:
 *
 *   - Polls the pointer's SCREEN coordinates, the same ones the touch marker
 *     was calibrated against, so there is no axis or sign to re-derive.
 *   - STITCHES across brief releases. A gap shorter than UI_SWIPE_STITCH_MS
 *     is contact bounce in the middle of a stroke, not the end of one.
 *   - Decides on the whole stroke, once, when it really ends.
 *   - Requires the movement to be decisively along ONE axis. An ambiguous
 *     diagonal does nothing, rather than picking the axis that happened to
 *     win by a pixel and taking you somewhere you did not ask to go.
 */
enum ui_swipe_dir {
	UI_SWIPE_NONE = 0,
	UI_SWIPE_LEFT,		/* finger travelled left */
	UI_SWIPE_RIGHT,
	UI_SWIPE_UP,
	UI_SWIPE_DOWN,
};

/*
 * Start watching the pointer. Call once, after the display and the input
 * device are up.
 *
 * `cb` runs from an LVGL timer -- so it is on the LVGL thread, in the same
 * context as an event callback, and must NOT run a transition itself. Flag
 * the request and let the mode loop do it, exactly as the gesture handler it
 * replaces did.
 */
void ui_swipe_init(void (*cb)(enum ui_swipe_dir dir));

/*
 * Whether the touch in progress (or the one that just ended) has travelled far
 * enough to be a drag rather than a tap.
 *
 * The edge tap zones need this. LVGL sends CLICKED on release whenever an
 * object was pressed and nothing scrolled -- it does not suppress it because
 * the touch turned out to be a swipe -- so a swipe that begins or ends inside
 * one of those 44x150 strips fires it. That is the OTHER half of "some of them
 * is detected as swipe to settings": not a misread direction at all, but a
 * button press that a failed swipe left behind.
 *
 * Answered live, from the stroke still being tracked, because CLICKED arrives
 * on release -- before the stitch window has expired and long before the
 * stroke is classified.
 */
bool ui_swipe_dragging(void);

#endif /* UI_SWIPE_H */
