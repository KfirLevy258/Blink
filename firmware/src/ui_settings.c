/*
 * The Settings panel, which today holds brightness and the software update.
 *
 * Originally it existed for recovery without a reflash: drop the network,
 * sign out, or reset everything, each one wiping a layer of saved state and
 * rebooting. All three were part of the standalone WiFi mode and were
 * removed with it on 2026-10-27 (the comment above `panel` explains why a
 * USB unit has no use for them).
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/reboot.h>
#include <zephyr/sys/printk.h>
/*
 * The gesture thresholds are per-indev fields with no public setter and no
 * Kconfig -- LVGL 9.3 hard-codes the defaults in lv_indev.c. See
 * tune_gestures() for why they cannot be left alone on this panel.
 */
#include <indev/lv_indev_private.h>
#include <lvgl.h>
#include <stdio.h>

#include "ui_settings.h"
#include "cfg_store.h"
#include "whatsnew.h"
#include "ui_boot.h"
#include "ui_anim.h"
#include "ui_swipe.h"
#include "ui_slide.h"
#include "fmt.h"
#include "version.h"
#include "backlight.h"
#include "ota.h"
#include "proto.h"
#include "upd_prompt.h"
#include "upd_row.h"
#include "usage_view.h"
#include "mem_stats.h"
#include "theme.h"
#include "ui_theme.h"

/*
 * Roles in the active theme (theme.c), read when drawn. The comments give
 * Dark's value, which is what these were as constants in 1.3.4 -- this
 * panel's green/amber/red were never the gauge's, and Dark keeps them apart.
 */
#define COL_BG		lv_color_hex(theme_pal()->bg)		/* #0E1116 */
#define COL_TRACK	lv_color_hex(theme_pal()->track)	/* #272C34 */
#define COL_TEXT	lv_color_hex(theme_pal()->text)		/* #E6E8EB */
#define COL_DIM		lv_color_hex(theme_pal()->dim)		/* #8A9199 */
#define COL_RED		lv_color_hex(theme_pal()->bad)		/* #E74C3C */
#define COL_GREEN	lv_color_hex(theme_pal()->ok)		/* #2ECC71 */
#define COL_AMBER	lv_color_hex(theme_pal()->warn)		/* #F1C40F */
/* card fill, sits above COL_BG (#161A20) */
#define COL_PANEL	lv_color_hex(theme_pal()->card)
/* the full-width section rules (#20252D) */
#define COL_LINE	lv_color_hex(theme_pal()->line)
/* factory tile: red-tinted, not solid red (#1E1412 / #7A2B23) */
#define COL_DANGER_BG	lv_color_hex(theme_pal()->danger_bg)
#define COL_DANGER_BD	lv_color_hex(theme_pal()->danger_bd)

/*
 * No destructive actions, and no confirm-then-reboot machinery behind them.
 *
 * The standalone WiFi mode had three -- drop the network, drop the sign-in
 * token, drop both -- and they were removed with it on 2026-10-27. A unit on
 * USB keeps neither a network nor a token, so none of them had anything
 * left to erase. The config record a
 * reset would wipe contains a brightness level and an OTA breadcrumb the next
 * boot clears anyway, so the whole ceremony -- a confirm dialog, a cold
 * reboot -- bought a return to 100% brightness, which the Brightness row does
 * on the spot. See the layout note in build_panel().
 */

static lv_obj_t *panel;		/* NULL when closed */
/* The software-update install dialog. */
static lv_obj_t *confirm;	/* NULL when no dialog is up */

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

/*
 * Deadline for a USB install to take the board away from us.
 *
 * Over USB the daemon answers ota_flash by closing the port and running
 * esptool, which resets this chip into the ROM loader within seconds -- so in
 * the healthy case nothing here runs again at all. The unhealthy case is a
 * daemon that dies between our consent and esptool's reset: the panel then
 * held "Keep the cable connected" forever, because nothing on the board was
 * watching for a hand-off that never came.
 *
 * Generous on purpose. A false expiry is only cosmetic -- if esptool starts
 * late the flash still succeeds and the next boot reports the new version --
 * whereas expiring early on a slow machine would call a working update failed.
 */
/* Long enough for the slowest thing that can legitimately happen before the
 * board is taken away: in a PAIR update the app downloads a ~12 MB binary,
 * self-tests it, replaces itself and reconnects, all while this screen is up
 * and no message arrives. 90 s covered a firmware-only install and would have
 * expired in the middle of that. */
#define USB_DL_DEADLINE_MS 300000
static int64_t usb_dl_deadline;

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
	/*
	 * On the green buttons ("Update now", "Yes, do it") the label wears the
	 * theme's ok_ink. It was always COL_TEXT, which is right on Dark and
	 * nowhere else: Mono's and Ink's text IS their ok colour (1.00:1), and
	 * Neon measured 1.29, Retro 1.36. tests/theme holds every theme but Dark
	 * to 4.5; Dark keeps the label it shipped with, which is 1.71:1.
	 */
	lv_obj_set_style_text_color(l, lv_color_eq(bg, COL_GREEN)
				       ? lv_color_hex(theme_pal()->ok_ink)
				       : COL_TEXT, 0);
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

/*
 * Brightness is five discrete levels -- 20/40/60/80/100, see backlight.h -- so
 * it is drawn as five stops rather than a pair of steppers. Five positions and
 * five targets means one press to reach any of them, where +/- needed four to
 * cross the range.
 */
#define BRIGHT_STOPS 5

static lv_obj_t *pct_lbl;	/* the brightness row's subtitle: "60%" */

/*
 * No "Main source" row.
 *
 * It toggled which provider owned the outer ring and the big number -- back
 * when both providers shared one gauge and one of them had to be chosen. They
 * do not share it any more: each has a page of its own, reached with a swipe,
 * and "which one is in front" is now answered by which page you are looking
 * at. A stored preference that decides the same thing a second time is a
 * second answer to a question that already has one.
 *
 * cfg_get/set_main_src stay: the value is still sent to the host on every
 * hello (proto.c), where the daemon uses it to break ties when it merges
 * sources. That is a HOST-side meaning, and it is not something to settle from
 * across the room with a fingertip.
 */
static lv_obj_t *bright_big;	/* the big readout on the brightness screen */
static lv_obj_t *bright_ov;	/* the brightness screen itself, or NULL */
static lv_obj_t *theme_ov;	/* the theme screen, or NULL */
static lv_obj_t *theme_grid;	/* its tiles' container */
static lv_obj_t *theme_edit_lbl;	/* the "Edit on computer" row's words */
static lv_obj_t *focus_ov;	/* the Focus screen, or NULL */
static lv_obj_t *focus_sub;	/* the Focus row's subtitle: "Session" */
static lv_obj_t *focus_note;	/* the Focus screen's line under its choices */
#define FOCUS_CHOICES 3
static lv_obj_t *focus_btn[FOCUS_CHOICES];
static void focus_forget(void);
static lv_obj_t *seg[BRIGHT_STOPS];

static void bright_refresh(void)
{
	uint8_t p = backlight_get();
	char b[8];

	snprintf(b, sizeof(b), "%d%%", p);
	/* Every one of these is optional: the row exists only while the panel
	 * is open, and the readout and stops only while the brightness screen
	 * is. Writing through a pointer whose object has been deleted is the
	 * NULL-deref this file has already been bitten by once. */
	if (pct_lbl) {
		lv_label_set_text(pct_lbl, b);
	}
	if (bright_big) {
		lv_label_set_text(bright_big, b);
	}
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		uint8_t lvl = 20 + i * 20;

		if (seg[i] == NULL) {
			continue;
		}
		/* Below the level reads as filled, the level itself as bright,
		 * above it as empty -- so the setting is legible as a shape,
		 * without reading the number. That matters here specifically:
		 * the screen is at its dimmest exactly when someone reaches
		 * for this control. */
		lv_obj_set_style_bg_color(seg[i],
			lvl < p ? COL_DIM : (lvl == p ? COL_TEXT : COL_TRACK), 0);
	}
}

/* --- Software update: tile state machine, confirm, progress overlay --- */

static void notice_ok_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	lv_obj_del(notice);
	notice = NULL;
}

void ui_settings_notice_dismiss(void)
{
	if (notice) {
		lv_obj_del(notice);
		notice = NULL;
	}
}

void ui_settings_notice(const char *txt)
{
	ui_settings_notice_titled(NULL, txt);
}

/* ---------------- What's new: a screen, because a box has a ceiling ------
 *
 * Lives on lv_layer_top ABOVE the notice, so Back deletes it and reveals the
 * notice underneath rather than having to rebuild it.
 *
 * Nothing here scrolls, and that is not an omission. ui_swipe.h has the
 * measurement: five deliberate swipes on this resistive panel produced
 * thirty press-release cycles, twelve of them holding fewer than two
 * samples. Paging is what this hardware can actually do.
 */
static lv_obj_t *wn;			/* NULL when closed */
static char wn_from[CFG_OTA_VER_MAX];
static char wn_to[CFG_OTA_VER_MAX];
static int wn_page, wn_pages;

#define WN_BODY_TOP 48
#define WN_BTN_Y   192

static void wn_draw(void);

static void wn_close_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (wn) {
		lv_obj_del(wn);
		wn = NULL;
	}
}

static void wn_prev_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (wn_page > 0) {
		wn_page--;
		wn_draw();
	}
}

static void wn_next_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (wn_page + 1 < wn_pages) {
		wn_page++;
		wn_draw();
	}
}

/* A pager chevron. Deliberately 44 px wide against the 120 px of Back: big
 * enough for a panel that reports a swipe as thirty separate touches. */
static lv_obj_t *wn_chev(const char *glyph, int x, bool live, lv_event_cb_t cb)
{
	/* The callback is attached either way -- mk_btn hands it straight to
	 * lv_obj_add_event_cb, which does not take NULL -- and the handlers
	 * bounds-check wn_page themselves, so a dimmed chevron is inert
	 * without a second mechanism for making it so. */
	lv_obj_t *b = mk_btn(wn, glyph, COL_TRACK, cb, NULL);

	lv_obj_set_size(b, 44, 36);
	lv_obj_align(b, LV_ALIGN_TOP_LEFT, x, WN_BTN_Y);
	if (!live) {
		/* Dimmed rather than removed: a control that vanishes at the
		 * ends of a list leaves the remaining one somewhere new every
		 * page, and on this panel that costs a mis-tap. */
		lv_obj_set_style_text_color(lv_obj_get_child(b, 0),
					    COL_LINE, 0);
	}
	return b;
}

/* Redraw the body for wn_page. The frame (title, rule, Back) is rebuilt with
 * it -- this screen is cheap and rebuilding is fewer moving parts than
 * tracking which labels belong to the last page. */
static void wn_draw(void)
{
	struct whatsnew_page pg[WHATSNEW_MAX_PAGES];
	int y = WN_BODY_TOP;

	if (!wn) {
		return;
	}
	lv_obj_clean(wn);

	lv_obj_t *title = lv_label_create(wn);

	lv_label_set_text(title, "What's new");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_LEFT, 12, 12);

	lv_obj_t *rule = lv_obj_create(wn);

	lv_obj_set_size(rule, 296, 1);
	lv_obj_align(rule, LV_ALIGN_TOP_LEFT, 12, 40);
	lv_obj_set_style_bg_color(rule, COL_LINE, 0);
	lv_obj_set_style_bg_opa(rule, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(rule, 0, 0);
	lv_obj_clear_flag(rule, LV_OBJ_FLAG_SCROLLABLE);

	wn_pages = whatsnew_paginate(wn_from, wn_to, pg,
				     (int)(sizeof(pg) / sizeof(pg[0])));
	/*
	 * Never offer a page that was not written into pg[].
	 *
	 * whatsnew_paginate returns the TRUE total on purpose, because the
	 * pager prints it -- but it only FILLS `max` of them, so a total past
	 * the end of this array would put "9 / 12" under a blank body. The
	 * host test asserts the table stays inside WHATSNEW_MAX_PAGES, so this
	 * cannot happen on a release that shipped; it is here because the cost
	 * of being wrong is a screen that describes nothing, and the clamp is
	 * one line. Under-reporting the count is the better of the two lies.
	 */
	if (wn_pages > (int)(sizeof(pg) / sizeof(pg[0]))) {
		wn_pages = (int)(sizeof(pg) / sizeof(pg[0]));
	}
	if (wn_page >= wn_pages) {
		wn_page = wn_pages ? wn_pages - 1 : 0;
	}
	/* Zero pages means no entry for this version, and pg[0] then holds
	 * nothing to read. The notice does not offer the button in that case,
	 * so this is a guard rather than a path. */
	if (wn_pages > 0) {
		const struct whatsnew_page *p = &pg[wn_page];

		for (int i = 0; i < p->count; i++) {
			int idx = p->first + i;

			if (i) {
				y += WHATSNEW_GAP_PX;
			}

			lv_obj_t *v = lv_label_create(wn);

			lv_label_set_text(v, whatsnew_version_at(idx));
			lv_obj_set_style_text_color(v, COL_DIM, 0);
			lv_obj_align(v, LV_ALIGN_TOP_LEFT, 12, y);
			y += WHATSNEW_VER_PX;

			lv_obj_t *l = lv_label_create(wn);

			lv_label_set_text(l, whatsnew_lines_at(idx));
			lv_obj_set_width(l, 296);
			lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
			lv_obj_set_style_text_color(l, COL_TEXT, 0);
			lv_obj_align(l, LV_ALIGN_TOP_LEFT, 12, y);
			/* One row per line. The copy rule in whatsnew.h keeps
			 * every line inside the 296 px width and the host test
			 * enforces it, so a line is a row and never wraps. */
			y += WHATSNEW_LINE_PX;
			for (const char *c = whatsnew_lines_at(idx); *c; c++) {
				if (*c == '\n') {
					y += WHATSNEW_LINE_PX;
				}
			}
		}
	}

	lv_obj_t *back = mk_btn(wn, "Back", COL_TRACK, wn_close_cb, NULL);

	lv_obj_set_size(back, 120, 36);
	lv_obj_align(back, LV_ALIGN_TOP_LEFT, 12, WN_BTN_Y);

	if (wn_pages > 1) {
		/* Two ints and " / ": 24 covers any page count that could
		 * exist, and -Wformat-truncation says so at 12. */
		char n[24];

		wn_chev(LV_SYMBOL_LEFT, 170, wn_page > 0, wn_prev_cb);
		wn_chev(LV_SYMBOL_RIGHT, 264, wn_page + 1 < wn_pages,
			wn_next_cb);

		lv_obj_t *num = lv_label_create(wn);

		snprintf(n, sizeof(n), "%d / %d", wn_page + 1, wn_pages);
		lv_label_set_text(num, n);
		lv_obj_set_style_text_color(num, COL_DIM, 0);
		lv_obj_align(num, LV_ALIGN_TOP_LEFT, 222, WN_BTN_Y + 10);
	}
}

