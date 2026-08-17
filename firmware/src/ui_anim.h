#ifndef UI_ANIM_H
#define UI_ANIM_H

#include <stdbool.h>

/*
 * The boot eyes clip as a gauge-screen easter egg: a right swipe (the left
 * chevron) plays it on loop until a swipe brings the gauges back.
 */

/*
 * How long the clip transition runs.
 *
 * Back to 250 ms. It was raised to 400 on 2026-07-26 for a reason that has
 * since been removed rather than solved: a full-screen redraw costs ~124 ms,
 * so 250 ms got through barely two frames, and stretching it to 400 spread
 * those few frames far enough apart to read as travel instead of a jump cut.
 * It bought no frames -- it only hid how few there were.
 *
 * The clip now slides as a bar rather than a whole screen (see ui_anim.c), so
 * the frames are actually there: ~14 ms each measured on hardware, about ten
 * of them across 250 ms. Holding 400 would just make a smooth animation slow.
 *
 * There is no companion SETTLE constant. ui_anim waits on the overlay's own
 * geometry -- see settle_slide(), which replaced a two-stage wait on LVGL's
 * prev_scr flag that an overlay never sets.
 */
#define UI_SLIDE_MS 250

/* Called from LVGL event context (the swipe handler); the mode loop notices
 * via ui_anim_pending() and runs the player from thread context. */
void ui_anim_request(void);
bool ui_anim_pending(void);

/* True while the tail of a clip-exit swipe may still be replaying onto the
 * screen underneath. Screen-level gesture handlers must ignore gestures while
 * it holds, or the swipe that left the clip acts a second time on what it
 * uncovered. */
bool ui_anim_gesture_muted(void);

/* Play until the user swipes back; returns with the previous screen
 * reloaded. `pump` (may be NULL) runs between frames so the mode's
 * background duties -- serial protocol, event queue -- stay alive. */
void ui_anim_run(void (*pump)(void));

#endif /* UI_ANIM_H */
