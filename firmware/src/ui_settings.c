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
#include <stdio.h>

#include "ui_settings.h"
#include "cfg_store.h"
#include "ui_boot.h"
#include "ui_anim.h"
#include "net_wifi.h"
#include "fmt.h"
#include "version.h"
#include "backlight.h"

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

static lv_obj_t *pct_lbl;	/* live "60%" readout */
static lv_obj_t *pips[5];	/* five level pips, filled up to the level */

static void bright_refresh(void)
{
	uint8_t p = backlight_get();
	char b[8];

	snprintf(b, sizeof(b), "%d%%", p);
	lv_label_set_text(pct_lbl, b);
	for (int i = 0; i < 5; i++) {
		lv_obj_set_style_bg_color(pips[i],
			(i + 1) * 20 <= p ? COL_GREEN : COL_TRACK, 0);
	}
}

/* user_data carries the step direction (+1 / -1). */
static void bright_step_cb(lv_event_t *e)
{
	backlight_step((int)(intptr_t)lv_event_get_user_data(e));
	bright_refresh();
}

static void show_confirm(void)
{
	confirm = lv_obj_create(panel);
	lv_obj_set_size(confirm, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(confirm, COL_BG, 0);
	lv_obj_set_style_bg_opa(confirm, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(confirm, 0, 0);
	lv_obj_set_style_pad_all(confirm, 0, 0);
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

/* The panel slides in from the right and back out again (user request
 * 2026-07-17) -- the motion says where settings lives and which way leads
 * home. 250 ms, ease-out. */
static bool closing;

static void slide_x(lv_obj_t *obj, int32_t from, int32_t to,
		    lv_anim_completed_cb_t done)
{
	lv_anim_t a;

	lv_anim_init(&a);
	lv_anim_set_var(&a, obj);
	lv_anim_set_exec_cb(&a, (lv_anim_exec_xcb_t)lv_obj_set_x);
	lv_anim_set_values(&a, from, to);
	lv_anim_set_duration(&a, 250);
	lv_anim_set_path_cb(&a, lv_anim_path_ease_out);
	if (done) {
		lv_anim_set_completed_cb(&a, done);
	}
	lv_anim_start(&a);
}

static void close_done(lv_anim_t *a)
{
	ARG_UNUSED(a);
	lv_obj_del(panel);	/* deletes confirm with it, if open */
	panel = NULL;
	confirm = NULL;
	closing = false;
}

static void close_panel(void)
{
	if (closing) {
		return;
	}
	closing = true;
	slide_x(panel, 0, LV_HOR_RES, close_done);
}

static void back_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (confirm == NULL) {
		close_panel();
	}
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
	/* Kill the theme's default padding: it silently shifted every child
	 * down, and the footer line landed on the last button (seen on
	 * hardware 2026-07-17). */
	lv_obj_set_style_pad_all(panel, 0, 0);
	lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_add_flag(panel, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(panel, panel_gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_set_pos(panel, LV_HOR_RES, 0);	/* offstage; slid in below */

	lv_obj_t *title = lv_label_create(panel);

	lv_label_set_text(title, "SETTINGS");
	lv_obj_set_style_text_color(title, COL_DIM, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 8);

	/* The back chevron: same visual language as the gauge edges, but a
	 * chevron reads as a button, so it IS one (tapped on hardware and
	 * found dead, 2026-07-17) -- tap or swipe right, both go home. */
	lv_obj_t *back = lv_btn_create(panel);

	lv_obj_set_size(back, 44, 88);
	lv_obj_set_style_bg_opa(back, LV_OPA_TRANSP, 0);
	lv_obj_set_style_shadow_width(back, 0, 0);
	lv_obj_align(back, LV_ALIGN_LEFT_MID, 0, 0);
	lv_obj_add_flag(back, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(back, back_cb, LV_EVENT_CLICKED, NULL);

	lv_obj_t *bl = lv_label_create(back);

	lv_label_set_text(bl, LV_SYMBOL_LEFT);
	lv_obj_set_style_text_color(bl, COL_DIM, 0);
	lv_obj_center(bl);

	/* --- Brightness (manual scale, tap-safe stepper) --- */
	lv_obj_t *blab = lv_label_create(panel);

	lv_label_set_text(blab, "BRIGHTNESS");
	lv_obj_set_style_text_color(blab, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(blab, 1, 0);
	lv_obj_align(blab, LV_ALIGN_TOP_MID, 0, 34);

	/* minus / plus: big, opposite corners -- the whole button is the target */
	lv_obj_t *minus = mk_btn(panel, LV_SYMBOL_MINUS, COL_TRACK,
				 bright_step_cb, (void *)(intptr_t)-1);
	lv_obj_set_size(minus, 58, 54);
	lv_obj_align(minus, LV_ALIGN_TOP_LEFT, 20, 56);

	lv_obj_t *plus = mk_btn(panel, LV_SYMBOL_PLUS, COL_TRACK,
				bright_step_cb, (void *)(intptr_t)1);
	lv_obj_set_size(plus, 58, 54);
	lv_obj_align(plus, LV_ALIGN_TOP_RIGHT, -20, 56);

	/* readout: percent + five pips, not a tap target */
	pct_lbl = lv_label_create(panel);
	lv_obj_set_style_text_color(pct_lbl, COL_TEXT, 0);
	lv_obj_align(pct_lbl, LV_ALIGN_TOP_MID, 0, 62);

	for (int i = 0; i < 5; i++) {
		pips[i] = lv_obj_create(panel);
		lv_obj_set_size(pips[i], 20, 6);
		lv_obj_set_style_radius(pips[i], 3, 0);
		lv_obj_set_style_border_width(pips[i], 0, 0);
		lv_obj_clear_flag(pips[i], LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_clear_flag(pips[i], LV_OBJ_FLAG_CLICKABLE);
		lv_obj_align(pips[i], LV_ALIGN_TOP_MID, -50 + i * 25, 92);
	}
	bright_refresh();

	/* --- Reset actions, as icon tiles --- */
	lv_obj_t *rlab = lv_label_create(panel);

	lv_label_set_text(rlab, "RESET");
	lv_obj_set_style_text_color(rlab, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(rlab, 1, 0);
	lv_obj_align(rlab, LV_ALIGN_TOP_MID, 0, 118);

	static const enum action acts[] = { ACT_WIFI, ACT_SIGNIN, ACT_FACTORY };
	static const char *const acticon[] = {
		[ACT_WIFI] = LV_SYMBOL_WIFI,
		[ACT_SIGNIN] = LV_SYMBOL_REFRESH,
		[ACT_FACTORY] = LV_SYMBOL_TRASH,
	};
	static const char *const acttext[] = {
		[ACT_WIFI] = "Wi-Fi",
		[ACT_SIGNIN] = "Sign-in",
		[ACT_FACTORY] = "Factory",
	};
	static const lv_align_t tilepos[] = {
		LV_ALIGN_TOP_LEFT, LV_ALIGN_TOP_MID, LV_ALIGN_TOP_RIGHT,
	};
	static const int tiledx[] = { 20, 0, -20 };

	for (int i = 0; i < 3; i++) {
		enum action a = acts[i];
		lv_obj_t *tile = lv_btn_create(panel);

		lv_obj_set_size(tile, 86, 72);
		lv_obj_set_style_bg_color(tile,
			a == ACT_FACTORY ? COL_RED : COL_TRACK, 0);
		lv_obj_set_style_shadow_width(tile, 0, 0);
		lv_obj_add_flag(tile, LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_align(tile, tilepos[i], tiledx[i], 140);
		lv_obj_add_event_cb(tile, act_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)a);

		lv_obj_t *ic = lv_label_create(tile);

		lv_label_set_text(ic, acticon[a]);
		lv_obj_set_style_text_color(ic, COL_TEXT, 0);
		lv_obj_align(ic, LV_ALIGN_TOP_MID, 0, 10);

		lv_obj_t *tx = lv_label_create(tile);

		lv_label_set_text(tx, acttext[a]);
		lv_obj_set_style_text_color(tx, COL_TEXT, 0);
		lv_obj_align(tx, LV_ALIGN_BOTTOM_MID, 0, -10);
	}

	/* Debug-me line: the first three questions a misbehaving device gets
	 * asked -- what build, which network, what address -- answered
	 * without a serial cable (which would reset the board). */
	char line[72];
	char ip[16], ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];

	if (net_wifi_sta_ip(ip, sizeof(ip)) &&
	    cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk))) {
		char ssid_a[CFG_SSID_MAX];

		fmt_ascii(ssid, ssid_a, sizeof(ssid_a));
		snprintf(line, sizeof(line), "Clauge %s  |  %s  |  %s",
			 CLAUGE_FW_VERSION, ssid_a, ip);
	} else {
		snprintf(line, sizeof(line), "Clauge %s", CLAUGE_FW_VERSION);
	}

	lv_obj_t *info = lv_label_create(panel);

	lv_label_set_text(info, line);
	lv_obj_set_style_text_color(info, COL_DIM, 0);
	/* One line, dotted if a long SSID would push it wider than the
	 * screen -- an auto-sized label neither wraps nor clips. */
	lv_obj_set_size(info, 308, 17);
	lv_label_set_long_mode(info, LV_LABEL_LONG_DOT);
	lv_obj_set_style_text_align(info, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(info, LV_ALIGN_BOTTOM_MID, 0, -6);

	slide_x(panel, LV_HOR_RES, 0, NULL);
}

static void scr_gesture_cb(lv_event_t *e)
{
	lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_active());

	if (panel != NULL) {
		return;
	}
	if (dir == LV_DIR_LEFT) {
		open_panel(lv_event_get_current_target(e));
	} else if (dir == LV_DIR_RIGHT) {
		/* The left chevron's promise: the boot clip on loop. Only
		 * flagged here -- the mode loop runs the player from thread
		 * context, never from inside an LVGL event. */
		ui_anim_request();
	}
}

/* Invisible tap strips along both screen edges: the chevrons drawn there
 * read as buttons, so tapping them must work too (tried on hardware
 * 2026-07-17). GESTURE_BUBBLE keeps the swipes alive across the strips. */
static void zone_settings_cb(lv_event_t *e)
{
	if (panel == NULL) {
		open_panel(lv_obj_get_screen(lv_event_get_target(e)));
	}
}

static void zone_anim_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (panel == NULL) {
		ui_anim_request();
	}
}

static void mk_edge_zone(lv_obj_t *scr, lv_align_t align, lv_event_cb_t cb)
{
	lv_obj_t *z = lv_btn_create(scr);

	lv_obj_set_size(z, 44, 150);
	lv_obj_set_style_bg_opa(z, LV_OPA_TRANSP, 0);
	lv_obj_set_style_shadow_width(z, 0, 0);
	lv_obj_align(z, align, 0, 0);
	lv_obj_add_flag(z, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(z, cb, LV_EVENT_CLICKED, NULL);
}

void ui_settings_attach(lv_obj_t *scr)
{
	lv_obj_add_event_cb(scr, scr_gesture_cb, LV_EVENT_GESTURE, NULL);
	mk_edge_zone(scr, LV_ALIGN_RIGHT_MID, zone_settings_cb);
	mk_edge_zone(scr, LV_ALIGN_LEFT_MID, zone_anim_cb);
}
