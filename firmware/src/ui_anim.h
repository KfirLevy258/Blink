#ifndef UI_ANIM_H
#define UI_ANIM_H

#include <stdbool.h>

/*
 * The boot eyes clip as a gauge-screen easter egg: a right swipe (the left
 * chevron) plays it on loop until a swipe brings the gauges back.
 */

/*
 * Gesture-mute window, not a slide duration.
 *
 * It was both, once. The clip transition no longer has a duration to set here
 * at all: ui_slide.c scrolls the panel a fixed number of steps and blocks
 * until it is done, so the length is however long eighty renders take.
 *
 * What survives is the timing of the mutes built on it. ui_slide_run() blocks
 * with pump() still feeding LVGL input, so the swipe that started a transition
 * is still arriving while it runs -- the handlers stay muted for a multiple of
 * this across the slide, then a short tail past it.
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
