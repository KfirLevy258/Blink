#ifndef UI_ANIM_H
#define UI_ANIM_H

#include <stdbool.h>

/*
 * The boot eyes clip as a gauge-screen easter egg: a right swipe (the left
 * chevron) plays it on loop until a swipe brings the gauges back.
 */

/*
 * How long a screen transition runs, and how long a caller must keep pumping
 * LVGL afterwards for it to actually finish.
 *
 * 400 ms, up from 250. Measured on hardware 2026-07-26: a full-screen redraw
 * costs ~124 ms, so a 250 ms transition got through barely two frames and read
 * as a jump cut rather than movement. This buys no extra frames per second --
 * nothing about rendering got faster -- it spreads the few frames the panel can
 * afford across enough time that the eye reads them as travel instead of a
 * stutter.
 *
 * SETTLE must stay comfortably above SLIDE. ui_anim streams decoded frames
 * straight at the panel behind LVGL's back, and beginning that -- or deleting
 * the outgoing screen -- while a transition is still animating tears the
 * display. The two move together; that is why they live here rather than as a
 * literal at each call site.
 */
#define UI_SLIDE_MS        400
#define UI_SLIDE_SETTLE_MS (UI_SLIDE_MS + 50)

/* Called from LVGL event context (the swipe handler); the mode loop notices
 * via ui_anim_pending() and runs the player from thread context. */
void ui_anim_request(void);
bool ui_anim_pending(void);

/* Play until the user swipes back; returns with the previous screen
 * reloaded. `pump` (may be NULL) runs between frames so the mode's
 * background duties -- serial protocol, event queue -- stay alive. */
void ui_anim_run(void (*pump)(void));

#endif /* UI_ANIM_H */
