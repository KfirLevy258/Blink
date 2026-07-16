/*
 * Settings panel: the recovery actions that used to require reflashing.
 *
 * Each action clears one persistence tier and reboots -- the boot path then
 * lands in the right flow by itself (no creds -> provisioning, no token ->
 * sign-in, nothing -> mode selection). Deliberately no in-place teardown of
 * the running mode: a cold reboot is simpler and reclaims all memory.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/printk.h>
#include <lvgl.h>

#include "ui_settings.h"
#include "cfg_store.h"
#include "ui_boot.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_RED		lv_color_hex(0xE74C3C)
#define COL_GREEN	lv_color_hex(0x2ECC71)

enum action {
	ACT_WIFI,	/* forget network, keep token */
	ACT_SIGNIN,	/* forget token, keep network */
	ACT_FACTORY,	/* forget everything */
};

static lv_obj_t *panel;		/* NULL when closed */
static lv_obj_t *confirm;	/* NULL when no dialog is up */
static enum action pending;

static const char *const act_label[] = {
	[ACT_WIFI] = "Reset WiFi",
	[ACT_SIGNIN] = "Re-sign-in",
	[ACT_FACTORY] = "Factory reset",
};

static void do_pending(void)
{
	printk("[settings] %s -> reboot\n", act_label[pending]);
	switch (pending) {
	case ACT_WIFI:
		cfg_clear_wifi();
		break;
	case ACT_SIGNIN:
		cfg_clear_token();
		break;
	case ACT_FACTORY:
		cfg_reset();
		break;
	}
	k_msleep(200);	/* let the NVS writes land */
	ui_boot_mark_intentional_reboot();
	sys_reboot(SYS_REBOOT_COLD);
}

static void confirm_yes_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	do_pending();	/* never returns */
}

static void confirm_no_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	lv_obj_del(confirm);
	confirm = NULL;
}

static lv_obj_t *mk_btn(lv_obj_t *parent, const char *txt, lv_color_t bg,
			lv_event_cb_t cb, void *user)
{
	lv_obj_t *b = lv_btn_create(parent);

	lv_obj_set_size(b, 200, 40);
	lv_obj_set_style_bg_color(b, bg, 0);
	/* A close-swipe starting ON a button must still reach the panel's
	 * gesture handler instead of dying in the button. */
	lv_obj_add_flag(b, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);

	lv_obj_t *l = lv_label_create(b);

	lv_label_set_text(l, txt);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_center(l);
	return b;
}

static void show_confirm(void)
{
	confirm = lv_obj_create(panel);
	lv_obj_set_size(confirm, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(confirm, COL_BG, 0);
	lv_obj_set_style_bg_opa(confirm, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(confirm, 0, 0);
	lv_obj_clear_flag(confirm, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(confirm);

	lv_obj_t *q = lv_label_create(confirm);

	lv_label_set_text_fmt(q, "%s?\nThe device will reboot.",
			      act_label[pending]);
	lv_obj_set_style_text_color(q, COL_TEXT, 0);
	lv_obj_set_style_text_align(q, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(q, LV_ALIGN_TOP_MID, 0, 40);

	/* Red is reserved for the one action that costs a full re-setup;
	 * painting every confirm red made them all look equally scary. */
	lv_obj_t *yes = mk_btn(confirm, "Yes, do it",
			       pending == ACT_FACTORY ? COL_RED : COL_GREEN,
			       confirm_yes_cb, NULL);

	lv_obj_align(yes, LV_ALIGN_BOTTOM_MID, 0, -70);

	lv_obj_t *no = mk_btn(confirm, "Cancel", COL_TRACK,
			      confirm_no_cb, NULL);

	lv_obj_align(no, LV_ALIGN_BOTTOM_MID, 0, -18);
}

static void act_cb(lv_event_t *e)
{
	pending = (enum action)(intptr_t)lv_event_get_user_data(e);
	show_confirm();
}

static void close_panel(void)
{
	lv_obj_del(panel);	/* deletes confirm with it, if open */
	panel = NULL;
	confirm = NULL;
}

static void back_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	close_panel();
}

static void panel_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (confirm == NULL &&
	    lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		close_panel();
	}
}

static void open_panel(lv_obj_t *parent_scr)
{
	panel = lv_obj_create(parent_scr);
	lv_obj_set_size(panel, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(panel, COL_BG, 0);
	lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(panel, 0, 0);
	lv_obj_set_style_radius(panel, 0, 0);
	lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_add_flag(panel, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(panel, panel_gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_center(panel);

	lv_obj_t *title = lv_label_create(panel);

	lv_label_set_text(title, "SETTINGS");
	lv_obj_set_style_text_color(title, COL_DIM, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 8);

	lv_obj_t *back = mk_btn(panel, LV_SYMBOL_LEFT " Back", COL_TRACK,
				back_cb, NULL);

	lv_obj_set_size(back, 84, 32);
	lv_obj_align(back, LV_ALIGN_TOP_LEFT, 6, 4);

	static const enum action acts[] = { ACT_WIFI, ACT_SIGNIN, ACT_FACTORY };

	for (int i = 0; i < 3; i++) {
		/* Factory reset wears red: it is the only one that costs a
		 * full re-setup. */
		lv_obj_t *b = mk_btn(panel, act_label[acts[i]],
				     acts[i] == ACT_FACTORY ? COL_RED : COL_TRACK,
				     act_cb, (void *)(intptr_t)acts[i]);

		lv_obj_align(b, LV_ALIGN_TOP_MID, 0, 52 + i * 52);
	}
}

static void scr_gesture_cb(lv_event_t *e)
{
	if (panel == NULL &&
	    lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_LEFT) {
		open_panel(lv_event_get_current_target(e));
	}
}

void ui_settings_attach(lv_obj_t *scr)
{
	lv_obj_add_event_cb(scr, scr_gesture_cb, LV_EVENT_GESTURE, NULL);
}
