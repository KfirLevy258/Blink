/*
 * The boot clip on demand: swipe right on the gauges and the eyes play on
 * loop until a swipe puts the gauges back.
 *
 * Rendering reuses the splash trick (see ui_boot.c): a bare clay rectangle is
 * painted exactly once, and the clip's delta-RLE frames stream straight to the
 * panel's GRAM between lv_timer_handler() calls -- which keep running
 * throughout, so touch input (the exit gesture) stays alive while LVGL never
 * repaints over the streamed frames.
 *
 * That rectangle is an OVERLAY on the live screen, not a loaded screen of its
 * own. It used to be the latter, and lv_scr_load_anim() moves both screens at
 * once: 76800 px dirty per frame at ~124 ms a redraw, which fit two to four
 * frames in the whole transition. As an overlay it can travel at the size of a
 * bar and only expand once parked -- the same trick, and the same reasoning,
 * as the settings panel in ui_settings.c.
 *
 * Two things the screen-load path gave away for free have to be done by hand
 * here; both are load-bearing and both are commented where they happen:
 * suppressing what is underneath (peers_set_hidden) and muting the gesture
 * that opened the clip (enter_mute_until).
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/display.h>
#include <lvgl.h>

#include "ui_anim.h"
#include "usage_view.h"
#include "bootanim.h"
#include "bootanim_dec.h"

#define STRIP_BYTES 4096

/* The height the overlay travels at. Matches ui_settings.c's PANEL_HDR_H for
 * the same reason -- 320x34 is ~14 ms a frame against a full screen's ~124 --
 * but is independent of it: nothing about the clip has to agree with the
 * settings header, and the clay bar has no content that fixes its height. */
#define CLIP_BAR_H 34

static const struct device *const disp =
	DEVICE_DT_GET(DT_CHOSEN(zephyr_display));

static volatile bool pending;
static int64_t mute_until;
static int64_t enter_mute_until;
static volatile bool leave;

void ui_anim_request(void)
{
	pending = true;
}

bool ui_anim_pending(void)
{
	return pending;
}

bool ui_anim_gesture_muted(void)
{
	return k_uptime_get() < mute_until;
}

static void gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	lv_dir_t d = lv_indev_get_gesture_dir(lv_indev_active());

	/*
	 * Ignore everything until the entry slide has landed and the swipe that
	 * caused it has drained.
	 *
	 * The screen-load path did not need this: LVGL blocks ALL input while
	 * disp->prev_scr is set (lv_indev.c), which held the opening swipe back
	 * until the transition finished and it could be dropped. An overlay
	 * never sets prev_scr, so nothing is blocked and the tail of the very
	 * gesture that ASKED for the clip lands right here -- on a handler that
	 * exits on either direction. Without this the show closes itself in the
	 * same motion that opened it.
	 */
	if (k_uptime_get() < enter_mute_until) {
		return;
	}

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

static void slide_to(lv_obj_t *obj, int32_t from, int32_t to)
{
	lv_anim_t a;

	lv_anim_init(&a);
	lv_anim_set_var(&a, obj);
	lv_anim_set_exec_cb(&a, (lv_anim_exec_xcb_t)lv_obj_set_x);
	lv_anim_set_values(&a, from, to);
	lv_anim_set_duration(&a, UI_SLIDE_MS);
	lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
	lv_anim_start(&a);
}

/*
 * Wait for the overlay slide to finish.
 *
 * This replaced a two-stage wait on disp->prev_scr. That flag was the only
 * honest signal a screen-load transition gave -- it needed two loops because
 * lv_screen_load_anim() clears it before returning and the anim's start_cb
 * only raises it on a LATER handler pass, so watching for NULL without first
 * watching for non-NULL silently degraded to no wait at all. An overlay never
 * sets prev_scr, so none of that applies and the geometry is the whole truth.
 *
 * lv_obj_update_layout() first because coords are stale until a layout pass
 * runs, and lv_obj_get_x() reads coords: without it a freshly positioned
 * object reads x=0 and a wait for x==0 returns before the slide has begun.
 *
 * The deadline is a hang guard, not a schedule. `pump` is the caller's
 * background duty -- protocol service and, on a test boot, the only watchdog
 * feeder -- so it has to keep running for however long this waits.
 */
static void settle_slide(lv_obj_t *ov, int32_t target, void (*pump)(void))
{
	int64_t end_by = k_uptime_get() + UI_SLIDE_MS * 4;

	lv_obj_update_layout(ov);
	while (lv_obj_get_x(ov) != target && k_uptime_get() < end_by) {
		if (pump) {
			pump();
		}
		lv_timer_handler();
		k_sleep(K_MSEC(5));
	}
}

