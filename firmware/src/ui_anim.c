/*
 * The boot clip on demand: swipe right on the gauges and the eyes play on
 * loop until a swipe puts the gauges back.
 *
 * Rendering reuses the splash trick (see ui_boot.c): the loaded LVGL screen
 * is a bare clay rectangle painted exactly once, and the clip's delta-RLE
 * frames stream straight to the panel's GRAM between lv_timer_handler()
 * calls -- which keep running throughout, so touch input (the exit gesture)
 * stays alive while LVGL never repaints over the streamed frames.
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/display.h>
#include <lvgl.h>

#include "ui_anim.h"
#include "usage_view.h"
#include "bootanim.h"
#include "bootanim_dec.h"

#define STRIP_BYTES 4096

static const struct device *const disp =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_display));

static volatile bool pending;
static volatile bool leave;

void ui_anim_request(void)
{
	pending = true;
}

bool ui_anim_pending(void)
{
	return pending;
}

static void gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	lv_dir_t d = lv_indev_get_gesture_dir(lv_indev_active());

	/* Any horizontal swipe leaves -- the user is "sliding back", and
	 * being fussy about the direction of an exit gesture helps nobody. */
	if (d == LV_DIR_LEFT || d == LV_DIR_RIGHT) {
		leave = true;
	}
}

static void blit_cb(uint16_t x, uint16_t y, uint16_t w, uint16_t h,
		    const uint8_t *pix, void *user)
{
	struct display_buffer_descriptor desc = {
		.buf_size = (size_t)w * h * 2,
		.width = w,
		.height = h,
		.pitch = w,
	};

	ARG_UNUSED(user);
	display_write(disp, x, y, &desc, pix);
}

/* Let a screen-load slide finish: LVGL animates it from the timer handler,
 * and streaming (or deleting the old screen) mid-slide would tear it. */
static void pump_ms(int ms)
{
	int64_t end = k_uptime_get() + ms;

	while (k_uptime_get() < end) {
		lv_timer_handler();
		k_sleep(K_MSEC(5));
	}
}

/* Pump input, protocol, and the 1 s countdown bookkeeping until `until`, so
 * the gauges return current instead of frozen-for-the-show. */
static void idle_until(int64_t until, void (*pump)(void), int64_t *last_tick)
{
	do {
		if (pump) {
			pump();
		}
		lv_timer_handler();

		int64_t now = k_uptime_get();

		if (now - *last_tick >= 1000) {
			usage_view_tick_1s();
			*last_tick = now;
		}
		if (leave) {
			return;
		}
		k_sleep(K_MSEC(5));
	} while (k_uptime_get() < until);
}

void ui_anim_run(void (*pump)(void))
{
	pending = false;
	leave = false;

	lv_obj_t *prev = lv_scr_act();
	lv_obj_t *scr = lv_obj_create(NULL);

	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_set_style_bg_color(scr, lv_color_hex(BOOTANIM_BG_RGB), 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_obj_add_event_cb(scr, gesture_cb, LV_EVENT_GESTURE, NULL);

	/* Slide in from the left, the way the swipe pointed (user request
	 * 2026-07-17). The clip starts only after the slide settles -- the
	 * streamed frames bypass LVGL and would tear a moving screen. */
	lv_scr_load_anim(scr, LV_SCR_LOAD_ANIM_MOVE_RIGHT, 250, 0, false);
	pump_ms(300);
	lv_refr_now(NULL);	/* painted once; frames stream over it */

	/* Borrowed from the LVGL pool for the show only, same as the splash
	 * (a permanent buffer starved the WiFi driver -- see ui_boot.c). */
	uint8_t *strip = lv_malloc(STRIP_BYTES);
	int64_t last_tick = k_uptime_get();
	bool playable = strip != NULL;

	while (!leave) {
		struct ba_header hdr;
		size_t off;

		if (!playable ||
		    !ba_parse_header(bootanim_blob, sizeof(bootanim_blob),
				     &hdr, &off)) {
			/* No RAM or corrupt blob: hold the flat clay; the
			 * swipe out still works. */
			idle_until(k_uptime_get() + 100, pump, &last_tick);
			continue;
		}

		int64_t next = k_uptime_get();

		for (int i = 0; i < hdr.nframes && !leave; i++) {
			if (ba_decode_frame(bootanim_blob,
					    sizeof(bootanim_blob), &off,
					    strip, STRIP_BYTES,
					    blit_cb, NULL) < 0) {
				playable = false;
				break;
			}
			next += 1000 / hdr.fps;
			idle_until(next, pump, &last_tick);
		}
		/* Hold the final frame a beat before looping. */
		idle_until(k_uptime_get() + 600, pump, &last_tick);
	}

	if (strip) {
		lv_free(strip);
	}
	/* Slide the gauges back over the show; delete the clip screen only
	 * once the transition is done rendering it. */
	lv_scr_load_anim(prev, LV_SCR_LOAD_ANIM_MOVE_LEFT, 250, 0, false);
	pump_ms(300);
	lv_obj_del(scr);
}
