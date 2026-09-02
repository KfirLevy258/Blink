/*
 * Render the real gauge screen on this machine and write it out as a PPM.
 *
 * Not a mock: it compiles firmware/src/usage_view.c unchanged and drives the
 * same public functions proto.c calls. What it replaces is the panel -- the
 * flush callback copies into a plain framebuffer instead of an ILI9341 -- and
 * the two Zephyr symbols usage_view.c reaches for.
 *
 * The point is the one thing the host tests cannot cover: what the layout
 * actually LOOKS like. usage_layout.h and its host test prove the boxes do not
 * overlap; only pixels show whether the result reads well.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <lvgl.h>
#include "usage_view.h"
#include "usage_layout.h"

static uint16_t fb[SCR_W * SCR_H];

static void flush_cb(lv_display_t *disp, const lv_area_t *area, uint8_t *px)
{
	uint16_t *src = (uint16_t *)px;

	for (int32_t y = area->y1; y <= area->y2; y++) {
		for (int32_t x = area->x1; x <= area->x2; x++) {
			fb[y * SCR_W + x] = *src++;
		}
	}
	lv_display_flush_ready(disp);
}

static void write_ppm(const char *path)
{
	FILE *f = fopen(path, "wb");

	if (!f) {
		perror("fopen");
		exit(1);
	}
	fprintf(f, "P6\n%d %d\n255\n", SCR_W, SCR_H);
	for (int i = 0; i < SCR_W * SCR_H; i++) {
		/* RGB565 -> RGB888, replicating the high bits into the low
		 * ones so full-scale stays full-scale. */
		uint16_t c = fb[i];
		uint8_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;
		uint8_t out[3] = { (uint8_t)((r << 3) | (r >> 2)),
				   (uint8_t)((g << 2) | (g >> 4)),
				   (uint8_t)((b << 3) | (b >> 2)) };
		fwrite(out, 1, 3, f);
	}
	fclose(f);
}

static void settle(void)
{
	/* LVGL needs a few passes: one to lay out, one to draw, and the
	 * animation timer wants real time to have moved. */
	for (int i = 0; i < 40; i++) {
		lv_tick_inc(16);
		lv_timer_handler();
	}
}

