#ifndef UI_SLIDE_H
#define UI_SLIDE_H

#include <lvgl.h>

#include "ui_slide_geom.h"

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

/*
 * Direction the existing image travels: UI_SLIDE_LEFT / RIGHT / UP / DOWN,
 * defined in ui_slide_geom.h alongside the strip arithmetic they select.
 *
 * VERTICAL IS WIPE-ONLY, and the reason is the paragraph above: the panel's
 * scroll register moves the screen sideways and there is no hardware path for
 * the other axis. A wipe does not use that register at all -- it paints each
 * strip straight into the screen columns or rows where it will be seen -- so
 * it is free of the constraint, and ui_slide_run() refuses a vertical
 * direction if the build is ever switched back to the scrolled slide.
 *
 * The cost is identical either way and on either axis: one full render of the
 * incoming screen, chopped into strips. A vertical transition is 60 steps
 * where a horizontal one is 80, because the screen is 240 tall and 320 wide.
 */

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
 *
 * `pump` must NOT call lv_timer_handler(). This drives lv_refr_now() itself,
 * and re-entering the refresh from inside the handler is the same unsafe
 * nesting the note above forbids -- and it would let LVGL timers create and
 * delete objects halfway through a transition. The consequence, which callers
 * have to design around rather than fix here, is that NO input is dispatched
 * for the length of a transition: touch points accumulate in the input msgq
 * and are delivered in one burst by the first lv_timer_handler() after it.
 * That is what the gesture mutes in ui_anim exist to absorb.
 */
void ui_slide_run(int dir, void (*pump)(void));

/*
 * The same transition with the pacing named explicitly.
 *
 * `step_px` is the strip width, and in wipe mode it is a SPEED control rather
 * than a quality one: the per-step refresh overhead dwarfs the pixels, so
 * halving the step count nearly halves the duration. It must divide the
 * travel exactly (320 and 240 both divide by 4 and 8) or it is ignored, since
 * a short final strip would leave a stripe of the outgoing screen behind.
 *
 * `min_ms` is a floor, not a target: a transition that already cost more than
 * this is not slowed further.
 *
 * ui_slide_run() is this with the defaults the settings panel and the boot
 * clip were tuned to, and those two should keep using it -- they were judged
 * by eye on hardware and there is nothing to gain by disturbing them.
 */
void ui_slide_run_paced(int dir, int step_px, int min_ms,
			void (*pump)(void));

/*
 * Pacing for the provider-page change specifically.
 *
 * 8 px over the 240-pixel axis is 30 steps where the default 4 px would be 60,
 * and the duration is made almost entirely of per-step overhead -- so this is
 * about half the time for a reveal that is still finer than the eye tracks at
 * 60 cm. The 650 ms default was reported as too slow for a page change, which
 * is a smaller act than opening a panel and should not cost the same.
 */
#define UI_SLIDE_PAGE_STEP_PX	8
#define UI_SLIDE_PAGE_MIN_MS	260

/*
 * Open a transition: render what is already dirty, then freeze.
 *
 * The freeze only blocks NEW invalidations -- LVGL's existing invalid-area
 * list survives it untouched, and ui_slide_run's first lv_refr_now() would
 * draw those areas too, at whatever scroll offset the panel had already moved
 * to. Callers reach a transition straight out of the network-event drain in
 * main.c with no lv_timer_handler() in between, so anything those handlers
 * invalidated is still queued: a status overlay that just un-hid itself is a
 * FULL-SCREEN area, which would paint the whole destination into every line at
 * once and leave nothing to slide.
 *
 * Draining first costs one honest repaint of the outgoing screen, at offset 0,
 * where it is invisible. Use this instead of ui_slide_freeze(true) wherever a
 * transition begins.
 */
void ui_slide_begin(void);

/*
 * Hide or restore lv_layer_top() outside a transition.
 *
 * ui_slide_begin() hides it and ui_slide_run() restores it, which covers the
 * slides themselves. Callers that hold the display between two slides -- the
 * clip player streams frames straight to GRAM for as long as the user watches
 * -- need it hidden for that stretch too: anything up there repaints over the
 * streamed image, and ui_touchfx's press echo lives exactly there, so every
 * touch would punch a hole through the animation.
 */
void ui_slide_top_hide(bool hide);

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

/*
 * There is deliberately no ui_slide_reset() here.
 *
 * A completed run lands on offset 0 by construction, and the only way to
 * abandon one part-way is the sys_reboot() inside a caller's pump -- after
 * which Zephyr's ili9xxx driver issues both a hardware reset pulse and a
 * SWRESET before the first pixel, so VSCRSADD and VSCRDEF are back to their
 * defaults anyway. A reset entry point had no caller and nothing to fix.
 */

#endif /* UI_SLIDE_H */
