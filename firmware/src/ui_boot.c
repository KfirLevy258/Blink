/*
 * Boot UX: splash, then an explicit UART/WiFi choice.
 *
 * The old boot decided silently (8 s UART sniff); this makes the decision
 * visible and overridable while keeping every unattended path automatic:
 * a talking daemon short-circuits to USB, and a stored mode wins after the
 * countdown.
 */
#include <zephyr/kernel.h>
#include <lvgl.h>

#include "ui_boot.h"
#include "proto.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_GREEN	lv_color_hex(0x2ECC71)

static lv_obj_t *scr;
static volatile int choice;	/* 0 = undecided, else an enum cfg_mode */

static void btn_cb(lv_event_t *e)
{
	choice = (int)(intptr_t)lv_event_get_user_data(e);
}

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

	pump(2000);
}

enum cfg_mode ui_boot_select(enum cfg_mode fallback, int timeout_s)
{
	choice = 0;
	lv_obj_clean(scr);	/* reuse the splash's screen object */

	lv_obj_t *title = lv_label_create(scr);

	lv_label_set_text(title, "SELECT MODE");
	lv_obj_set_style_text_color(title, COL_DIM, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 14);

	static const struct {
		const char *txt;
		enum cfg_mode mode;
		lv_coord_t x;
	} btns[] = {
		{ "USB / UART", CFG_MODE_USB, -78 },
		{ "WiFi", CFG_MODE_WIFI, 78 },
	};

	for (int i = 0; i < 2; i++) {
		lv_obj_t *b = lv_btn_create(scr);

		lv_obj_set_size(b, 132, 96);
		lv_obj_align(b, LV_ALIGN_TOP_MID, btns[i].x, 62);
		lv_obj_set_style_bg_color(b, COL_TRACK, 0);
		lv_obj_add_event_cb(b, btn_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)btns[i].mode);

		lv_obj_t *l = lv_label_create(b);

		lv_label_set_text(l, btns[i].txt);
		lv_obj_set_style_text_color(l, COL_TEXT, 0);
		lv_obj_center(l);
	}

	lv_obj_t *cd = lv_label_create(scr);

	lv_label_set_text(cd, "");
	lv_obj_set_style_text_color(cd, COL_DIM, 0);
	lv_obj_align(cd, LV_ALIGN_BOTTOM_MID, 0, -12);

	bool have_fallback = (fallback != CFG_MODE_UNSET);
	int64_t deadline = k_uptime_get() + (int64_t)timeout_s * 1000;
	int last_shown = -1;

	for (;;) {
		proto_service();
		if (proto_host_seen()) {
			/* A daemon is talking: plugged-into-PC stays
			 * zero-touch. */
			return CFG_MODE_USB;
		}
		if (choice) {
			return (enum cfg_mode)choice;
		}
		if (have_fallback) {
			int left = (int)((deadline - k_uptime_get() + 999) / 1000);

			if (left <= 0) {
				return fallback;
			}
			if (left != last_shown) {
				lv_label_set_text_fmt(cd,
					"Continuing with %s in %d s",
					fallback == CFG_MODE_USB ? "USB" : "WiFi",
					left);
				last_shown = left;
			}
		}
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

void ui_boot_teardown(void)
{
	if (scr) {
		lv_obj_del(scr);
		scr = NULL;
	}
}
