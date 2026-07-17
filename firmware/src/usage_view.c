/*
 * The gauge screen: Session (5h) and Weekly (7d) as arcs, with live countdowns.
 *
 * This is the one sink both data sources feed -- the USB bridge today, direct
 * WiFi fetching later -- so both modes render through identical code.
 */
#include <zephyr/kernel.h>
#include <lvgl.h>
#include <stdio.h>

#include "usage_view.h"
#include "fmt.h"
#include "cfg_store.h"

/* Severity colours: green under 60%, amber approaching, red near the limit. */
#define COL_BG		lv_color_hex(0x0E1116)
#define COL_PANEL	lv_color_hex(0x05070A)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_GREEN	lv_color_hex(0x2ECC71)
#define COL_GREEN_INK	lv_color_hex(0x06210F)
#define COL_AMBER	lv_color_hex(0xF1C40F)
#define COL_RED		lv_color_hex(0xE74C3C)
#define COL_GREY	lv_color_hex(0x555B63)

struct gauge {
	lv_obj_t *arc;
	lv_obj_t *pct;
	lv_obj_t *name;
	lv_obj_t *countdown;
	int32_t resets_in_s;	/* -1 = unknown; ticked down locally */
};

static struct gauge session, weekly;
static lv_obj_t *dot;
static lv_obj_t *hint;
static lv_obj_t *age_lbl;
static lv_obj_t *clock_lbl;
static lv_obj_t *overlay;	/* full-screen "no data" takeover */
static lv_obj_t *wait_big;	/* the takeover's CONNECTING title */
static bool built;
static bool have_data;		/* distinguishes "no host yet" from "host lost" */
static int32_t age_s = -1;	/* seconds since the last usage message */
static double last_s_pct = -1;	/* latest numbers, for the near-limit hint */
static double last_w_pct = -1;

/* Long-press peek: a card of per-model weekly numbers that STAYS up -- tap
 * a row to point the weekly gauge at that model, tap anywhere else (or wait
 * out the timer) to dismiss. v1 hid it on finger-up, which on this jittery
 * panel meant it vanished before it could be read, let alone used
 * (hardware 2026-07-17). */
#define PEEK_ROWS 2			/* Claude's windows today: all + fable */
static lv_obj_t *peek;
static lv_obj_t *peek_row[PEEK_ROWS];
static lv_obj_t *peek_row_lbl[PEEK_ROWS];
static int weekly_sel;			/* 0 all models, 1 fable */
static int peek_ttl;			/* auto-hide countdown, 0 = idle */
static int64_t peek_shown_ms;
static double model_fable = -1;

static void render_weekly(void);
static void render_age(void);
static void peek_fill(void);

void usage_view_set_models(double fable_pct)
{
	model_fable = fable_pct;
	if (built) {
		render_weekly();
		if (peek && !lv_obj_has_flag(peek, LV_OBJ_FLAG_HIDDEN)) {
			peek_fill();
		}
	}
}

/* One CONNECTING screen for every boot, USB and standalone alike: a segmented
 * bar that fills green as the worker gets through it, current step named
 * below (option D of the mockups, user-picked 2026-07-15; unified across
 * modes per user request 2026-07-16). The mode passes its own step list to
 * usage_view_boot_begin(). */
#define BOOT_STEPS_MAX 3
static lv_obj_t *boot_seg[BOOT_STEPS_MAX];
static lv_obj_t *boot_cnt;	/* "1 / 3" over the bar */
static lv_obj_t *boot_step;	/* current step name under the bar */
static int boot_active = -1;	/* segment currently pulsing, -1 = none */
static const char *const *boot_txt;
static int boot_n;

static lv_color_t severity(double pct)
{
	if (pct >= 90.0) {
		return COL_RED;
	}
	if (pct >= 60.0) {
		return COL_AMBER;
	}
	return COL_GREEN;
}

