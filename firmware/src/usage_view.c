/*
 * The gauge screen: Session (5h) and Weekly (7d) as arcs, with live countdowns.
 *
 * This is the one sink both data sources feed -- the USB bridge today, direct
 * WiFi fetching later -- so both modes render through identical code.
 */
#include <zephyr/kernel.h>
#include <lvgl.h>
#include <stdio.h>
#include <string.h>

#include "usage_view.h"
#include "usage_layout.h"
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

/*
 * Provider identity colours.
 *
 * Colour now says WHICH TOOL a ring belongs to, which means it is no longer
 * saying how close that tool is to its limit. Severity moved to the numerals
 * instead -- the percentage in the middle of each gauge still runs
 * green/amber/red -- so the warning is still there, just carried by the text
 * rather than the ring. Worth knowing when reading this file: the ring tells
 * you whose, the number tells you how bad.
 */
#define COL_CLAUDE	lv_color_hex(0xD97757)	/* the brand's warm orange */
#define COL_CODEX	lv_color_hex(0x10A37F)	/* a teal, well clear of it */
#define COL_OTHER	lv_color_hex(0x6E8BC4)	/* anything else: a cool blue */

struct gauge {
	lv_obj_t *arc;
	lv_obj_t *arc2;		/* second provider, inner ring; NULL-safe */
	lv_obj_t *pct;
	lv_obj_t *p2;		/* the inner ring's own small readout */
	lv_obj_t *name;
	lv_obj_t *countdown;	/* primary provider's time left */
	lv_obj_t *countdown2;	/* the second provider's, when there is one */
	int32_t resets_in_s;	/* -1 = unknown; ticked down locally */
	int32_t resets2_in_s;	/* ...and the second provider's */
};

static struct gauge session, weekly;
static lv_obj_t *dot;
static lv_obj_t *hint;
static lv_obj_t *age_lbl;
static lv_obj_t *clock_lbl;
static lv_obj_t *act_pip;	/* execution state, as a coloured pip */
static lv_obj_t *sess_lbl;	/* "3s 7a" -- open sessions and live agents */
static enum usage_activity activity = USAGE_ACTIVITY_NONE;
static char provider2_tag[12];	/* "" when there is only one provider */
static lv_obj_t *overlay;	/* full-screen "no data" takeover */
static lv_obj_t *wait_big;	/* the takeover's CONNECTING title */
static bool built;
static bool have_data;		/* distinguishes "no host yet" from "host lost" */
static int32_t age_s = -1;	/* seconds since the last usage message */
static double last_s_pct = -1;	/* latest numbers, for the near-limit hint */
static double last_w_pct = -1;
static enum usage_status last_status = USAGE_STATUS_DISCONNECTED;

/*
 * Per-model weekly windows -- and the long-press card that picks between
 * them -- exist only in standalone WiFi mode. That mode reads Claude's usage
 * endpoint directly and gets a per-model breakdown with it. The USB path
 * gets its numbers from Claude Code's status line, and that payload carries
 * the two overall windows and nothing else; pc/statusline_source.py sends an
 * empty models list because there is nothing to put in one.
 *
 * So on a USB unit the card offered a choice between a real number and a
 * permanent "--%" -- and because the choice is kept in NVS, choosing the
 * empty one stuck across reboots, leaving a gauge reading "--%" for good
 * with no route back that anyone would find. Compiled out rather than
 * disabled: a control that cannot do anything is worse than no control.
 */
#if IS_ENABLED(CONFIG_CLAUGE_WIFI_MODE)
#define HAVE_PER_MODEL 1

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
#else
#define HAVE_PER_MODEL 0
#endif

static void render_weekly(void);
static void render_age(void);
#if HAVE_PER_MODEL
static void peek_fill(void);
#endif

