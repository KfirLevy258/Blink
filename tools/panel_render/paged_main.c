/*
 * The existing gauge screen, one provider per page, stacked vertically.
 *
 * Nothing here draws a new widget: it compiles firmware/src/usage_view.c
 * unchanged and drives the same public calls proto.c does. The only addition
 * is the page rail, which is what the firmware would add on top.
 *
 * The point of the split is not a new look -- it is that the merged panel had
 * to fit four numbers (two providers x two windows) on 320x240, and that
 * budget is what produced the doubled arcs, the small grey second percentage
 * and the two-line countdowns. One provider per page removes all of them at
 * once and leaves the screen that was already there.
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

	fprintf(f, "P6\n%d %d\n255\n", SCR_W, SCR_H);
	for (int i = 0; i < SCR_W * SCR_H; i++) {
		uint16_t c = fb[i];
		uint8_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;
		uint8_t o[3] = { (uint8_t)((r << 3) | (r >> 2)),
				 (uint8_t)((g << 2) | (g >> 4)),
				 (uint8_t)((b << 3) | (b >> 2)) };
		fwrite(o, 1, 3, f);
	}
	fclose(f);
}

static void settle(void)
{
	for (int i = 0; i < 40; i++) {
		lv_tick_inc(16);
		lv_timer_handler();
	}
}

int main(int argc, char **argv)
{
	static uint8_t buf[SCR_W * 40 * 2];
	const char *dir = argc > 1 ? argv[1] : ".";
	/* One desk, one moment: claude is quiet, codex is nearly out. That is
	 * the case the merged panel handled worst and the rail handles best. */
	struct { const char *n; bool second; int page; } sc[] = {
		{ "solo", false, 0 },
		{ "p1",   true,  0 },
		{ "p2",   true,  1 },
	};

	lv_init();
	for (unsigned i = 0; i < sizeof(sc) / sizeof(sc[0]); i++) {
		lv_display_t *disp = lv_display_create(SCR_W, SCR_H);
		char path[256];

		lv_display_set_flush_cb(disp, flush_cb);
		lv_display_set_buffers(disp, buf, NULL, sizeof(buf),
				       LV_DISPLAY_RENDER_MODE_PARTIAL);
		usage_view_init();
		settle();
		usage_view_set_status(USAGE_STATUS_OK);
		usage_view_set_provider1("claude");
		usage_view_update(27.0, 13231, 42.0, 598831);
		if (sc[i].second) {
			usage_view_set_provider2("codex", 97.0, 94.0, 372, 34200, false);
		}
		usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
		usage_view_set_sessions(1, 0);
		usage_view_set_clock(14, 5);
		if (sc[i].page) {
			usage_view_page_step(1);
		}
		settle();

		snprintf(path, sizeof(path), "%s/pgv-%s.ppm", dir, sc[i].n);
		write_ppm(path);
		printf("wrote %s\n", path);
		usage_view_deinit();
	}
	return 0;
}
