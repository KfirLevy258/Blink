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
#include "fmt.h"

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
static lv_obj_t *spin;
static struct step steps[3];
static lv_obj_t *qr;
static lv_obj_t *cap;
static lv_obj_t *pill;
static lv_obj_t *brand;
static lv_obj_t *panel;
static lv_obj_t *err_box;	/* the single live join-failure popup, or NULL */

static volatile int pending = -1;
static char pending_detail[40];

/* Read by on_station() from the net-mgmt callback context, written on the
 * LVGL thread -- volatile for the same cross-thread visibility reason as
 * `pending` below. */
static volatile int applied = UI_SETUP_WAIT;	/* last state apply() ran */

static void on_station(int count)
{
	printk("[setup] on_station count=%d applied=%d pending=%d\n",
	       count, applied, pending);
	/* Once the AP is gone (reboot-to-join or the join itself), stragglers
	 * must not drag the screen back to the boarding-pass join steps. */
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
	 * Height is pinned to one line: with auto height LONG_DOT never fires --
	 * the label just wraps and the extra line lands on the next step (a long
	 * SSID sat on top of the Stage 3 title, 2026-07-15).
	 */
	s->title = lv_label_create(parent);
	lv_label_set_text(s->title, title);
	lv_obj_set_style_text_font(s->title, &lv_font_montserrat_14, 0);
	lv_obj_set_style_text_color(s->title, COL_TEXT, 0);
	lv_obj_set_size(s->title, 128, 17);
	lv_label_set_long_mode(s->title, LV_LABEL_LONG_DOT);
	lv_obj_align(s->title, LV_ALIGN_TOP_LEFT, 56, y - 1);

	s->sub = lv_label_create(parent);
	lv_label_set_text(s->sub, sub);
	lv_obj_set_style_text_font(s->sub, &lv_font_montserrat_14, 0);
	lv_obj_set_style_text_color(s->sub, COL_DIM, 0);
	lv_obj_set_size(s->sub, 128, 17);
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

	lv_label_set_text(brand, "CLAUGE");
	lv_obj_set_style_text_color(brand, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(brand, 2, 0);
	lv_obj_align(brand, LV_ALIGN_TOP_LEFT, 16, 14);

	build_step(&steps[0], scr, "1", "Join device", "Scan the code", 54);
	build_step(&steps[1], scr, "2", "Connect WiFi", "Pick network", 110);
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

	/* 92 px + the white border leaves clear air between the code and the
	 * green seam -- at 100 px the two nearly touched (user feedback
	 * 2026-07-16). Payloads here are short; 92 px scans fine. */
	qr = lv_qrcode_create(panel);
	lv_qrcode_set_size(qr, 92);
	lv_qrcode_set_dark_color(qr, lv_color_black());
	lv_qrcode_set_light_color(qr, lv_color_white());
	lv_qrcode_update(qr, payload, strlen(payload));
	lv_obj_set_style_border_width(qr, 6, 0);
	lv_obj_set_style_border_color(qr, lv_color_white(), 0);
	lv_obj_align(qr, LV_ALIGN_TOP_MID, 0, 46);

	/* Directly under the QR (46 + 92 + 2*6 border + 14 gap), sharing its
	 * center line -- one column, not code-at-top / text-at-bottom
	 * (user feedback 2026-07-17). Fixed offsets, so no layout pass is
	 * needed for them to agree. */
	cap = lv_label_create(panel);
	lv_label_set_text(cap, "Scan to\nbegin");
	lv_obj_set_width(cap, 124);
	lv_label_set_long_mode(cap, LV_LABEL_LONG_WRAP);
	lv_obj_set_style_text_color(cap, COL_DIM, 0);
	lv_obj_set_style_text_align(cap, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(cap, LV_ALIGN_TOP_MID, 0, 164);

	pill = lv_obj_create(panel);
	lv_obj_set_size(pill, 104, 36);
	lv_obj_set_style_radius(pill, 18, 0);
	lv_obj_set_style_bg_color(pill, COL_PILLBG, 0);
	lv_obj_set_style_bg_opa(pill, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(pill, 0, 0);
	lv_obj_clear_flag(pill, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(pill);

	lv_obj_t *pl = lv_label_create(pill);

	lv_label_set_text(pl, LV_SYMBOL_OK "  Online");
	lv_obj_set_style_text_color(pl, COL_GREEN, 0);
	lv_obj_center(pl);
	lv_obj_add_flag(pill, LV_OBJ_FLAG_HIDDEN);

	/* Busy spinner for the joining/waiting moments; per-state code
	 * unhides it and parks the caption underneath. */
	spin = lv_spinner_create(panel);
	lv_obj_set_size(spin, 40, 40);
	lv_obj_align(spin, LV_ALIGN_CENTER, 0, 0);
	lv_obj_set_style_arc_color(spin, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(spin, COL_GREEN, LV_PART_INDICATOR);
	lv_obj_set_style_arc_width(spin, 5, LV_PART_MAIN);
	lv_obj_set_style_arc_width(spin, 5, LV_PART_INDICATOR);
	lv_obj_add_flag(spin, LV_OBJ_FLAG_HIDDEN);

	step_active(&steps[0]);  lv_label_set_text(steps[0].numlbl, "1");
	step_pending(&steps[1]);
	step_pending(&steps[2]);

	net_wifi_set_sta_cb(on_station);
	lv_scr_load(scr);
}

static void error_popup_ok_cb(lv_event_t *e)
{
	/* Deleting the popup from its own button's event: async, so LVGL
	 * finishes the click processing on a live object first. */
	err_box = NULL;
	lv_obj_del_async(lv_event_get_user_data(e));
}

/* Modal join-failure notice over the boarding pass; OK dismisses it and the
 * normal WAIT screen (already applied underneath) carries on. */
static void error_popup(const char *msg)
{
	/* Singleton. A phone hopping on/off the setup AP flaps the state back
	 * through WAIT, which re-enters here -- without this, each pass stacks a
	 * fresh popup over the last. Never keep more than one on screen. */
	if (err_box) {
		lv_obj_del(err_box);
	}

	lv_obj_t *box = lv_obj_create(scr);

	err_box = box;
	lv_obj_set_size(box, 220, 110);
	lv_obj_set_style_radius(box, 12, 0);
	lv_obj_set_style_bg_color(box, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(box, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(box, COL_RED, 0);
	lv_obj_set_style_border_width(box, 1, 0);
	lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(box);

	lv_obj_t *l = lv_label_create(box);

	lv_label_set_text(l, msg);
	lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
	lv_obj_set_width(l, 190);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(l, LV_ALIGN_TOP_MID, 0, 2);

	lv_obj_t *btn = lv_btn_create(box);

	lv_obj_set_size(btn, 84, 32);
	/* LVGL 9 births every child SCROLLABLE + GESTURE_BUBBLE. On the jittery
	 * XPT2046 panel a tap that drifts a few px can then be eaten as a scroll or
	 * gesture so CLICKED never fires; clear both, exactly as the settings
	 * buttons do, so a slightly-smeared tap still registers. */
	lv_obj_clear_flag(btn, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(btn, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_align(btn, LV_ALIGN_BOTTOM_MID, 0, -2);
	lv_obj_set_style_bg_color(btn, COL_RED, 0);
	lv_obj_add_event_cb(btn, error_popup_ok_cb, LV_EVENT_CLICKED, box);

	lv_obj_t *bl = lv_label_create(btn);

	lv_label_set_text(bl, "OK");
	lv_obj_set_style_text_color(bl, COL_TEXT, 0);
	lv_obj_center(bl);
}

static void apply(enum ui_setup_state st, const char *detail)
{
	applied = (int)st;

	/* Panel baseline every state starts from: spinner hidden, caption
	 * parked at the bottom. States opt back in below, and the QR check
	 * after the switch re-hangs the caption under a visible code. */
	lv_obj_add_flag(spin, LV_OBJ_FLAG_HIDDEN);
	lv_obj_align(cap, LV_ALIGN_BOTTOM_MID, 0, -22);

	switch (st) {
	case UI_SETUP_WAIT:
		step_active(&steps[0]);  lv_label_set_text(steps[0].numlbl, "1");
		lv_label_set_text(steps[0].title, "Join device");
		lv_label_set_text(steps[0].sub, "Scan the code");
		step_pending(&steps[1]); lv_label_set_text(steps[1].numlbl, "2");
		lv_label_set_text(steps[1].title, "Connect WiFi");
		lv_label_set_text(steps[1].sub, "Pick network");
		lv_obj_set_style_text_color(steps[1].sub, COL_DIM, 0);
		if (detail && detail[0]) {
			/* Re-entry after a failed join: the reason arrives in
			 * detail and gets a dismissable popup -- the phone
			 * form alone was too easy to miss (user feedback
			 * 2026-07-16). */
			error_popup(detail);
		}
		step_pending(&steps[2]); lv_label_set_text(steps[2].numlbl, "3");
		lv_obj_clear_flag(qr, LV_OBJ_FLAG_HIDDEN);
		lv_obj_clear_flag(cap, LV_OBJ_FLAG_HIDDEN);
		lv_obj_add_flag(pill, LV_OBJ_FLAG_HIDDEN);
		lv_qrcode_update(qr, net_wifi_ap_qr(), strlen(net_wifi_ap_qr()));
		lv_label_set_text(cap, "Scan to\nbegin");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		break;
	case UI_SETUP_PHONE:
		step_done(&steps[0]);
		lv_label_set_text(steps[0].title, "Phone joined");
		lv_label_set_text(steps[0].sub, "Use the page");
		step_active(&steps[1]); lv_label_set_text(steps[1].numlbl, "2");
		lv_label_set_text(cap, "Page open\non phone");
		break;
	case UI_SETUP_CONNECTING:
		step_done(&steps[0]);
		step_active(&steps[1]); lv_label_set_text(steps[1].numlbl, "2");
		lv_label_set_text(steps[1].title, "Connect WiFi");
		lv_label_set_text(steps[1].sub, detail ? detail : "Joining...");
		lv_obj_add_flag(qr, LV_OBJ_FLAG_HIDDEN);
		/* Busy look: spinner dead-center on the panel, caption right
		 * under it (user request 2026-07-16). */
		lv_obj_clear_flag(spin, LV_OBJ_FLAG_HIDDEN);
		lv_obj_align(cap, LV_ALIGN_CENTER, 0, 44);
		lv_label_set_text(cap, "Joining\nnetwork...");
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
		/* Never the raw URL: it reads as debug output, and the QR
		 * carries it anyway (user feedback 2026-07-16). */
		lv_label_set_text(cap, "Scan to\nsign in");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		break;
	case UI_SETUP_SIGNIN:
		lv_label_set_text(cap, "Signing in...");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		break;
	case UI_SETUP_DONE:
		step_done(&steps[2]);
		lv_obj_add_flag(qr, LV_OBJ_FLAG_HIDDEN);
		lv_obj_clear_flag(pill, LV_OBJ_FLAG_HIDDEN);
		lv_label_set_text(cap, "All set");
		lv_obj_set_style_text_color(cap, COL_GREEN, 0);
		break;
	case UI_SETUP_RESTART: {
		/* Farewell toast: the reboot right after this is by design,
		 * and an unannounced restart reads as a crash (user request
		 * 2026-07-15). */
		lv_obj_t *box = lv_obj_create(scr);

		lv_obj_set_size(box, 210, 68);
		lv_obj_set_style_radius(box, 12, 0);
		lv_obj_set_style_bg_color(box, COL_PANEL, 0);
		lv_obj_set_style_bg_opa(box, LV_OPA_COVER, 0);
		lv_obj_set_style_border_color(box, COL_GREEN, 0);
		lv_obj_set_style_border_width(box, 1, 0);
		lv_obj_clear_flag(box, LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_center(box);

		lv_obj_t *l = lv_label_create(box);

		lv_label_set_text_fmt(l, "%s\nRestarting now...",
				      detail ? detail : "Setup finished");
		lv_obj_set_style_text_color(l, COL_TEXT, 0);
		lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
		lv_obj_center(l);
		break;
	}
	case UI_SETUP_ERROR:
		/* A popup, like every other failure: the red caption in the
		 * panel corner was too easy to miss (user feedback
		 * 2026-07-16). The sign-in QR state stays live underneath,
		 * so dismissing it leaves a usable screen. */
		lv_label_set_text(cap, "Scan to\nsign in");
		lv_obj_set_style_text_color(cap, COL_DIM, 0);
		error_popup(detail && detail[0] ? detail
			    : "Sign-in failed. Try again on the phone.");
		break;
	}

	/* One column: whenever the QR is on screen, its caption hangs
	 * directly under it at the same fixed offset as ui_setup_show(). */
	if (!lv_obj_has_flag(qr, LV_OBJ_FLAG_HIDDEN)) {
		lv_obj_align(cap, LV_ALIGN_TOP_MID, 0, 164);
	}
}

void ui_setup_set_state(enum ui_setup_state state, const char *detail)
{
	if (detail) {
		/* Built-in fonts are ASCII-only; an SSID's smart quote would
		 * draw as an empty box. (URLs pass through unchanged.) */
		fmt_ascii(detail, pending_detail, sizeof(pending_detail));
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
	printk("[setup] apply state=%d\n", st);
	apply((enum ui_setup_state)st, pending_detail[0] ? pending_detail : NULL);
}