void usage_view_set_models(double fable_pct)
{
#if !HAVE_PER_MODEL
	/* Nothing on this build produces a per-model number: the daemon sends
	 * no such key and proto.c passes its -1 default straight through.
	 * Kept as a symbol so both data paths still share one header. */
	ARG_UNUSED(fable_pct);
}
#else
	/* Per-model windows reset with the overall weekly, so a zero weekly
	 * means the fable number is stale too. Derive that from the weekly
	 * state itself (last_w_pct) rather than a flag set by an earlier call,
	 * so BOTH ways the weekly can empty are covered: an update reporting
	 * the reset, AND the local countdown ticking to zero via
	 * expire_weekly(). fable is a component of the weekly, so weekly 0%
	 * always implies fable 0%. */
	if (last_w_pct == 0 && fable_pct >= 0) {
		fable_pct = 0;
	}
	model_fable = fable_pct;
	if (built) {
		render_weekly();
		if (peek && !lv_obj_has_flag(peek, LV_OBJ_FLAG_HIDDEN)) {
			peek_fill();
		}
		/* The near-limit hint is computed in set_status from model_fable,
		 * but the models event usually lands right AFTER the usage update
		 * that ran it -- so re-run it here or the Fable warning lags a
		 * whole poll and is missing on the first frames after a reboot
		 * (user-reported 2026-07-20). */
		if (last_status == USAGE_STATUS_OK) {
			usage_view_set_status(USAGE_STATUS_OK);
		}
	}
}
#endif /* HAVE_PER_MODEL */

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

