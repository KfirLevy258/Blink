/*
 * Sleep mode (docs/sleep-mode-design.md).
 *
 * Three clips: closing once, the loop for as long as the host is silent,
 * opening once. The clips are drawn straight to the panel by the BAN1 player,
 * over a plain LVGL screen in the clip's own ground colour; that screen is
 * also what takes the tap. Every frame gap services the protocol, so the
 * first word from a waking app is heard within a frame.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>

#include "bootclip.h"
#include "proto.h"
#include "ui_boot.h"
#include "ui_settings.h"
#include "ui_sleep.h"
#include "usage_view.h"

#define PEEK_MS 10000

static volatile bool tapped;

static void tap_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	tapped = true;
}

static bool host_back(void)
{
	return proto_host_seen();
}

static bool host_back_or_tap(void)
{
	return proto_host_seen() || tapped;
}

static void service(void)
{
	proto_service();
	lv_timer_handler();
	k_sleep(K_MSEC(5));
}

/* A tap: the dashboard as it was, with a word about why nothing moves. Ten
 * seconds, or until the host speaks, then back to dozing. */
static void peek(lv_obj_t *prev, lv_obj_t *sleep_scr)
{
	int64_t until = k_uptime_get() + PEEK_MS;

	lv_scr_load(prev);
	ui_settings_notice("Your computer may be asleep.");
	lv_refr_now(NULL);
	while (k_uptime_get() < until && !proto_host_seen()) {
		service();
	}
	ui_settings_notice_dismiss();
	if (!proto_host_seen()) {
		lv_scr_load(sleep_scr);
		lv_refr_now(NULL);
	}
}

void ui_sleep_run(void)
{
	const struct bootclip *close = sleepclip_active(SLEEP_CLOSE);
	const struct bootclip *loop = sleepclip_active(SLEEP_LOOP);
	const struct bootclip *open = sleepclip_active(SLEEP_OPEN);
	lv_obj_t *prev = lv_scr_act();
	lv_obj_t *scr = lv_obj_create(NULL);

	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_add_flag(scr, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_set_style_bg_color(scr, lv_color_hex(close->bg_rgb), 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_obj_add_event_cb(scr, tap_cb, LV_EVENT_CLICKED, NULL);
	lv_scr_load(scr);
	lv_refr_now(NULL);
	printk("[sleep] host silent; closing eyes (%s)\n", close->name);

	ui_boot_play_clip(close->blob, close->blob_len, host_back);
	while (!proto_host_seen()) {
		tapped = false;
		if (!ui_boot_play_clip(loop->blob, loop->blob_len,
				       host_back_or_tap)) {
			/* A loop that will not decode must not spin. */
			int64_t until = k_uptime_get() + 1000;

			while (k_uptime_get() < until) {
				service();
			}
		}
		if (tapped && !proto_host_seen()) {
			peek(prev, scr);
		}
	}
	printk("[sleep] host back; opening eyes\n");
	ui_boot_play_clip(open->blob, open->blob_len, NULL);

	/* Back to the dashboard as it was, figures flagged old until the app
	 * sends fresh ones (its first poll after waking, within a minute). */
	lv_scr_load(prev);
	lv_obj_del(scr);
	usage_view_set_status(USAGE_STATUS_STALE);
	lv_refr_now(NULL);
}