int main(int argc, char **argv)
{
	static uint8_t buf[SCR_W * 40 * 2];
	const char *out = argc > 1 ? argv[1] : "panel.ppm";
	int scene = argc > 2 ? atoi(argv[2]) : 0;

	lv_init();
	lv_display_t *disp = lv_display_create(SCR_W, SCR_H);

	lv_display_set_flush_cb(disp, flush_cb);
	lv_display_set_buffers(disp, buf, NULL, sizeof(buf),
			       LV_DISPLAY_RENDER_MODE_PARTIAL);

	usage_view_init();
	settle();

	/* Clear the boot takeover so the gauges are what we photograph. */
	usage_view_set_status(USAGE_STATUS_OK);   /* data is sound, so the dot reports activity */

	switch (scene) {
	case 0:	/* the ordinary desk: one provider, one session */
		usage_view_set_provider1("claude");
		usage_view_update(27.0, 13231, 42.0, 598831);
		usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
		usage_view_set_sessions(1, 0);
		usage_view_set_clock(14, 05);
		break;
	case 1:	/* the busy one: several sessions, agents, context filling */
		usage_view_update(78.0, 1800, 91.0, 90000);
		usage_view_set_activity(USAGE_ACTIVITY_WAITING);
		usage_view_set_sessions(3, 7);
		usage_view_set_provider1("claude");
		usage_view_set_provider2("codex", 34.0, 61.0, 4320, 259200, false);
		usage_view_set_clock(23, 47);
		break;
	case 2:	/* the worst case for the layout: everything at once, wide.
		 * NOT countdown 0 -- usage_view treats exactly 0 as "this
		 * window just rolled over" and zeroes the percentage with it,
		 * which is correct behaviour and made the first version of
		 * this scene render two empty dials. */
		usage_view_update(100.0, 359999, 100.0, 604799);
		usage_view_set_activity(USAGE_ACTIVITY_FAILED);
		usage_view_set_sessions(9, 9);
		usage_view_set_provider1("claude");
		usage_view_set_provider2("codex", 100.0, 100.0, 60, 3600, false);
		usage_view_set_clock(23, 59);
		break;
	case 3:	/* codex ALONE -- the outer ring is whoever the daemon made
		 * primary, so this one must not be wearing Claude's colour */
		usage_view_set_provider1("codex");
		usage_view_update(52.0, 5400, 18.0, 400000);
		usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
		usage_view_set_sessions(1, 0);
		usage_view_set_clock(9, 12);
		break;
	case 5:	/* Claude Desktop with no Claude Code.
		 *
		 * The configuration this whole feature exists for: percentages
		 * are live, and NO source on the machine has a reset time, so
		 * the countdown slot would otherwise read "--" on both dials.
		 * The session gauge shows the rate instead; the weekly one
		 * keeps its "--", because a seven-day slope over half an hour
		 * is noise and the daemon never sends one. */
		usage_view_set_provider1("claude");
		usage_view_update(42.0, -1, 17.0, -1);
		usage_view_set_burn(14.2);
		usage_view_set_activity(USAGE_ACTIVITY_NONE);
		usage_view_set_sessions(0, 0);
		usage_view_set_clock(10, 30);
		break;
	case 6:	/* ONE session running. The floor of the pip row: a single
		 * mark has to read as a mark and not as dirt on the glass. */
		usage_view_set_provider1("claude");
		usage_view_update(27.0, 13231, 42.0, 598831);
		usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
		usage_view_set_sessions(1, 0);
		usage_view_set_counts(1, 1, 0, 0);
		usage_view_set_clock(14, 05);
		break;
	case 7:	/* SIX sessions, a mixed row. This is the scene the whole
		 * design turns on: six is the last count drawn one-per-session,
		 * and the question no arithmetic can answer is whether six pips
		 * read as SIX or as a smear. 1 failed, 1 waiting, 3 running,
		 * 1 finished -- so it also shows all three colours at once. */
		usage_view_set_provider1("claude");
		usage_view_update(63.0, 4200, 51.0, 300000);
		usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
		usage_view_set_sessions(6, 2);
		usage_view_set_counts(6, 3, 1, 1);
		usage_view_set_clock(14, 05);
		break;
	case 8:	/* SEVEN -- one more than six, and the row must change mode
		 * rather than grow. 4 running, 2 waiting, 1 finished: no
		 * failures, so a state is skipped and three groups still fit. */
		usage_view_set_provider1("claude");
		usage_view_update(63.0, 4200, 51.0, 300000);
		usage_view_set_activity(USAGE_ACTIVITY_WAITING);
		usage_view_set_sessions(7, 2);
		usage_view_set_counts(7, 4, 2, 0);
		usage_view_set_clock(14, 05);
		break;
	case 9:	/* EIGHT, all four states live. The overflow rule fires here:
		 * only three groups fit, so FINISHED is dropped and the row
		 * shows the two states that actually need a person plus what
		 * is working. */
		usage_view_set_provider1("claude");
		usage_view_update(63.0, 4200, 51.0, 300000);
		usage_view_set_activity(USAGE_ACTIVITY_FAILED);
		usage_view_set_sessions(8, 2);
		usage_view_set_counts(8, 4, 1, 1);
		usage_view_set_clock(14, 05);
		break;
	case 10: /* SIXTEEN with a two-digit tally -- the case the measured
		  * width path exists for, and the one where a numeral could
		  * reach the wordmark. Worst clock too: 20:48 is four of the
		  * widest digits, so the row is squeezed from both sides. */
		usage_view_set_provider1("claude");
		usage_view_update(88.0, 900, 74.0, 120000);
		usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
		usage_view_set_sessions(16, 4);
		usage_view_set_counts(16, 12, 1, 1);
		usage_view_set_clock(20, 48);
		break;
	case 11: /* The daemon's clamp is 0..9999 and the firmware trusts its
		  * own destination rather than that number, so this is what
		  * four digits per group actually looks like. Nothing here is
		  * reachable on a desk; it is here because the wall guard is
		  * the only thing standing between it and the wordmark. */
		usage_view_set_provider1("claude");
		usage_view_update(88.0, 900, 74.0, 120000);
		usage_view_set_activity(USAGE_ACTIVITY_FAILED);
		usage_view_set_sessions(9999, 0);
		usage_view_set_counts(29997, 9999, 9999, 9999);
		usage_view_set_clock(20, 48);
		break;
	case 4:	/* nothing known: every optional field absent */
		usage_view_set_provider1("");
		usage_view_update(-1.0, -1, -1.0, -1);
		usage_view_set_activity(USAGE_ACTIVITY_NONE);
		usage_view_set_sessions(1, 0);
		break;
	}
	settle();
	write_ppm(out);
	printf("wrote %s (scene %d)\n", out, scene);
	return 0;
}