/* Which colour a ring wears, by the name the daemon gave its provider. */
static lv_color_t provider_color(const char *tag)
{
	if (!tag || !tag[0]) {
		return COL_CLAUDE;
	}
	if (strcmp(tag, "claude") == 0) {
		return COL_CLAUDE;
	}
	if (strcmp(tag, "codex") == 0) {
		return COL_CODEX;
	}
	/* A provider this firmware has never heard of still gets a colour of
	 * its own rather than borrowing one that means something else. */
	return COL_OTHER;
}

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
	lv_obj_set_size(g->arc, GAUGE_ARC_SZ, GAUGE_ARC_SZ);
	lv_obj_align(g->arc, LV_ALIGN_TOP_MID, cx, GAUGE_ARC_Y);
	lv_arc_set_rotation(g->arc, 135);
	lv_arc_set_bg_angles(g->arc, 0, 270);
	lv_arc_set_range(g->arc, 0, 100);
	lv_arc_set_value(g->arc, 0);
	/* The knob is kept, not deleted: it is the only part that tracks the
	 * end of the indicator, and that is where the provider ball goes. The
	 * arc stays unclickable -- this is still a readout. */
	lv_obj_clear_flag(g->arc, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_set_style_pad_all(g->arc, GAUGE_BALL_PAD, LV_PART_KNOB);
	lv_obj_set_style_bg_color(g->arc, COL_CLAUDE, LV_PART_KNOB);
	lv_obj_set_style_bg_opa(g->arc, LV_OPA_COVER, LV_PART_KNOB);
	lv_obj_set_style_arc_width(g->arc, GAUGE_ARC_W, LV_PART_MAIN);
	lv_obj_set_style_arc_width(g->arc, GAUGE_ARC_W, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(g->arc, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(g->arc, COL_GREEN, LV_PART_INDICATOR);

	/* The second provider's ring, hidden until one exists. Created before
	 * the labels so the text draws over it, not under. */
	g->arc2 = lv_arc_create(parent);
	lv_obj_set_size(g->arc2, GAUGE_ARC2_SZ, GAUGE_ARC2_SZ);
	lv_obj_align(g->arc2, LV_ALIGN_TOP_MID, cx, GAUGE_ARC2_Y);
	lv_arc_set_rotation(g->arc2, 135);
	lv_arc_set_bg_angles(g->arc2, 0, 270);
	lv_arc_set_range(g->arc2, 0, 100);
	lv_arc_set_value(g->arc2, 0);
	lv_obj_clear_flag(g->arc2, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_set_style_pad_all(g->arc2, GAUGE_BALL_PAD, LV_PART_KNOB);
	lv_obj_set_style_bg_color(g->arc2, COL_CODEX, LV_PART_KNOB);
	lv_obj_set_style_bg_opa(g->arc2, LV_OPA_COVER, LV_PART_KNOB);
	lv_obj_set_style_arc_width(g->arc2, GAUGE_ARC2_W, LV_PART_MAIN);
	lv_obj_set_style_arc_width(g->arc2, GAUGE_ARC2_W, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(g->arc2, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(g->arc2, COL_GREEN, LV_PART_INDICATOR);
	lv_obj_add_flag(g->arc2, LV_OBJ_FLAG_HIDDEN);
	lv_obj_add_flag(g->arc2, LV_OBJ_FLAG_GESTURE_BUBBLE);

	g->pct = lv_label_create(parent);
	lv_label_set_text(g->pct, "--%");
	lv_obj_set_style_text_color(g->pct, COL_TEXT, 0);
	lv_obj_set_style_text_font(g->pct, &lv_font_montserrat_20, 0);
	lv_obj_align(g->pct, LV_ALIGN_TOP_MID, cx, GAUGE_PCT_Y);

	g->name = lv_label_create(parent);
	lv_label_set_text(g->name, title);
	lv_obj_set_style_text_color(g->name, COL_DIM, 0);
	lv_obj_align(g->name, LV_ALIGN_TOP_MID, cx, GAUGE_NAME_Y);

	/* The inner ring's own figure, small and dim under the countdown. The
	 * primary provider keeps the big number; this one is there to be
	 * noticed, not read first. */
	g->p2 = lv_label_create(parent);
	lv_label_set_text(g->p2, "");
	lv_obj_set_style_text_color(g->p2, COL_DIM, 0);
	lv_obj_align(g->p2, LV_ALIGN_TOP_MID, cx, GAUGE_P2PCT_Y);

	/* Both countdowns sit under the caption, each in its provider's
	 * colour. Centred on the gauge while there is one; pushed apart into
	 * a pair the moment a second provider appears. */
	g->resets2_in_s = -1;
	g->countdown = lv_label_create(parent);
	lv_label_set_text(g->countdown, "--");
	lv_obj_set_style_text_color(g->countdown, COL_CLAUDE, 0);
	lv_obj_align(g->countdown, LV_ALIGN_TOP_MID, cx, GAUGE_CD_Y);

	g->countdown2 = lv_label_create(parent);
	lv_label_set_text(g->countdown2, "");
	lv_obj_set_style_text_color(g->countdown2, COL_CODEX, 0);
	lv_obj_align(g->countdown2, LV_ALIGN_TOP_MID, cx, GAUGE_CD_Y);
	lv_obj_add_flag(g->countdown2, LV_OBJ_FLAG_HIDDEN);
}

static void render_countdown(struct gauge *g)
{
	char buf[FMT_COUNTDOWN_MAX];
	bool paired = g->countdown2 &&
		      !lv_obj_has_flag(g->countdown2, LV_OBJ_FLAG_HIDDEN);
	lv_coord_t cx = (g == &weekly) ? GAUGE_CX : -GAUGE_CX;

	fmt_countdown(g->resets_in_s, buf, sizeof(buf));
	lv_label_set_text(g->countdown, buf);

	/* One provider: centred under its gauge. Two: pushed apart so each
	 * countdown sits under nothing but its own colour. Re-aligned on every
	 * render rather than once at build time, because the second provider
	 * can arrive and leave while the board is running. */
	lv_obj_align(g->countdown, LV_ALIGN_TOP_MID,
		     paired ? cx - GAUGE_CD_DX : cx, GAUGE_CD_Y);

	if (paired) {
		fmt_countdown(g->resets2_in_s, buf, sizeof(buf));
		lv_label_set_text(g->countdown2, buf);
		lv_obj_align(g->countdown2, LV_ALIGN_TOP_MID, cx + GAUGE_CD_DX,
			     GAUGE_CD_Y);
	}
}

#if HAVE_PER_MODEL
static const char *const sel_name[PEEK_ROWS] = {
	"WEEKLY 7d", "WEEKLY FABLE",
};
static const char *const sel_label[PEEK_ROWS] = { "All models", "Fable" };

static double sel_pct(int sel)
{
	return sel == 1 ? model_fable : last_w_pct;
}
#endif

/* The weekly arc shows whichever source the peek card selected. The
 * countdown stays the overall weekly reset -- the per-model windows reset
 * with it. */
static void render_weekly(void)
{
#if HAVE_PER_MODEL
	double pct = sel_pct(weekly_sel);

	lv_label_set_text(weekly.name, sel_name[weekly_sel]);
#else
	/* One window, so the gauge never needs to say which one it is showing
	 * -- but it still says it, because "WEEKLY 7d" is the honest name for
	 * the number under it. */
	double pct = last_w_pct;

	lv_label_set_text(weekly.name, "WEEKLY 7d");
#endif
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

#if HAVE_PER_MODEL
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
#endif /* HAVE_PER_MODEL */

static lv_obj_t *gauge_scr;

void usage_view_deinit(void)
{
	/* Free the gauge screen so it never coexists with the setup screen --
	 * with no PSRAM the LVGL heap cannot hold both. */
	built = false;
	boot_n = 0;
	boot_active = -1;
	/* These hang off gauge_scr and die with it. Nulling them matters
	 * because the setters below are called from the protocol thread and
	 * would otherwise write through a freed pointer between the delete and
	 * the next init. */
	act_pip = NULL;
	sess_lbl = NULL;
	session.arc2 = NULL;
	weekly.arc2 = NULL;
	session.countdown2 = NULL;
	weekly.countdown2 = NULL;
	session.p2 = NULL;
	weekly.p2 = NULL;
	activity = USAGE_ACTIVITY_NONE;
#if HAVE_PER_MODEL
	peek = NULL;
	peek_ttl = 0;
#endif
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

	lv_label_set_text(title, BRAND_TEXT);
	lv_obj_set_style_text_color(title, COL_DIM, 0);
	lv_obj_set_style_text_letter_space(title, 2, 0);
	lv_obj_align(title, LV_ALIGN_TOP_MID, 0, TITLE_Y);

	dot = lv_obj_create(scr);
	lv_obj_set_size(dot, DOT_SZ, DOT_SZ);
	lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_border_width(dot, 0, 0);
	lv_obj_set_style_bg_color(dot, COL_GREY, 0);
	lv_obj_align(dot, LV_ALIGN_TOP_RIGHT, -12, HDR_ROW_Y);
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
	lv_obj_align(hint, LV_ALIGN_BOTTOM_MID, 0, -HINT_BOTTOM_OFF);

	/* Data age. The countdowns tick locally and keep moving even when the
	 * host is dead, so they look alive regardless; this is the only figure
	 * on screen that reveals whether the numbers are actually fresh.
	 */
	age_lbl = lv_label_create(scr);
	lv_label_set_text(age_lbl, "");
	lv_obj_set_style_text_color(age_lbl, COL_DIM, 0);
	lv_obj_align(age_lbl, LV_ALIGN_TOP_RIGHT, -30, HDR_ROW_Y);

	/* Wall clock. Blank until a time source and timezone are known -- an
	 * empty label beats a confidently wrong one. */
	clock_lbl = lv_label_create(scr);
	lv_label_set_text(clock_lbl, "");
	lv_obj_set_style_text_color(clock_lbl, COL_DIM, 0);
	lv_obj_align(clock_lbl, LV_ALIGN_TOP_LEFT, 10, HDR_ROW_Y);

	/* Which model is in use, directly under the brand. Blank until the
	 * daemon says -- an empty line reads as "nothing to report", where a
	 * placeholder would read as a model actually named that. */
	/* Execution state, in the left column under the clock. Hidden at
	 * USAGE_ACTIVITY_NONE rather than shown grey: a dark corner says
	 * nothing, and a grey pip says "idle", which is a different claim. */
	act_pip = lv_obj_create(scr);
	lv_obj_set_size(act_pip, ACT_PIP_SZ, ACT_PIP_SZ);
	lv_obj_set_style_radius(act_pip, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_border_width(act_pip, 0, 0);
	lv_obj_set_style_bg_color(act_pip, COL_GREEN, 0);
	lv_obj_align(act_pip, LV_ALIGN_TOP_LEFT, ACT_PIP_X, ACT_PIP_Y);
	lv_obj_add_flag(act_pip, LV_OBJ_FLAG_HIDDEN);
	/* Same reason the status dot bubbles: a swipe starting here must still
	 * reach the screen underneath, or the settings gesture goes dead on
	 * whatever fraction of the panel this covers. */
	lv_obj_add_flag(act_pip, LV_OBJ_FLAG_GESTURE_BUBBLE);

	/* The bottom line: session and agent counts, and the second provider's
	 * name when there is one. Shared with the hint, which outranks both --
	 * the hint is empty when all is well, which is exactly when these are
	 * worth reading. */
	sess_lbl = lv_label_create(scr);
	lv_label_set_text(sess_lbl, "");
	lv_obj_set_style_text_color(sess_lbl, COL_DIM, 0);
	lv_obj_align(sess_lbl, LV_ALIGN_BOTTOM_MID, 0, -SESS_BOTTOM_OFF);

	build_gauge(&session, scr, -GAUGE_CX, "SESSION 5h");
	build_gauge(&weekly, scr, GAUGE_CX, "WEEKLY 7d");


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

#if HAVE_PER_MODEL
	/* The peek card, hidden until a long press. Created before the
	 * takeover overlay, which therefore keeps it unreachable while there
	 * is no data to peek at. */
	peek = lv_obj_create(scr);
	lv_obj_set_size(peek, 240, 152);
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
	lv_obj_align(pt, LV_ALIGN_TOP_MID, 0, 12);

	/* 46 px rows, nearly card-wide: the 30 px originals were a miss-tap
	 * lottery on this panel (user feedback 2026-07-17). */
	for (int i = 0; i < PEEK_ROWS; i++) {
		peek_row[i] = lv_btn_create(peek);
		lv_obj_set_size(peek_row[i], 208, 46);
		lv_obj_set_style_shadow_width(peek_row[i], 0, 0);
		lv_obj_align(peek_row[i], LV_ALIGN_TOP_MID, 0, 40 + i * 54);
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
#endif /* HAVE_PER_MODEL */
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

/*
 * A window whose reset moment has passed is over, whatever percentage came
 * with it: the usage API keeps reporting the ended window (old utilization,
 * passed resets_at) for minutes afterwards, which left a stale gauge + "now"
 * on screen long past the reset (user-reported 2026-07-17). 0 remaining
 * seconds means "already reset" -- show the new, empty window instead, with
 * an unknown countdown (the next window starts on the next activity).
 */
void usage_view_update(double session_pct, int32_t session_resets_in_s,
		       double weekly_pct, int32_t weekly_resets_in_s)
{
	if (!built) {
		return;
	}

	if (session_resets_in_s == 0) {
		session_pct = 0;
		session_resets_in_s = -1;
	}
	if (weekly_resets_in_s == 0) {
		weekly_pct = 0;
		weekly_resets_in_s = -1;
	}

	char buf[8];

	/* A negative percentage means "the daemon does not have this number" --
	 * pc/statusline_source.py sends -1.0 when the window is absent from
	 * Claude Code's payload entirely. render_weekly() has always honoured
	 * that; this side did not, so an absent five_hour window rendered as a
	 * confident green 0%, which is the frozen-meter reading the sentinel
	 * exists to prevent. */
	if (session_pct < 0) {
		lv_label_set_text(session.pct, "--%");
		lv_arc_set_value(session.arc, 0);
	} else {
		snprintf(buf, sizeof(buf), "%d%%", (int)(session_pct + 0.5));
		lv_label_set_text(session.pct, buf);
		lv_arc_set_value(session.arc, (int32_t)(session_pct + 0.5));
		lv_obj_set_style_arc_color(session.arc, severity(session_pct),
					   LV_PART_INDICATOR);
	}
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

/* The countdown just hit zero: this window is over right now, never mind
 * that the API will keep echoing it for a while (see usage_view_update).
 * Flip the gauge to the new, empty window immediately. */
static void expire_session(void)
{
	session.resets_in_s = -1;
	render_countdown(&session);
	last_s_pct = 0;
	lv_label_set_text(session.pct, "0%");
	lv_arc_set_value(session.arc, 0);
	lv_obj_set_style_arc_color(session.arc, severity(0), LV_PART_INDICATOR);
}

static void expire_weekly(void)
{
	weekly.resets_in_s = -1;
	render_countdown(&weekly);
	last_w_pct = 0;
#if HAVE_PER_MODEL
	if (model_fable >= 0) {
		model_fable = 0;	/* per-model windows reset with the weekly */
	}
#endif
	render_weekly();
#if HAVE_PER_MODEL
	if (peek && !lv_obj_has_flag(peek, LV_OBJ_FLAG_HIDDEN)) {
		peek_fill();
	}
#endif
}

void usage_view_tick_1s(void)
{
	if (!built) {
		return;
	}

	struct gauge *gs[] = { &session, &weekly };

	for (int i = 0; i < 2; i++) {
		if (gs[i]->resets_in_s > 0) {
			if (--gs[i]->resets_in_s == 0) {
				if (gs[i] == &session) {
					expire_session();
				} else {
					expire_weekly();
				}
			} else {
				render_countdown(gs[i]);
			}
		}
	}

	if (age_s >= 0) {
		age_s++;
		render_age();
	}

#if HAVE_PER_MODEL
	if (peek_ttl > 0 && --peek_ttl == 0) {
		peek_hide();
	}
#endif
}

/*
 * Execution state -> pip colour, and whether it breathes.
 *
 * RUNNING is the only animated one, and it reuses the boot bar's pulse
 * exactly (see boot_pulse_cb): a fade between LV_OPA_30 and full, 600 ms each
 * way, repeating. A steady dot cannot distinguish "working" from "finished
 * and left it that way", which is the whole distinction this indicator adds.
 */
static void act_pulse_cb(void *obj, int32_t v)
{
	lv_obj_set_style_bg_opa((lv_obj_t *)obj, (lv_opa_t)v, 0);
}

void usage_view_set_activity(enum usage_activity a)
{
	if (!act_pip) {
		return;
	}

	/* Always stop first. Leaving a previous pulse running would keep
	 * writing opacity behind whatever the new state sets, so a
	 * RUNNING -> STUCK transition would show a breathing red pip that
	 * reads as "still working" at exactly the moment it is not. */
	lv_anim_delete(act_pip, act_pulse_cb);
	lv_obj_set_style_bg_opa(act_pip, LV_OPA_COVER, 0);
	activity = a;

	if (a == USAGE_ACTIVITY_NONE) {
		lv_obj_add_flag(act_pip, LV_OBJ_FLAG_HIDDEN);
		return;
	}
	lv_obj_clear_flag(act_pip, LV_OBJ_FLAG_HIDDEN);

	switch (a) {
	case USAGE_ACTIVITY_WAITING:
		lv_obj_set_style_bg_color(act_pip, COL_AMBER, 0);
		break;
	case USAGE_ACTIVITY_STUCK:
	case USAGE_ACTIVITY_FAILED:
		lv_obj_set_style_bg_color(act_pip, COL_RED, 0);
		break;
	default:
		lv_obj_set_style_bg_color(act_pip, COL_GREEN, 0);
		break;
	}

	if (a == USAGE_ACTIVITY_RUNNING) {
		lv_anim_t an;

		lv_anim_init(&an);
		lv_anim_set_var(&an, act_pip);
		lv_anim_set_exec_cb(&an, act_pulse_cb);
		lv_anim_set_values(&an, LV_OPA_30, LV_OPA_COVER);
		lv_anim_set_duration(&an, 600);
		lv_anim_set_playback_duration(&an, 600);
		lv_anim_set_repeat_count(&an, LV_ANIM_REPEAT_INFINITE);
		lv_anim_start(&an);
	}
}


/*
 * The bottom line, which several things want and only one can have.
 *
 * It carries the second provider's name -- the inner rings are unlabelled, so
 * this is the only place that says whose they are -- and the session and agent
 * counts. The hint outranks both and is handled separately: it is empty when
 * all is well, which is exactly when these are worth reading.
 */
static int sess_n, agent_n;

static void refresh_bottom_line(void)
{
	char buf[48];

	if (!sess_lbl) {
		return;
	}
	if (provider2_tag[0] && sess_n > 1) {
		snprintf(buf, sizeof(buf), "inner ring: %s   %d sessions",
			 provider2_tag, sess_n);
	} else if (provider2_tag[0]) {
		snprintf(buf, sizeof(buf), "inner ring: %s", provider2_tag);
	} else if (sess_n > 1 && agent_n > 0) {
		snprintf(buf, sizeof(buf), "%d sessions  %d agents", sess_n,
			 agent_n);
	} else if (sess_n > 1) {
		snprintf(buf, sizeof(buf), "%d sessions", sess_n);
	} else if (agent_n > 0) {
		snprintf(buf, sizeof(buf), "%d agents", agent_n);
	} else {
		buf[0] = '\0';
	}
	lv_label_set_text(sess_lbl, buf);
}

static void set_ring2(struct gauge *g, const char *tag, double pct)
{
	char buf[24];

	if (!g->arc2) {
		return;
	}
	if (pct < 0.0 || pct > 100.0) {
		lv_obj_add_flag(g->arc2, LV_OBJ_FLAG_HIDDEN);
		lv_label_set_text(g->p2, "");
		return;
	}
	lv_obj_clear_flag(g->arc2, LV_OBJ_FLAG_HIDDEN);
	lv_arc_set_value(g->arc2, (int32_t)(pct + 0.5));
	lv_obj_set_style_arc_color(g->arc2, severity(pct), LV_PART_INDICATOR);
	/* The number alone. The hollow is GAUGE_HOLLOW_W wide and
	 * "codex 100%" is not, and repeating the tag on both gauges would say
	 * the same thing twice anyway -- it is named once on the bottom line. */
	(void)tag;
	snprintf(buf, sizeof(buf), "%d%%", (int)(pct + 0.5));
	lv_label_set_text(g->p2, buf);
}

void usage_view_set_provider1(const char *tag)
{
	if (!session.arc) {
		return;
	}
	/* The outer ring is whichever provider the daemon made primary, which
	 * on a Codex-only machine is Codex -- so the colour follows the NAME,
	 * not the ring position. */
	lv_obj_set_style_bg_color(session.arc, provider_color(tag),
				  LV_PART_KNOB);
	lv_obj_set_style_bg_color(weekly.arc, provider_color(tag),
				  LV_PART_KNOB);
	lv_obj_set_style_text_color(session.countdown, provider_color(tag), 0);
	lv_obj_set_style_text_color(weekly.countdown, provider_color(tag), 0);
}

void usage_view_set_provider2(const char *tag, double session_pct,
			      double weekly_pct, int32_t session_resets_in_s,
			      int32_t weekly_resets_in_s)
{
	if (!session.arc2 || !weekly.arc2) {
		return;
	}
	if (!tag || !tag[0]) {
		/* No second provider. Both rings and both readouts go away
		 * entirely rather than sitting at zero, which would read as a
		 * provider that exists and has used nothing. */
		provider2_tag[0] = '\0';
		set_ring2(&session, "", -1.0);
		set_ring2(&weekly, "", -1.0);
		lv_obj_add_flag(session.countdown2, LV_OBJ_FLAG_HIDDEN);
		lv_obj_add_flag(weekly.countdown2, LV_OBJ_FLAG_HIDDEN);
		render_countdown(&session);
		render_countdown(&weekly);
		refresh_bottom_line();
		return;
	}
	snprintf(provider2_tag, sizeof(provider2_tag), "%s", tag);
	set_ring2(&session, tag, session_pct);
	set_ring2(&weekly, tag, weekly_pct);

	lv_obj_set_style_bg_color(session.arc2, provider_color(tag),
				  LV_PART_KNOB);
	lv_obj_set_style_bg_color(weekly.arc2, provider_color(tag),
				  LV_PART_KNOB);
	lv_obj_set_style_text_color(session.countdown2, provider_color(tag), 0);
	lv_obj_set_style_text_color(weekly.countdown2, provider_color(tag), 0);

	session.resets2_in_s = session_resets_in_s;
	weekly.resets2_in_s = weekly_resets_in_s;
	lv_obj_clear_flag(session.countdown2, LV_OBJ_FLAG_HIDDEN);
	lv_obj_clear_flag(weekly.countdown2, LV_OBJ_FLAG_HIDDEN);
	render_countdown(&session);
	render_countdown(&weekly);
	refresh_bottom_line();
}

void usage_view_set_sessions(int n_sessions, int n_agents)
{
	/* Clamped at 9 rather than widened: past nine the exact number stops
	 * changing what anyone does about it. */
	sess_n = n_sessions > 9 ? 9 : n_sessions;
	agent_n = n_agents > 9 ? 9 : n_agents;
	refresh_bottom_line();
}


void usage_view_set_status(enum usage_status status)
{
	if (!built) {
		return;
	}
	last_status = status;

	lv_color_t c;
	lv_color_t tc = COL_DIM;
	const char *text;

	switch (status) {
	case USAGE_STATUS_OK:
		c = COL_GREEN;
		/* Near-limit call-out: the whole point of a glanceable
		 * display is knowing this before the API tells you no
		 * (user request 2026-07-17). Session first -- it bites
		 * sooner. At 100% (the display rounds >=99.5 up to "100%")
		 * the limit is spent, not "almost" -- say so (user-reported
		 * 2026-07-20). */
		if (last_s_pct >= 99.5) {
			text = "Session used up";
			tc = COL_RED;
		} else if (last_s_pct >= 95.0) {
			text = "Session almost used up";
			tc = COL_RED;
		} else if (last_w_pct >= 99.5) {
			text = "Weekly used up";
			tc = COL_RED;
		} else if (last_w_pct >= 95.0) {
			text = "Weekly almost used up";
			tc = COL_RED;
#if HAVE_PER_MODEL
		} else if (model_fable >= 99.5) {
			text = "Fable weekly used up";
			tc = COL_RED;
		} else if (model_fable >= 95.0) {
			/* Fable's window is a real limit of its own; it can
			 * run dry while the overall weekly still looks calm. */
			text = "Fable weekly almost used up";
			tc = COL_RED;
#endif
		} else {
			text = "";
		}
		break;
	case USAGE_STATUS_STALE:
		c = COL_AMBER;
		tc = COL_AMBER;
		/* Not "rate-limited": this state means the daemon has no fresh
		 * reading, which is usually its owner being away from Claude Code.
		 * The old wording is from when a 429 from the usage endpoint was
		 * the only way to get here; that endpoint is gone, and telling
		 * someone they are rate-limited when they are not is worse than
		 * saying nothing. */
		text = "Reading is old - showing last known";
		break;
	case USAGE_STATUS_ERROR:
		c = COL_RED;
		tc = COL_RED;
		text = "Error - showing last known";
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

	usage_view_sync_takeover();
}

/*
 * Apply the takeover rule from the CURRENT state.
 *
 * The full takeover is only for "we never had any data". Every other state has
 * real numbers behind it, and covering those up would throw away information
 * the user wants.
 *
 * Split out from set_status so it can be re-asserted. ui_anim hides every
 * sibling of its overlay for the length of the clip and restores them after,
 * from a record taken when the clip STARTED -- so if the first data arrives
 * mid-clip, that record is stale and the restore would put the CONNECTING bar
 * back over live gauges (user-reported 2026-08-18). This function is the one
 * authority on whether the bar belongs on screen; the restore calls it rather
 * than deciding for itself.
 */
void usage_view_sync_takeover(void)
{
	if (!built || overlay == NULL) {
		return;
	}
	if (last_status == USAGE_STATUS_DISCONNECTED && !have_data) {
		lv_obj_clear_flag(overlay, LV_OBJ_FLAG_HIDDEN);
		lv_obj_move_foreground(overlay);
	} else {
		lv_obj_add_flag(overlay, LV_OBJ_FLAG_HIDDEN);
	}
}

bool usage_view_takeover_active(void)
{
	return built && overlay != NULL &&
	       lv_obj_has_flag(overlay, LV_OBJ_FLAG_HIDDEN) == false;
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
