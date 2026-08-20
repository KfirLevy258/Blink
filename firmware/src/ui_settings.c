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
#include "ui_slide.h"
#include "net_wifi.h"
#include "fmt.h"
#include "version.h"
#include "backlight.h"
#include "ota.h"
#include "usage_view.h"

#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_RED		lv_color_hex(0xE74C3C)
#define COL_GREEN	lv_color_hex(0x2ECC71)
#define COL_PANEL	lv_color_hex(0x161A20)	/* card fill, sits above COL_BG */
#define COL_LINE	lv_color_hex(0x20252D)	/* the full-width section rules */
#define COL_DANGER_BG	lv_color_hex(0x1E1412)	/* factory tile: red-tinted, not solid red */
#define COL_DANGER_BD	lv_color_hex(0x7A2B23)

enum action {
	ACT_WIFI,	/* forget network, keep token */
	ACT_SIGNIN,	/* forget token, keep network */
	ACT_FACTORY,	/* forget everything */
};

static lv_obj_t *panel;		/* NULL when closed */
static lv_obj_t *confirm;	/* NULL when no dialog is up */
static enum action pending;

/* Software-update widgets (all NULL when their owner is closed/absent). */
static lv_obj_t *notice;	/* outcome popup on the top layer */
static lv_obj_t *upd_btn;
static lv_obj_t *upd_lbl;
static lv_timer_t *upd_timer;	/* whole session, not just the panel */
/* Download progress. Full-screen and touch-swallowing on purpose, so it must
 * be torn down the moment the download stops -- a successful install ends in a
 * reboot, but a FAILED one returns to a live UI, and leaving this up locked the
 * screen against every tap (user-reported 2026-07-25: "after I confirmed the
 * popup the screen looks stuck"). It was never deleted anywhere. */
static lv_obj_t *dl_overlay;
static lv_obj_t *dl_bar;
static lv_obj_t *dl_lbl;
static lv_obj_t *dl_sub;	/* time remaining, under the bar */
static lv_obj_t *dl_src;	/* which link the image is coming over */
/*
 * Download-rate estimator. A fixed baseline (first percent seen, extrapolated
 * to the end) was the first attempt and was unusable: for the first ~20 s it
 * divides a 1-2 percent delta by a couple of seconds, so a single slow TCP
 * window swings the answer by minutes, and because the baseline never moves
 * the early noise never washes out (user-reported 2026-07-25).
 *
 * Instead: measure ms-per-percent over each percent step, smooth it with an
 * exponential moving average, and hold back any number at all until a few
 * steps have landed. Between steps the shown value counts down off the clock
 * rather than sitting still, so it reads as a timer instead of a stutter.
 */
static int64_t dl_first_ms;	/* first non-zero percent: warm-up reference */
static int64_t dl_last_ms;	/* when dl_last_pct was observed */
static uint8_t dl_last_pct;
static int dl_mspp;		/* EMA of milliseconds per percent */
static int dl_samples;
static int64_t dl_eta_ms;	/* remaining, as computed at dl_eta_at_ms */
static int64_t dl_eta_at_ms;
static int dl_shown_left = -1;	/* last printed value, to stop 1 s jitter */
static enum ota_ui_state upd_seen = OTA_UI_IDLE;
static int64_t upd_revert_at;	/* "Up to date" shows briefly, then idles */

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
	/* In LVGL 9 every child is born SCROLLABLE *and* GESTURE_BUBBLE. Clear
	 * both: a tap that drifts a few px on the jittery panel was bubbling up
	 * as a right-swipe and closing settings. A control's gesture now stops
	 * at the control; swipe-to-close still works from the bare panel. */
	lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(b, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, user);

	lv_obj_t *l = lv_label_create(b);

	lv_label_set_text(l, txt);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_center(l);
	return b;
}

