/*
 * Boot splash. Doubles as the mode-detection window: proto_service() runs
 * under the animation, so a PC daemon's reply to our boot-time hello is
 * already in by the time main() decides USB vs WiFi. No selection screen --
 * v1 had one, and on hardware it was pure friction (the answer is always
 * detectable: a daemon talks, or it doesn't).
 */
#include <zephyr/kernel.h>
#include <lvgl.h>

#include "ui_boot.h"
#include "proto.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_GREEN	lv_color_hex(0x2ECC71)

static lv_obj_t *scr;

/* Pump UI + protocol for `ms`, so the splash doubles as the daemon-detect
 * window. */
static void pump(int ms)
{
	int64_t end = k_uptime_get() + ms;

	while (k_uptime_get() < end) {
		proto_service();
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

static void anim_opa_cb(void *obj, int32_t v)
{
	lv_obj_set_style_opa(obj, (lv_opa_t)v, 0);
}

void ui_boot_splash(void)
{
	scr = lv_obj_create(NULL);
	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_set_style_bg_color(scr, COL_BG, 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_scr_load(scr);

	lv_obj_t *title = lv_label_create(scr);

	lv_label_set_text(title, "CLAUDE CODE");
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_set_style_text_font(title, &lv_font_montserrat_20, 0);
	lv_obj_align(title, LV_ALIGN_CENTER, 0, -14);

	lv_obj_t *spin = lv_spinner_create(scr);

	lv_obj_set_size(spin, 36, 36);
	lv_obj_align(spin, LV_ALIGN_CENTER, 0, 34);
	lv_obj_set_style_arc_color(spin, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(spin, COL_GREEN, LV_PART_INDICATOR);

	lv_anim_t a;

	lv_anim_init(&a);
	lv_anim_set_var(&a, title);
	lv_anim_set_values(&a, LV_OPA_TRANSP, LV_OPA_COVER);
	lv_anim_set_duration(&a, 800);
	lv_anim_set_exec_cb(&a, anim_opa_cb);
	lv_anim_start(&a);

	pump(2500);
}

void ui_boot_teardown(void)
{
	if (scr) {
		lv_obj_del(scr);
		scr = NULL;
	}
}
