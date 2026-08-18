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
 * for the whole transition, and pump() does NOT run lv_timer_handler() -- see
 * ui_slide.h -- so nothing is dispatched to LVGL while it runs. Touch points
 * queue in the input msgq instead and are delivered in one burst by the first
 * lv_timer_handler() afterwards. The mute therefore has to outlive the slide
 * and then some: it is held for a multiple of this across the transition, so
 * the window is still open when the burst lands, plus a short tail past it.
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

/*
 * Arm that window for `ms` from now.
 *
 * Exposed so ui_settings can guard its own open/close transitions with it.
 * Those block for a full slide with no input dispatched, exactly like the
 * clip's, so the buffered swipe lands on whatever the transition uncovered --
 * and a RIGHT swipe there is what asks for the clip, so settings closing could
 * start the eyes by itself.
 */
void ui_anim_gesture_mute(int ms);

/* Play until the user swipes back; returns with the previous screen
 * reloaded. `pump` (may be NULL) runs between frames so the mode's
 * background duties -- serial protocol, event queue -- stay alive. */
void ui_anim_run(void (*pump)(void));

#endif /* UI_ANIM_H */
