/*
 * Provisioning screen ("boarding pass"), three stages.
 *
 * Left: a three-step checklist -- Join device, Connect WiFi, Sign in -- that
 * ticks green as the user advances. Right: a distinctly darker panel (with a
 * green seam) holding the WiFi-join QR and a caption that tracks state.
 */
#include <zephyr/kernel.h>
#include <lvgl.h>
#include <string.h>
#include <stdio.h>

#include "ui_setup.h"
#include "net_wifi.h"

/* Backgrounds are deliberately two distinct values so the QR panel reads as its
 * own zone rather than blending into the screen.
 */
#define COL_BG		lv_color_hex(0x0E1116)	/* screen */
#define COL_PANEL	lv_color_hex(0x05070A)	/* QR panel -- clearly darker */
#define COL_SEAM	lv_color_hex(0x2ECC71)	/* divider */
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_GREEN	lv_color_hex(0x2ECC71)
#define COL_GREEN_INK	lv_color_hex(0x06210F)
#define COL_TRACK	lv_color_hex(0x2A313B)
#define COL_PILLBG	lv_color_hex(0x0F2A1B)
#define COL_RED		lv_color_hex(0xE74C3C)

struct step {
	lv_obj_t *num;
	lv_obj_t *numlbl;
	lv_obj_t *title;
	lv_obj_t *sub;
};

static lv_obj_t *scr;
static struct step steps[3];
static lv_obj_t *qr;
static lv_obj_t *cap;
static lv_obj_t *pill;
static lv_obj_t *brand;
static lv_obj_t *panel;

static volatile int pending = -1;
static char pending_detail[40];

static int applied = UI_SETUP_WAIT;	/* last state apply() ran */

static void on_station(int count)
{
	/* Once the join attempt starts the AP is gone; stragglers must not
	 * drag the screen back to the boarding-pass join steps. */
	if (applied >= UI_SETUP_CONNECTING) {
		return;
	}
	if (count > 0 && pending < UI_SETUP_PHONE) {
		pending = UI_SETUP_PHONE;
	} else if (count == 0 && (pending < 0 || pending <= UI_SETUP_PHONE)) {
		pending = UI_SETUP_WAIT;
	}
}

