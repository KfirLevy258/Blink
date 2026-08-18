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
#include "ui_slide.h"

#define STRIP_BYTES 4096

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

void ui_anim_gesture_mute(int ms)
{
	int64_t until = k_uptime_get() + ms;

	if (until > mute_until) {	/* never shorten a window already open */
		mute_until = until;
	}
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

/* Marks a sibling this file hid, so the restore pass can tell it from one that
 * was already hidden before the clip started. Free for application use; no
 * other module in the tree touches the USER flags. */
#define PEER_HID_BY_US	LV_OBJ_FLAG_USER_1

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
 * object with a hidden parent, so no descendant of one of these siblings can
 * invalidate no matter how deeply nested or what starts it -- a countdown
 * label, a spinner, a status change arriving over the wire. The countdown
 * keeps running and stays correct; it simply never reaches the display, so the
 * gauges are current the moment they are uncovered rather than frozen at the
 * time the clip started. (It reaches the overlay's SIBLINGS only. Anything
 * parented to lv_layer_top() is drawn unconditionally by every refresh and is
 * not covered here -- ui_slide_top_hide() is what handles that layer, and
 * ui_anim_run holds it hidden for the length of the show.)
 *
 * Restoring is not "clear HIDDEN on everything": two of the gauge screen's own
 * children are legitimately hidden in the steady state -- the long-press peek
 * card, and the full-screen CONNECTING overlay once data has arrived. Blanket
 * clearing brings both back, and neither reliably hides itself again: the peek
 * only decrements its TTL while already visible, so it would sit over the
 * gauges indefinitely. So the hide pass marks what it actually hid with
 * USER_1, and the restore pass unhides exactly that set.
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
			if (lv_obj_has_flag(c, LV_OBJ_FLAG_HIDDEN)) {
				continue;	/* already hidden; not ours */
			}
			lv_obj_add_flag(c, LV_OBJ_FLAG_HIDDEN | PEER_HID_BY_US);
		} else if (lv_obj_has_flag(c, PEER_HID_BY_US)) {
			lv_obj_clear_flag(c, LV_OBJ_FLAG_HIDDEN | PEER_HID_BY_US);
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

	/*
	 * Built frozen and already full size. Creating it would otherwise
	 * invalidate the screen, and refreshing that invalidation repaints over
	 * the gauge pixels the scroll is about to carry away.
	 */
	ui_slide_begin();

	lv_obj_t *ov = lv_obj_create(lv_scr_act());

	lv_obj_set_size(ov, LV_PCT(100), LV_PCT(100));
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
	lv_obj_set_pos(ov, 0, 0);
	lv_obj_update_layout(ov);
	peers_set_hidden(ov, true);
	ui_slide_freeze(false);

	/*
	 * In from the left, the way the swipe pointed (user request
	 * 2026-07-17) -- the gauges leave rightwards and the clay arrives
	 * behind them, the whole screen moving at once. Settings goes the other
	 * way, so the two easter eggs stay opposite.
	 *
	 * The mute is set wide before the run and tightened after, because
	 * ui_slide_run() blocks for the whole transition and nothing dispatches
	 * input while it does -- pump() does not run lv_timer_handler(). The
	 * tail of the opening swipe is therefore still sitting in the input
	 * msgq when the slide ends, and arrives in the burst just after it.
	 * Without the mute it reaches gesture_cb, which exits on either
	 * direction, and the show closes itself in the motion that started it.
	 */
	enter_mute_until = k_uptime_get() + UI_SLIDE_MS * 6;
	mute_until = enter_mute_until;
	ui_slide_run(UI_SLIDE_RIGHT, pump);
	enter_mute_until = k_uptime_get() + 250;

	/*
	 * That slide's settle restored lv_layer_top(); hide it again for the
	 * show. The clip streams frames straight to GRAM, and anything on that
	 * layer is drawn by every refresh no matter what area is being
	 * refreshed -- ui_touchfx's press echo lives there and its timer keeps
	 * running inside idle_until(), so each touch (the exit swipe included)
	 * blooms a circle over the streamed image and leaves a flat clay hole
	 * where the overlay repaints behind it. Restored by the exit slide.
	 */
	ui_slide_top_hide(true);

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
	 * Unhide the gauges and drop the overlay before the slide, so the
	 * strips it paints have the restored screen to draw. All of it frozen:
	 * either operation would invalidate the screen, and refreshing that
	 * would repaint over the clip pixels still in GRAM that the scroll is
	 * about to carry off.
	 *
	 * Mute the gauge screen's gestures across the slide: the tail of the
	 * exit swipe lands on the RESTORED screen, where a LEFT swipe replays
	 * into ui_settings' handler and opens the settings panel by itself, and
	 * a RIGHT one asks for the clip again. gesture_cb here exits on EITHER
	 * direction, so both halves of that were reachable; clearing `pending`
	 * afterwards only ever caught the right-swipe one.
	 */
	ui_slide_begin();
	peers_set_hidden(ov, false);
	/* The restore above replays what was visible when the clip STARTED. Let
	 * the view re-decide the one piece of that which can legitimately have
	 * changed since: the first data can arrive mid-clip, and putting the
	 * CONNECTING bar back over live gauges is the 2026-08-18 report. */
	usage_view_sync_takeover();
	lv_obj_del(ov);
	lv_obj_update_layout(lv_screen_active());
	ui_slide_freeze(false);

	mute_until = k_uptime_get() + UI_SLIDE_MS * 6;
	ui_slide_run(UI_SLIDE_LEFT, pump);

	/* Short tail past the drain, then hand gestures back -- long enough to
	 * cover the replay, short enough that a deliberate second swipe works. */
	mute_until = k_uptime_get() + 250;

	/* Drop any request the exit swipe itself raised. A right swipe on the
	 * gauge screen is exactly what ASKS for the clip, so the tail of the
	 * gesture -- or a release landing on the left edge zone -- sets pending
	 * again through ui_settings' handlers. The exit slide above blocks long
	 * enough for those events to land, so clearing here is the last word. */
	pending = false;
}
