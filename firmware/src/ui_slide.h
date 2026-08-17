#ifndef UI_SLIDE_H
#define UI_SLIDE_H

#include <lvgl.h>

/*
 * Full-screen slide transitions driven by the ILI9341's own scroll hardware.
 *
 * The reason this exists: an LVGL slide has to redraw every pixel it moves,
 * which is ~124 ms for this screen -- two or three frames across a whole
 * transition. But a slide does not CHANGE anything, it only moves it, and the
 * panel already holds the rendered image in its GRAM. VSCRSAR (0x37) tells the
 * panel which GRAM line to display first, so the image moves for the cost of a
 * two-byte SPI write and no rendering at all.
 *
 * Because rotation=90 sets MADCTL_MV, the panel's native vertical axis is the
 * landscape screen's HORIZONTAL one -- so this scrolls sideways, which is the
 * direction the transitions actually go. Confirmed on hardware 2026-08-17.
 *
 * The scroll is cyclic over exactly 320 lines, which is precisely one screen:
 * there is no room to stage a second one. That turns out not to matter. At
 * offset k, screen column c shows GRAM line (k + c) mod 320, so as the old
 * screen shifts off one edge it exposes GRAM lines that can be overwritten
 * with the new screen before they appear at the other. Fill line L with the
 * incoming screen's column L and the arithmetic closes itself: line L becomes
 * visible exactly as k passes it, and at k = 320 the offset is back to 0 with
 * GRAM holding the new screen at its natural coordinates. No fixup after.
 *
 * The cost of a transition is therefore ONE full render, spread across as many
 * frames as it has steps, instead of one full render per frame.
 */

/* Direction the existing image travels. */
#define UI_SLIDE_LEFT	1	/* old screen exits left, new enters from right */
#define UI_SLIDE_RIGHT	(-1)	/* old screen exits right, new enters from left */

/*
 * Run one transition, blocking until it completes.
 *
 * MUST be called from thread context, never from an LVGL event callback: it
 * drives lv_refr_now() itself, and re-entering the refresh from inside
 * lv_timer_handler() is not safe. Callers flag the request and run it from
 * their mode loop, the same way ui_anim does.
 *
 * The caller sets the object tree up first -- whatever is visible when a strip
 * is painted is what lands in that strip -- and does so with invalidation
 * disabled (see ui_slide_freeze) so that setup does not itself repaint over
 * the pixels this is about to scroll.
 *
 * `pump` (may be NULL) runs each step so the caller's background duties, and
 * on a test boot the watchdog, stay alive.
 */
void ui_slide_run(int dir, void (*pump)(void));

/*
 * Suspend/resume LVGL invalidation around the tree surgery a transition needs.
 *
 * Creating the incoming panel, or hiding the outgoing widgets, invalidates the
 * screen -- and a refresh of that invalidation would repaint the very pixels
 * the scroll is about to move, erasing the outgoing image before it can leave.
 * Freezing means the setup records nothing and ui_slide_run's strips are the
 * only areas that ever get drawn.
 */
void ui_slide_freeze(bool frozen);

/* Return the panel to an unscrolled state. Called on the paths that abandon a
 * transition; a completed one already lands on zero. */
void ui_slide_reset(void);

#endif /* UI_SLIDE_H */
