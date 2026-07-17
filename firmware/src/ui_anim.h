#ifndef UI_ANIM_H
#define UI_ANIM_H

#include <stdbool.h>

/*
 * The boot eyes clip as a gauge-screen easter egg: a right swipe (the left
 * chevron) plays it on loop until a swipe brings the gauges back.
 */

/* Called from LVGL event context (the swipe handler); the mode loop notices
 * via ui_anim_pending() and runs the player from thread context. */
void ui_anim_request(void);
bool ui_anim_pending(void);

/* Play until the user swipes back; returns with the previous screen
 * reloaded. `pump` (may be NULL) runs between frames so the mode's
 * background duties -- serial protocol, event queue -- stay alive. */
void ui_anim_run(void (*pump)(void));

#endif /* UI_ANIM_H */