/*
 * Blank (or restore) everything sharing the overlay's parent.
 *
 * The clip streams straight to GRAM, so ANY repaint of what lies underneath
 * punches a clay-coloured hole through it. As a separate screen that could not
 * happen -- the gauge screen was not the active one and its invalidations went
 * nowhere. As an overlay it is a live hazard, and there is a guaranteed
 * source: idle_until() ticks the 1 s countdown for the whole show.
 *
 * Hiding stops it at the source rather than chasing individual widgets:
 * lv_obj_area_is_visible() returns false for a hidden object AND for any
 * object with a hidden parent, so nothing beneath can invalidate no matter how
 * deeply nested or what starts it -- a countdown label, a spinner, a status
 * change arriving over the wire. The countdown keeps running and stays
 * correct; it simply never reaches the display, so the gauges are current the
 * moment they are uncovered rather than frozen at the time the clip started.
 */
static void peers_set_hidden(lv_obj_t *ov, bool hide)
{
	lv_obj_t *parent = lv_obj_get_parent(ov);
	uint32_t n = lv_obj_get_child_count(parent);

	for (uint32_t i = 0; i < n; i++) {
		lv_obj_t *c = lv_obj_get_child(parent, i);

		if (c == ov) {
			continue;
		}
		if (hide) {
			lv_obj_add_flag(c, LV_OBJ_FLAG_HIDDEN);
		} else {
			lv_obj_clear_flag(c, LV_OBJ_FLAG_HIDDEN);
		}
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

	lv_obj_t *ov = lv_obj_create(lv_scr_act());

	lv_obj_set_size(ov, LV_PCT(100), CLIP_BAR_H);
	lv_obj_set_style_bg_color(ov, lv_color_hex(BOOTANIM_BG_RGB), 0);
	lv_obj_set_style_bg_opa(ov, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(ov, 0, 0);
	lv_obj_set_style_radius(ov, 0, 0);
	lv_obj_set_style_pad_all(ov, 0, 0);
	lv_obj_clear_flag(ov, LV_OBJ_FLAG_SCROLLABLE);
	/* Gestures stop here instead of bubbling on to the gauge screen's own
	 * handler, which would open settings underneath the clip. */
	lv_obj_clear_flag(ov, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(ov, gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_set_pos(ov, -LV_HOR_RES, 0);	/* offstage, left */

	/* Slide in from the left, the way the swipe pointed (user request
	 * 2026-07-17) -- as a bar, not the whole screen.
	 *
	 * Mute BOTH handlers across it. gesture_cb because the opening swipe's
	 * tail would otherwise exit the clip in the same motion that started
	 * it, and the gauge screen's because until the bar has covered it a
	 * stray gesture there still opens settings behind the show. */
	enter_mute_until = k_uptime_get() + UI_SLIDE_MS + 250;
	mute_until = enter_mute_until;
	slide_to(ov, -LV_HOR_RES, 0);
	settle_slide(ov, 0, pump);

	/* Full size only once parked -- the streamed frames bypass LVGL and
	 * would tear a moving object. Then blank what is underneath, and only
	 * then paint the clay the frames stream over. */
	lv_obj_set_size(ov, LV_PCT(100), LV_PCT(100));
	peers_set_hidden(ov, true);
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
	/*
	 * Restore the gauges BEFORE the bar shrinks off them, so the strip it
	 * uncovers has something to draw. They are repainted here while the
	 * overlay still covers the screen, which costs one full redraw and
	 * shows nothing; the visible change is the shrink on the line after.
	 *
	 * Mute the gauge screen's gestures across the slide: the tail of the
	 * exit swipe lands on the RESTORED screen, where a LEFT swipe replays
	 * into ui_settings' handler and opens the settings panel by itself, and
	 * a RIGHT one asks for the clip again. gesture_cb here exits on EITHER
	 * direction, so both halves of that were reachable; clearing `pending`
	 * afterwards only ever caught the right-swipe one.
	 */
	peers_set_hidden(ov, false);
	mute_until = k_uptime_get() + UI_SLIDE_MS * 6;
	lv_obj_set_size(ov, LV_PCT(100), CLIP_BAR_H);
	slide_to(ov, 0, -LV_HOR_RES);
	settle_slide(ov, -LV_HOR_RES, pump);

	/* Kill the animation before the object it drives. settle_slide's
	 * deadline is a hang guard, so it can return with the slide still
	 * running, and the anim would then be stepping freed memory. */
	lv_anim_delete(ov, NULL);
	lv_obj_del(ov);

	/* Short tail past the drain, then hand gestures back -- long enough to
	 * cover the replay, short enough that a deliberate second swipe works. */
	mute_until = k_uptime_get() + 250;

	/* Drop any request the exit swipe itself raised. A right swipe on the
	 * gauge screen is exactly what ASKS for the clip, so the tail of the
	 * gesture -- or a release landing on the left edge zone -- sets pending
	 * again through ui_settings' handlers. settle_slide above has already
	 * let those events land, so clearing here is the last word. */
	pending = false;
}