/* Swipe right to leave, the same gesture that closes the settings panel.
 * Reached only because GESTURE_BUBBLE is cleared below -- the flag is what
 * decides whether this handler ever runs or the screen underneath gets the
 * swipe instead. */
static void wn_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		wn_close_cb(NULL);
	}
}

static void wn_open_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (wn) {
		return;
	}
	wn = lv_obj_create(lv_layer_top());
	lv_obj_set_size(wn, 320, 240);
	lv_obj_set_style_bg_color(wn, COL_BG, 0);
	lv_obj_set_style_bg_opa(wn, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(wn, 0, 0);
	lv_obj_set_style_pad_all(wn, 0, 0);
	lv_obj_set_style_radius(wn, 0, 0);
	lv_obj_clear_flag(wn, LV_OBJ_FLAG_SCROLLABLE);
	/*
	 * Do NOT bubble, for the reason build_panel gives and one more.
	 *
	 * In LVGL 9 every child is born GESTURE_BUBBLE, so a swipe anywhere on
	 * this full-screen overlay travelled up to the gauge screen -- whose
	 * handler happily started a slide to settings while this was still
	 * sitting on lv_layer_top. Reported from the panel on 2026-09-09: the
	 * slide begins and comes apart halfway. Every other full-screen thing
	 * in this file clears this flag; this one did not, and it is the only
	 * one that also covers the whole screen from the top layer.
	 */
	lv_obj_clear_flag(wn, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(wn, wn_gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_center(wn);
	wn_page = 0;
	wn_draw();
}

void ui_settings_whatsnew_dismiss(void)
{
	wn_close_cb(NULL);
}

/*
 * The post-update notice: a tick, the version, how much changed, and a way
 * into the detail.
 *
 * The list itself is NOT here. It was, and the argument that produced this
 * shape was about how many rows would fit in a box clamped to 230 px with
 * scrolling cleared -- where a row too many lands under the OK button, off
 * the panel, on the only control that dismisses it. The screen behind
 * "What's new" has no such ceiling and can page, so the notice gets to be
 * two lines and an answer to the only question a customer actually has
 * after an update, which is whether it worked.
 */
void ui_settings_notice_update(const char *version, const char *summary,
			       const char *from, const char *to)
{
	if (notice) {
		lv_obj_del(notice);
	}
	snprintf(wn_from, sizeof(wn_from), "%s", from ? from : "");
	snprintf(wn_to, sizeof(wn_to), "%s", to ? to : "");

	notice = lv_obj_create(lv_layer_top());
	lv_obj_set_width(notice, 300);
	lv_obj_set_height(notice, LV_SIZE_CONTENT);
	lv_obj_set_style_min_height(notice, 110, 0);
	lv_obj_set_style_max_height(notice, 230, 0);
	lv_obj_set_style_pad_all(notice, 12, 0);
	lv_obj_set_style_pad_row(notice, 12, 0);
	lv_obj_set_style_bg_color(notice, COL_BG, 0);
	lv_obj_set_style_bg_opa(notice, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(notice, COL_TRACK, 0);
	lv_obj_set_style_border_width(notice, 1, 0);
	lv_obj_clear_flag(notice, LV_OBJ_FLAG_SCROLLABLE);
	/* Same reason as the What's new screen: a swipe that lands on a modal
	 * must not reach the screen behind it and start a slide the modal is
	 * still covering. This one predates that screen and was never swiped
	 * hard enough to show it. */
	lv_obj_clear_flag(notice, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_set_flex_flow(notice, LV_FLEX_FLOW_COLUMN);
	lv_obj_set_flex_align(notice, LV_FLEX_ALIGN_CENTER,
			      LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER);

	/* Tick and version on ONE row. Stacked, they cost a row and a gap --
	 * 46 px of a 230 px box -- for no more information. */
	lv_obj_t *head = lv_obj_create(notice);

	lv_obj_set_size(head, 270, 24);
	lv_obj_set_style_bg_opa(head, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_width(head, 0, 0);
	lv_obj_set_style_pad_all(head, 0, 0);
	lv_obj_clear_flag(head, LV_OBJ_FLAG_SCROLLABLE);

	lv_obj_t *tick = lv_label_create(head);

	lv_label_set_text(tick, LV_SYMBOL_OK);
	lv_obj_set_style_text_color(tick, COL_GREEN, 0);

	lv_obj_t *ver = lv_label_create(head);

	lv_label_set_text_fmt(ver, "Updated to %s", version);
	lv_obj_set_style_text_font(ver, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(ver, COL_TEXT, 0);

	/* Measured, then centred as a pair: aligning each to the row
	 * separately would leave the gap between them at whatever the
	 * version string happened to make it. */
	lv_obj_update_layout(head);
	{
		int tw = lv_obj_get_width(tick);
		int vw = lv_obj_get_width(ver);
		int x = (270 - (tw + 8 + vw)) / 2;

		lv_obj_align(tick, LV_ALIGN_LEFT_MID, x, 0);
		lv_obj_align(ver, LV_ALIGN_LEFT_MID, x + tw + 8, 0);
	}

	if (summary && summary[0]) {
		lv_obj_t *sum = lv_label_create(notice);

		lv_label_set_text(sum, summary);
		lv_obj_set_width(sum, 270);
		lv_label_set_long_mode(sum, LV_LABEL_LONG_WRAP);
		lv_obj_set_style_text_color(sum, COL_DIM, 0);
		lv_obj_set_style_text_align(sum, LV_TEXT_ALIGN_CENTER, 0);
	}

	lv_obj_t *row = lv_obj_create(notice);

	lv_obj_set_size(row, 270, 36);
	lv_obj_set_style_bg_opa(row, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_width(row, 0, 0);
	lv_obj_set_style_pad_all(row, 0, 0);
	lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);

	/* Only when there is something behind it. A release with no entry in
	 * the table would open an empty screen. */
	if (summary && summary[0]) {
		lv_obj_t *more = mk_btn(row, "What's new", COL_BG,
					wn_open_cb, NULL);

		lv_obj_set_size(more, 128, 36);
		lv_obj_set_style_border_color(more, COL_TRACK, 0);
		lv_obj_set_style_border_width(more, 1, 0);
		lv_obj_align(more, LV_ALIGN_LEFT_MID, 0, 0);

		lv_obj_t *ok = mk_btn(row, "OK", COL_TRACK, notice_ok_cb, NULL);

		lv_obj_set_size(ok, 128, 36);
		lv_obj_align(ok, LV_ALIGN_RIGHT_MID, 0, 0);
	} else {
		lv_obj_t *ok = mk_btn(row, "OK", COL_TRACK, notice_ok_cb, NULL);

		lv_obj_set_size(ok, 120, 36);
		lv_obj_align(ok, LV_ALIGN_CENTER, 0, 0);
	}

	lv_obj_update_layout(notice);
	lv_obj_center(notice);
}

void ui_settings_notice_titled(const char *title, const char *body)
{
	if (notice) {
		lv_obj_del(notice);
	}
	/*
	 * Sized by its text, not fixed. A 130 px box held three lines; the
	 * fourth ran under the OK button (an OTA error message did exactly
	 * that on the desk, 2026-08-29). A flex column puts the button below
	 * whatever the label needs, and the height follows -- clamped to the
	 * screen, with the label scrolling inside in the absurd case.
	 */
	notice = lv_obj_create(lv_layer_top());
	lv_obj_set_width(notice, 300);
	lv_obj_set_height(notice, LV_SIZE_CONTENT);
	lv_obj_set_style_min_height(notice, 110, 0);
	lv_obj_set_style_max_height(notice, 230, 0);
	lv_obj_set_style_pad_all(notice, 12, 0);
	lv_obj_set_style_pad_row(notice, 12, 0);
	lv_obj_set_style_bg_color(notice, COL_BG, 0);
	lv_obj_set_style_bg_opa(notice, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(notice, COL_TRACK, 0);
	lv_obj_set_style_border_width(notice, 1, 0);
	lv_obj_clear_flag(notice, LV_OBJ_FLAG_SCROLLABLE);
	/* And gestures stop here. See the note on the What's new screen: in
	 * LVGL 9 these are born GESTURE_BUBBLE, so a swipe that lands on a
	 * modal travels past it and slides the screen it is covering. This is
	 * the builder every OTHER notice goes through -- the update failures,
	 * "Couldn't check for updates" -- and it had the same hole. */
	lv_obj_clear_flag(notice, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_set_flex_flow(notice, LV_FLEX_FLOW_COLUMN);
	lv_obj_set_flex_align(notice, LV_FLEX_ALIGN_CENTER, LV_FLEX_ALIGN_CENTER,
			      LV_FLEX_ALIGN_CENTER);

	/*
	 * A title, when the caller has one, in the settings screen's heading
	 * face at full brightness -- and the body under it dim.
	 *
	 * Every notice in this family was one centred sentence in one weight,
	 * which is right for one sentence and wrong the moment there are six.
	 * The update popup grew a list of what changed and read as a block of
	 * undifferentiated text: nothing said which line was the headline and
	 * which were the details, so the eye had nowhere to land. Two weights
	 * and the flex row gap do the whole job.
	 *
	 * Still CENTRED, unlike a list anywhere else. This box is 300 px on a
	 * 320 px panel seen from across a desk, and every other popup on it is
	 * centred; a left edge here would be the only one on the device.
	 */
	if (title && title[0]) {
		lv_obj_t *h = lv_label_create(notice);

		lv_label_set_text(h, title);
		lv_obj_set_width(h, 270);
		lv_label_set_long_mode(h, LV_LABEL_LONG_WRAP);
		lv_obj_set_style_text_font(h, &lv_font_montserrat_16, 0);
		lv_obj_set_style_text_color(h, COL_TEXT, 0);
		lv_obj_set_style_text_align(h, LV_TEXT_ALIGN_CENTER, 0);
	}

	lv_obj_t *l = lv_label_create(notice);

	lv_label_set_text(l, body);
	lv_obj_set_width(l, 270);
	lv_label_set_long_mode(l, LV_LABEL_LONG_WRAP);
	lv_obj_set_style_text_color(l, title && title[0] ? COL_DIM : COL_TEXT, 0);
	lv_obj_set_style_text_align(l, LV_TEXT_ALIGN_CENTER, 0);

	lv_obj_t *ok = mk_btn(notice, "OK", COL_TRACK, notice_ok_cb, NULL);

	lv_obj_set_size(ok, 120, 36);
	/* Centre once the content has a size; before layout it is 0 x 0. */
	lv_obj_update_layout(notice);
	lv_obj_center(notice);
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

/*
 * Draws the box, and nothing else. WHETHER to draw it -- the once-per-boot
 * latch, the withdrawal, and the two different reasons the latch lifts again
 * -- is upd_prompt_step() in upd_prompt.h, so that all of it can be reached
 * from a host test instead of only from a finger on the panel.
 */
static void upd_prompt_show(const struct ota_ui *snap)
{
	/* Called only for UPD_PROMPT_SHOW, which already answered every
	 * question the guard that used to sit here was asking. This is the one
	 * moment the box is really put up rather than every service tick that
	 * finds it already there. Pairs with the withdrawal line. */
	printk("[ota] offer shown: %s\n", snap->version);

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
	/* Same hole, and this is the prompt the 2026-09-09 report was about --
	 * a customer already trying to act on it. A swipe that drifts off the
	 * buttons must not slide the screen out from under the offer. */
	lv_obj_clear_flag(upd_prompt, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_center(upd_prompt);

	lv_obj_t *l = lv_label_create(upd_prompt);

	if (proto_ota_app_version()[0]) {
		/* Both halves in one tap. Saying so is not a detail: the app on
		 * the computer restarts as part of this, and someone watching
		 * the gauge should not have to guess why. */
		lv_label_set_text_fmt(l, "Version %s is available.\n"
				      "This also updates the app on your "
				      "computer.", snap->version);
	} else {
		lv_label_set_text_fmt(l, "Version %s is available.",
				      snap->version);
	}
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
		/*
		 * And gestures, most of all here.
		 *
		 * This is the screen that says "keep the cable connected" while
		 * the daemon writes slot 0 with esptool. A stray swipe bubbling
		 * past it started a slide animation on top of a flash in
		 * progress -- the one moment on this device where the panel
		 * should do nothing at all but sit still and be read.
		 */
		lv_obj_clear_flag(dl_overlay, LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_center(dl_overlay);

		dl_lbl = lv_label_create(dl_overlay);
		lv_obj_set_style_text_color(dl_lbl, COL_TEXT, 0);
		lv_obj_align(dl_lbl, LV_ALIGN_CENTER, 0, -30);

		/* Which link this is arriving on. Serial tops out near 11 KB/s
		 * against WiFi's hundreds, so a USB update legitimately takes
		 * minutes -- saying so up front is the difference between
		 * "slow" and "stuck" (user request 2026-08-20). The WiFi
		 * branch below is ota.c's HTTPS route, which no shipped build
		 * reaches since the WiFi mode went (2026-10-27); it stays with
		 * that half of ota.c. */
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
		/* Warn about the blackout explicitly. esptool resets the board
		 * into the ROM loader to write it, so the panel stops being
		 * driven at all and simply goes dark for the duration -- with
		 * no warning that reads as a crash, not an update. */
		/* Back to two. It was raised to four for a second esptool pass
		 * that read the image back off the chip -- a check write_flash
		 * had already performed by MD5, so the two minutes bought
		 * nothing and are gone again. Vague and true beats precise and
		 * wrong, and both halves of that apply to being too pessimistic. */
		lv_label_set_text(dl_sub,
				  "The screen goes dark for about 2 minutes.\n"
				  "Keep the cable connected.");
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

/* Paint one of upd_row.h's answers. The words and the colour live together
 * so a state cannot pick up the wrong pairing -- which is how "Up to date"
 * came to be drawn in green over an app that was a release behind. The label
 * budget is ~13 characters (see OTA_UI_AVAILABLE below). */
static void set_upd_row(lv_obj_t *lbl, enum upd_row row)
{
	switch (row) {
	case UPD_ROW_READY:
		lv_label_set_text(lbl, "Update ready");
		lv_obj_set_style_text_color(lbl, COL_GREEN, 0);
		break;
	case UPD_ROW_APP_OLD:
		/* Amber, not red: nothing is broken, the figures keep
		 * flowing. It is the one thing this screen cannot fix. */
		lv_label_set_text(lbl, "App is old");
		lv_obj_set_style_text_color(lbl, COL_AMBER, 0);
		break;
	case UPD_ROW_UP_TO_DATE:
		lv_label_set_text(lbl, "Up to date");
		lv_obj_set_style_text_color(lbl, COL_GREEN, 0);
		break;
	case UPD_ROW_BLANK:
	default:
		lv_label_set_text(lbl, "");
		lv_obj_set_style_text_color(lbl, COL_DIM, 0);
		break;
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

	/*
	 * The offer is an assertion about the CURRENT state, so it goes away
	 * when that state does.
	 *
	 * Nothing used to close this but the two buttons on it. It appeared
	 * on OTA_UI_AVAILABLE and then stayed on the panel through every
	 * state after it -- CHECKING, UP_TO_DATE, FAILED, and IDLE, which is
	 * where it stops changing, so "still there" meant "there for good".
	 *
	 * That is what a customer tapped on 2026-09-09. proto.c answers every
	 * `welcome` with a check, so the daemon restarting -- which is what
	 * `blink update` does -- moved the board to CHECKING and cleared
	 * ota_staged underneath a green "Update now" button that went on
	 * sitting there. This was first written up as a race between two
	 * request flags on one loop turn. It is not: the button outlives the
	 * state that justified it by thirty seconds or by the rest of the
	 * boot, and the customer is not tapping into a narrow window, they
	 * are tapping something that has been wrong for a while.
	 *
	 * Re-arming upd_prompt_done is the point of doing it here rather than
	 * in upd_prompt_close(). A prompt the USER answered must not come
	 * back -- "Later" means later, not in five seconds -- but one the
	 * board withdrew was never answered at all, so the next genuine
	 * AVAILABLE has to be allowed to ask again. Without that, a daemon
	 * restart at the wrong moment would eat the offer for the whole boot.
	 */
	{
		struct upd_prompt_turn pt = upd_prompt_step(
			snap.st, upd_prompt != NULL, upd_prompt_done,
			panel || confirm || notice || dl_overlay);

		if (pt.act == UPD_PROMPT_WITHDRAW) {
			upd_prompt_close();
			/* Logged because this is the moment the reported
			 * fault used to NOT happen, and there was no way to
			 * tell from outside whether the offer on the glass
			 * still meant anything. A support reader looking at
			 * why a customer's tap did nothing needs to see that
			 * the board had already withdrawn it. */
			printk("[ota] offer withdrawn: state is no longer available\n");
		}
		/* Applied verbatim, including the FAILED case that lifts it
		 * with no box open -- the one a tap leaves behind, and the one
		 * that made a half-finished pair update permanent. */
		upd_prompt_done = pt.latched;
		if (pt.act == UPD_PROMPT_SHOW) {
			upd_prompt_show(&snap);
		}
	}

	switch (snap.st) {
	case OTA_UI_DOWNLOADING:
		dl_overlay_show(&snap, false);
		if (ota_ui_source() == OTA_SRC_USB) {
			if (upd_seen != OTA_UI_DOWNLOADING) {
				usb_dl_deadline = k_uptime_get() +
						  USB_DL_DEADLINE_MS;
			} else if (k_uptime_get() > usb_dl_deadline) {
				ota_ui_set(OTA_UI_FAILED, NULL, 0);
				ota_ui_set_error("the computer stopped responding");
			}
		}
		break;
	case OTA_UI_REBOOTING:
		dl_overlay_show(&snap, true);
		break;
	case OTA_UI_AVAILABLE:
		break;		/* the offer is raised above, by upd_prompt_step */
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
			/*
			 * AVAILABLE counts as an update, not as a check.
			 *
			 * A consent the daemon never answered lands here
			 * WITHOUT passing through DOWNLOADING -- main.c raises
			 * FAILED straight from AVAILABLE (see upd_tap.h) --
			 * and the customer who tapped "Update now" was then
			 * told "Couldn't check for updates", an answer to a
			 * question they had not asked. It reads as the board
			 * being confused, which is worse than the fault.
			 */
			const char *base = (upd_seen == OTA_UI_DOWNLOADING ||
					    upd_seen == OTA_UI_AVAILABLE) ?
				"Update failed. The current version keeps running." :
				"Couldn't check for updates.";

			/*
			 * The prompt is let back in by upd_prompt_step()
			 * above, which lifts the latch on FAILED whether or
			 * not a box is open. It used to be done here, where
			 * no test could reach it -- and it is the rule that a
			 * tap disables, since answering the prompt closes it
			 * and leaves the withdrawal rule nothing to act on.
			 * Re-arming costs nothing: getting back to AVAILABLE
			 * still takes a fresh check that succeeds.
			 */

			/* Say which failure it was when we know. "Update
			 * failed" alone is true of a hash that did not match,
			 * a release that never downloaded and a chip we
			 * refuse to write, and the three want different
			 * answers from whoever is standing there. */
			if (snap.err[0]) {
				static char msg[160];
				/* The reasons arrive terse and lowercase --
				 * "sha256 mismatch" -- because they are also
				 * log lines. On screen they are a sentence. */
				int n = snprintf(msg, sizeof(msg), "%s\n", base);

				if (n > 0 && n < (int)sizeof(msg) - 1) {
					snprintf(msg + n, sizeof(msg) - n, "%s",
						 snap.err);
					if (msg[n] >= 'a' && msg[n] <= 'z') {
						msg[n] -= 'a' - 'A';
					}
				}
				ui_settings_notice(msg);
			} else {
				ui_settings_notice(base);
			}
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
		/* The title and chevron already say "Software update"; the
		 * subtitle is a STATUS, blank until there's something to report.
		 * A pending badge is the exception.
		 *
		 * So is an out-of-date app: the board can update itself from
		 * here, but the half running on the customer's computer cannot
		 * be reached from this screen at all, so this is the only place
		 * it can be said. Amber, not green -- nothing is broken.
		 * Budget is ~13 characters (see OTA_UI_AVAILABLE below). */
		set_upd_row(upd_lbl, upd_row_idle(ota_badge(),
						  proto_host_outdated()));
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_CHECKING:
		lv_label_set_text(upd_lbl, "Checking...");
		lv_obj_set_style_text_color(upd_lbl, COL_DIM, 0);
		lv_obj_add_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_UP_TO_DATE:
		/* "No newer firmware" is not "this product is current". See
		 * upd_row.h: saying so in green over an app a release behind
		 * is what sent a customer round in a circle -- the row said
		 * the app was old, the tap said everything was fine. */
		set_upd_row(upd_lbl, upd_row_checked(proto_host_outdated()));
		lv_obj_clear_state(upd_btn, LV_STATE_DISABLED);
		break;
	case OTA_UI_AVAILABLE:
		/* Version only. This is the row's subtitle now, 134 px under
		 * "Software update" (COL_R_W - COL_SUB_INSET), and it ellipsizes
		 * past that; when it sat beside the name in a 296 px row the
		 * size string made it 23 characters and the two overlapped
		 * (user-reported 2026-07-25). Keep any future state text within
		 * the budget "Update ready" (98 px) already fits. */
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
/*
 * A pending provider-page change, -1 or +1, 0 for none. Same reason want_open
 * exists: the page change is a wipe transition now, and ui_slide_run() must
 * not be entered from inside an LVGL event callback.
 *
 * Only the LAST direction is kept rather than a queue. Two swipes delivered
 * during a transition (input is not dispatched for its length, so they arrive
 * as a burst afterwards) should land the user one page further, not replay a
 * second 650 ms transition for a page they have already left.
 */
static volatile int want_page;
static volatile bool want_close;

/* What the panel is built from: the back control, list rows, and the
 * brightness screen. */
/*
 * The back control.
 *
 * It was 40 x 26 drawn with a 12 px extended touch area -- 54 x 40 reachable
 * once the part hanging off the screen edges is discounted, which is
 * 9.6 x 7.1 mm on this panel. The width was never the problem; 7.1 mm of
 * height is barely over the point where a resistive panel starts guessing, and
 * the DRAWN button was 40 x 26, so it looked like something you were not meant
 * to press even where it answered.
 *
 * Now 60 x 36 drawn inside a 40 px bar, with 10 px of extended area: 72 x 48
 * reachable, 12.8 x 8.5 mm. It stops exactly where the first row begins, so
 * the two never contend for a press.
 */
static lv_obj_t *mk_back(lv_obj_t *parent, lv_event_cb_t cb)
{
	lv_obj_t *b = lv_btn_create(parent);

	lv_obj_set_size(b, 60, 36);
	lv_obj_align(b, LV_ALIGN_TOP_LEFT, 6, 2);
	/*
	 * Unbordered, and it keeps the 60 x 36 hit target anyway.
	 *
	 * The border was there to make the control announce itself at 40 x 26,
	 * where an unmarked chevron read as a label. The SIZE is what fixed
	 * that -- 72 x 48 reachable, 12.8 x 8.5 mm -- and once it was big
	 * enough the box around it was a second answer to a solved problem,
	 * sitting in the one corner of the panel that should be quietest
	 * (user request 2026-08-27). The filled ground stays: it is what
	 * separates the chevron from the title rule behind it.
	 */
	lv_obj_set_style_bg_color(b, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(b, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(b, 0, 0);
	lv_obj_set_style_radius(b, 9, 0);
	lv_obj_set_style_shadow_width(b, 0, 0);
	lv_obj_set_style_pad_all(b, 0, 0);
	lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
	/* Gestures MUST bubble from here: the extended area covers the seam and
	 * a strip of bare panel, and whatever the hit test lands on becomes the
	 * gesture's origin -- with bubbling off, a swipe starting in that region
	 * died on this button instead of closing the panel. */
	lv_obj_add_flag(b, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_set_ext_click_area(b, 10);
	lv_obj_add_event_cb(b, cb, LV_EVENT_CLICKED, NULL);

	lv_obj_t *l = lv_label_create(b);

	lv_label_set_text(l, LV_SYMBOL_LEFT);
	lv_obj_set_style_text_color(l, COL_DIM, 0);
	lv_obj_center(l);
	return b;
}

/*
 * A list row: 296 x 56, which is 52.7 x 10.0 mm on a 2.8" 320x240 panel
 * (5.62 px/mm). A fingertip covers about 9 mm, so this is the smallest a row
 * can be and still be pressed on purpose rather than on average.
 *
 * Two lines, because the height is there either way and each subtitle earns
 * it: the level without opening brightness, both version numbers without
 * opening the updater, and what a factory reset does before you commit to
 * finding out.
 */
/* `h` is a parameter because the number of rows decides it: the panel's usable
 * height is shared out among however many there are, so a row is as tall as it
 * can afford to be rather than a fixed 56. See the layout note at the call
 * site. Labels align to the row's middle, so they follow whatever height it
 * is given. */
/*
 * The rows' shared styles. Each row used to carry its own copy of the same
 * seven properties, and each of its labels a colour: measured on host LVGL
 * with this file, the Focus row (a fourth row) put 1.5 KB on the settings
 * panel, and sharing them took 240 B of that back with the panel rendering
 * byte for byte the same. Re-set on every panel build, never while a row
 * exists: build_panel() only runs once the old panel is gone.
 */
static lv_style_t st_row;	/* the row itself, and its title's colour */
static lv_style_t st_row_dim;	/* its subtitle and chevron */
static bool st_row_ready;

static void row_styles(void)
{
	if (!st_row_ready) {
		lv_style_init(&st_row);
		lv_style_init(&st_row_dim);
		st_row_ready = true;
	}
	lv_style_set_bg_color(&st_row, COL_PANEL);
	lv_style_set_bg_opa(&st_row, LV_OPA_COVER);
	lv_style_set_border_color(&st_row, COL_TRACK);
	lv_style_set_border_width(&st_row, 1);
	lv_style_set_radius(&st_row, 10);
	lv_style_set_shadow_width(&st_row, 0);
	lv_style_set_pad_all(&st_row, 0);
	lv_style_set_text_color(&st_row, COL_TEXT);
	lv_style_set_text_color(&st_row_dim, COL_DIM);
}

static lv_obj_t *mk_row(lv_obj_t *parent, int y, int h, const char *title,
			const char *sub, bool danger, lv_event_cb_t cb,
			void *user, lv_obj_t **sub_out)
{
	lv_obj_t *row = lv_btn_create(parent);

	lv_obj_set_size(row, 296, h);
	lv_obj_align(row, LV_ALIGN_TOP_LEFT, 12, y);
	/* Everything rows have in common is in row_styles(), shared; only a
	 * danger row carries its own colours on top. */
	lv_obj_add_style(row, &st_row, 0);
	if (danger) {
		lv_obj_set_style_bg_color(row, COL_DANGER_BG, 0);
		lv_obj_set_style_border_color(row, COL_DANGER_BD, 0);
		lv_obj_set_style_text_color(row, COL_RED, 0);
	}
	/* Both flags off, as everywhere else on this panel: a horizontal drag
	 * on a SCROLLABLE button is eaten as a scroll and the tap never fires,
	 * and a press that drifts must not bubble into a panel close. */
	lv_obj_clear_flag(row, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(row, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(row, cb, LV_EVENT_CLICKED, user);

	lv_obj_t *t = lv_label_create(row);

	lv_label_set_text(t, title);	/* its colour is the row's, inherited */
	/* A NULL subtitle is a one-line row, and the title then sits on the
	 * row's own middle rather than 11 px above a line that is not there.
	 * No row is one-line today: the update row's version line moved out
	 * to the footer, and its state took the subtitle's place when the
	 * row went to half width for Focus. */
	lv_obj_align(t, LV_ALIGN_LEFT_MID, 14, sub ? -11 : 0);

	lv_obj_t *sl = NULL;

	if (sub != NULL) {
		sl = lv_label_create(row);
		lv_label_set_text(sl, sub);
		lv_obj_add_style(sl, &st_row_dim, 0);
		/* Width-bounded and dotted. A centred line escapes this 320 px
		 * panel around 45 characters, and two overflow bugs have
		 * shipped that way. */
		lv_obj_set_width(sl, 236);
		lv_label_set_long_mode(sl, LV_LABEL_LONG_DOT);
		lv_obj_align(sl, LV_ALIGN_LEFT_MID, 14, 11);
	}

	lv_obj_t *chev = lv_label_create(row);

	lv_label_set_text(chev, LV_SYMBOL_RIGHT);
	if (!danger) {
		lv_obj_add_style(chev, &st_row_dim, 0);
	}
	lv_obj_align(chev, LV_ALIGN_RIGHT_MID, -12, 0);

	if (sub_out) {
		*sub_out = sl;
	}
	return row;
}

/* --- The brightness screen ------------------------------------------- */

/*
 * Brightness and Focus free the settings panel's contents while they are up,
 * the way the theme screen does (see MEMORY at the theme screen): on host
 * LVGL with this file, Brightness drawn over the whole panel peaked at
 * 30,632 B once the Focus row was on it, against a board pool of 30,632
 * usable. Without the panel underneath it is gauges plus one screen: 25,496
 * for either, which is the panel's own build peak -- the sub-screens are no
 * longer the deepest thing Settings draws. (Host, Dark, two providers;
 * pointers are twice the board's, so the [mem] lines are the real figure.)
 *
 * So opening one empties the panel object -- kept, so "is settings open"
 * still answers yes and its gesture handler still runs -- and closing one
 * deletes the panel and builds it again, which is also what brings the rows
 * back showing the level and the choice just made. Both directions are
 * deferred through lv_async_call, because every caller is an event on an
 * object the change deletes: the row being tapped, or the back button.
 *
 * Every pointer into the panel is forgotten before it is emptied:
 * bright_refresh() and focus_refresh() write through them.
 */
static bool sub_closing;	/* a close is queued; see sub_close() */
static void build_panel(lv_obj_t *parent_scr);
static void show_focus(lv_event_t *e);

static void panel_empty(void)
{
	upd_btn = NULL;
	upd_lbl = NULL;
	pct_lbl = NULL;
	focus_sub = NULL;
	lv_obj_clean(panel);
}

static void sub_closed_cb(void *unused)
{
	ARG_UNUSED(unused);
	sub_closing = false;
	if (panel == NULL) {
		return;		/* the whole panel went in the meantime */
	}
	if (bright_ov != NULL) {
		/* Before the delete, so it still measures the screen at its
		 * deepest: this one, the gauges, and an empty panel. */
		mem_stats_report("brightness-close");
		bright_big = NULL;
		for (int i = 0; i < BRIGHT_STOPS; i++) {
			seg[i] = NULL;
		}
		bright_ov = NULL;
	} else if (focus_ov != NULL) {
		mem_stats_report("focus-close");
		focus_forget();
	} else {
		return;
	}
	lv_obj_t *scr = lv_obj_get_parent(panel);

	lv_obj_del(panel);
	panel = NULL;
	build_panel(scr);
}

/* Taps that land while the close is queued -- a second press of the back
 * button, a swipe after it -- find sub_closing set and do nothing more. */
static void sub_close(void)
{
	if ((bright_ov == NULL && focus_ov == NULL) || sub_closing) {
		return;
	}
	sub_closing = true;
	lv_async_call(sub_closed_cb, NULL);
}

static void bright_close(void)
{
	sub_close();
}

static void bright_close_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	bright_close();
}

static void bright_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	/* The same swipe that closes the panel closes this first. Without it
	 * the gesture would bubble past an overlay that is covering the panel
	 * and shut the whole thing, which is not what the motion looks like it
	 * should do from here. */
	if (lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		bright_close();
	}
}

static void bright_stop_cb(lv_event_t *e)
{
	backlight_set((uint8_t)(intptr_t)lv_event_get_user_data(e));
	bright_refresh();
}

static void show_bright(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (bright_ov || focus_ov || theme_ov || confirm || panel == NULL) {
		return;
	}
	panel_empty();		/* see sub_closed_cb() */
	bright_ov = lv_obj_create(panel);
	lv_obj_set_size(bright_ov, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(bright_ov, COL_BG, 0);
	lv_obj_set_style_bg_opa(bright_ov, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(bright_ov, 0, 0);
	lv_obj_set_style_radius(bright_ov, 0, 0);
	lv_obj_set_style_pad_all(bright_ov, 0, 0);
	lv_obj_clear_flag(bright_ov, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(bright_ov, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(bright_ov, bright_gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_center(bright_ov);

	mk_back(bright_ov, bright_close_cb);

	lv_obj_t *title = lv_label_create(bright_ov);

	lv_label_set_text(title, "Brightness");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

	mk_line(bright_ov, 40);

	bright_big = lv_label_create(bright_ov);
	lv_obj_set_style_text_font(bright_big, &lv_font_montserrat_20, 0);
	lv_obj_set_style_text_color(bright_big, COL_TEXT, 0);
	lv_obj_align(bright_big, LV_ALIGN_TOP_MID, 0, 50);

	/* Five stops, 56 x 116 -- 10.0 x 20.6 mm. The whole screen is spent on
	 * them because this is the one control that gets used while the panel
	 * is too dim to read. */
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		uint8_t lvl = 20 + i * 20;

		seg[i] = lv_btn_create(bright_ov);
		lv_obj_set_size(seg[i], 56, 116);
		lv_obj_align(seg[i], LV_ALIGN_TOP_LEFT, 12 + i * 60, 86);
		lv_obj_set_style_bg_opa(seg[i], LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(seg[i], 0, 0);
		lv_obj_set_style_radius(seg[i], 8, 0);
		lv_obj_set_style_shadow_width(seg[i], 0, 0);
		lv_obj_set_style_pad_all(seg[i], 0, 0);
		lv_obj_clear_flag(seg[i], LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_clear_flag(seg[i], LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_add_event_cb(seg[i], bright_stop_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)lvl);
	}

	lv_obj_t *hint = lv_label_create(bright_ov);

	lv_label_set_text(hint, "Tap a level");
	lv_obj_set_style_text_color(hint, COL_DIM, 0);
	lv_obj_align(hint, LV_ALIGN_TOP_MID, 0, 210);

	bright_refresh();	/* paints the stops and the readout */
}

/* --- The theme screen ------------------------------------------------- */

/*
 * Six slots a page, two across and three down, 144 x 56 each: 25.6 x 10.0 mm,
 * the same 10 mm height as every settings row, because that is the smallest a
 * target can be on this resistive panel and still be pressed on purpose.
 * Twelve built-ins and a custom one do not fit one screen at any size worth
 * pressing, and the panel cannot scroll reliably (ui_swipe.h has the
 * measurements: one physical swipe arrives as five or six short presses), so
 * the list pages instead, from a button in the title bar. "Edit on computer"
 * is the last slot of every page when the app can open the builder; it used
 * to be a 36 px strip under the grid, the one target on the screen below the
 * 10 mm line.
 *
 * Each tile is drawn IN its theme -- its ground, its name in its text, and a
 * chip running from its plenty colour to its almost-gone one -- so the list
 * is its own preview, and a theme that is wrong for the room is visible as
 * wrong before it is chosen.
 *
 * MEMORY. This screen used to be built over the settings panel, and it was
 * the deepest stack on the board: in the 2026-10-27 review's host harness,
 * with a gauge screen carrying two providers (25,272 B), the three trees
 * together came to 44,200 B, 5.9 KB more than Brightness -- in a 30 KB pool
 * the board had already measured at 21 KB on the gauges alone. With asserts
 * off a failed allocation is a NULL dereference, then a fault, then a reboot.
 * So opening it frees the settings panel's contents first and closing it
 * builds them again (rebuild_cb already did exactly that for a theme change),
 * and the tiles and their chips share TWO static styles, one each, for
 * everything they have in common, rather than every object carrying its own
 * copy. A second harness, one provider and a lighter gauge screen (16,216 B),
 * measures the same code in Dark at a 35,160 B peak before and 26,640 B
 * after, against Brightness's 29,232: the theme screen is no longer the
 * deepest one. Host pointers are twice the board's, so `[mem] theme-close`
 * is the board's own figure and the one to trust.
 */
#define THEME_SLOTS 6
#define THEME_TILE_W 144
#define THEME_TILE_H 56
#define THEME_TILE_GAP 6
#define THEME_GRID_Y 46
/* The name sits top-left and the chip under it, so a name has the tile's
 * whole inner width rather than what a chip beside it leaves. "Colour-blind"
 * once ended 2 px short of a chip on its right; widths are pixels, and the
 * label is clipped to this many of them whatever its text. */
#define THEME_NAME_W (THEME_TILE_W - 2 * 10)

BUILD_ASSERT(THEME_GRID_Y + 3 * THEME_TILE_H + 2 * THEME_TILE_GAP <= 240,
	     "the theme grid runs off the panel");

/*
 * "Edit on computer": the board asks, the daemon opens the builder in a
 * browser and says so. Contract 2026-10-27: ok 1 means a browser opened;
 * ok 0, or no answer inside 5 s, means the user has to start it themselves,
 * and the slot says how. One ask per 10 s, so a double tap does not open two
 * browser tabs.
 */
#define EDIT_WAIT_MS 5000
#define EDIT_COOLDOWN_MS 10000

static const char *const edit_name = "Edit on computer";
static const char *const edit_waiting = "Opening on your computer";
static const char *const edit_opened = "Opened on your computer";
static const char *const edit_run = "Run `blink theme` on your computer";

static int theme_page;		/* survives a repaint, so the page stays put */
static const char *edit_note;	/* what the slot says; NULL is its name */
static lv_timer_t *edit_timer;	/* non-NULL while waiting for the daemon */
static int64_t edit_sent_ms;
static bool edit_ever_sent;
static bool theme_closing;	/* a close is queued; see theme_close() */

/* Shared by every tile and every chip; see MEMORY above. Static, so they
 * cost no pool beyond their property arrays, allocated once. */
static lv_style_t st_tile, st_chip;

static void show_theme(bool fresh);
static void build_panel(lv_obj_t *parent_scr);

/* Every theme that can be offered, Dark first. Custom only once a usable one
 * has been saved -- an all-zero record would be a black-on-black tile. */
static int theme_list(int ids[THEME_N])
{
	struct theme_custom t;
	int n = 0;

	for (int i = 0; i < THEME_BUILTIN_N; i++) {
		ids[n++] = i;
	}
	if (ui_theme_custom_stored(&t)) {
		ids[n++] = THEME_CUSTOM;
	}
	return n;
}

/* Theme tiles a page holds: all six, or five and the Edit slot. */
static int theme_per_page(void)
{
	return proto_host_can_theme() ? THEME_SLOTS - 1 : THEME_SLOTS;
}

static int theme_pages(void)
{
	int ids[THEME_N];

	return (theme_list(ids) + theme_per_page() - 1) / theme_per_page();
}

/*
 * Was this release a tap? LVGL 9.3 sends CLICKED on release even when the
 * press became a gesture, so the back swipe -- which most people start on
 * whatever is under their thumb -- chose and STORED the theme under it on its
 * way out. Same reasoning as zone_was_a_tap(), and the same two witnesses:
 * ui_swipe, which reads the panel directly, and LVGL's own gesture.
 */
static bool tile_was_a_tap(void)
{
	return !ui_swipe_dragging() &&
	       lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_NONE;
}

static void edit_paint(void)
{
	if (theme_edit_lbl) {
		lv_label_set_text(theme_edit_lbl,
				  edit_note ? edit_note : edit_name);
	}
}

static void edit_timeout(lv_timer_t *t)
{
	ARG_UNUSED(t);
	edit_timer = NULL;	/* repeat count 1: LVGL frees it */
	edit_note = edit_run;
	edit_paint();
}

static void theme_edit_cb(lv_event_t *e)
{
	int64_t now = k_uptime_get();

	ARG_UNUSED(e);
	if (!tile_was_a_tap()) {
		return;
	}
	if (edit_ever_sent && now - edit_sent_ms < EDIT_COOLDOWN_MS) {
		/* No second browser tab, but no blank answer either: a tap
		 * here used to do nothing at all while the slot read its own
		 * name again (a visit to another page resets it). Say what the
		 * last ask came to, or that it opened, if nothing has said. */
		if (edit_note == NULL || edit_note == edit_name) {
			edit_note = edit_opened;
		}
		edit_paint();
		return;
	}
	edit_ever_sent = true;
	edit_sent_ms = now;
	proto_send_theme_edit();
	edit_note = edit_waiting;
	edit_paint();
	if (edit_timer) {
		lv_timer_reset(edit_timer);
		return;
	}
	edit_timer = lv_timer_create(edit_timeout, EDIT_WAIT_MS, NULL);
	if (edit_timer == NULL) {
		/* Out of pool. Say the fallback now rather than wait for an
		 * answer that has nothing left to land on. */
		edit_note = edit_run;
		edit_paint();
		return;
	}
	lv_timer_set_repeat_count(edit_timer, 1);
}

void ui_settings_theme_edit_reply(bool ok)
{
	if (edit_timer == NULL) {
		return;		/* nobody asked, or the 5 s already ran out */
	}
	lv_timer_delete(edit_timer);
	edit_timer = NULL;
	edit_note = ok ? edit_opened : edit_run;
	edit_paint();
}

/*
 * Leave the theme screen: delete it and build the settings panel back, both
 * from lv_async_call because every caller is an event on a child of what gets
 * deleted. Until it runs the theme screen stays up and inert.
 */
static void theme_closed_cb(void *unused)
{
	ARG_UNUSED(unused);
	theme_closing = false;
	if (panel == NULL || theme_ov == NULL) {
		return;		/* the whole panel went in the meantime */
	}
	lv_obj_t *scr = lv_obj_get_parent(panel);

	/* Measured the way brightness-close is, just before the delete. */
	mem_stats_report("theme-close");
	theme_ov = NULL;
	theme_grid = NULL;
	theme_edit_lbl = NULL;
	lv_obj_del(panel);
	panel = NULL;
	build_panel(scr);
}

static void theme_close(void)
{
	if (theme_ov == NULL || theme_closing) {
		return;
	}
	theme_closing = true;
	lv_async_call(theme_closed_cb, NULL);
}

static void theme_close_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	theme_close();
}

static void theme_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	/* As on the brightness screen: the closing swipe closes this first. */
	if (lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		theme_close();
	}
}

static void theme_pick_cb(lv_event_t *e)
{
	int id = (int)(intptr_t)lv_event_get_user_data(e);

	if (!tile_was_a_tap() || theme_closing) {
		return;
	}
	/* Choosing what is already stored and showing costs a flash write
	 * and buys nothing. */
	if (id == ui_theme_stored_id() && id == theme_active_id()) {
		return;
	}
	/* Repaints everything, this screen included -- see
	 * ui_settings_apply_theme(), which rebuilds it after this returns. */
	ui_theme_choose(id);
}

/* The shared styles, in the active theme. Re-set on every build: a style's
 * colours are values, and the theme may have changed since the last one. */
static void theme_styles(void)
{
	/* lv_style_init once only: a second init would drop the property
	 * arrays the tiles of an earlier build were given, without freeing
	 * them. The setters below overwrite values in place. */
	static bool initialised;

	if (!initialised) {
		lv_style_init(&st_tile);
		lv_style_init(&st_chip);
		initialised = true;
	}
	lv_style_set_width(&st_tile, THEME_TILE_W);
	lv_style_set_height(&st_tile, THEME_TILE_H);
	lv_style_set_bg_opa(&st_tile, LV_OPA_COVER);
	lv_style_set_border_color(&st_tile, COL_TRACK);
	lv_style_set_border_width(&st_tile, 1);
	lv_style_set_radius(&st_tile, 10);
	lv_style_set_shadow_width(&st_tile, 0);
	lv_style_set_pad_all(&st_tile, 0);

	lv_style_set_width(&st_chip, 40);
	lv_style_set_height(&st_chip, 8);
	lv_style_set_radius(&st_chip, 4);
	lv_style_set_border_width(&st_chip, 0);
	lv_style_set_bg_opa(&st_chip, LV_OPA_COVER);
	lv_style_set_bg_grad_dir(&st_chip, LV_GRAD_DIR_HOR);
}

static lv_obj_t *mk_slot(int k)
{
	lv_obj_t *tile = lv_btn_create(theme_grid);

	lv_obj_add_style(tile, &st_tile, 0);
	lv_obj_align(tile, LV_ALIGN_TOP_LEFT,
		     12 + (k % 2) * (THEME_TILE_W + 8),
		     (k / 2) * (THEME_TILE_H + THEME_TILE_GAP));
	lv_obj_clear_flag(tile, LV_OBJ_FLAG_SCROLLABLE);
	/*
	 * Gestures BUBBLE from a tile, up to theme_gesture_cb. They used to
	 * stop here, so a back swipe started on a tile -- six of them fill the
	 * screen, so most back swipes -- did nothing. tile_was_a_tap() is what
	 * keeps the same swipe from choosing the tile it started on.
	 */
	lv_obj_add_flag(tile, LV_OBJ_FLAG_GESTURE_BUBBLE);
	return tile;
}

static void theme_fill(void)
{
	int ids[THEME_N];
	int n = theme_list(ids);
	int per = theme_per_page();
	int first = theme_page * per;

	theme_edit_lbl = NULL;
	lv_obj_clean(theme_grid);
	for (int k = 0; k < per && first + k < n; k++) {
		int id = ids[first + k];
		const struct palette *p = theme_builtin(id);
		struct palette cp;

		if (p == NULL) {
			struct theme_custom t;

			ui_theme_custom_stored(&t);
			theme_derive(&t, &cp);
			p = &cp;
		}
		lv_obj_t *tile = mk_slot(k);

		lv_obj_set_style_bg_color(tile, lv_color_hex(p->bg), 0);
		/* The chosen one is ringed in THIS screen's text colour: the
		 * tile's own colours belong to its theme, and its ground can
		 * be the same as the panel's. */
		if (id == theme_active_id()) {
			lv_obj_set_style_border_color(tile, COL_TEXT, 0);
			lv_obj_set_style_border_width(tile, 2, 0);
		}
		lv_obj_add_event_cb(tile, theme_pick_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)id);

		lv_obj_t *name = lv_label_create(tile);

		lv_label_set_text(name, theme_name(id));
		lv_obj_set_style_text_color(name, lv_color_hex(p->text), 0);
		lv_obj_set_width(name, THEME_NAME_W);
		lv_label_set_long_mode(name, LV_LABEL_LONG_CLIP);
		lv_obj_align(name, LV_ALIGN_TOP_LEFT, 10, 10);

		lv_obj_t *chip = lv_obj_create(tile);

		lv_obj_add_style(chip, &st_chip, 0);
		lv_obj_set_style_bg_color(chip, lv_color_hex(p->low), 0);
		lv_obj_set_style_bg_grad_color(chip, lv_color_hex(p->high), 0);
		/* Not clickable, or a press on the chip would stop there and
		 * never reach the tile. */
		lv_obj_clear_flag(chip, LV_OBJ_FLAG_CLICKABLE);
		lv_obj_align(chip, LV_ALIGN_BOTTOM_LEFT, 10, -12);
	}

	/*
	 * Only when the app on the other end said it can open the builder
	 * ("caps":["theme"] in its welcome). A newer board talking to an
	 * older app must not offer a button that sends a message nothing
	 * will answer.
	 */
	if (proto_host_can_theme()) {
		lv_obj_t *slot = mk_slot(THEME_SLOTS - 1);

		lv_obj_set_style_bg_color(slot, COL_PANEL, 0);
		lv_obj_add_event_cb(slot, theme_edit_cb, LV_EVENT_CLICKED, NULL);
		theme_edit_lbl = lv_label_create(slot);
		lv_obj_set_width(theme_edit_lbl, THEME_NAME_W);
		lv_label_set_long_mode(theme_edit_lbl, LV_LABEL_LONG_WRAP);
		lv_obj_set_style_text_align(theme_edit_lbl, LV_TEXT_ALIGN_CENTER,
					    0);
		lv_obj_set_style_text_color(theme_edit_lbl, COL_TEXT, 0);
		lv_obj_center(theme_edit_lbl);
		edit_paint();
	}
}

static void theme_page_cb(lv_event_t *e)
{
	int pages = theme_pages();
	lv_obj_t *lbl = lv_obj_get_child(lv_event_get_target(e), 0);
	char buf[32];

	if (!tile_was_a_tap()) {
		return;		/* a swipe that began on the button */
	}

	theme_page = (theme_page + 1) % pages;
	snprintf(buf, sizeof(buf), "%d/%d " LV_SYMBOL_RIGHT, theme_page + 1,
		 pages);
	lv_label_set_text(lbl, buf);
	theme_fill();	/* the grid is a sibling of this button, not a parent */
}

/* `fresh`: a visit from the Theme row, as opposed to a repaint of one
 * already open (rebuild_cb). */
static void show_theme(bool fresh)
{
	int ids[THEME_N];
	int n = theme_list(ids);
	int pages = theme_pages();

	if (theme_ov || bright_ov || focus_ov || confirm || panel == NULL) {
		return;
	}
	if (fresh) {
		/* A fresh visit, not a repaint: start from the active theme's
		 * page and from the slot's own name -- unless an ask is still
		 * in flight, whose answer is worth seeing. */
		for (int i = 0; i < n; i++) {
			if (ids[i] == theme_active_id()) {
				theme_page = i / theme_per_page();
				break;
			}
		}
		if (edit_timer == NULL && (!edit_ever_sent ||
		    k_uptime_get() - edit_sent_ms >= EDIT_COOLDOWN_MS)) {
			edit_note = NULL;
		}
	}
	if (theme_page >= pages) {
		theme_page = 0;
	}

	/* The settings panel's contents go first; see MEMORY above. The
	 * panel object itself stays, so everything that asks "is settings
	 * open" still gets yes, and its own gesture handler still runs. */
	upd_btn = NULL;
	upd_lbl = NULL;
	pct_lbl = NULL;
	focus_forget();		/* the Focus row's subtitle goes too */
	lv_obj_clean(panel);
	theme_styles();

	theme_ov = lv_obj_create(panel);
	lv_obj_set_size(theme_ov, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(theme_ov, COL_BG, 0);
	lv_obj_set_style_bg_opa(theme_ov, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(theme_ov, 0, 0);
	lv_obj_set_style_radius(theme_ov, 0, 0);
	lv_obj_set_style_pad_all(theme_ov, 0, 0);
	lv_obj_clear_flag(theme_ov, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(theme_ov, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(theme_ov, theme_gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_center(theme_ov);

	mk_back(theme_ov, theme_close_cb);

	lv_obj_t *title = lv_label_create(theme_ov);

	lv_label_set_text(title, "Theme");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

	if (pages > 1) {
		/* The same 60 x 36 as the back control, in the opposite
		 * corner, saying which page this is and that there is more. */
		lv_obj_t *pg = lv_btn_create(theme_ov);
		char buf[32];

		lv_obj_set_size(pg, 60, 36);
		lv_obj_align(pg, LV_ALIGN_TOP_RIGHT, -6, 2);
		lv_obj_set_style_bg_color(pg, COL_PANEL, 0);
		lv_obj_set_style_bg_opa(pg, LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(pg, 0, 0);
		lv_obj_set_style_radius(pg, 9, 0);
		lv_obj_set_style_shadow_width(pg, 0, 0);
		lv_obj_set_style_pad_all(pg, 0, 0);
		lv_obj_clear_flag(pg, LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_clear_flag(pg, LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_set_ext_click_area(pg, 10);
		lv_obj_add_event_cb(pg, theme_page_cb, LV_EVENT_CLICKED, NULL);

		lv_obj_t *l = lv_label_create(pg);

		snprintf(buf, sizeof(buf), "%d/%d " LV_SYMBOL_RIGHT,
			 theme_page + 1, pages);
		lv_label_set_text(l, buf);
		lv_obj_set_style_text_color(l, COL_DIM, 0);
		lv_obj_center(l);
	}

	mk_line(theme_ov, 40);

	theme_grid = lv_obj_create(theme_ov);
	lv_obj_set_size(theme_grid, LV_HOR_RES,
			3 * THEME_TILE_H + 2 * THEME_TILE_GAP);
	lv_obj_align(theme_grid, LV_ALIGN_TOP_LEFT, 0, THEME_GRID_Y);
	lv_obj_set_style_bg_opa(theme_grid, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_width(theme_grid, 0, 0);
	lv_obj_set_style_pad_all(theme_grid, 0, 0);
	lv_obj_clear_flag(theme_grid, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(theme_grid, LV_OBJ_FLAG_CLICKABLE);
	theme_fill();
}

/*
 * The Theme row's tap. Deferred, because opening the theme screen deletes the
 * settings panel's contents -- the row this tap is still being delivered to
 * among them.
 */
static void theme_open_cb(void *unused)
{
	ARG_UNUSED(unused);
	show_theme(true);
}

static void theme_row_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	/* The row sits where a swipe to close Settings often begins. */
	if (!tile_was_a_tap()) {
		return;
	}
	if (theme_ov == NULL && bright_ov == NULL && focus_ov == NULL &&
	    confirm == NULL) {
		lv_async_call(theme_open_cb, NULL);
	}
}

/* --- The Focus screen ------------------------------------------------- */

/*
 * Off, Session or Weekly: three choices, because "on" alone is not a setting
 * -- the owner picks which window goes big (user answer 2026-10-25: chosen in
 * Settings, never automatically). Stored as the two bytes cfg_store reserved
 * for it; Off keeps the window, so turning Focus back on lands where it was.
 *
 * Three 93 x 100 buttons, 16.5 x 17.8 mm, side by side so the choice reads as
 * one control. They share two static styles instead of carrying a dozen
 * local properties each, and the panel's contents are freed while this is
 * up (see sub_closed_cb): the LVGL pool is shared with the gauge screen, and
 * the theme screen was found running it out (orchestrator review,
 * 2026-10-27).
 */
#define FOCUS_BTN_W 93
#define FOCUS_BTN_H 100
#define FOCUS_BTN_Y 56
#define FOCUS_NOTE_Y 176

BUILD_ASSERT(12 + FOCUS_CHOICES * FOCUS_BTN_W + (FOCUS_CHOICES - 1) * 8 <= 308,
	     "the Focus choices run past the right margin");
BUILD_ASSERT(FOCUS_BTN_Y + FOCUS_BTN_H < FOCUS_NOTE_Y,
	     "the Focus note sits on the choices");

static const char *const focus_words[FOCUS_CHOICES] = {
	"Off", "Session", "Weekly"
};
/* What each choice does, in a line the panel can hold (the longest is
 * 29 characters, about 200 px). */
static const char *const focus_notes[FOCUS_CHOICES] = {
	"Both windows, side by side",
	"Session big, weekly as a line",
	"Weekly big, session as a line",
};

static lv_style_t focus_st_btn;	/* every choice */
static lv_style_t focus_st_on;	/* ...and the chosen one, as LV_STATE_CHECKED */
static bool focus_styles_made;	/* both styles above hold properties */

/* 0 off, 1 session, 2 weekly: what the store says now. */
static int focus_choice(void)
{
	if (!cfg_get_focus_on()) {
		return 0;
	}
	return cfg_get_focus_win() ? 2 : 1;
}

static const char *focus_name(void)
{
	return focus_words[focus_choice()];
}

static void focus_refresh(void)
{
	int c = focus_choice();

	if (focus_sub) {
		lv_label_set_text(focus_sub, focus_words[c]);
	}
	if (focus_note) {
		lv_label_set_text(focus_note, focus_notes[c]);
	}
	for (int i = 0; i < FOCUS_CHOICES; i++) {
		if (focus_btn[i] == NULL) {
			continue;
		}
		if (i == c) {
			lv_obj_add_state(focus_btn[i], LV_STATE_CHECKED);
		} else {
			lv_obj_clear_state(focus_btn[i], LV_STATE_CHECKED);
		}
	}
}

/*
 * The styles are (re)filled each time the screen opens, never while it is
 * open: a theme change rebuilds the whole panel (rebuild_cb), so by the time
 * this runs again no object still points at them, and the colours are the
 * new theme's roles. Reset first, so a property list never grows.
 */
static void focus_styles(void)
{
	if (focus_styles_made) {
		lv_style_reset(&focus_st_btn);
		lv_style_reset(&focus_st_on);
	}
	lv_style_init(&focus_st_btn);
	lv_style_set_bg_color(&focus_st_btn, COL_PANEL);
	lv_style_set_bg_opa(&focus_st_btn, LV_OPA_COVER);
	lv_style_set_border_color(&focus_st_btn, COL_TRACK);
	lv_style_set_border_width(&focus_st_btn, 1);
	lv_style_set_radius(&focus_st_btn, 10);
	lv_style_set_shadow_width(&focus_st_btn, 0);
	lv_style_set_pad_all(&focus_st_btn, 0);
	/* Inherited by the word inside, so it needs no style of its own. */
	lv_style_set_text_color(&focus_st_btn, COL_DIM);

	/* Ringed in the text colour, as the chosen theme tile is, and its
	 * word brightened to match. */
	lv_style_init(&focus_st_on);
	lv_style_set_border_color(&focus_st_on, COL_TEXT);
	lv_style_set_border_width(&focus_st_on, 2);
	lv_style_set_text_color(&focus_st_on, COL_TEXT);
	focus_styles_made = true;
}

/* Everything on the Focus screen, and its row's subtitle, die with the panel:
 * forget them before it goes, so focus_refresh() never writes through a
 * deleted object. */
static void focus_forget(void)
{
	focus_ov = NULL;
	focus_sub = NULL;
	focus_note = NULL;
	for (int i = 0; i < FOCUS_CHOICES; i++) {
		focus_btn[i] = NULL;
	}
}

/* Deferred, and the panel is built again behind it: see sub_closed_cb(),
 * which also prints "focus-close" before anything is freed. */
static void focus_close(void)
{
	sub_close();
}

static void focus_close_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	focus_close();
}

static void focus_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	/* As on the brightness screen: the closing swipe closes this first. */
	if (lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		focus_close();
	}
}

static void focus_pick_cb(lv_event_t *e)
{
	int c = (int)(intptr_t)lv_event_get_user_data(e);
	uint8_t on = c != 0;
	/* Off keeps whichever window was last chosen. */
	uint8_t win = c == 0 ? cfg_get_focus_win() : (c == 2);

	/* A back swipe that starts on a button is a swipe: without this it
	 * stored that button's choice on the way out (review, 12 of 12 runs).
	 * Nor does a press count once the close is queued. */
	if (!tile_was_a_tap() || sub_closing) {
		return;
	}
	if (c == focus_choice()) {
		return;		/* a flash write that buys nothing */
	}
	if (cfg_set_focus(on, win) != 0) {
		printk("[focus] could not store the choice\n");
	}
	/* The gauge screen is underneath this one and changes in place, so
	 * the panel opens onto the new layout when it closes. Drawn from
	 * what was stored, so the screen never shows a choice the next boot
	 * would not. */
	usage_view_set_focus(cfg_get_focus_on(), cfg_get_focus_win());
	focus_refresh();
}

static void show_focus(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (focus_ov || bright_ov || theme_ov || confirm || panel == NULL) {
		return;
	}
	/* The panel's contents go first, as for Brightness (sub_closed_cb).
	 * Then the styles: nothing refers to them once the old screen and
	 * panel are gone. */
	panel_empty();
	focus_styles();

	focus_ov = lv_obj_create(panel);
	lv_obj_set_size(focus_ov, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(focus_ov, COL_BG, 0);
	lv_obj_set_style_bg_opa(focus_ov, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(focus_ov, 0, 0);
	lv_obj_set_style_radius(focus_ov, 0, 0);
	lv_obj_set_style_pad_all(focus_ov, 0, 0);
	lv_obj_clear_flag(focus_ov, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(focus_ov, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(focus_ov, focus_gesture_cb, LV_EVENT_GESTURE, NULL);
	lv_obj_center(focus_ov);

	mk_back(focus_ov, focus_close_cb);

	lv_obj_t *title = lv_label_create(focus_ov);

	lv_label_set_text(title, "Focus");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 10);

	mk_line(focus_ov, 40);

	for (int i = 0; i < FOCUS_CHOICES; i++) {
		lv_obj_t *b = lv_btn_create(focus_ov);

		/* The LVGL theme's default button styles come off first, so
		 * the shared pair is the whole look. */
		lv_obj_remove_style_all(b);
		lv_obj_add_style(b, &focus_st_btn, 0);
		lv_obj_add_style(b, &focus_st_on, LV_STATE_CHECKED);
		lv_obj_set_size(b, FOCUS_BTN_W, FOCUS_BTN_H);
		lv_obj_align(b, LV_ALIGN_TOP_LEFT, 12 + i * (FOCUS_BTN_W + 8),
			     FOCUS_BTN_Y);
		/* Not scrollable, or a drag is eaten as a scroll and the tap
		 * never fires. Gestures BUBBLE, as from a theme tile: the three
		 * buttons fill the middle of the screen, so most back swipes
		 * start on one, and they must reach focus_gesture_cb to close
		 * it. tile_was_a_tap() in focus_pick_cb keeps that swipe from
		 * also choosing the button it started on. */
		lv_obj_clear_flag(b, LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_add_flag(b, LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_add_event_cb(b, focus_pick_cb, LV_EVENT_CLICKED,
				    (void *)(intptr_t)i);

		lv_obj_t *l = lv_label_create(b);

		lv_label_set_text(l, focus_words[i]);
		lv_obj_center(l);
		focus_btn[i] = b;
	}

	focus_note = lv_label_create(focus_ov);
	lv_obj_set_style_text_color(focus_note, COL_DIM, 0);
	lv_obj_align(focus_note, LV_ALIGN_TOP_MID, 0, FOCUS_NOTE_Y);

	focus_refresh();
}

/*
 * The Brightness and Focus rows' taps. Deferred, like the Theme row's,
 * because opening either empties the panel, the tapped row among it. A
 * second tap before it runs queues a second open, which finds the screen
 * already up and returns.
 */
static void bright_open_cb(void *unused)
{
	ARG_UNUSED(unused);
	show_bright(NULL);
}

static void focus_open_cb(void *unused)
{
	ARG_UNUSED(unused);
	show_focus(NULL);
}

static bool sub_can_open(void)
{
	return theme_ov == NULL && bright_ov == NULL && focus_ov == NULL &&
	       confirm == NULL && !sub_closing;
}

static void bright_row_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	/* As the Theme row: a swipe that starts here is a swipe, not a tap. */
	if (tile_was_a_tap() && sub_can_open()) {
		lv_async_call(bright_open_cb, NULL);
	}
}

static void focus_row_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (tile_was_a_tap() && sub_can_open()) {
		lv_async_call(focus_open_cb, NULL);
	}
}

static void build_panel(lv_obj_t *parent_scr);

/* Thread context. The tree surgery happens frozen: creating the panel would
 * otherwise invalidate the screen, and refreshing that invalidation would
 * repaint over the very gauge pixels the scroll is about to move away. */
static void do_open(void (*pump)(void))
{
	/* The gauge screen's stretch ends here; see mem_stats.h. */
	mem_stats_report("gauges");
	ui_slide_begin();
	/* The live screen, not one cached when the gesture landed. The cached
	 * pointer outlived its object on the WiFi mode's failed-join path,
	 * where usage_view_deinit() deleted the gauge screen before
	 * provisioning. That path is gone (2026-10-27); asking the live
	 * screen costs nothing and keeps the next such path safe. */
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
	/* Built, slid in and drawn: the panel's own peak. */
	mem_stats_report("settings");
}

/*
 * Thread context, and the shortest of the three transitions to set up: the
 * page change edits the widgets that are already on screen rather than
 * building or deleting a tree, so the "incoming screen" is the same objects
 * carrying the other provider's numbers.
 *
 * Frozen for the edit, exactly like do_open: render_gauges() invalidates
 * everything it retexts, and refreshing that would repaint the destination
 * over the whole screen at once -- leaving the wipe nothing to reveal.
 */
/*
 * The page change is not a transition any more.
 *
 * Three were tried on this axis. A cut did not read as a swipe. A wipe read as
 * a repaint, because the two pages are the same layout and the boundary
 * between them has nothing to be made of. A wipe with a bright leading edge
 * gave that boundary something to see and still did not feel natural -- which
 * it was not: a bar sweeping the panel is an object that exists nowhere else
 * on this device and means nothing when it arrives.
 *
 * All three were transitions between two PICTURES. This is an instrument, and
 * the motion that belongs to one is the needle moving. usage_view_page_step()
 * now animates the rings from the reading they were showing to the other
 * provider's, and nothing is covered or revealed at all. See the note above it.
 *
 * So this does not block, does not touch the display, does not need the strip
 * machinery, and does not need `pump` -- an LVGL animation runs from
 * lv_timer_handler like everything else on the screen. It stays a mode-loop
 * request rather than moving back into the swipe callback because the
 * can_page() re-check there still matters: a usage message can remove the
 * second provider in the gap between the swipe and this.
 */
static void do_page(int delta, void (*pump)(void))
{
	ARG_UNUSED(pump);

	usage_view_page_step(delta);

	/*
	 * NO MUTE. This used to swallow the next swipe for 250 ms and that is
	 * what "working sometimes but not sometimes" was: of five vertical
	 * strokes logged on 2026-08-28, five fired and only FOUR changed the
	 * page. The fifth landed inside the mute and was dropped in silence.
	 *
	 * The mute exists to absorb the tail of a swipe REPLAYED after a
	 * blocking transition -- input is not dispatched while ui_slide_run
	 * holds the display, so it arrives in a burst afterwards. Neither half
	 * of that is true here any more. A page change is an animation and
	 * blocks nothing, so no burst accumulates; and ui_swipe reads the
	 * panel directly rather than LVGL's queue, so there is no replay to
	 * absorb in the first place. It also fires at most once per stroke and
	 * needs a genuinely new one -- reports stopping for a whole re-arm
	 * window -- before it can fire again, which is the actual protection
	 * against a stroke counting twice.
	 *
	 * So the mute was guarding against something that cannot happen, at
	 * the cost of the thing people do most: page back and forth.
	 */
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

	/* Everything shown since "settings", the brightness screen included
	 * when it was opened and closed in between. */
	mem_stats_report("settings-close");
	ui_slide_begin();
	/* upd_timer deliberately survives the panel -- see upd_timer_cb. It is
	 * created once in ui_settings_attach(). */
	upd_btn = NULL;		/* dies with the panel */
	upd_lbl = NULL;
	pct_lbl = NULL;
	bright_big = NULL;
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		seg[i] = NULL;
	}
	theme_ov = NULL;	/* these die with the panel too */
	theme_grid = NULL;
	theme_edit_lbl = NULL;
	focus_forget();
	lv_obj_del(panel);	/* deletes confirm and any overlay with it */
	panel = NULL;
	confirm = NULL;
	bright_ov = NULL;
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
	want_page = 0;
	closing = false;
}

/*
 * Somebody is working this panel -- it is open, or a gesture has asked for
 * something and no mode loop has run it yet.
 *
 * The latch half is the part that is easy to get wrong. A gesture never opens
 * anything itself; it raises one of these flags and ui_settings_service(),
 * which only a mode loop calls, runs the transition from thread context. So
 * a latch is not a record of a past intention, it is a request still waiting
 * to be answered, and any code that takes the mode loop away from it -- the
 * doze in ui_sleep.c does exactly that -- has to know the request is there.
 * Otherwise the swipe does nothing, and then does something worse: the flag
 * sits until the board next wakes on its own and the panel slides open by
 * itself, hours later, in front of nobody.
 */
bool ui_settings_busy(void)
{
	return panel != NULL || want_open || want_close || want_page != 0;
}

void ui_settings_service(void (*pump)(void))
{
	if (want_open && panel == NULL) {
		want_open = false;
		do_open(pump);
	} else if (want_close && panel != NULL) {
		want_close = false;
		do_close(pump);
	} else if (want_page != 0 && panel == NULL) {
		/*
		 * Last, and only with the panel closed. A page change is the
		 * least important of the three, and re-checking can_page here
		 * matters: the flag was set when the gesture landed, and a
		 * usage message can have removed the second provider in the
		 * gap -- which would run a transition to a page that no longer
		 * exists.
		 */
		int step = want_page;

		want_page = 0;
		if (usage_view_can_page(step)) {
			do_page(step, pump);
		}
	}
	want_open = false;
	want_close = false;
	want_page = 0;
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
	/* bright_ov covers the panel and carries its own back control, so this
	 * one is unreachable while it is up -- but the extended touch area
	 * reaches past the overlay's edge, and a press landing there must not
	 * close the panel out from under it. */
	if (confirm == NULL && bright_ov == NULL && theme_ov == NULL &&
	    focus_ov == NULL) {
		close_panel();
	}
}

static void panel_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (confirm == NULL && bright_ov == NULL && theme_ov == NULL &&
	    focus_ov == NULL &&
	    lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_RIGHT) {
		close_panel();
	}
}

/*
 * Rebuild the open panel in the active theme, on the screen it is open on.
 *
 * Rebuilt, not restyled: unlike the gauge screen this panel holds no readings
 * -- every label on it is computed when it is built -- so building it again
 * IS the restyle, with none of the bookkeeping. Asynchronous because the most
 * common caller is a tap on a theme tile, i.e. an event handler running on an
 * object this deletes.
 *
 * Skipped while a confirm dialog is up (the install prompt would vanish under
 * the user's finger) or the panel is closing; the next open is built in the
 * new theme either way.
 */
static void rebuild_cb(void *unused)
{
	ARG_UNUSED(unused);
	if (panel == NULL || closing || confirm != NULL) {
		return;
	}
	bool theme_was_open = theme_ov != NULL;
	bool bright_was_open = bright_ov != NULL;
	bool focus_was_open = focus_ov != NULL;
	lv_obj_t *scr = lv_obj_get_parent(panel);

	upd_btn = NULL;
	upd_lbl = NULL;
	pct_lbl = NULL;
	bright_big = NULL;
	for (int i = 0; i < BRIGHT_STOPS; i++) {
		seg[i] = NULL;
	}
	theme_ov = NULL;
	theme_grid = NULL;
	theme_edit_lbl = NULL;
	bright_ov = NULL;
	focus_forget();
	lv_obj_del(panel);
	panel = NULL;
	build_panel(scr);
	if (theme_was_open) {
		show_theme(false);
	} else if (bright_was_open) {
		show_bright(NULL);
	} else if (focus_was_open) {
		show_focus(NULL);
	}
}

/*
 * The top layer, which outlives the panel and so is not rebuilt with it.
 *
 * What's new redraws its page into the same screen; the update prompt and the
 * download screen are taken down and put back from the live OTA state by the
 * functions that always put them up. The plain notice keeps its colours until
 * it is dismissed: it holds text nothing here kept a copy of, and it is the
 * one that is dismissed in a tap.
 */
static void repaint_top_cb(void *unused)
{
	struct ota_ui snap;

	ARG_UNUSED(unused);
	if (wn) {
		lv_obj_set_style_bg_color(wn, COL_BG, 0);
		wn_draw();
	}
	if (upd_prompt) {
		ota_ui_get(&snap);
		upd_prompt_close();
		upd_prompt_show(&snap);
	}
	if (dl_overlay) {
		dl_overlay_hide();
		upd_timer_cb(NULL);	/* the state that showed it shows it again */
	}
}

void ui_settings_apply_theme(void)
{
	if (panel != NULL) {
		lv_async_call(rebuild_cb, NULL);
	}
	if (wn || upd_prompt || dl_overlay) {
		lv_async_call(repaint_top_cb, NULL);
	}
}

static void build_panel(lv_obj_t *parent_scr)
{
	row_styles();
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

	/*
	 * Two rows and nothing else.
	 *
	 * The panel is 240 px tall and a fingertip covers about 9 mm, which is
	 * 51 px at this panel's 5.62 px/mm. Take 40 for the title bar and 200
	 * is left. There were three rows at 56 px (10.0 mm) while a "Reset to
	 * defaults" row existed; it does not any more, because in this build
	 * the only thing it reset was brightness -- and the row directly above
	 * it sets brightness without a reboot.
	 *
	 * The freed height goes into the two that remain rather than being
	 * left as a hole under them: 72 px each, 12.8 mm, with 24 px of margin
	 * above and below the pair so it reads as centred instead of
	 * top-aligned with something missing.
	 *
	 * What this replaces was measured and did not pass: 30 px rows are
	 * 5.3 mm, sitting 5 px apart, so one press covered both and which one
	 * fired came down to where the pressure centroid landed.
	 */
/*
 * 56 px is 10.0 mm against a ~9 mm fingertip -- the smallest a row can be and
 * still be pressed on purpose rather than on average.
 *
 * TWO rows now, brightness and software update: the main-source row went when
 * the providers got a page each. The height it freed is not redistributed. It
 * goes to the footer, which is the version pair and the one instruction this
 * device needs to give -- both of them things you read rather than press, and
 * neither of which belongs inside a button.
 */
#define ROW_H 56
#define ROW_TOP 56		/* first row, clear of the title rule at y=40 */
#define ROW_GAP 8

/* The two columns: Brightness and Focus on the left, Theme and Software
 * update on the right. See the layout note in build_panel(). */
#define COL_L_W 120
#define COL_R_W 168
#define COL_R_X (12 + COL_L_W + ROW_GAP)
/* A subtitle starts 14 px in and must stop short of the chevron. */
#define COL_SUB_INSET 34

BUILD_ASSERT(COL_L_W + ROW_GAP + COL_R_W == 296,
	     "the two settings columns no longer fill the 296 px row");

/*
 * The footer: two centred lines under the last row, on the panel itself.
 *
 * The version pair used to be the update row's subtitle, which put a fact
 * inside a control -- so reading the version meant looking at a button, and
 * the button's own state ("Update ready") had to be squeezed in beside it,
 * right-aligned and clear of a chevron. Separating them gives the button one
 * job and the footer room for the second line, which is the thing an update
 * actually needs someone to know.
 */
#define FOOT_Y1 192
#define FOOT_Y2 212

/*
 * Two rows and a two-line footer, inside 240. Checked here rather than
 * eyeballed, because the last time these moved a row went off the bottom of a
 * 240 px screen and nobody noticed until it was flashed. FONT_LINE_H is 16 for
 * the default montserrat_14, so the second line ends at 228.
 */
BUILD_ASSERT(ROW_TOP + 2 * ROW_H + ROW_GAP <= FOOT_Y1,
	     "the settings rows now overlap the footer");
BUILD_ASSERT(FOOT_Y2 + 16 <= 240,
	     "the settings footer no longer fits on the panel");
	/* No green edge seam here. It existed as a left-edge cue back when the
	 * back control was a bare chevron that was easy to miss; the control is
	 * now a bordered 60 x 36 button that announces itself, so the seam was
	 * a second hint for something that no longer needs one -- and green is
	 * the gauge's "live" colour, which it was quietly spending. */
	mk_back(panel, back_cb);

	lv_obj_t *title = lv_label_create(panel);

	lv_label_set_text(title, "Settings");
	lv_obj_set_style_text_font(title, &lv_font_montserrat_16, 0);
	lv_obj_set_style_text_color(title, COL_TEXT, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 11);

	mk_line(panel, 40);

	char sub[56];

	/*
	 * Four controls in two rows, a narrow column and a wide one.
	 *
	 * There is no third row to give anything: two rows and the two-line
	 * footer already use the 240 px (the BUILD_ASSERTs above). Splitting
	 * the WIDTH keeps every target at the 56 px -- 10.0 mm -- height this
	 * panel needs. Brightness and Theme shared the first row at 144 px each
	 * until Focus needed a place; then the split moved to 120 | 168,
	 * because the right-hand titles are the long ones. "Software update"
	 * measures 121 px in montserrat_14 (lv_text_get_size on host), and a
	 * 144 px half puts its last letters under the chevron at 123; 168 holds
	 * it with the chevron clear. The narrow column is the tight one now:
	 * "Brightness" ends 4 px short of its chevron (review, 2026-10-27), so
	 * a longer word there would need the split moved back. "Focus" (43 px)
	 * has room, and Theme's wider subtitle now holds "Colour-blind safe"
	 * whole.
	 */
	snprintf(sub, sizeof(sub), "%d%%", backlight_get());
	lv_obj_t *half = mk_row(panel, ROW_TOP, ROW_H, "Brightness", sub, false,
				bright_row_cb, NULL, &pct_lbl);

	lv_obj_set_width(half, COL_L_W);
	lv_obj_set_width(pct_lbl, COL_L_W - COL_SUB_INSET);

	lv_obj_t *tsub;

	half = mk_row(panel, ROW_TOP, ROW_H, "Theme",
		      theme_name(theme_active_id()), false, theme_row_cb, NULL,
		      &tsub);
	lv_obj_set_width(half, COL_R_W);
	lv_obj_align(half, LV_ALIGN_TOP_LEFT, COL_R_X, ROW_TOP);
	lv_obj_set_width(tsub, COL_R_W - COL_SUB_INSET);

	half = mk_row(panel, ROW_TOP + (ROW_H + ROW_GAP), ROW_H, "Focus",
		      focus_name(), false, focus_row_cb, NULL, &focus_sub);
	lv_obj_set_width(half, COL_L_W);
	lv_obj_set_width(focus_sub, COL_L_W - COL_SUB_INSET);

	/*
	 * The row's state is its subtitle now -- "Up to date", "Install
	 * 1.4.0" -- where it sat beside the name while the row had 296 px.
	 * Same label, same words and colours (set_upd_row), one line lower.
	 * "Update ready" is 98 px, inside the 134 the subtitle has.
	 */
	upd_btn = mk_row(panel, ROW_TOP + (ROW_H + ROW_GAP), ROW_H,
			 "Software update", "", false, upd_cb, NULL, &upd_lbl);
	lv_obj_set_width(upd_btn, COL_R_W);
	lv_obj_align(upd_btn, LV_ALIGN_TOP_LEFT, COL_R_X,
		     ROW_TOP + (ROW_H + ROW_GAP));
	lv_obj_set_width(upd_lbl, COL_R_W - COL_SUB_INSET);

	/*
	 * Both versions, on the panel rather than in the button.
	 *
	 * This is the only place on the device that can say which HALF of the
	 * pair is behind -- the app's version is otherwise invisible from the
	 * panel -- so it says both, or says the board's alone when no host has
	 * introduced itself.
	 */
	lv_obj_t *ver = lv_label_create(panel);

	if (proto_host_version()[0]) {
		snprintf(sub, sizeof(sub), "Blink %s  |  App %s",
			 BLINK_FW_VERSION, proto_host_version());
	} else {
		snprintf(sub, sizeof(sub), "Blink %s", BLINK_FW_VERSION);
	}
	lv_label_set_text(ver, sub);
	lv_obj_set_style_text_color(ver, COL_DIM, 0);
	lv_obj_align(ver, LV_ALIGN_TOP_MID, 0, FOOT_Y1);

	/*
	 * The one instruction this device gives, and it is here because this
	 * is the screen where it matters: an update writes a new image over
	 * USB and a board that loses power halfway is a board that has to be
	 * recovered with a cable anyway. Stated as a standing condition rather
	 * than fired as a warning mid-download -- by the time a progress bar
	 * could say it, unplugging has already happened.
	 */
	lv_obj_t *keep = lv_label_create(panel);

	lv_label_set_text(keep, "Keep the cable connected");
	lv_obj_set_style_text_color(keep, COL_DIM, 0);
	lv_obj_align(keep, LV_ALIGN_TOP_MID, 0, FOOT_Y2);

	upd_timer_cb(NULL);	/* correct the row before the first tick */
}

/*
 * A stroke in progress, for the rail to draw.
 *
 * Guarded by the same three conditions the completed stroke is, and for the
 * same reasons: with the panel open the gauge screen is not visible, and
 * during the mute the page has just changed and the rail is already saying so.
 * Showing a preview in either case would be the indicator contradicting the
 * screen.
 *
 * Horizontal strokes report nothing. They mean settings and the boot clip,
 * which are not page changes, and growing a rail mark for one would promise
 * something that is not about to happen.
 */
/*
 * Is something modal on lv_layer_top waiting to be answered?
 *
 * The screen must not move out from under it. Clearing GESTURE_BUBBLE on
 * those objects does NOT achieve this and it is worth writing down why: that
 * flag governs LVGL's own gesture dispatch, and screen navigation on this
 * board does not use it. ui_swipe reads the touch device directly, because
 * LVGL's detector cannot survive a panel that reports one physical swipe as
 * five or six short presses -- ui_swipe.h has the measurements. So a swipe
 * bound for settings never passes through the LVGL bubbling at all, and a
 * modal that stops gestures there is still swiped straight off the screen.
 *
 * Reported from the panel on 2026-09-10, after a first fix that sealed every
 * modal against the wrong mechanism.
 *
 * `panel` is checked separately by both callers: it is a screen rather than
 * an overlay, and it has its own swipe-to-close.
 */
static bool modal_up(void)
{
	return notice != NULL || wn != NULL || upd_prompt != NULL ||
	       dl_overlay != NULL;
}

static void swipe_progress_cb(enum ui_swipe_dir dir, int pct)
{
	int delta = 0;

	/* No page preview under a modal either: the rail would grow a mark
	 * promising a page change that modal_up() is about to refuse. */
	if (panel == NULL && !modal_up() && !ui_anim_gesture_muted()) {
		if (dir == UI_SWIPE_UP) {
			delta = 1;
		} else if (dir == UI_SWIPE_DOWN) {
			delta = -1;
		}
	}
	usage_view_page_preview(delta, delta ? pct : 0);
}

/*
 * A completed stroke, from ui_swipe rather than from LVGL.
 *
 * LVGL's own gesture detector cannot survive this panel -- one physical swipe
 * arrives as five or six short presses and it resets its accumulator on every
 * one of them. The measurements and the reasoning are in ui_swipe.h. What
 * reaches here is one event per stroke, already stitched across the dropouts
 * and already refused if it was too diagonal to call.
 *
 * Runs on the LVGL thread from a timer, so the rules that governed the gesture
 * callback still govern this: flag the request, never run a transition here.
 */
static void swipe_cb(enum ui_swipe_dir dir)
{
	/*
	 * A modal owns the screen until it is answered -- with one exception,
	 * which is the full-screen one: What's new leaves on a right swipe,
	 * the same stroke that closes settings. Routed through here rather
	 * than through an LV_EVENT_GESTURE handler because this is the
	 * detector that actually works on this hardware.
	 */
	if (wn != NULL) {
		if (dir == UI_SWIPE_RIGHT) {
			wn_close_cb(NULL);
		}
		return;
	}
	if (modal_up()) {
		return;
	}
	if (panel != NULL) {
		return;
	}
	if (ui_anim_gesture_muted()) {
		return;	/* the swipe that just closed the clip, replayed */
	}
	if (dir == UI_SWIPE_LEFT) {
		/* Not off the CONNECTING screen. A swipe is the ACCIDENTAL
		 * route -- see the edge zones below, which stay open on
		 * purpose. */
		if (usage_view_takeover_active()) {
			return;
		}
		want_open = true;	/* run from the mode loop; see do_open */
	} else if (dir == UI_SWIPE_RIGHT) {
		/* The left chevron's promise: the boot clip on loop. Only
		 * flagged here -- the mode loop runs the player from thread
		 * context, never from inside an LVGL event. */
		ui_anim_request();
	} else if (dir == UI_SWIPE_UP || dir == UI_SWIPE_DOWN) {
		/*
		 * The provider stack. Content follows the finger, the way a
		 * list does: swiping UP pulls the next page in from below.
		 *
		 * Flagged for the mode loop like the two above, and for the
		 * same reason: this became a wipe transition when the cut was
		 * judged not to read as a swipe, and ui_slide_run() owns
		 * lv_refr_now() and cannot be re-entered from inside
		 * lv_timer_handler(). It used to run right here, back when a
		 * page change was one repaint.
		 *
		 * Asked BEFORE flagging, not after: arming a transition that
		 * cannot move is 650 ms of frozen panel for nothing, and with
		 * one provider reporting -- or during the CONNECTING takeover,
		 * where there is no data and so only one page -- that is every
		 * vertical swipe there is.
		 */
		int step = (dir == UI_SWIPE_UP) ? 1 : -1;

		/*
		 * At the end of the stack, a vertical swipe goes the only way
		 * it can.
		 *
		 * Up is "next" because content follows the finger, and that is
		 * a defensible model right up until someone uses it. Across
		 * three builds on 2026-08-27 every vertical stroke the user
		 * made went DOWN -- including after the cue was corrected to
		 * point up -- and on page 0 down asks for a page that does not
		 * exist, so nothing happened, six times.
		 *
		 * The device has TWO pages. "Which direction is forwards" is a
		 * question a two-item stack does not really have, and the ask
		 * was "a swipe down/up will switch the mode", not "a swipe up
		 * advances an ordered list". So when the requested direction
		 * has nowhere to go and the other one does, take the other one.
		 *
		 * This is not a wrap. In the middle of a longer stack both
		 * directions are available and each still does its own thing;
		 * only an end, where one of them is dead, hands over.
		 */
		if (!usage_view_can_page(step) && usage_view_can_page(-step)) {
			step = -step;
		}
		if (usage_view_can_page(step)) {
			want_page = step;
		}
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
 * This tap is NOT blocked. It was kept as the escape hatch for the standalone
 * WiFi mode: a WiFi join that succeeded while the fetch never did left the
 * bar up indefinitely with have_data false, and with no way into settings
 * there was no way to sign out or change network short of a power cycle.
 * That mode was removed on 2026-10-27, which leaves the tap with a weaker
 * reason -- the software-update row and the version are still worth reaching
 * from a board that is waiting -- and it was left alone rather than
 * re-decided, because the removal was meant to change no shipped byte. A tap
 * on the edge chevron is a deliberate act rather than an accidental one.
 */
/*
 * A tap on an edge zone -- but only if it was a tap.
 *
 * LVGL sends CLICKED on release whenever an object was pressed and nothing
 * scrolled. It does not suppress it because the touch turned out to be a
 * swipe, so a swipe that begins or ends inside one of these 44x150 strips
 * fires the strip's button as well as the swipe. On the gauge screen that
 * means a vertical swipe near an edge opens the settings panel, which is half
 * of what "some of them is detected as swipe to settings" was: not a misread
 * direction, but a stray button press left behind by one.
 *
 * The mute does not cover this. It is set when a transition STARTS, and this
 * arrives on release -- before ui_swipe has even decided whether the stroke
 * was a swipe. ui_swipe_dragging() answers from the stroke still in progress,
 * which is the only thing that knows in time.
 */
static bool zone_was_a_tap(void)
{
	/*
	 * modal_up() is the term this was missing, and the geometry is why it
	 * took two wrong fixes to find.
	 *
	 * These zones live on the gauge screen, 44 px wide at each edge. The
	 * What's new screen is a full 320x240 and physically covers them, so
	 * it looked fixed. The notice is 300 px wide and centred, which
	 * leaves a 10 px strip of live edge zone down each side -- and on a
	 * resistive panel a stroke that breaks contact arrives as a press,
	 * which is exactly what ui_swipe_dragging() is unable to call a drag.
	 * So a swipe across the notice opened settings underneath it, while
	 * the same swipe across What's new did nothing, and the difference
	 * was 20 px of width rather than anything either fix had touched.
	 *
	 * Reported twice from the panel on 2026-09-10. The first fix sealed
	 * LVGL's gesture bubbling, which screen navigation does not use; the
	 * second taught swipe_cb about modals, which is right and is not the
	 * route a tap takes. This is the third and it is the one under the
	 * finger.
	 */
	return panel == NULL && !modal_up() && !ui_anim_gesture_muted() &&
	       !ui_swipe_dragging();
}

static void zone_settings_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (zone_was_a_tap()) {
		want_open = true;
	}
}

static void zone_anim_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (zone_was_a_tap()) {
		ui_anim_request();
	}
}

/*
 * Tapping the rail changes page, which is what makes up/down as reliable as
 * left/right finally are.
 *
 * The two horizontal gestures have had a tap path since the beginning: a
 * chevron drawn at each edge with an invisible 44x150 zone behind it. That is
 * the ONLY reason they feel dependable -- measured on 2026-08-28 they miss at
 * about the same rate as the vertical one (a 32 px horizontal stroke was
 * refused in the same session that refused 17, 19 and 22 px vertical ones).
 * The difference is that a missed horizontal swipe leaves a chevron to press,
 * and a missed vertical one left nothing at all, so every miss was a dead end.
 *
 * A resistive panel is good at presses and bad at slides, and that is not
 * something thresholds can fix: every reliability problem in this file's
 * history traced to contact pressure breaking under a MOVING finger. The swipe
 * stays, because it works for a committed stroke and it is the faster way once
 * you know it. It is just no longer the only way.
 *
 * No new furniture. The zone sits over the band the rail and the provider name
 * already occupy, so what you press is what was already telling you there was
 * somewhere to go -- exactly the arrangement the edge chevrons have, where the
 * drawn thing is the affordance and the hit area is invisible and much larger
 * than it.
 *
 * 200 x 44 is 35.6 x 7.8 mm. Wider than it looks like it needs to be because
 * the thing being aimed at is 6 px tall, and the cost of overshooting is
 * nothing: below the countdowns there is only this.
 */
#define PAGE_ZONE_W	200
#define PAGE_ZONE_H	44

static void zone_page_cb(lv_event_t *e)
{
	ARG_UNUSED(e);

	if (!zone_was_a_tap()) {
		return;
	}
	/*
	 * Same end-of-stack rule as the swipe, so the two controls cannot
	 * disagree about where a tap goes: try forwards, and take backwards if
	 * forwards has nowhere to go. With two pages that is simply "the other
	 * one".
	 */
	if (usage_view_can_page(1)) {
		want_page = 1;
	} else if (usage_view_can_page(-1)) {
		want_page = -1;
	}
}

static void mk_page_zone(lv_obj_t *scr)
{
	lv_obj_t *z = lv_btn_create(scr);

	lv_obj_set_size(z, PAGE_ZONE_W, PAGE_ZONE_H);
	lv_obj_set_style_bg_opa(z, LV_OPA_TRANSP, 0);
	lv_obj_set_style_shadow_width(z, 0, 0);
	lv_obj_align(z, LV_ALIGN_BOTTOM_MID, 0, 0);
	/* A swipe that starts here must still reach the screen, or putting a
	 * target under the rail would kill the gesture it is meant to back up. */
	lv_obj_add_flag(z, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_add_event_cb(z, zone_page_cb, LV_EVENT_CLICKED, NULL);
	/* Behind everything: it is a hit area, not a surface, and the rail and
	 * the name have to keep drawing over it. */
	lv_obj_move_background(z);
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

/*
 * Loosen LVGL's own gesture thresholds, for the two places still using them.
 *
 * The gauge screen does not any more -- see swipe_cb and ui_swipe.h. But the
 * settings panel and the brightness overlay both close on a swipe, and those
 * still go through LVGL, so its two hard-coded defaults still apply there
 * (lv_indev.c, no Kconfig and no setter, which is why this reaches into the
 * private header).
 *
 *   gesture_min_velocity  3   a sample that moved less than this in BOTH axes
 *                             ZEROES the accumulator. Not "ignore this
 *                             sample" -- it throws away everything counted so
 *                             far. LVGL samples faster than this panel
 *                             reports, so a large share of ticks see no new
 *                             point and each one discards the stroke.
 *   gesture_limit        50   and then it wants 50 px on top, which is 21% of
 *                             a 240 px screen -- 8.9 mm of travel.
 *
 * 1 and 24 is as far as this can be pushed: the floor cannot go below 1 (at 0
 * the comparison is never true, which would leave the accumulator running
 * across the whole press), and at 1 a repeated identical point STILL trips it.
 * That ceiling is precisely why the gauge screen stopped using this path
 * rather than tuning it further. These two remaining callers are closing
 * something that also has a back button, so a swipe that misses is an
 * inconvenience rather than a dead end.
 */
#define GESTURE_MIN_VELOCITY	1
#define GESTURE_LIMIT_PX	24

static void tune_gestures(void)
{
	lv_indev_t *in = NULL;

	while ((in = lv_indev_get_next(in)) != NULL) {
		if (lv_indev_get_type(in) != LV_INDEV_TYPE_POINTER) {
			continue;
		}
		in->gesture_min_velocity = GESTURE_MIN_VELOCITY;
		in->gesture_limit = GESTURE_LIMIT_PX;
	}
}

void ui_settings_attach(lv_obj_t *scr)
{
	tune_gestures();
	/*
	 * The gauge screen's swipes come from ui_swipe, not from LVGL. There
	 * is deliberately no LV_EVENT_GESTURE handler here any more: leaving
	 * one would double-fire, and it would double-fire WRONGLY, since the
	 * fragments LVGL classifies are the ones ui_swipe exists to stitch
	 * back together.
	 */
	ui_swipe_init(swipe_cb, swipe_progress_cb);
	mk_edge_zone(scr, LV_ALIGN_RIGHT_MID, zone_settings_cb);
	mk_edge_zone(scr, LV_ALIGN_LEFT_MID, zone_anim_cb);
	mk_page_zone(scr);

	/* The OTA watcher runs from here on, not from the panel build: the boot
	 * prompt, the download bar and the outcome popup are all screen-level
	 * and have to work with settings shut. See upd_timer_cb. */
	if (upd_timer == NULL) {
		upd_timer = lv_timer_create(upd_timer_cb, 250, NULL);
	}
}
