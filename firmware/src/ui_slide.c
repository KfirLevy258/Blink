/*
 * Hardware-scrolled full-screen transitions. See ui_slide.h for why.
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/mipi_dbi.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>

#include "ui_slide.h"

/* The panel's scroll axis is its native vertical: 320 lines, which rotation=90
 * presents as the landscape screen's horizontal. So a "line" here is a screen
 * COLUMN, and there are LV_HOR_RES of them. */
#define SCROLL_LINES	320

/*
 * Columns painted, and scrolled, per step.
 *
 * 4 gives 80 steps across the screen. The whole transition renders 320 columns
 * once however this is set -- the step size only decides how finely that work
 * is chopped. It also sets the width of the one artifact this scheme cannot
 * avoid: GRAM is exactly one screen, so there is nowhere off-screen to stage a
 * strip, and a line is briefly visible between arriving at the incoming edge
 * and being painted. 4 px of that is a seam; 8 was a visible band.
 */
#define STEP_COLS	4

static const struct device *const dbi =
	DEVICE_DT_GET(DT_PARENT(DT_CHOSEN(zephyr_display)));
static const struct mipi_dbi_config dbi_cfg =
	MIPI_DBI_CONFIG_DT(DT_CHOSEN(zephyr_display),
			   SPI_OP_MODE_MASTER | SPI_WORD_SET(8), 0);

static bool area_defined;

/* VSCRDEF: top fixed 0, scrolling 320, bottom fixed 0 -- the whole panel
 * scrolls. Sent once; it survives until the panel is reset. */
static void define_scroll_area(void)
{
	uint8_t def[6] = { 0x00, 0x00,
			   SCROLL_LINES >> 8, SCROLL_LINES & 0xFF,
			   0x00, 0x00 };

	if (area_defined) {
		return;
	}
	if (mipi_dbi_command_write(dbi, &dbi_cfg, 0x33, def, sizeof(def)) == 0) {
		area_defined = true;
	} else {
		printk("[slide] VSCRDEF refused; transitions will not scroll\n");
	}
}

static void scroll_to(int line)
{
	uint8_t sar[2] = { (uint8_t)(line >> 8), (uint8_t)(line & 0xFF) };

	mipi_dbi_command_write(dbi, &dbi_cfg, 0x37, sar, sizeof(sar));
}

void ui_slide_freeze(bool frozen)
{
	lv_display_enable_invalidation(lv_display_get_default(), !frozen);
}

void ui_slide_reset(void)
{
	if (area_defined) {
		scroll_to(0);
	}
}

void ui_slide_run(int dir, void (*pump)(void))
{
	lv_display_t *disp = lv_display_get_default();
	lv_obj_t *scr = lv_screen_active();

	define_scroll_area();
	if (!area_defined) {
		/* No scroll: fall back to simply painting the new screen once.
		 * Ugly, but a transition that does not happen beats a screen
		 * that never gets drawn. */
		lv_obj_invalidate(scr);
		lv_refr_now(disp);
		return;
	}

	/*
	 * Frozen for the whole run, not just the caller's setup.
	 *
	 * pump() runs lv_timer_handler(), so the countdown and status widgets
	 * keep invalidating themselves throughout -- and a repaint of theirs
	 * writes to un-scrolled GRAM coordinates, which the live scroll offset
	 * then presents somewhere else entirely. That is the stray fragment of
	 * one screen appearing on the far side of the other (user-reported
	 * 2026-08-17). Freezing drops those invalidations; the widgets are
	 * repainted by the settle below, and the countdown re-ticks within a
	 * second anyway.
	 */
	ui_slide_freeze(true);

	for (int j = STEP_COLS; j <= SCROLL_LINES; j += STEP_COLS) {
		lv_area_t strip;
		int off;

		/*
		 * Which lines are arriving at the incoming edge this step.
		 *
		 * Ascending for a leftward exit -- offset j puts lines 0..j-1
		 * at the right, so line j-STEP..j is what just wrapped round.
		 * Descending for a rightward one -- offset 320-j puts lines
		 * 320-j.. at the left.
		 *
		 * Either way the invariant is the same and it is what makes the
		 * whole scheme close: line L holds the incoming screen's column
		 * L, so at the final step the offset is 0 and GRAM is simply
		 * the new screen, in order, with nothing to correct.
		 */
		if (dir == UI_SLIDE_LEFT) {
			strip.x1 = j - STEP_COLS;
			off = j;
		} else {
			strip.x1 = SCROLL_LINES - j;
			off = SCROLL_LINES - j;
		}
		strip.x2 = strip.x1 + STEP_COLS - 1;
		strip.y1 = 0;
		strip.y2 = LV_VER_RES - 1;

		/*
		 * Scroll FIRST, then paint. The other order paints the strip
		 * while it is still at the OUTGOING edge, so a sliver of the
		 * incoming screen flashes on the wrong side of the display and
		 * only then travels round -- visible as a shimmer down the far
		 * edge for the whole transition (user-reported 2026-08-17).
		 * After the scroll those same lines are at the incoming edge,
		 * which is where their content belongs.
		 */
		scroll_to(off % SCROLL_LINES);

		/* Unfrozen for exactly one call: this strip must be the ONLY
		 * area LVGL considers dirty. See the freeze around the loop. */
		ui_slide_freeze(false);
		lv_obj_invalidate_area(scr, &strip);
		ui_slide_freeze(true);
		lv_refr_now(disp);

		if (pump) {
			pump();
		}
		/* Yield rather than sleep. A timed sleep here added its own
		 * delay to all eighty steps and was most of what made the
		 * transition feel slow; the render is the pacing, and pump()
		 * has already serviced the protocol and fed the watchdog. */
		k_yield();
	}

	/*
	 * A completed run ends on offset 0 by construction (j == 320 for a
	 * leftward exit, 320-j == 0 for a rightward one), with GRAM holding the
	 * new screen at its natural coordinates -- no scroll to undo.
	 *
	 * The settle is for LVGL's benefit, not the picture's: everything that
	 * tried to invalidate while frozen was dropped, so one honest full
	 * repaint puts its idea of the screen back in step with the panel's. It
	 * costs ~124 ms and draws exactly what is already there, so it is not
	 * visible -- unlike the same repaint in the middle of a transition,
	 * which is the whole problem this file exists to avoid.
	 */
	ui_slide_freeze(false);
	lv_obj_invalidate(scr);
	lv_refr_now(disp);
}
