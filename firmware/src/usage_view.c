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
static bool built;

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
	lv_obj_align(g->arc, LV_ALIGN_TOP_MID, cx, 30);
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
	lv_obj_align(g->pct, LV_ALIGN_TOP_MID, cx, 78);

	lv_obj_t *name = lv_label_create(parent);

	lv_label_set_text(name, title);
	lv_obj_set_style_text_color(name, COL_DIM, 0);
	lv_obj_align(name, LV_ALIGN_TOP_MID, cx, 152);

	g->countdown = lv_label_create(parent);
	lv_label_set_text(g->countdown, "--");
	lv_obj_set_style_text_color(g->countdown, COL_TEXT, 0);
	lv_obj_align(g->countdown, LV_ALIGN_TOP_MID, cx, 174);
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

	/* An idle board holding "--%" looks broken. Say what it is waiting for. */
	hint = lv_label_create(scr);
	lv_label_set_text(hint, "waiting for host...");
	lv_obj_set_style_text_color(hint, COL_DIM, 0);
	lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -6);

	build_gauge(&session, scr, -78, "SESSION 5h");
	build_gauge(&weekly, scr, 78, "WEEKLY 7d");

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

	usage_view_set_status(USAGE_STATUS_OK);
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
		c = COL_GREY;
		text = "waiting for host...";
		break;
	}
	lv_obj_set_style_bg_color(dot, c, 0);
	lv_label_set_text(hint, text);
}
