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

/* Severity colours: green under 60%, amber approaching, red near the limit. */
#define COL_BG		lv_color_hex(0x0E1116)
#define COL_TRACK	lv_color_hex(0x272C34)
#define COL_TEXT	lv_color_hex(0xE6E8EB)
#define COL_DIM		lv_color_hex(0x8A9199)
#define COL_GREEN	lv_color_hex(0x2ECC71)
#define COL_AMBER	lv_color_hex(0xF1C40F)
#define COL_RED		lv_color_hex(0xE74C3C)
#define COL_GREY	lv_color_hex(0x555B63)

struct gauge {
	lv_obj_t *arc;
	lv_obj_t *pct;
	lv_obj_t *countdown;
	int32_t resets_in_s;	/* -1 = unknown; ticked down locally */
};

static struct gauge session, weekly;
static lv_obj_t *dot;
static lv_obj_t *hint;
static lv_obj_t *age_lbl;
static lv_obj_t *overlay;	/* full-screen "no data" takeover */
static bool built;
static bool have_data;		/* distinguishes "no host yet" from "host lost" */
static int32_t age_s = -1;	/* seconds since the last usage message */

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

	lv_obj_t *name = lv_label_create(parent);

	lv_label_set_text(name, title);
	lv_obj_set_style_text_color(name, COL_DIM, 0);
	lv_obj_align(name, LV_ALIGN_TOP_MID, cx, 166);

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

void usage_view_init(void)
{
	lv_obj_t *scr = lv_scr_act();

	lv_obj_set_style_bg_color(scr, COL_BG, 0);
	lv_obj_set_style_bg_opa(scr, LV_OPA_COVER, 0);

	lv_obj_t *title = lv_label_create(scr);

	lv_label_set_text(title, "CLAUDE CODE");
	lv_obj_set_style_text_color(title, COL_DIM, 0);
	lv_obj_align(title, LV_ALIGN_TOP_LEFT, 10, 6);

	dot = lv_obj_create(scr);
	lv_obj_set_size(dot, 12, 12);
	lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_border_width(dot, 0, 0);
	lv_obj_set_style_bg_color(dot, COL_GREY, 0);
	lv_obj_align(dot, LV_ALIGN_TOP_RIGHT, -12, 8);

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
	lv_label_set_text(age_lbl, "never");
	lv_obj_set_style_text_color(age_lbl, COL_DIM, 0);
	lv_obj_align(age_lbl, LV_ALIGN_TOP_RIGHT, -30, 8);

	build_gauge(&session, scr, -78, "SESSION 5h");
	build_gauge(&weekly, scr, 78, "WEEKLY 7d");

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
	lv_obj_center(overlay);

	lv_obj_t *big = lv_label_create(overlay);

	lv_label_set_text(big, "WAITING FOR HOST");
	lv_obj_set_style_text_color(big, COL_TEXT, 0);
	lv_obj_set_style_text_font(big, &lv_font_montserrat_20, 0);
	lv_obj_align(big, LV_ALIGN_CENTER, 0, -18);

	lv_obj_t *sub = lv_label_create(overlay);

	lv_label_set_text(sub, "start the bridge daemon on the PC");
	lv_obj_set_style_text_color(sub, COL_DIM, 0);
	lv_obj_align(sub, LV_ALIGN_CENTER, 0, 10);

	lv_obj_t *spin = lv_spinner_create(overlay);

	lv_obj_set_size(spin, 28, 28);
	lv_obj_align(spin, LV_ALIGN_CENTER, 0, 52);
	lv_obj_set_style_arc_color(spin, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(spin, COL_GREEN, LV_PART_INDICATOR);

	built = true;
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

	snprintf(buf, sizeof(buf), "%d%%", (int)(weekly_pct + 0.5));
	lv_label_set_text(weekly.pct, buf);
	lv_arc_set_value(weekly.arc, (int32_t)(weekly_pct + 0.5));
	lv_obj_set_style_arc_color(weekly.arc, severity(weekly_pct), LV_PART_INDICATOR);
	weekly.resets_in_s = weekly_resets_in_s;
	render_countdown(&weekly);

	have_data = true;
	age_s = 0;
	usage_view_set_status(USAGE_STATUS_OK);
}

static void render_age(void)
{
	char buf[FMT_COUNTDOWN_MAX];

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
}

void usage_view_set_status(enum usage_status status)
{
	if (!built) {
		return;
	}

	lv_color_t c;
	const char *text;

	switch (status) {
	case USAGE_STATUS_OK:
		c = COL_GREEN;
		text = "";
		break;
	case USAGE_STATUS_STALE:
		c = COL_AMBER;
		text = "rate-limited - showing last known";
		break;
	case USAGE_STATUS_ERROR:
		c = COL_RED;
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
			text = "HOST LOST - numbers are frozen";
		} else {
			c = COL_GREY;
			text = "";
		}
		break;
	}
	lv_obj_set_style_bg_color(dot, c, 0);
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