static void build_gauge(struct gauge *g, lv_obj_t *parent, lv_coord_t cx,
			const char *title)
{
	g->resets_in_s = -1;

	g->arc = lv_arc_create(parent);
	lv_obj_set_size(g->arc, 116, 116);
	lv_obj_align(g->arc, LV_ALIGN_TOP_MID, cx, 44);
	lv_arc_set_rotation(g->arc, 135);
	lv_arc_set_bg_angles(g->arc, 0, 270);
	lv_arc_set_range(g->arc, 0, 100);
	lv_arc_set_value(g->arc, 0);
	lv_obj_remove_style(g->arc, NULL, LV_PART_KNOB);	/* a readout, not a control */
	lv_obj_clear_flag(g->arc, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_set_style_arc_width(g->arc, 12, LV_PART_MAIN);
	lv_obj_set_style_arc_width(g->arc, 12, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(g->arc, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(g->arc, COL_GREEN, LV_PART_INDICATOR);

	g->pct = lv_label_create(parent);
	lv_label_set_text(g->pct, "--%");
	lv_obj_set_style_text_color(g->pct, COL_TEXT, 0);
	lv_obj_set_style_text_font(g->pct, &lv_font_montserrat_20, 0);
	lv_obj_align(g->pct, LV_ALIGN_TOP_MID, cx, 92);

	g->name = lv_label_create(parent);
	lv_label_set_text(g->name, title);
	lv_obj_set_style_text_color(g->name, COL_DIM, 0);
	lv_obj_align(g->name, LV_ALIGN_TOP_MID, cx, 166);

	g->countdown = lv_label_create(parent);
	lv_label_set_text(g->countdown, "--");
	lv_obj_set_style_text_color(g->countdown, COL_TEXT, 0);
	lv_obj_align(g->countdown, LV_ALIGN_TOP_MID, cx, 188);
}

static void render_countdown(struct gauge *g)
{
	char buf[FMT_COUNTDOWN_MAX];

	fmt_countdown(g->resets_in_s, buf, sizeof(buf));
	lv_label_set_text(g->countdown, buf);
}

static const char *const sel_name[PEEK_ROWS] = {
	"WEEKLY 7d", "WEEKLY FABLE",
};
static const char *const sel_label[PEEK_ROWS] = { "All models", "Fable" };

static double sel_pct(int sel)
{
	return sel == 1 ? model_fable : last_w_pct;
}

/* The weekly arc shows whichever source the peek card selected. The
 * countdown stays the overall weekly reset -- the per-model windows reset
 * with it. */
static void render_weekly(void)
{
	double pct = sel_pct(weekly_sel);

	lv_label_set_text(weekly.name, sel_name[weekly_sel]);
	if (pct < 0) {
		lv_label_set_text(weekly.pct, "--%");
		lv_arc_set_value(weekly.arc, 0);
		return;
	}

	char buf[8];

	snprintf(buf, sizeof(buf), "%d%%", (int)(pct + 0.5));
	lv_label_set_text(weekly.pct, buf);
	lv_arc_set_value(weekly.arc, (int32_t)(pct + 0.5));
	lv_obj_set_style_arc_color(weekly.arc, severity(pct), LV_PART_INDICATOR);
}

static void peek_fill(void)
{
	for (int i = 0; i < PEEK_ROWS; i++) {
		double pct = sel_pct(i);
		char val[8] = "--";
		char buf[24];

		if (pct >= 0) {
			snprintf(val, sizeof(val), "%d%%", (int)(pct + 0.5));
		}
		snprintf(buf, sizeof(buf), "%s  %s", sel_label[i], val);
		lv_label_set_text(peek_row_lbl[i], buf);
		/* The selected row wears green, like a done checklist step. */
		lv_obj_set_style_bg_color(peek_row[i],
			i == weekly_sel ? COL_GREEN : COL_TRACK, 0);
		lv_obj_set_style_text_color(peek_row_lbl[i],
			i == weekly_sel ? COL_GREEN_INK : COL_TEXT, 0);
	}
}

static void peek_hide(void)
{
	peek_ttl = 0;
	lv_obj_add_flag(peek, LV_OBJ_FLAG_HIDDEN);
}

static void peek_row_cb(lv_event_t *e)
{
	weekly_sel = (int)(intptr_t)lv_event_get_user_data(e);
	cfg_set_weekly_sel((uint8_t)weekly_sel);	/* survives reboots */
	render_weekly();
	peek_hide();
}

/* LONG_PRESSED on the screen. */
static void peek_open_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	peek_fill();
	lv_obj_clear_flag(peek, LV_OBJ_FLAG_HIDDEN);
	peek_ttl = 8;
	peek_shown_ms = k_uptime_get();
}

/* CLICKED on the screen: dismiss -- but the release of the opening
 * long-press itself arrives as a click, so only later taps count. */
static void peek_scrim_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (peek && !lv_obj_has_flag(peek, LV_OBJ_FLAG_HIDDEN) &&
	    k_uptime_get() - peek_shown_ms > 600) {
		peek_hide();
	}
}

static lv_obj_t *gauge_scr;

void usage_view_deinit(void)
{
	/* Free the gauge screen so it never coexists with the setup screen --
	 * with no PSRAM the LVGL heap cannot hold both. */
	built = false;
	boot_n = 0;
	boot_active = -1;
	peek = NULL;
	peek_ttl = 0;
	if (gauge_scr) {
		lv_obj_del(gauge_scr);
		gauge_scr = NULL;
	}
}

void usage_view_init(void)
{
	gauge_scr = lv_obj_create(NULL);
	lv_obj_t *scr = gauge_scr;

	lv_obj_clear_flag(scr, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_set_style_pad_all(scr, 0, 0);
	lv_obj_set_style_border_width(scr, 0, 0);
	lv_obj_set_style_bg_color(scr, COL_BG, 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);
	lv_scr_load(scr);

	/* Header: brand dead-center, quiet data in the corners -- clock left,
	 * age + dot right, everything at the same small size (the 20 px clock
	 * shouted over the gauges; user feedback 2026-07-16). */
	lv_obj_t *title = lv_label_create(scr);

	lv_label_set_text(title, "CLAUGE");
	lv_obj_set_style_text_color(title, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(title, 2, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, 6);

	dot = lv_obj_create(scr);
	lv_obj_set_size(dot, 12, 12);
	lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_border_width(dot, 0, 0);
	lv_obj_set_style_bg_color(dot, COL_GREY, 0);
	lv_obj_align(dot, LV_ALIGN_TOP_RIGHT, -12, 8);
	/* A swipe starting on this dot must still reach the screen below it,
	 * or the settings gesture goes dead whenever the touch lands here. */
	lv_obj_add_flag(dot, LV_OBJ_FLAG_GESTURE_BUBBLE);

	/* Carries the amber/red explanation. Empty when all is well: the gauges
	 * still hold real (if stale) numbers in those states, so they stay visible
	 * rather than being covered up.
	 */
	hint = lv_label_create(scr);
	lv_label_set_text(hint, "");
	lv_obj_set_style_text_color(hint, COL_DIM, 0);
	lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -6);

	/* Data age. The countdowns tick locally and keep moving even when the
	 * host is dead, so they look alive regardless; this is the only figure
	 * on screen that reveals whether the numbers are actually fresh.
	 */
	age_lbl = lv_label_create(scr);
	lv_label_set_text(age_lbl, "");
	lv_obj_set_style_text_color(age_lbl, COL_DIM, 0);
	lv_obj_align(age_lbl, LV_ALIGN_TOP_RIGHT, -30, 8);

	/* Wall clock. Blank until a time source and timezone are known -- an
	 * empty label beats a confidently wrong one. */
	clock_lbl = lv_label_create(scr);
	lv_label_set_text(clock_lbl, "");
	lv_obj_set_style_text_color(clock_lbl, COL_DIM, 0);
	lv_obj_align(clock_lbl, LV_ALIGN_TOP_LEFT, 10, 8);

	build_gauge(&session, scr, -78, "SESSION 5h");
	build_gauge(&weekly, scr, 78, "WEEKLY 7d");

	/* Edge affordances: without them nobody discovers the swipes (user
	 * feedback 2026-07-16). Right chevron pulls in settings, left one
	 * plays the boot clip. Labels don't catch input, so swipes starting
	 * on them still reach the screen. */
	lv_obj_t *chev = lv_label_create(scr);

	lv_label_set_text(chev, LV_SYMBOL_RIGHT);
	lv_obj_set_style_text_color(chev, COL_GREY, 0);
	lv_obj_align(chev, LV_ALIGN_RIGHT_MID, -3, 0);

	chev = lv_label_create(scr);
	lv_label_set_text(chev, LV_SYMBOL_LEFT);
	lv_obj_set_style_text_color(chev, COL_GREY, 0);
	lv_obj_align(chev, LV_ALIGN_LEFT_MID, 3, 0);

	/* The peek card, hidden until a long press. Created before the
	 * takeover overlay, which therefore keeps it unreachable while there
	 * is no data to peek at. */
	peek = lv_obj_create(scr);
	lv_obj_set_size(peek, 210, 116);
	lv_obj_set_style_radius(peek, 12, 0);
	lv_obj_set_style_bg_color(peek, COL_PANEL, 0);
	lv_obj_set_style_bg_opa(peek, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(peek, COL_TRACK, 0);
	lv_obj_set_style_border_width(peek, 1, 0);
	lv_obj_set_style_pad_all(peek, 0, 0);
	lv_obj_clear_flag(peek, LV_OBJ_FLAG_SCROLLABLE);
	lv_obj_clear_flag(peek, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_center(peek);
	lv_obj_add_flag(peek, LV_OBJ_FLAG_HIDDEN);

	lv_obj_t *pt = lv_label_create(peek);

	lv_label_set_text(pt, "WEEKLY GAUGE SHOWS");
	lv_obj_set_style_text_color(pt, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(pt, 1, 0);
	lv_obj_align(pt, LV_ALIGN_TOP_MID, 0, 10);

	for (int i = 0; i < PEEK_ROWS; i++) {
		peek_row[i] = lv_btn_create(peek);
		lv_obj_set_size(peek_row[i], 174, 30);
		lv_obj_set_style_shadow_width(peek_row[i], 0, 0);
		lv_obj_align(peek_row[i], LV_ALIGN_TOP_MID, 0, 36 + i * 36);
		lv_obj_add_event_cb(peek_row[i], peek_row_cb,
				    LV_EVENT_CLICKED, (void *)(intptr_t)i);

		peek_row_lbl[i] = lv_label_create(peek_row[i]);
		lv_label_set_text(peek_row_lbl[i], "");
		lv_obj_center(peek_row_lbl[i]);
	}

	lv_obj_add_event_cb(scr, peek_open_cb, LV_EVENT_LONG_PRESSED, NULL);
	lv_obj_add_event_cb(scr, peek_scrim_cb, LV_EVENT_CLICKED, NULL);

	/* The peek-card choice from last boot; render so the gauge's name
	 * says which window it shows from the very first frame. */
	weekly_sel = cfg_get_weekly_sel();
	if (weekly_sel >= PEEK_ROWS) {
		weekly_sel = 0;		/* stale NVS from an older layout */
	}
	render_weekly();

	/* With no host there is genuinely nothing to show, so take over the whole
	 * screen. A board sitting quietly at "--%" reads as broken; this says what
	 * it is actually waiting for. Created last, so it sits above the gauges.
	 */
	overlay = lv_obj_create(scr);
	lv_obj_set_size(overlay, LV_PCT(100), LV_PCT(100));
	lv_obj_set_style_bg_color(overlay, COL_BG, 0);
	lv_obj_set_style_bg_opa(overlay, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(overlay, 0, 0);
	lv_obj_set_style_radius(overlay, 0, 0);
	lv_obj_clear_flag(overlay, LV_OBJ_FLAG_SCROLLABLE);
	/* Full-screen: without this a swipe starting here never reaches the
	 * screen, so the settings gesture is dead the whole time we're
	 * waiting for a host. */
	lv_obj_add_flag(overlay, LV_OBJ_FLAG_GESTURE_BUBBLE);
	lv_obj_center(overlay);

	/* The takeover has exactly one form: the CONNECTING bar. USB mode used
	 * to get its own "waiting for host" prose here; two different waiting
	 * screens for the same moment read as two different products (user
	 * feedback 2026-07-16). */
	wait_big = lv_label_create(overlay);

	lv_label_set_text(wait_big, "CONNECTING");
	lv_obj_set_style_text_color(wait_big, COL_TEXT, 0);
	lv_obj_set_style_text_font(wait_big, &lv_font_montserrat_20, 0);
	lv_obj_align(wait_big, LV_ALIGN_TOP_MID, 0, 44);

	boot_cnt = lv_label_create(overlay);
	lv_label_set_text(boot_cnt, "");
	lv_obj_set_style_text_color(boot_cnt, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(boot_cnt, 2, 0);
	lv_obj_align(boot_cnt, LV_ALIGN_TOP_MID, 0, 88);

	for (int i = 0; i < BOOT_STEPS_MAX; i++) {
		boot_seg[i] = lv_obj_create(overlay);
		lv_obj_set_size(boot_seg[i], 76, 8);
		lv_obj_set_style_radius(boot_seg[i], 4, 0);
		lv_obj_set_style_border_width(boot_seg[i], 0, 0);
		lv_obj_set_style_bg_color(boot_seg[i], COL_TRACK, 0);
		lv_obj_set_style_bg_opa(boot_seg[i], LV_OPA_COVER, 0);
		lv_obj_clear_flag(boot_seg[i], LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_add_flag(boot_seg[i], LV_OBJ_FLAG_HIDDEN);
	}

	boot_step = lv_label_create(overlay);
	lv_label_set_text(boot_step, "");
	lv_obj_set_style_text_color(boot_step, COL_DIM, 0);
	lv_obj_align(boot_step, LV_ALIGN_TOP_MID, 0, 146);

	built = true;
}

void usage_view_boot_begin(const char *const *steps, int nsteps)
{
	if (!built) {
		return;
	}
	boot_txt = steps;
	boot_n = MIN(nsteps, BOOT_STEPS_MAX);
	boot_active = -1;

	/* Center the row for this mode's segment count. */
	for (int i = 0; i < BOOT_STEPS_MAX; i++) {
		if (i < boot_n) {
			lv_obj_align(boot_seg[i], LV_ALIGN_TOP_MID,
				     i * 82 - (boot_n - 1) * 41, 118);
			lv_obj_clear_flag(boot_seg[i], LV_OBJ_FLAG_HIDDEN);
		} else {
			lv_obj_add_flag(boot_seg[i], LV_OBJ_FLAG_HIDDEN);
		}
	}
	usage_view_boot_stage(0);
}

bool usage_view_have_data(void)
{
	return have_data;
}

void usage_view_update(double session_pct, int32_t session_resets_in_s,
		       double weekly_pct, int32_t weekly_resets_in_s)
{
	if (!built) {
		return;
	}

	char buf[8];

	snprintf(buf, sizeof(buf), "%d%%", (int)(session_pct + 0.5));
	lv_label_set_text(session.pct, buf);
	lv_arc_set_value(session.arc, (int32_t)(session_pct + 0.5));
	lv_obj_set_style_arc_color(session.arc, severity(session_pct), LV_PART_INDICATOR);
	session.resets_in_s = session_resets_in_s;
	render_countdown(&session);

	weekly.resets_in_s = weekly_resets_in_s;
	render_countdown(&weekly);

	have_data = true;
	age_s = 0;
	last_s_pct = session_pct;
	last_w_pct = weekly_pct;
	render_weekly();
	render_age();
	usage_view_set_status(USAGE_STATUS_OK);
}

static void render_age(void)
{
	char buf[FMT_COUNTDOWN_MAX];

	/* Fresh data needs no caption: the varying-width "12s" next to the
	 * dot unbalanced the centered title (user feedback 2026-07-17). The
	 * age appears only once it is old enough to be worth knowing. */
	if (age_s >= 0 && age_s < 120) {
		lv_label_set_text(age_lbl, "");
		return;
	}
	fmt_age(age_s, buf, sizeof(buf));
	lv_label_set_text(age_lbl, buf);
}

void usage_view_tick_1s(void)
{
	if (!built) {
		return;
	}

	struct gauge *gs[] = { &session, &weekly };

	for (int i = 0; i < 2; i++) {
		if (gs[i]->resets_in_s > 0) {
			gs[i]->resets_in_s--;
			render_countdown(gs[i]);
		}
	}

	if (age_s >= 0) {
		age_s++;
		render_age();
	}

	if (peek_ttl > 0 && --peek_ttl == 0) {
		peek_hide();
	}
}

void usage_view_set_status(enum usage_status status)
{
	if (!built) {
		return;
	}

	lv_color_t c;
	lv_color_t tc = COL_DIM;
	const char *text;

	switch (status) {
	case USAGE_STATUS_OK:
		c = COL_GREEN;
		/* Near-limit call-out: the whole point of a glanceable
		 * display is knowing this before the API tells you no
		 * (user request 2026-07-17). Session first -- it bites
		 * sooner. */
		if (last_s_pct >= 95.0) {
			text = "Session almost used up";
			tc = COL_RED;
		} else if (last_w_pct >= 95.0) {
			text = "Weekly almost used up";
			tc = COL_RED;
		} else if (model_fable >= 95.0) {
			/* Fable's window is a real limit of its own; it can
			 * run dry while the overall weekly still looks calm. */
			text = "Fable weekly almost used up";
			tc = COL_RED;
		} else {
			text = "";
		}
		break;
	case USAGE_STATUS_STALE:
		c = COL_AMBER;
		tc = COL_AMBER;
		text = "rate-limited - showing last known";
		break;
	case USAGE_STATUS_ERROR:
		c = COL_RED;
		tc = COL_RED;
		text = "error - showing last known";
		break;
	default:
		/* Two very different situations wear the same status. */
		if (have_data) {
			/* The host died but we still hold real numbers. Keep them
			 * on screen -- they were true once -- but stop implying
			 * they are live. The countdowns go on ticking by
			 * themselves, so without this the board would look
			 * perfectly healthy while showing a frozen snapshot.
			 */
			c = COL_RED;
			tc = COL_RED;
			text = "HOST LOST - numbers are frozen";
		} else {
			c = COL_GREY;
			text = "";
		}
		break;
	}
	lv_obj_set_style_bg_color(dot, c, 0);
	lv_obj_set_style_text_color(hint, tc, 0);
	lv_label_set_text(hint, text);

	/* The full takeover is only for "we never had any data". Every other
	 * state has real numbers behind it, and covering those up would throw
	 * away information the user wants.
	 */
	if (status == USAGE_STATUS_DISCONNECTED && !have_data) {
		lv_obj_clear_flag(overlay, LV_OBJ_FLAG_HIDDEN);
		lv_obj_move_foreground(overlay);
	} else {
		lv_obj_add_flag(overlay, LV_OBJ_FLAG_HIDDEN);
	}
}

static void boot_pulse_cb(void *obj, int32_t v)
{
	lv_obj_set_style_bg_opa((lv_obj_t *)obj, (lv_opa_t)v, 0);
}

void usage_view_boot_stage(int stage)
{
	if (!built || boot_n <= 0) {
		return;
	}
	if (stage > boot_n) {
		stage = boot_n;
	}

	if (boot_active >= 0 && boot_active != stage) {
		lv_anim_delete(boot_seg[boot_active], boot_pulse_cb);
	}

	for (int i = 0; i < boot_n; i++) {
		lv_obj_set_style_bg_opa(boot_seg[i], LV_OPA_COVER, 0);
		lv_obj_set_style_bg_color(boot_seg[i],
					  i < stage ? COL_GREEN : COL_TRACK, 0);
	}

	if (stage < boot_n) {
		/* The active segment breathes: with no spinner on this form,
		 * this is the motion that says the board is alive. */
		lv_obj_set_style_bg_color(boot_seg[stage], COL_GREEN, 0);
		if (boot_active != stage) {
			lv_anim_t a;

			lv_anim_init(&a);
			lv_anim_set_var(&a, boot_seg[stage]);
			lv_anim_set_exec_cb(&a, boot_pulse_cb);
			lv_anim_set_values(&a, LV_OPA_30, LV_OPA_COVER);
			lv_anim_set_duration(&a, 600);
			lv_anim_set_playback_duration(&a, 600);
			lv_anim_set_repeat_count(&a, LV_ANIM_REPEAT_INFINITE);
			lv_anim_start(&a);
		}
		lv_label_set_text(boot_step, boot_txt[stage]);

		char cbuf[16];

		snprintf(cbuf, sizeof(cbuf), "%d / %d", stage + 1, boot_n);
		lv_label_set_text(boot_cnt, cbuf);
		boot_active = stage;
	} else {
		lv_label_set_text(boot_step, "Ready");
		lv_label_set_text(boot_cnt, "");
		boot_active = -1;
	}
}

void usage_view_set_clock(int hh, int mm)
{
	if (!built) {
		return;
	}
	if (hh < 0) {
		lv_label_set_text(clock_lbl, "");
		return;
	}

	char buf[16];

	snprintf(buf, sizeof(buf), "%02d:%02d", hh, mm);
	lv_label_set_text(clock_lbl, buf);
}