static void step_pending(struct step *s)
{
	lv_obj_set_style_bg_opa(s->num, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_color(s->num, COL_TRACK, 0);
	lv_obj_set_style_text_color(s->numlbl, COL_DIM, 0);
	lv_obj_set_style_text_color(s->title, COL_DIM, 0);
}
static void step_active(struct step *s)
{
	lv_obj_set_style_bg_opa(s->num, LV_OPA_COVER, 0);
	lv_obj_set_style_bg_color(s->num, COL_GREEN, 0);
	lv_obj_set_style_border_color(s->num, COL_GREEN, 0);
	lv_obj_set_style_text_color(s->numlbl, COL_GREEN_INK, 0);
	lv_obj_set_style_text_color(s->title, COL_TEXT, 0);
}
static void step_done(struct step *s)
{
	step_active(s);
	lv_label_set_text(s->numlbl, LV_SYMBOL_OK);
}

static void build_step(struct step *s, lv_obj_t *parent, const char *num,
		       const char *title, const char *sub, lv_coord_t y)
{
	s->num = lv_obj_create(parent);
	lv_obj_set_size(s->num, 30, 30);
	lv_obj_set_style_radius(s->num, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_border_width(s->num, 2, 0);
	lv_obj_set_style_pad_all(s->num, 0, 0);
	lv_obj_set_style_bg_color(s->num, COL_GREEN, 0);
	lv_obj_clear_flag(s->num, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_align(s->num, LV_ALIGN_TOP_LEFT, 16, y);

	s->numlbl = lv_label_create(s->num);
	lv_label_set_text(s->numlbl, num);
	lv_obj_set_style_text_font(s->numlbl, &lv_font_montserrat_14, 0);
	lv_obj_center(s->numlbl);

	/* Constrain the text column so a long title can never run under the QR
	 * panel: it dots ("Sign in to C...") instead of overflowing the seam.
	 * Left column is 188px wide (panel starts at 320-132); text starts at 56.
	 */
	s->title = lv_label_create(parent);
	lv_label_set_text(s->title, title);
	lv_obj_set_style_text_font(s->title, &lv_font_montserrat_14, 0);
	lv_obj_set_style_text_color(s->title, COL_TEXT, 0);
	lv_obj_set_width(s->title, 128);
	lv_label_set_long_mode(s->title, LV_LABEL_LONG_DOT);
	lv_obj_align(s->title, LV_ALIGN_TOP_LEFT, 56, y - 1);

	s->sub = lv_label_create(parent);
	lv_label_set_text(s->sub, sub);
	lv_obj_set_style_text_font(s->sub, &lv_font_montserrat_14, 0);
	lv_obj_set_style_text_color(s->sub, COL_DIM, 0);
	lv_obj_set_width(s->sub, 128);
	lv_label_set_long_mode(s->sub, LV_LABEL_LONG_DOT);
	lv_obj_align(s->sub, LV_ALIGN_TOP_LEFT, 56, y + 18);
}

void ui_setup_show(void)
{
	scr = lv_obj_create(NULL);
	lv_obj_set_style_bg_color(scr, COL_BG, 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_set_style_pad_all(scr, 0, 0);
	lv_obj_set_style_border_width(scr, 0, 0);

	brand = lv_label_create(scr);

	lv_label_set_text(brand, "CLAUDE USAGE");
	lv_obj_set_style_text_color(brand, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(brand, 2, 0);
	lv_obj_align(brand, LV_ALIGN_TOP_LEFT, 16, 14);

	build_step(&steps[0], scr, "1", "Join device", "scan the code", 54);
	build_step(&steps[1], scr, "2", "Connect WiFi", "pick network", 110);
	build_step(&steps[2], scr, "3", "Sign in", "Claude account", 166);

	/* QR panel -- darker ground, green seam on the left edge. */
	panel = lv_obj_create(scr);

	lv_obj_set_size(panel, 132, 240);
	lv_obj_align(panel, LV_ALIGN_TOP_RIGHT, 0, 0);
	lv_obj_set_style_bg_color(panel, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(panel, LV_OPA_COVER, 0);
	lv_obj_set_style_radius(panel, 0, 0);
	lv_obj_set_style_border_width(panel, 3, 0);
	lv_obj_set_style_border_side(panel, LV_BORDER_SIDE_LEFT, 0);
	lv_obj_set_style_border_color(panel, COL_SEAM, 0);
	lv_obj_clear_flag(panel, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_set_style_pad_all(panel, 0, 0);

	const char *payload = net_wifi_ap_qr();

	qr = lv_qrcode_create(panel);
	lv_qrcode_set_size(qr, 100);
	lv_qrcode_set_dark_color(qr, lv_color_black());
	lv_qrcode_set_light_color(qr, lv_color_white());
	lv_qrcode_update(qr, payload, strlen(payload));
	lv_obj_set_style_border_width(qr, 6, 0);
	lv_obj_set_style_border_color(qr, lv_color_white(), 0);
	lv_obj_align(qr, LV_ALIGN_TOP_MID, 0, 46);

	cap = lv_label_create(panel);
	lv_label_set_text(cap, "scan to\nbegin");
	lv_obj_set_style_text_color(cap, COL_DIM, 0);
	lv_obj_set_style_text_align(cap, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(cap, LV_ALIGN_BOTTOM_MID, 0, -22);

	pill = lv_obj_create(panel);
	lv_obj_set_size(pill, 104, 36);
	lv_obj_set_style_radius(pill, 18, 0);
	lv_obj_set_style_bg_color(pill, COL_PILLBG, 0);
	lv_obj_set_style_bg_opa(pill, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(pill, 0, 0);
	lv_obj_clear_flag(pill, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(pill);

	lv_obj_t *pl = lv_label_create(pill);

	lv_label_set_text(pl, LV_SYMBOL_OK "  online");
	lv_obj_set_style_text_color(pl, COL_GREEN, 0);
	lv_obj_center(pl);
	lv_obj_add_flag(pill, LV_OBJ_FLAG_HIDDEN);

	step_active(&steps[0]);  lv_label_set_text(steps[0].numlbl, "1");
	step_pending(&steps[1]);
	step_pending(&steps[2]);

	net_wifi_set_sta_cb(on_station);
	lv_scr_load(scr);
}

static void apply(enum ui_setup_state st, const char *detail)
{
	applied = (int)st;

	switch (st) {
	case UI_SETUP_WAIT:
		step_active(&steps[0]);  lv_label_set_text(steps[0].numlbl, "1");
		lv_label_set_text(steps[0].title, "Join device");
		lv_label_set_text(steps[0].sub, "scan the code");
		step_pending(&steps[1]); lv_label_set_text(steps[1].numlbl, "2");
		step_pending(&steps[2]); lv_label_set_text(steps[2].numlbl, "3");
		lv_obj_clear_flag(qr, LV_OBJ_FLAG_HIDDEN);
		lv_obj_clear_flag(cap, LV_OBJ_FLAG_HIDDEN);
		lv_obj_add_flag(pill, LV_OBJ_FLAG_HIDDEN);
		lv_qrcode_update(qr, net_wifi_ap_qr(), strlen(net_wifi_ap_qr()));
		lv_label_set_text(cap, "scan to\nbegin");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		break;
	case UI_SETUP_PHONE:
		step_done(&steps[0]);
		lv_label_set_text(steps[0].title, "Phone joined");
		lv_label_set_text(steps[0].sub, "use the page");
		step_active(&steps[1]); lv_label_set_text(steps[1].numlbl, "2");
		lv_label_set_text(cap, "page open\non phone");
		break;
	case UI_SETUP_CONNECTING:
		step_done(&steps[0]);
		step_active(&steps[1]); lv_label_set_text(steps[1].numlbl, "2");
		lv_label_set_text(steps[1].title, "Connect WiFi");
		lv_label_set_text(steps[1].sub, detail ? detail : "joining\xE2\x80\xA6");
		lv_obj_add_flag(qr, LV_OBJ_FLAG_HIDDEN);
		lv_label_set_text(cap, "joining\nnetwork\xE2\x80\xA6");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		break;
	case UI_SETUP_WIFI_OK:
		/* detail = the sign-in URL; it becomes the QR payload. */
		step_done(&steps[0]);
		step_done(&steps[1]);
		lv_label_set_text(steps[1].title, "WiFi connected");
		step_active(&steps[2]); lv_label_set_text(steps[2].numlbl, "3");
		if (detail && detail[0]) {
			lv_qrcode_update(qr, detail, strlen(detail));
		}
		lv_obj_clear_flag(qr, LV_OBJ_FLAG_HIDDEN);
		lv_label_set_text(cap, detail ? detail : "scan to\nsign in");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		break;
	case UI_SETUP_SIGNIN:
		lv_label_set_text(cap, "signing\nin\xE2\x80\xA6");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		break;
	case UI_SETUP_DONE:
		step_done(&steps[2]);
		lv_obj_add_flag(qr, LV_OBJ_FLAG_HIDDEN);
		lv_obj_clear_flag(pill, LV_OBJ_FLAG_HIDDEN);
		lv_label_set_text(cap, "all set");
		lv_obj_set_style_text_color(cap, COL_GREEN, 0);
		break;
	case UI_SETUP_ERROR:
		lv_label_set_text(cap, detail ? detail : "error");
		lv_obj_set_style_text_color(cap, COL_RED, 0);
		break;
	}
}

void ui_setup_set_state(enum ui_setup_state state, const char *detail)
{
	if (detail) {
		strncpy(pending_detail, detail, sizeof(pending_detail) - 1);
		pending_detail[sizeof(pending_detail) - 1] = '\0';
	} else {
		pending_detail[0] = '\0';
	}
	pending = (int)state;
}

void ui_setup_service(void)
{
	int st = pending;

	if (st < 0 || !scr) {
		return;
	}
	pending = -1;
	apply((enum ui_setup_state)st, pending_detail[0] ? pending_detail : NULL);
}