/* A full-width hairline rule at row y -- the grouped-list separator. */
static void mk_line(lv_obj_t *parent, int y)
{
	lv_obj_t *l = lv_obj_create(parent);

	lv_obj_set_size(l, LV_HOR_RES, 1);
	lv_obj_align(l, LV_ALIGN_TOP_LEFT, 0, y);
	lv_obj_set_style_bg_color(l, COL_LINE, 0);
	lv_obj_set_style_bg_opa(l, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(l, 0, 0);
	lv_obj_set_style_radius(l, 0, 0);
	lv_obj_clear_flag(l, LV_OBJ_FLAG_SCROLLABLE);
}

/* A raised panel-coloured card (decoration only -- not clickable, so the
 * controls sitting on top of it still get every touch). */
static lv_obj_t *mk_card(lv_obj_t *parent, int x, int y, int w, int h)
{
	lv_obj_t *c = lv_obj_create(parent);

	lv_obj_set_size(c, w, h);
	lv_obj_align(c, LV_ALIGN_TOP_LEFT, x, y);
	lv_obj_set_style_bg_color(c, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(c, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(c, COL_TRACK, 0);
	lv_obj_set_style_border_width(c, 1, 0);
	lv_obj_set_style_radius(c, 11, 0);
	lv_obj_set_style_pad_all(c, 0, 0);
	lv_obj_clear_flag(c, LV_OBJ_FLAG_SCROLLABLE);
	/* The card is clickable-by-default and sits under the brightness gap;
	 * without this a drift-press between the steppers would bubble up and
	 * close the panel. Swallow the gesture here. */
	lv_obj_clear_flag(c, LV_OBJ_FLAG_GESTURE_BUBBLE);
	return c;
}

static lv_obj_t *pct_lbl;	/* live "70%" readout (the number is the level now) */

static void bright_refresh(void)
{
	uint8_t p = backlight_get();
	char b[8];

	snprintf(b, sizeof(b), "%d%%", p);
	lv_label_set_text(pct_lbl, b);
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

/* --- Software update: tile state machine, confirm, progress overlay --- */

static void notice_ok_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	lv_obj_del(notice);
	notice = NULL;
}

void ui_settings_notice(const char *txt)
{
	if (notice) {
		lv_obj_del(notice);
	}
	notice = lv_obj_create(lv_layer_top());
	lv_obj_set_size(notice, 300, 130);
	lv_obj_set_style_bg_color(notice, COL_BG, 0);
	lv_obj_set_style_bg_opa(notice, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(notice, COL_TRACK, 0);
	lv_obj_set_style_border_width(notice, 1, 0);
	lv_obj_clear_flag(notice, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(notice);

	lv_obj_t *l = lv_label_create(notice);

	lv_label_set_text(l, txt);
	lv_obj_set_width(l, 270);
	lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(l, LV_ALIGN_TOP_MID, 0, 8);

	lv_obj_t *ok = mk_btn(notice, "OK", COL_TRACK, notice_ok_cb, NULL);

	lv_obj_set_size(ok, 120, 36);
	lv_obj_align(ok, LV_ALIGN_BOTTOM_MID, 0, -4);
}

/*
 * Boot-time "an update is waiting" prompt.
 *
 * Lives on lv_layer_top() like the notice, NOT inside the settings panel:
 * it has to appear on the gauge screen with nothing else open, which is
 * exactly where show_install_confirm() cannot go (that one is a child of
 * `panel`). Shown at most once per boot -- an update the user answered
 * "Later" to must not re-ask on every OTA state tick.
 */
static lv_obj_t *upd_prompt;
static bool upd_prompt_done;

static void upd_prompt_close(void)
{
	if (upd_prompt) {
		lv_obj_del(upd_prompt);
		upd_prompt = NULL;
	}
}

static void upd_prompt_later_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	upd_prompt_close();
}

static void upd_prompt_now_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	upd_prompt_close();
	/* The download overlay is screen-level and upd_timer now runs whether
	 * or not the panel is open, so the bar appears without going through
	 * settings. */
	ota_request_install();
}

static void upd_prompt_show(const struct ota_ui *snap)
{
	/* Never stack on top of something the user is already answering. */
	if (upd_prompt_done || upd_prompt || panel || confirm || notice ||
	    dl_overlay) {
		return;
	}
	upd_prompt_done = true;

	upd_prompt = lv_obj_create(lv_layer_top());
	lv_obj_set_size(upd_prompt, 300, 130);
	lv_obj_set_style_bg_color(upd_prompt, COL_BG, 0);
	lv_obj_set_style_bg_opa(upd_prompt, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(upd_prompt, COL_GREEN, 0);
	lv_obj_set_style_border_width(upd_prompt, 1, 0);
	/* Zero the container padding before laying anything out. lv_obj_create
	 * inherits the theme's default pad, which shrinks the usable width --
	 * two 130 px buttons at +/-12 px from the edges then did not fit inside
	 * it and overlapped (user-reported 2026-08-20). */
	lv_obj_set_style_pad_all(upd_prompt, 0, 0);
	lv_obj_clear_flag(upd_prompt, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_center(upd_prompt);

	lv_obj_t *l = lv_label_create(upd_prompt);

	lv_label_set_text_fmt(l, "Version %s is available.", snap->version);
	lv_obj_set_width(l, 270);
	lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
	lv_obj_set_style_text_color(l, COL_TEXT, 0);
	lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(l, LV_ALIGN_TOP_MID, 0, 10);

	/*
	 * Placed from the CENTRE, not from the edges: +/-70 either side of the
	 * midline puts two 132 px buttons at x 14..146 and 154..286 inside the
	 * 300 px popup, an 8 px gap between them and 14 px to each edge -- and
	 * it stays symmetric whatever the container padding turns out to be,
	 * which edge-relative offsets did not.
	 */
	lv_obj_t *yes = mk_btn(upd_prompt, "Update now", COL_GREEN,
			       upd_prompt_now_cb, NULL);

	lv_obj_set_size(yes, 132, 36);
	lv_obj_align(yes, LV_ALIGN_BOTTOM_MID, -70, -10);

	lv_obj_t *no = mk_btn(upd_prompt, "Later", COL_TRACK,
			      upd_prompt_later_cb, NULL);

	lv_obj_set_size(no, 132, 36);
	lv_obj_align(no, LV_ALIGN_BOTTOM_MID, 70, -10);
}

static void install_yes_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	ota_request_install();
	lv_obj_del(confirm);
	confirm = NULL;
}

static void show_install_confirm(const struct ota_ui *snap)
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

	lv_label_set_text_fmt(q, "Install version %s?\nThe screen restarts when done.",
			      snap->version);
	lv_obj_set_style_text_color(q, COL_TEXT, 0);
	lv_obj_set_style_text_align(q, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(q, LV_ALIGN_TOP_MID, 0, 40);

	lv_obj_t *yes = mk_btn(confirm, "Yes, do it", COL_GREEN,
			       install_yes_cb, NULL);

	lv_obj_align(yes, LV_ALIGN_BOTTOM_MID, 0, -70);

	lv_obj_t *no = mk_btn(confirm, "Cancel", COL_TRACK,
			      confirm_no_cb, NULL);

	lv_obj_align(no, LV_ALIGN_BOTTOM_MID, 0, -18);
}

static void upd_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	struct ota_ui snap;

	ota_ui_get(&snap);
	if (snap.st == OTA_UI_IDLE || snap.st == OTA_UI_UP_TO_DATE) {
		ota_request_check();
	} else if (snap.st == OTA_UI_AVAILABLE && confirm == NULL) {
		show_install_confirm(&snap);
	}
}

/* Children go with the parent; null the handles so a later show() rebuilds
 * them instead of writing through dangling pointers. */
static void dl_overlay_hide(void)
{
	if (!dl_overlay) {
		return;
	}
	lv_obj_del(dl_overlay);
	dl_overlay = NULL;
	dl_lbl = NULL;
	dl_bar = NULL;
	dl_sub = NULL;
	dl_src = NULL;
}

static void dl_overlay_show(const struct ota_ui *snap, bool rebooting)
{
	if (!dl_overlay) {
		/* Full-screen and clickable: it swallows every touch, so
		 * nothing underneath (panel close included) can run. Only
		 * the reboot removes it. */
		dl_overlay = lv_obj_create(lv_layer_top());
		lv_obj_set_size(dl_overlay, LV_PCT(100), LV_PCT(100));
		lv_obj_set_style_bg_color(dl_overlay, COL_BG, 0);
		lv_obj_set_style_bg_opa(dl_overlay, LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(dl_overlay, 0, 0);
		lv_obj_set_style_radius(dl_overlay, 0, 0);
		lv_obj_clear_flag(dl_overlay, LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_center(dl_overlay);

		dl_lbl = lv_label_create(dl_overlay);
		lv_obj_set_style_text_color(dl_lbl, COL_TEXT, 0);
		lv_obj_align(dl_lbl, LV_ALIGN_CENTER, 0, -30);

		/* Which link this is arriving on. Serial tops out near 11 KB/s
		 * against WiFi's hundreds, so a USB update legitimately takes
		 * minutes -- saying so up front is the difference between
		 * "slow" and "stuck" (user request 2026-08-20). */
		dl_src = lv_label_create(dl_overlay);
		lv_obj_set_style_text_color(dl_src, COL_DIM, 0);
		lv_obj_align(dl_src, LV_ALIGN_CENTER, 0, -8);

		dl_bar = lv_bar_create(dl_overlay);
		lv_obj_set_size(dl_bar, 260, 12);
		lv_bar_set_range(dl_bar, 0, 100);
		lv_obj_set_style_bg_color(dl_bar, COL_TRACK, 0);
		lv_obj_set_style_bg_color(dl_bar, COL_GREEN, LV_PART_INDICATOR);
		lv_obj_align(dl_bar, LV_ALIGN_CENTER, 0, 10);

		dl_sub = lv_label_create(dl_overlay);
		lv_obj_set_style_text_color(dl_sub, COL_DIM, 0);
		/* Bounded and wrapping, not free-running. A single centred line
		 * runs off both edges of a 320 px panel at about 45 characters,
		 * which is how "This takes a few minutes. Keep the device
		 * powered." ended up hanging off the screen (user-reported
		 * 2026-07-26 -- the second overflow of the evening, after the
		 * update row). Constraining the label means future copy wraps
		 * instead of silently escaping. */
		lv_obj_set_width(dl_sub, 280);
		lv_label_set_long_mode(dl_sub, LV_LABEL_LONG_WRAP);
		lv_obj_set_style_text_align(dl_sub, LV_TEXT_ALIGN_CENTER, 0);
		lv_obj_align(dl_sub, LV_ALIGN_CENTER, 0, 34);
		lv_label_set_text(dl_sub, "");

		dl_first_ms = 0;
		dl_last_ms = 0;
		dl_last_pct = 0;
		dl_mspp = 0;
		dl_samples = 0;
		dl_eta_ms = 0;
		dl_eta_at_ms = 0;
		dl_shown_left = -1;
	}
	if (rebooting) {
		/* NOT "Restarting...": this frame is the LAST thing the panel
		 * draws, and it stays frozen on screen for as long as MCUboot
		 * spends swapping slot1 into slot0 -- nothing repaints until the
		 * new image boots. Claiming a restart that visibly is not
		 * happening reads as a hang (user-reported 2026-07-25).
		 *
		 * "A few minutes", not a number. Measured host-clock swap times
		 * for a 1.28 MB image: 357 s on the pre-2026-07-26 bootloader,
		 * 179 s after the sector-sized copy buffer landed (0.4.6), and
		 * 121 s once the encrypted 0xFF fill was deferred (0.4.7). The
		 * spread is the whole point of staying vague. The app cannot
		 * tell which bootloader it is running on -- MCUboot is not
		 * delivered over the air, so an OTA'd image may sit on either --
		 * and a device that promises "3 minutes" then takes six has
		 * recreated the exact "is it stuck?" problem this text exists to
		 * prevent. Vague and true beats precise and wrong.
		 *
		 * Do NOT re-derive this from MCUboot's own log timestamps: they
		 * read 17.3 s and 0.9 s for those same two swaps. */
		lv_label_set_text(dl_lbl, "Installing update...");
		lv_label_set_text(dl_src, "");
		lv_label_set_text(dl_sub,
				  "This takes a few minutes.\nKeep the device powered.");
		lv_bar_set_value(dl_bar, 100, LV_ANIM_OFF);
		return;
	}

	if (ota_ui_source() == OTA_SRC_USB) {
		/*
		 * Over USB the daemon drives esptool and the board is not part
		 * of the transfer at all -- it cannot see a byte count, so
		 * there is no honest percentage to show. A bar frozen at 0 for
		 * a minute reads as a hang, which is the exact failure the
		 * "Installing update..." wording elsewhere in this file exists
		 * to avoid, so hide it and say what is actually happening.
		 */
		lv_label_set_text_fmt(dl_lbl, "Updating to %s...", snap->version);
		lv_label_set_text(dl_src, "Over the USB cable");
		lv_obj_add_flag(dl_bar, LV_OBJ_FLAG_HIDDEN);
		lv_label_set_text(dl_sub,
				  "Keep the cable connected.\nThis takes about a minute.");
		return;
	}

	lv_obj_clear_flag(dl_bar, LV_OBJ_FLAG_HIDDEN);
	lv_label_set_text_fmt(dl_lbl, "Downloading version %s...", snap->version);
	lv_label_set_text(dl_src, "Over WiFi");
	lv_bar_set_value(dl_bar, snap->pct, LV_ANIM_OFF);

	int64_t now = k_uptime_get();

	if (snap->pct == 0) {
		return;			/* no body bytes yet */
	}
	if (dl_last_ms == 0) {		/* first percent: start the clock */
		dl_first_ms = now;
		dl_last_ms = now;
		dl_last_pct = snap->pct;
		return;
	}

	if (snap->pct > dl_last_pct) {
		int dp = snap->pct - dl_last_pct;
		int inst = (int)((now - dl_last_ms) / dp);

		/* EMA, alpha 1/4. Slow enough to ride out a stalled window,
		 * quick enough to track a genuine rate change. */
		dl_mspp = dl_mspp ? (dl_mspp * 3 + inst) / 4 : inst;
		dl_last_pct = snap->pct;
		dl_last_ms = now;
		dl_samples++;

		dl_eta_ms = (int64_t)dl_mspp * (100 - snap->pct);
		dl_eta_at_ms = now;
	}

	/* Warm-up: say nothing rather than something wrong. The bar is already
	 * showing that work is happening. */
	if (dl_samples < 4 || now - dl_first_ms < 5000) {
		return;
	}

	int64_t remain = dl_eta_ms - (now - dl_eta_at_ms);
	int left = remain > 0 ? (int)(remain / 1000) : 0;

	/* Coarser buckets further out: nobody needs 1 s resolution on a
	 * two-minute estimate, and the quantisation is what kills the twitch. */
	if (left > 90) {
		left = (left + 5) / 10 * 10;
	} else if (left > 30) {
		left = (left + 2) / 5 * 5;
	}

	if (left <= 5) {
		if (dl_shown_left != 0) {
			dl_shown_left = 0;
			lv_label_set_text(dl_sub, "Almost done...");
		}
		return;
	}

	/* Let it fall freely but resist rising: an estimate that keeps growing
	 * feels broken even when it is honest, so only admit a slowdown once it
	 * is unmistakable (>20% worse than what is on screen). */
	if (dl_shown_left > 0 && left > dl_shown_left) {
		int slack = dl_shown_left / 5 + 3;

		if (left - dl_shown_left < slack) {
			return;
		}
	}
	if (left == dl_shown_left) {
		return;
	}
	dl_shown_left = left;
	if (left >= 60) {
		lv_label_set_text_fmt(dl_sub, "About %d min %d s left",
				      left / 60, left % 60);
	} else {
		lv_label_set_text_fmt(dl_sub, "About %d s left", left);
	}
}

static void upd_timer_cb(lv_timer_t *t)
{
	ARG_UNUSED(t);
	struct ota_ui snap;

	ota_ui_get(&snap);

	/*
	 * Screen-level state first, and unconditionally.
	 *
	 * This timer runs for the whole session now, not only while the panel
	 * is open, so an install started from the boot prompt still gets its
	 * progress bar and its outcome popup -- both of those live on
	 * lv_layer_top() and never needed the panel. Everything touching
	 * upd_lbl/upd_btn is fenced below instead: those die with the panel,
	 * and writing through them once it is shut is a NULL deref.
	 */
	if (snap.st != OTA_UI_DOWNLOADING && snap.st != OTA_UI_REBOOTING) {
		dl_overlay_hide();
	}

	switch (snap.st) {
	case OTA_UI_DOWNLOADING:
		dl_overlay_show(&snap, false);
		break;
	case OTA_UI_REBOOTING:
		dl_overlay_show(&snap, true);
		break;
	case OTA_UI_AVAILABLE:
		upd_prompt_show(&snap);
		break;
	case OTA_UI_UP_TO_DATE:
		/* Bounce back to IDLE even with the panel shut, so a stale
		 * "Up to date" cannot greet the next open. */
		if (upd_seen != OTA_UI_UP_TO_DATE) {
			upd_revert_at = k_uptime_get() + 3000;
		} else if (k_uptime_get() > upd_revert_at) {
			ota_ui_set(OTA_UI_IDLE, NULL, 0);
		}
		break;
	case OTA_UI_FAILED:
		if (upd_seen != OTA_UI_FAILED) {
			ui_settings_notice(upd_seen == OTA_UI_DOWNLOADING ?
				"Update failed. The current version keeps running." :
				"Couldn't check for updates.");
			ota_ui_set(OTA_UI_IDLE, NULL, 0);
		}
		break;
	default:
		break;
	}

	if (upd_lbl == NULL) {		/* panel shut: no row to update */
		upd_seen = snap.st;
		return;
	}

	switch (snap.st) {
	case OTA_UI_IDLE:
		/* Left label + chevron already say "Software update"; the right
		 * side is a STATUS, blank until there's something to report, so
		 * it can't crowd the title. A pending badge is the exception. */
		lv_label_set_text(upd_lbl, ota_badge() ? "Update ready" : "");
		lv_obj_set_style_text_color(upd_lbl,
					    ota_badge() ? COL_GREEN : COL_DIM, 0);
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_CHECKING:
		lv_label_set_text(upd_lbl, "Checking...");
		lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
		lv_obj_add_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_UP_TO_DATE:
		lv_label_set_text(upd_lbl, "Up to date");
		lv_obj_set_style_text_color(upd_lbl, COL_GREEN, 0);
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_AVAILABLE:
		/* Version only: this row shares 296 px with "Software update" on
		 * the left, which leaves ~13 characters here before the two
		 * collide (the size string made it 23 and they overlapped --
		 * user-reported 2026-07-25). Keep any future state text within
		 * the same budget as "Update ready". */
		lv_label_set_text_fmt(upd_lbl, "Install %s", snap.version);
		lv_obj_set_style_text_color(upd_lbl, COL_GREEN, 0);
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_FAILED:
		lv_label_set_text(upd_lbl, "");
		lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	default:
		break;
	}
	upd_seen = snap.st;
}

/*
 * The panel slides in from the right and back out again (user request
 * 2026-07-17) -- the motion says where settings lives and which way leads
 * home. The WHOLE screen moves, and does so without LVGL redrawing it.
 *
 * Two earlier attempts are worth recording, because both look like tuning
 * problems and are not.
 *
 * Sliding it with LVGL costs a full redraw per frame -- ~124 ms here, so a
 * 400 ms transition rendered about three frames and read as a stutter. Note it
 * is the object's SIZE that governs that, not how it is painted:
 * lv_obj_invalidate() works off lv_obj_get_coords() and never looks at bg_opa.
 *
 * Travelling as a header-height bar and expanding on arrival did fix the
 * framerate -- measured ~14 ms a frame against ~124 -- but a bar sliding while
 * the rest of the screen appears from nowhere is incoherent motion, and being
 * smooth does not redeem it (user-reported 2026-08-17, twice). Anything
 * between the two only trades one complaint for the other: cost scales with
 * the area that moves, so "more of the screen" and "more frames" are one dial
 * pulled in opposite directions.
 *
 * The way out is not to redraw at all. See ui_slide.c -- the panel's own
 * scroll register moves the image already sitting in its GRAM, so a transition
 * costs ONE full render spread across forty steps instead of one per frame.
 *
 * The consequence here is that open and close cannot run where they are asked
 * for. ui_slide_run() drives lv_refr_now() itself, and re-entering the refresh
 * from inside lv_timer_handler() is not safe, so the gesture handlers only
 * raise a flag and the mode loop runs the transition from thread context --
 * exactly as ui_anim does for the clip.
 */
static bool closing;
static volatile bool want_open;
static volatile bool want_close;

static void build_panel(lv_obj_t *parent_scr);

/* Thread context. The tree surgery happens frozen: creating the panel would
 * otherwise invalidate the screen, and refreshing that invalidation would
 * repaint over the very gauge pixels the scroll is about to move away. */
static void do_open(void (*pump)(void))
{
	ui_slide_begin();
	/* The live screen, not one cached when the gesture landed. The cached
	 * pointer outlived its object on the failed-join path, where
	 * usage_view_deinit() deletes the gauge screen before provisioning. */
	build_panel(lv_screen_active());
	lv_obj_update_layout(panel);
	ui_slide_freeze(false);

	/*
	 * Mute across the transition, the way ui_anim does for the clip.
	 * ui_slide_run() blocks for the whole slide with no input dispatched
	 * (pump() does not run lv_timer_handler()), so every touch point of the
	 * opening swipe -- and any impatient second one -- is delivered in a
	 * burst afterwards, against a screen that has changed underneath it.
	 */
	ui_anim_gesture_mute(UI_SLIDE_MS * 6);

	/* Gauges exit left, settings enters from the right. */
	ui_slide_run(UI_SLIDE_LEFT, pump);
	ui_anim_gesture_mute(250);	/* short tail past the input burst */
}

static void do_close(void (*pump)(void))
{
	/*
	 * Re-test the download guard here, not just in close_panel().
	 *
	 * close_panel() refuses while an overlay is up, but it now only LATCHES
	 * the request -- and the overlay can appear in the gap before this
	 * runs: the worker raises OTA_UI_DOWNLOADING on its own 250 ms tick and
	 * upd_timer_cb shows the overlay from the next lv_timer_handler, both
	 * of which can land after a swipe has already passed the guard.
	 *
	 * upd_timer_cb is the ONLY caller of dl_overlay_hide(). It used to die
	 * with the panel, which left a full-screen, opaque, touch-swallowing
	 * object orphaned on lv_layer_top() with nothing alive to remove it --
	 * the screen locked against every tap, user-reported 2026-07-25. The
	 * timer now outlives the panel (see upd_timer_cb), so that particular
	 * trap is gone; this guard stays because closing mid-download still
	 * tears down the panel under a live install for no gain.
	 */
	if (dl_overlay) {
		closing = false;
		return;
	}

	ui_slide_begin();
	/* upd_timer deliberately survives the panel -- see upd_timer_cb. It is
	 * created once in ui_settings_attach(). */
	upd_btn = NULL;		/* dies with the panel */
	upd_lbl = NULL;
	lv_obj_del(panel);	/* deletes confirm with it, if open */
	panel = NULL;
	confirm = NULL;
	lv_obj_update_layout(lv_screen_active());
	ui_slide_freeze(false);

	/*
	 * Same mute as do_open, and it matters more here: this uncovers the
	 * gauge screen, where a replayed RIGHT swipe is exactly what ASKS for
	 * the eye clip. Without it, closing settings could start the clip by
	 * itself.
	 */
	ui_anim_gesture_mute(UI_SLIDE_MS * 6);

	/* Settings exits right, the gauges come back in from the left. */
	ui_slide_run(UI_SLIDE_RIGHT, pump);
	ui_anim_gesture_mute(250);
	closing = false;
}

void ui_settings_drop_pending(void)
{
	want_open = false;
	want_close = false;
	closing = false;
}

void ui_settings_service(void (*pump)(void))
{
	if (want_open && panel == NULL) {
		want_open = false;
		do_open(pump);
	} else if (want_close && panel != NULL) {
		want_close = false;
		do_close(pump);
	}
	want_open = false;
	want_close = false;
}

static void close_panel(void)
{
	if (closing || dl_overlay) {	/* no closing under a download */
		return;
	}
	closing = true;
	want_close = true;	/* serviced from the mode loop; see do_close */
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

static void build_panel(lv_obj_t *parent_scr)
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
	/* Do NOT bubble: with the default GESTURE_BUBBLE the panel's own swipes
	 * travelled up to the screen (whose handler ignores gestures while the
	 * panel is open), so swipe-to-close only ever worked by accident. Stop
	 * gestures here and let panel_gesture_cb below act on them. */
	lv_obj_clear_flag(panel, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(panel, panel_gesture_cb, LV_EVENT_GESTURE, NULL);
	/* Parked. Nothing animates the panel itself any more -- the whole
	 * screen moves under it, in ui_slide_run(). */
	lv_obj_set_pos(panel, 0, 0);

	/* --- Header: green edge seam + back chevron + title + rule. The
	 * chevron used to be a full-height LEFT_MID button that the reset tiles
	 * kept covering; it now lives in the top bar (still a left-edge cue via
	 * the seam). Tap or swipe-right both go home. */
	lv_obj_t *seam = lv_obj_create(panel);

	lv_obj_set_size(seam, 3, 30);
	lv_obj_align(seam, LV_ALIGN_TOP_LEFT, 0, 0);
	lv_obj_set_style_bg_color(seam, COL_GREEN, 0);
	lv_obj_set_style_bg_opa(seam, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(seam, 0, 0);
	lv_obj_set_style_radius(seam, 0, 0);
	lv_obj_clear_flag(seam, LV_OBJ_FLAG_SCROLLABLE);

	lv_obj_t *back = lv_btn_create(panel);

	/* Drawn narrow and short (x 2..42, y 2..28), no gesture-bubble: it used
	 * to be 48 wide and sat right on top of the brightness "-" stepper, so a
	 * high tap on "-" hit the chevron and bounced you home (user-reported
	 * 2026-07-20). The steppers below start at y=39 -- an 11px gap. */
	lv_obj_set_size(back, 40, 26);
	lv_obj_set_style_bg_opa(back, LV_OPA_TRANSP, 0);
	lv_obj_set_style_shadow_width(back, 0, 0);
	lv_obj_align(back, LV_ALIGN_TOP_LEFT, 2, 2);
	/* Gestures MUST bubble from here. The extended touch area below covers
	 * the green seam and a strip of bare panel, and whatever the hit test
	 * lands on becomes the gesture's origin -- so with bubbling off, a
	 * swipe-right starting anywhere in that region died on this button
	 * instead of reaching panel_gesture_cb, silently un-closing ~814 px of
	 * a panel whose own seam comment promises the swipe works.
	 * close_panel() is idempotent (its `closing` guard), so a release that
	 * also fires back_cb costs nothing. */
	lv_obj_add_flag(back, LV_OBJ_FLAG_GESTURE_BUBBLE);
	/*
	 * 40x26 is too small to hit reliably on a resistive panel that already
	 * needs 16 averaged reads per report -- reported as "not really
	 * clickable" 2026-07-27. Grow the TOUCH area without growing the drawn
	 * button: the chevron stays visually where it belongs in the top bar,
	 * but answers to a region 12 px larger on every side. That is 64x50 nominally;
	 * 10 px of it falls off the top and left edges of the screen, so the reachable
	 * area is 54x40.
	 *
	 * Safe against the 2026-07-20 regression even though the extended area
	 * now reaches into the stepper row: lv_indev hit-tests children last to
	 * first and takes the first hit, and "minus" is created after this, so
	 * it wins wherever the two overlap. Keep it that way -- moving this
	 * button's creation below the steppers would silently restore the bug.
	 */
	lv_obj_set_ext_click_area(back, 12);
	lv_obj_add_event_cb(back, back_cb, LV_EVENT_CLICKED, NULL);

	lv_obj_t *bl = lv_label_create(back);

	lv_label_set_text(bl, LV_SYMBOL_LEFT);
	lv_obj_set_style_text_color(bl, COL_DIM, 0);
	lv_obj_center(bl);

	lv_obj_t *title = lv_label_create(panel);

	lv_label_set_text(title, "Settings");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 6);

	mk_line(panel, 30);

	/* --- Brightness: card, big opposite-corner steppers, and a stacked
	 * "Brightness / 70%" readout. The pips are gone -- the number already
	 * says the level (user-reported the panel was too crowded). --- */
	mk_card(panel, 12, 34, 296, 50);

	/* Wide steppers filling the row to a 4px edge inset (minus x16..104, plus
	 * x216..304), leaving ~112px in the centre for the value/label. Centred
	 * in the 50px card (y39..79): the card runs y34..84, a 5px margin top and
	 * bottom. The back chevron ends at y28, so the 11px gap keeps a high tap
	 * on "-" off it even at the left edge. */
	lv_obj_t *minus = mk_btn(panel, LV_SYMBOL_MINUS, COL_TRACK,
				 bright_step_cb, (void *)(intptr_t)-1);
	lv_obj_set_size(minus, 88, 40);
	lv_obj_set_style_radius(minus, 8, 0);
	lv_obj_align(minus, LV_ALIGN_TOP_LEFT, 16, 39);

	lv_obj_t *plus = mk_btn(panel, LV_SYMBOL_PLUS, COL_TRACK,
				bright_step_cb, (void *)(intptr_t)1);
	lv_obj_set_size(plus, 88, 40);
	lv_obj_set_style_radius(plus, 8, 0);
	lv_obj_align(plus, LV_ALIGN_TOP_RIGHT, -16, 39);

	lv_obj_t *blab = lv_label_create(panel);

	lv_label_set_text(blab, "Brightness");
	lv_obj_set_style_text_color(blab, COL_DIM, 0);
	lv_obj_align(blab, LV_ALIGN_TOP_MID, 0, 40);

	pct_lbl = lv_label_create(panel);
	lv_obj_set_style_text_font(pct_lbl, &lv_font_montserrat_20, 0);
	lv_obj_set_style_text_color(pct_lbl, COL_TEXT, 0);
	lv_obj_align(pct_lbl, LV_ALIGN_TOP_MID, 0, 56);
	bright_refresh();

	/* --- Software update: its own row directly under brightness, fenced
	 * by rules so it never reads as a fourth reset action --- */
	mk_line(panel, 89);

	upd_btn = lv_btn_create(panel);
	lv_obj_set_size(upd_btn, 296, 30);
	lv_obj_align(upd_btn, LV_ALIGN_TOP_LEFT, 12, 94);
	lv_obj_set_style_bg_color(upd_btn, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(upd_btn, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(upd_btn, COL_TRACK, 0);
	lv_obj_set_style_border_width(upd_btn, 1, 0);
	lv_obj_set_style_radius(upd_btn, 9, 0);
	lv_obj_set_style_shadow_width(upd_btn, 0, 0);
	lv_obj_set_style_pad_all(upd_btn, 0, 0);
	/* Same as the other controls: don't scroll its (wide) contents and don't
	 * bubble drift-gestures into a close. A horizontal drag on a SCROLLABLE
	 * button gets eaten as a scroll and the tap never fires -- that is why
	 * this row felt dead (user-reported 2026-07-20). */
	lv_obj_clear_flag(upd_btn, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(upd_btn, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(upd_btn, upd_cb, LV_EVENT_CLICKED, NULL);

	lv_obj_t *swl = lv_label_create(upd_btn);

	lv_label_set_text(swl, "Software update");
	lv_obj_set_style_text_color(swl, COL_TEXT, 0);
	lv_obj_align(swl, LV_ALIGN_LEFT_MID, 12, 0);

	lv_obj_t *swchev = lv_label_create(upd_btn);

	lv_label_set_text(swchev, LV_SYMBOL_RIGHT);
	lv_obj_set_style_text_color(swchev, COL_DIM, 0);
	lv_obj_align(swchev, LV_ALIGN_RIGHT_MID, -10, 0);

	/* The right-hand label IS the OTA state readout (driven by
	 * upd_timer_cb); "Software update" on the left never changes. */
	upd_lbl = lv_label_create(upd_btn);
	lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
	lv_obj_align(upd_lbl, LV_ALIGN_RIGHT_MID, -26, 0);

	upd_timer_cb(NULL);	/* correct the row before the first tick */

	/* --- Reset actions: divider, centred heading, three big tiles --- */
	mk_line(panel, 129);

	lv_obj_t *rlab = lv_label_create(panel);

	lv_label_set_text(rlab, "RESET");
	lv_obj_set_style_text_color(rlab, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(rlab, 2, 0);
	lv_obj_align(rlab, LV_ALIGN_TOP_MID, 0, 134);

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
	static const int tilex[] = { 12, 114, 216 };

	for (int i = 0; i < 3; i++) {
		enum action a = acts[i];
		bool danger = a == ACT_FACTORY;
		lv_obj_t *tile = lv_btn_create(panel);
		lv_color_t fg = danger ? COL_RED : COL_TEXT;

		lv_obj_set_size(tile, 92, 58);
		lv_obj_set_style_bg_color(tile,
			danger ? COL_DANGER_BG : COL_PANEL, 0);
		lv_obj_set_style_border_color(tile,
			danger ? COL_DANGER_BD : COL_TRACK, 0);
		lv_obj_set_style_border_width(tile, 1, 0);
		lv_obj_set_style_radius(tile, 12, 0);
		lv_obj_set_style_shadow_width(tile, 0, 0);
		/* Zero the theme's button padding: it shrank the 58px content box
		 * until the icon (top) and label (bottom) overprinted each other
		 * (user-reported 2026-07-20). And no gesture-bubble -> a drift on
		 * a tile can't close the panel. */
		lv_obj_set_style_pad_all(tile, 0, 0);
		lv_obj_clear_flag(tile, LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_align(tile, LV_ALIGN_TOP_LEFT, tilex[i], 152);
		lv_obj_add_event_cb(tile, act_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)a);

		lv_obj_t *ic = lv_label_create(tile);

		lv_label_set_text(ic, acticon[a]);
		lv_obj_set_style_text_color(ic, fg, 0);
		lv_obj_align(ic, LV_ALIGN_TOP_MID, 0, 8);

		lv_obj_t *tx = lv_label_create(tile);

		lv_label_set_text(tx, acttext[a]);
		lv_obj_set_style_text_color(tx, fg, 0);
		lv_obj_align(tx, LV_ALIGN_BOTTOM_MID, 0, -8);
	}

	/* Debug-me line: build + network, answered without a serial cable
	 * (which would reset the board). IP dropped at the user's request
	 * (2026-07-20); the SSID gets more room, capped so it can't overrun. */
	char line[56];
	char ip[16], ssid[CFG_SSID_MAX], psk[CFG_PSK_MAX];

	if (net_wifi_sta_ip(ip, sizeof(ip)) &&
	    cfg_get_wifi(ssid, sizeof(ssid), psk, sizeof(psk))) {
		char ssid_a[CFG_SSID_MAX];

		fmt_ascii(ssid, ssid_a, sizeof(ssid_a));
		snprintf(line, sizeof(line), "Clauge %s  |  %.20s",
			 CLAUGE_FW_VERSION, ssid_a);
	} else {
		snprintf(line, sizeof(line), "Clauge %s", CLAUGE_FW_VERSION);
	}

	lv_obj_t *info = lv_label_create(panel);

	lv_label_set_text(info, line);
	lv_obj_set_style_text_color(info, COL_DIM, 0);
	/* One line, dotted if it would run wider than the screen. Narrower than
	 * the screen so the dots land inside the right edge, not on it. */
	lv_obj_set_size(info, 296, 17);
	lv_label_set_long_mode(info, LV_LABEL_LONG_DOT);
	lv_obj_set_style_text_align(info, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(info, LV_ALIGN_BOTTOM_MID, 0, -2);

}

static void scr_gesture_cb(lv_event_t *e)
{
	lv_dir_t dir = lv_indev_get_gesture_dir(lv_indev_active());

	if (panel != NULL) {
		return;
	}
	if (ui_anim_gesture_muted()) {
		return;	/* the swipe that just closed the clip, replayed */
	}
	if (dir == LV_DIR_LEFT) {
		/* Not off the CONNECTING screen. A swipe is the ACCIDENTAL
		 * route -- see the edge zones below, which stay open on
		 * purpose. */
		if (usage_view_takeover_active()) {
			return;
		}
		want_open = true;	/* run from the mode loop; see do_open */
	} else if (dir == LV_DIR_RIGHT) {
		/* The left chevron's promise: the boot clip on loop. Only
		 * flagged here -- the mode loop runs the player from thread
		 * context, never from inside an LVGL event. */
		ui_anim_request();
	}
}

/*
 * Invisible tap strips along both screen edges: the chevrons drawn there
 * read as buttons, so tapping them must work too (tried on hardware
 * 2026-07-17). GESTURE_BUBBLE keeps the swipes alive across the strips.
 *
 * Both zones consult the gesture mute, which is not obvious: they are CLICKED
 * handlers, and a swipe is not a click. But LVGL sends CLICKED on release
 * whenever the object was pressed and nothing scrolled -- it does not suppress
 * it because it already sent GESTURE (lv_indev.c) -- so a swipe whose release
 * happens to land inside a 44x150 strip fires that strip's button as well. The
 * mute is what stops the replayed tail of a transition swipe from re-opening
 * the thing the transition just closed.
 *
 * Both routes into settings check the takeover, because there are two of them
 * and they are covered differently. The swipe arrives because usage_view marks
 * the takeover GESTURE_BUBBLE on purpose; the tap arrives because these strips
 * are created by ui_settings_attach(), which main.c runs AFTER
 * usage_view_init(), so they sit ABOVE the overlay and it never occludes them.
 *
 * The two routes are treated DIFFERENTLY while the CONNECTING bar is up (user
 * request 2026-08-18, refined the same day).
 *
 * The swipe is blocked. A board that has not connected yet has nothing in
 * settings the user can act on, and sliding away from the one screen that says
 * what it is waiting for reads as the board losing its place. That reverses
 * the intent recorded on the overlay's GESTURE_BUBBLE flag, which exists so
 * "the settings gesture is dead the whole time we're waiting for a host"
 * would not happen -- that is now the behaviour we want for swipes.
 *
 * This tap is NOT blocked, and that is deliberate. Blocking both would strand
 * the board: a WiFi join that succeeds while the fetch never does leaves the
 * bar up indefinitely with have_data false, and with no way into settings
 * there is no way to sign out or change network short of a power cycle. A tap
 * on the edge chevron is a deliberate act rather than an accidental one, so it
 * stays as the escape hatch.
 */
static void zone_settings_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (panel == NULL && !ui_anim_gesture_muted()) {
		want_open = true;
	}
}

static void zone_anim_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (panel == NULL && !ui_anim_gesture_muted()) {
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

	/* The OTA watcher runs from here on, not from the panel build: the boot
	 * prompt, the download bar and the outcome popup are all screen-level
	 * and have to work with settings shut. See upd_timer_cb. */
	if (upd_timer == NULL) {
		upd_timer = lv_timer_create(upd_timer_cb, 250, NULL);
	}
}
