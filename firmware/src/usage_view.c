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
/*
 * The severity ramp: green, amber, red -- and all three sit inside a 1.16x
 * luminance band on purpose.
 *
 * They used to span 2.30x, INVERTED: amber at 11.39:1 was the brightest thing
 * on the panel and red at 4.95:1 was the dimmest, so the merely-getting-close
 * colour shouted over the critical one. Brightness is attention on a dark
 * panel, so that was the hierarchy the eye actually applied, whatever the code
 * intended.
 *
 * The fix is not a brighter red -- a red bright enough to outshine a yellow is
 * a pale salmon and stops reading as red at all; that is the physics of
 * luminance, not a palette choice. So luminance is held flat and urgency is
 * carried by hue and by the arc's own area. A 95% arc is a nearly-complete
 * ring; it does not need to shout as well.
 *
 * WHAT CHANGED, 2026-08-27, and it is the part worth reading. Holding
 * luminance flat was right; paying for it with SATURATION was not. The first
 * version of this ramp ran 0.58 -> 0.66 -> 0.72 and called that gradient the
 * urgency signal. On the actual panel the user's verdict was "the green looks
 * like gray", and they were right twice over:
 *
 *   - 0.58 saturation is 42% of the way to grey before the panel touches it,
 *     and the old green sat at hue 150 with MORE BLUE THAN RED (#4AB07D:
 *     R=74, B=125). That is a sea green, and a sea green on a cheap ILI9341
 *     is a grey.
 *   - a saturation ramp of 0.58 -> 0.72 is not a signal anyone can read at
 *     60 cm. Hue is what carries green/amber/red, instantly, for everyone who
 *     can resolve it; the arc's area carries it for everyone else. The ramp
 *     was spending real colour to encode something nothing else needed.
 *
 * So saturation is now near the top of the range at every step (0.92, 0.96,
 * 1.00) and the flat band is unchanged. All three were re-derived against the
 * background AND re-checked after quantisation to RGB565, which is what the
 * panel actually shows -- see tests/usage_contrast, where computing contrast
 * for a colour the hardware cannot display was the gap that let this ship.
 */
#define COL_GREEN	lv_color_hex(0x0DA243)
#define COL_GREEN_INK	lv_color_hex(0x06210F)
#define COL_AMBER	lv_color_hex(0xBA8107)
#define COL_RED		lv_color_hex(0xFF1900)
/* 3.91:1 against the background. Was #555B63 at 2.76:1, which is under the
 * 3:1 minimum for a graphic element -- and these are the swipe chevrons, which
 * exist only because nobody discovered the gestures without them. An
 * affordance too dim to notice is not an affordance. */
#define COL_GREY	lv_color_hex(0x6B7280)

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
/*
 * Identity colours, and they sit BELOW the severity ramp deliberately.
 *
 * The teal used to measure 10.16:1 -- brighter than every severity colour, so
 * a codex ball at 5% pulled more attention than a Claude arc at 95%. Identity
 * outranking urgency is exactly backwards. Both now sit inside the severity
 * band, still 1.56:1 apart from each other so the pair survives for anyone who
 * cannot resolve the hue difference.
 */
#define COL_OTHER	lv_color_hex(0x4387DF)	/* anything else: a cool blue */

struct gauge {
	lv_obj_t *arc;
	lv_obj_t *pct;
	lv_obj_t *name;
	lv_obj_t *countdown;	/* primary provider's time left */
	int32_t resets_in_s;	/* -1 = unknown; ticked down locally */
	int32_t resets2_in_s;	/* ...and the second provider's */
	/*
	 * How fast this window is filling, %/hour, or 0 for "no answer".
	 *
	 * Drawn in the countdown's place, and ONLY when there is no countdown.
	 * The daemon guarantees the two are mutually exclusive (pc/normalizer
	 * drops the rate the moment any source supplies a reset time), so this
	 * never has to arbitrate between them -- it just prefers the real
	 * countdown and falls back.
	 */
	double burn;
};

static struct gauge session, weekly;
/*
 * ONE attention indicator, not two.
 *
 * There used to be a status dot top-right (is the data trustworthy) and an
 * activity pip top-left (what is Claude Code doing). Two unlabelled circles in
 * the two most prominent corners, in the same green/amber/red vocabulary,
 * saying unrelated things -- so a red dot could mean "you are rate limited", "a
 * tool is wedged" or "the cable fell out", and the panel gave no way to tell.
 *
 * They were never two facts. Both answer "is something wrong, and how badly":
 * OK -> stale -> error, and running/idle -> waiting -> stuck/failed are the
 * same axis. So they collapse: one dot, coloured by the WORSE of the two, and
 * the hint line -- already there, already empty when all is well -- says which
 * one fired. Colour means one thing again.
 */
static lv_obj_t *dot;
static enum usage_status data_health = USAGE_STATUS_DISCONNECTED;
static lv_obj_t *hint;
static lv_obj_t *age_lbl;
static lv_obj_t *clock_lbl;
static enum usage_activity activity = USAGE_ACTIVITY_NONE;
static char provider1_tag[12];	/* whichever provider the daemon made primary */
static lv_obj_t *provider_lbl;	/* the tag, spelled out under the brand */
static char provider2_tag[12];	/* "" when there is only one provider */

/*
 * PAGES. One provider per screen, reached by swiping vertically.
 *
 * There is only ONE set of gauge widgets. The pages are identical in shape --
 * a session arc, a weekly arc, two countdowns -- so a second tree would be the
 * same objects twice over on a board with 96 KB of DRAM to spare. What changes
 * between pages is which provider's numbers are pushed into them, so the page
 * is a data selection, not a screen.
 *
 * Both pages' countdowns tick regardless of which one is showing: a window
 * does not stop closing because you are not looking at it, and a page that
 * caught up only when you arrived would show a stale figure for the moment
 * that matters most.
 */
struct page_data {
	/*
	 * This page's own age. See usage_view_set_provider2 -- the panel used
	 * to carry one flag for both providers, and showed it whichever page
	 * was in front of you.
	 */
	bool stale;
	double s_pct, w_pct;
	int32_t s_in_s, w_in_s;
	/* Per page, like everything else here: the rate describes THIS
	 * provider's session window, and page 1 has its own reset times and so
	 * never carries one. */
	double burn;
	bool have;
};
static struct page_data pg[RAIL_PAGES_MAX];
static int cur_page;
static lv_obj_t *rail_dot[RAIL_PAGES_MAX];

static const char *page_tag(int i)
{
	return i == 0 ? provider1_tag : provider2_tag;
}

/*
 * A provider tag as a NAME: "codex" -> "Codex".
 *
 * The tags arrive lowercase from the daemon, which is right for a wire
 * protocol and wrong for a panel -- BLINK is the only all-caps thing in the
 * header, and everything beneath it is sentence case. Two places need this
 * now, the name under the brand and the line that says where the other page
 * is, and they were drifting apart the moment there were two of them.
 */
static void tag_cased(char *dst, size_t n, const char *tag)
{
	snprintf(dst, n, "%s", tag);
	if (dst[0] >= 'a' && dst[0] <= 'z') {
		dst[0] = (char)(dst[0] - 'a' + 'A');
	}
}

static int page_count(void)
{
	return provider2_tag[0] ? 2 : 1;
}
/* Defined below; set_provider2 has to call it, because whether a second
 * provider exists is what decides if the FIRST one is coloured at all. */
static void refresh_provider1(void);
static void refresh_rail(void);
static void render_gauges(void);
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
	lv_obj_set_style_bg_color(g->arc, COL_TEXT, LV_PART_KNOB);
	lv_obj_set_style_bg_opa(g->arc, LV_OPA_COVER, LV_PART_KNOB);
	/*
	 * A ring of the panel's own ground, so the ball separates from the arc
	 * it sits on whatever colour that arc happens to be. Without it the
	 * ball measured 1.24:1 against a red arc and 1.07:1 against a green one
	 * -- it vanished into the track at exactly the moments the track was
	 * saying something.
	 */
	lv_obj_set_style_border_color(g->arc, COL_BG, LV_PART_KNOB);
	lv_obj_set_style_border_width(g->arc, GAUGE_BALL_RING, LV_PART_KNOB);
	lv_obj_set_style_border_opa(g->arc, LV_OPA_COVER, LV_PART_KNOB);
	lv_obj_set_style_arc_width(g->arc, GAUGE_ARC_W, LV_PART_MAIN);
	lv_obj_set_style_arc_width(g->arc, GAUGE_ARC_W, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(g->arc, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(g->arc, COL_GREEN, LV_PART_INDICATOR);

	/* The second provider's ring, hidden until one exists. Created before
	 * the labels so the text draws over it, not under. */
	/*
	 * Fixed width, centred text.
	 *
	 * Montserrat's digits are PROPORTIONAL -- measured from the font data,
	 * '1' is 5.19 px at size 14 against 9.38 px for '4', and 7.38 vs 13.38
	 * at size 20. An auto-sized label therefore changes width as the digits
	 * change, and a countdown ticking once a second re-lays-out its whole
	 * row. Pinning the box stops that reaching anything else on the panel.
	 * The text still re-centres inside it, which is what a clock does.
	 */
	g->pct = lv_label_create(parent);
	lv_label_set_text(g->pct, "--%");
	lv_obj_set_style_text_color(g->pct, COL_TEXT, 0);
	lv_obj_set_style_text_font(g->pct, &lv_font_montserrat_20, 0);
	lv_obj_set_width(g->pct, GAUGE_PCT_MAX_W);
	lv_obj_set_style_text_align(g->pct, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(g->pct, LV_ALIGN_TOP_MID, cx, GAUGE_PCT_Y);

	g->name = lv_label_create(parent);
	lv_label_set_text(g->name, title);
	lv_obj_set_style_text_color(g->name, COL_DIM, 0);
	lv_obj_align(g->name, LV_ALIGN_TOP_MID, cx, GAUGE_NAME_Y);

	/* The inner ring's own figure, small and dim under the countdown. The
	 * primary provider keeps the big number; this one is there to be
	 * noticed, not read first. */

	/* Both countdowns sit under the caption, stacked, each naming its own
	 * provider. Centred on the gauge whether there is one or two -- the
	 * second simply appears on the line below. */
	g->resets2_in_s = -1;
	g->countdown = lv_label_create(parent);
	lv_label_set_text(g->countdown, "--");
	lv_obj_set_style_text_color(g->countdown, COL_DIM, 0);
	lv_obj_set_width(g->countdown, GAUGE_CD_MAX_W);
	lv_obj_set_style_text_align(g->countdown, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(g->countdown, LV_ALIGN_TOP_MID, cx, GAUGE_CD_Y);

}

static void render_countdown(struct gauge *g)
{
	char buf[FMT_COUNTDOWN_MAX];

	/*
	 * The real countdown always wins. The rate is what goes here when the
	 * source cannot say when the window rolls -- Claude Desktop with no
	 * Claude Code -- and it is a different KIND of statement, so it is
	 * never mixed with one: either a time or a rate, never both, never one
	 * standing in for the other while the other exists.
	 */
	if (g->resets_in_s < 0 && g->burn > 0) {
		fmt_burn(g->burn, buf, sizeof(buf));
		if (buf[0] != '\0') {
			lv_label_set_text(g->countdown, buf);
			return;
		}
	}
	fmt_countdown(g->resets_in_s, buf, sizeof(buf));
	/*
	 * A duration, and nothing else. The name used to ride with the number
	 * because the panel showed two providers at once and a bare figure had
	 * to be matched to one of them. One provider per page removes the
	 * question, so the answer stops being worth a line under both gauges.
	 */
	lv_label_set_text(g->countdown, buf);

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
	provider_lbl = NULL;
	for (int i = 0; i < RAIL_PAGES_MAX; i++) {
		rail_dot[i] = NULL;
	}
	cur_page = 0;
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
	lv_obj_set_width(hint, STATUS_MAX_W);
	lv_obj_set_style_text_align(hint, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(hint, LV_ALIGN_TOP_MID, 0, STATUS_Y);



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

	/* Whose numbers these are, directly under the brand. Blank until the
	 * daemon says -- an empty line reads as "nothing to report", where a
	 * placeholder would read as a provider actually named that. */
	for (int i = 0; i < RAIL_PAGES_MAX; i++) {
		rail_dot[i] = lv_obj_create(scr);
		lv_obj_set_size(rail_dot[i], RAIL_DOT_W, RAIL_H);
		lv_obj_set_style_radius(rail_dot[i], LV_RADIUS_CIRCLE, 0);
		lv_obj_set_style_border_width(rail_dot[i], 0, 0);
		lv_obj_set_style_bg_color(rail_dot[i], COL_GREY, 0);
		lv_obj_add_flag(rail_dot[i], LV_OBJ_FLAG_HIDDEN);
		/* A swipe that starts on a mark must still reach the screen,
		 * or the gesture dies exactly where its own affordance is. */
		lv_obj_add_flag(rail_dot[i], LV_OBJ_FLAG_GESTURE_BUBBLE);
		/*
		 * ...and neither may a TAP die there. lv_obj_create() sets
		 * CLICKABLE by default, and LVGL's hit test walks children
		 * back to front, so a dot -- created here, after the page
		 * zone was moved to the background -- won the hit and swallowed
		 * the tap. The dots carry no click handler, so nothing
		 * happened at all: a dead spot sitting exactly on the marks
		 * that advertise the page change. The arc (:325) and the peek
		 * card (:678) were cleared for the same reason; these were
		 * missed.
		 */
		lv_obj_clear_flag(rail_dot[i], LV_OBJ_FLAG_CLICKABLE);
	}

	/*
	 * Whose numbers these are -- and, with two providers, the control that
	 * changes it.
	 *
	 * It used to sit under the brand while the bottom of the screen
	 * carried a second line naming the OTHER provider: two names on one
	 * screen for one page, the header saying "Codex" while the pill said
	 * "Claude", and the reader left to work out which was which. One name
	 * is enough and the one worth keeping is the page you are on.
	 *
	 * Down here it is the same object as the control -- mk_page_zone puts
	 * the hit area over this band -- which is the idiom the settings
	 * panel's old "Main source" row used: the value IS the button.
	 */
	provider_lbl = lv_label_create(scr);
	lv_label_set_text(provider_lbl, "");
	lv_obj_set_style_text_color(provider_lbl, COL_TEXT, 0);
	lv_obj_set_style_bg_color(provider_lbl, COL_PANEL, 0);
	lv_obj_set_style_radius(provider_lbl, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_pad_hor(provider_lbl, 10, 0);
	lv_obj_set_style_pad_ver(provider_lbl, PILL_PAD_V, 0);
	lv_label_set_long_mode(provider_lbl, LV_LABEL_LONG_DOT);
	lv_obj_set_style_text_align(provider_lbl, LV_TEXT_ALIGN_CENTER, 0);
	lv_obj_align(provider_lbl, LV_ALIGN_BOTTOM_MID, 0, -PILL_BOTTOM_OFF);

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

/* Push the current page's numbers into the one set of gauge widgets. */
static void render_gauges(void)
{
	struct page_data *p = &pg[cur_page];
	char buf[8];

	if (!built) {
		return;
	}

	/* A negative percentage means "the daemon does not have this number" --
	 * pc/statusline_source.py sends -1.0 when the window is absent from
	 * Claude Code's payload entirely. render_weekly() has always honoured
	 * that; this side did not, so an absent five_hour window rendered as a
	 * confident green 0%, which is the frozen-meter reading the sentinel
	 * exists to prevent. */
	if (p->s_pct < 0) {
		lv_label_set_text(session.pct, "--%");
		lv_arc_set_value(session.arc, 0);
	} else {
		snprintf(buf, sizeof(buf), "%d%%", (int)(p->s_pct + 0.5));
		lv_label_set_text(session.pct, buf);
		lv_arc_set_value(session.arc, (int32_t)(p->s_pct + 0.5));
		lv_obj_set_style_arc_color(session.arc, severity(p->s_pct),
					   LV_PART_INDICATOR);
	}
	session.resets_in_s = p->s_in_s;
	session.burn = p->burn;
	render_countdown(&session);

	/* No rate on the weekly gauge. A seven-day slope measured over half an
	 * hour is noise, and the daemon does not send one. */
	weekly.resets_in_s = p->w_in_s;
	render_countdown(&weekly);

	last_s_pct = p->s_pct;
	last_w_pct = p->w_pct;
	render_weekly();
	refresh_provider1();	/* the name under the brand follows the page */
}

/*
 * Whether "the reading is old" is true of the page being SHOWN.
 *
 * The status arrives as one flag describing the FIRST provider, and with two
 * providers that is a statement about one of two pages. A machine running
 * Claude Code all day with Codex touched once that morning has a stale codex
 * reading and a live claude one -- and the claude page was announcing
 * "Reading is old" over numbers that were updating in front of the user
 * (reported 2026-08-28). Freshness belongs to a reading, not to the panel.
 *
 * With one provider there is nothing to distinguish and the flag is the whole
 * truth, so it is taken as-is: pg[0].stale is only written by the daemon that
 * also sets the status, and a board talking to an older daemon never receives
 * p2_stale at all. Falling back to the status keeps that case behaving exactly
 * as it did.
 */
static bool stale_here(void)
{
	if (page_count() < 2) {
		return true;
	}
	return pg[cur_page].stale;
}

/*
 * The worse of a page's two windows.
 *
 * A rail mark has one colour and two windows to speak for, so it speaks for
 * whichever is closer to the wall. Averaging them would let a fresh weekly
 * quota talk a spent session down into amber, which is the reading that
 * matters getting hidden by the one that does not.
 */
static double page_worst(int i)
{
	double a = pg[i].s_pct, b = pg[i].w_pct;

	return a > b ? a : b;
}

/*
 * A page change, at whatever point through it the rail currently is.
 *
 * ONE description covers the whole thing: `rail_a` is the mark being left,
 * `rail_b` is the mark being reached, and `rail_pct` is how far the handover
 * has got. Settled is simply rail_a = -1 and rail_pct = 100.
 *
 * That unification is the fix for "the tail of the swipe is not showing". The
 * first version drove this only from the finger, and the finger does not
 * supply enough of it: the panel emits 2 to 5 reports for a whole stroke,
 * sometimes jumping 149 px between two of them, so the window between the drag
 * line and the threshold usually contains NO sample at all. The rail went from
 * idle to settled with nothing drawn in between -- correct, and invisible.
 *
 * So the finger starts the handover and an animation FINISHES it. Once the
 * swipe fires, the rail keeps travelling to the new page under its own power
 * over RAIL_SETTLE_MS instead of snapping. That part is drawn rather than
 * sampled, so it does not care how few reports the panel gave -- and it is
 * what is actually visible on a fast flick, where the whole stroke is over in
 * 70 ms.
 *
 * The rail is the right place for this and the only cheap one: it is already
 * the answer to "which page am I on", it is six pixels tall so redrawing it
 * costs nothing, and moving the weight from one mark to the other says which
 * page AND how far through in the same picture.
 */
#define RAIL_SETTLE_MS		200

/*
 * Both marks are named explicitly rather than one of them being "cur_page".
 *
 * They are not the same page at the two ends of this. DURING a stroke cur_page
 * is still the page being LEFT -- nothing has changed yet. After the swipe
 * fires cur_page has already advanced, so it is the page being REACHED. Keying
 * the drawing off it would mean the rail ran backwards across that instant,
 * which is the one moment it most needs to be continuous.
 */
static int rail_a = -1;		/* the mark being left, -1 when settled */
static int rail_b;		/* the mark being reached */
static int rail_pct = 100;	/* how far the handover has got */

/* Linear, in whole pixels: these are 6 and 16 px wide, so there is nothing an
 * easing curve could express that the panel could show. */
static int lerp_px(int a, int b, int pct)
{
	return a + (b - a) * pct / 100;
}

static void refresh_rail(void)
{
	int n = page_count();
	int span = n * RAIL_PITCH - (RAIL_PITCH - RAIL_DOT_W);
	int x = (SCR_W - span) / 2;
	bool handing_over;

	if (!rail_dot[0]) {
		return;
	}
	/*
	 * Settled means rail_b IS the current page, whatever moved cur_page.
	 * Most of this file's callers change the page without going through a
	 * swipe at all -- provider 2 arriving or disappearing falls back to
	 * page 0 on its own -- and without this the rail would keep drawing
	 * the page that was current when the last stroke ended.
	 */
	if (rail_a < 0) {
		rail_b = cur_page;
		rail_pct = 100;
	}
	handing_over = rail_a >= 0 && rail_a < n && rail_a != rail_b;
	for (int i = 0; i < RAIL_PAGES_MAX; i++) {
		int w = RAIL_DOT_W;
		lv_opa_t opa = LV_OPA_50;

		/* One page: nowhere to go, so there is no indicator to decode
		 * and no gesture to discover. The single-provider desk keeps
		 * exactly the screen it had. */
		if (i >= n || n < 2) {
			lv_obj_add_flag(rail_dot[i], LV_OBJ_FLAG_HIDDEN);
			continue;
		}
		/*
		 * The two marks trade places continuously: the one being left
		 * gives up exactly what the one being reached takes on. Half
		 * way through they are the same size, which is the honest
		 * picture of a page change that is under way and not finished
		 * -- true both of a stroke still being made and of one that has
		 * fired and is still landing.
		 */
		if (i == rail_b) {
			w = lerp_px(handing_over ? RAIL_DOT_W : RAIL_ACT_W,
				    RAIL_ACT_W, rail_pct);
			opa = (lv_opa_t)lerp_px(handing_over ? LV_OPA_50
							     : LV_OPA_COVER,
						LV_OPA_COVER, rail_pct);
		} else if (handing_over && i == rail_a) {
			w = lerp_px(RAIL_ACT_W, RAIL_DOT_W, rail_pct);
			opa = (lv_opa_t)lerp_px(LV_OPA_COVER, LV_OPA_50,
						rail_pct);
		}
		lv_obj_clear_flag(rail_dot[i], LV_OBJ_FLAG_HIDDEN);
		lv_obj_set_size(rail_dot[i], w, RAIL_H);
		/* Centred on its own slot whatever width it currently has, so
		 * it grows from the middle rather than off to one side. */
		lv_obj_set_pos(rail_dot[i],
			       x + i * RAIL_PITCH - (w - RAIL_DOT_W) / 2,
			       SCR_H - RAIL_BOTTOM_OFF - RAIL_H);
		lv_obj_set_style_bg_color(rail_dot[i],
					  pg[i].have ? severity(page_worst(i))
						     : COL_GREY, 0);
		lv_obj_set_style_bg_opa(rail_dot[i], opa, 0);
	}
	/* The pill names the page, so it follows the rail: both answer "which
	 * one am I on", and neither may be left behind by a change to the
	 * other. */
	refresh_provider1();
}

/*
 * Move one page, and say whether there is one to move to.
 *
 * This used to run straight from the gesture callback as a CUT -- retext the
 * labels, re-value the arcs, done in one repaint -- on the reasoning that a
 * vertical transition had no hardware path: the panel's scroll register moves
 * the screen sideways only, so up/down would mean a full LVGL redraw per
 * frame, and two or three stepping frames read as broken.
 *
 * That reasoning was about the SLIDE. The shipped transition is a WIPE
 * (ui_slide.c, UI_SLIDE_WIPE), which never touches the scroll register: it
 * paints the incoming screen one strip at a time, straight into the columns or
 * rows where it will be seen. That costs one full render for the whole
 * transition however finely it is chopped, and it does not care which axis it
 * chops along -- so the vertical direction was available the entire time and
 * the cut was paying a price the design had already stopped charging.
 *
 * The user's verdict on the cut, looking at the board: "the swipe up/down is
 * not showing like a swipe".
 *
 * So the page change is a transition now, which moves it out of the gesture
 * callback: ui_slide_run() drives lv_refr_now() itself and must not be
 * re-entered from inside lv_timer_handler(). The gesture flags the direction
 * and ui_settings_service() runs it from the mode loop, the same way opening
 * settings and the boot clip already do. can_page() is what lets the gesture
 * decline to arm a 650 ms blocking transition that would change nothing.
 */
bool usage_view_can_page(int delta)
{
	int n = page_count();
	int next = cur_page + delta;

	return built && n >= 2 && next >= 0 && next < n;
}

/*
 * The page change, as the gauges re-reading rather than the screen changing.
 *
 * Three transitions were tried on this axis before this one. A CUT was
 * reported as not looking like a swipe. A WIPE reads as a repaint, because
 * the two pages are the same layout and the boundary between them has nothing
 * to be made of. A wipe with a bright leading edge gave that boundary
 * something to see, and it still "doesn't feel natural" -- which it is not: a
 * bar sweeping the panel is an object that exists nowhere else on this device
 * and means nothing when it arrives.
 *
 * What was wrong with all three is that they were transitions between two
 * PICTURES. This is an instrument, and the honest motion for one is the needle
 * moving: the ring travels from the percentage it was showing to the one the
 * other provider is at, and the number under it counts along. Nothing is
 * covered, nothing slides, nothing is revealed. The screen does not change --
 * the reading does.
 *
 * It is also the only motion this hardware can render smoothly. A full-screen
 * transition costs a whole repaint however it is chopped up; the arcs and
 * their labels are a fraction of the panel, and LVGL invalidates only what
 * actually moved.
 */
#define PAGE_MORPH_MS		380
#define MORPH_STEPS		256
#define MORPH_MID		(MORPH_STEPS / 2)

static struct {
	double s_from, w_from;	/* what the rings were showing */
	double s_to, w_to;	/* what the page being moved to says */
	bool jump;		/* one side has no number: cut, do not travel */
	bool swapped;		/* the midpoint text swap has happened */
} morph;

/*
 * The words do not interpolate, so they cross the middle instead.
 *
 * A provider's NAME has no midpoint between "Claude" and "Codex", and neither
 * does a countdown -- rolling "6d 22h" towards "4d 15h" would be inventing a
 * duration that is true of nothing. They fade out, change while they cannot be
 * read, and fade back: the standard answer, and it costs only the label boxes.
 *
 * The numbers do NOT do this. They travel, because a percentage between two
 * percentages is a real percentage and watching it cross is the whole point.
 */
static void morph_text_opa(int32_t t)
{
	int d = t < MORPH_MID ? MORPH_MID - t : t - MORPH_MID;
	lv_opa_t o = (lv_opa_t)(d * LV_OPA_COVER / MORPH_MID);

	lv_obj_set_style_text_opa(provider_lbl, o, 0);
	lv_obj_set_style_text_opa(session.countdown, o, 0);
	lv_obj_set_style_text_opa(weekly.countdown, o, 0);
}

/* One ring: where it is now, on the way from one reading to the other. */
static void morph_arc(struct gauge *g, double pct)
{
	char buf[8];

	if (pct < 0) {
		lv_label_set_text(g->pct, "--%");
		lv_arc_set_value(g->arc, 0);
		return;
	}
	snprintf(buf, sizeof(buf), "%d%%", (int)(pct + 0.5));
	lv_label_set_text(g->pct, buf);
	lv_arc_set_value(g->arc, (int32_t)(pct + 0.5));
	/*
	 * Severity follows the value it is describing, so the ring changes
	 * colour where it crosses the threshold rather than at either end.
	 * Interpolating the colours themselves would put the ring through hues
	 * that mean nothing -- olive between green and amber -- and this
	 * palette's whole job is that a colour means one thing.
	 */
	lv_obj_set_style_arc_color(g->arc, severity(pct), LV_PART_INDICATOR);
}

static void morph_exec(void *unused, int32_t t)
{
	ARG_UNUSED(unused);

	if (!built) {
		return;
	}
	if (t >= MORPH_MID && !morph.swapped) {
		/*
		 * Halfway, with the words invisible: everything that cannot be
		 * interpolated changes here, in one go, behind the fade.
		 */
		morph.swapped = true;
		refresh_provider1();
		session.resets_in_s = pg[cur_page].s_in_s;
		session.burn = pg[cur_page].burn;
		weekly.resets_in_s = pg[cur_page].w_in_s;
		render_countdown(&session);
		render_countdown(&weekly);
	}
	morph_text_opa(t);

	if (morph.jump) {
		/*
		 * "--%" is not a number and there is no path between it and
		 * one. A ring travelling out of a blank reading would be
		 * animating a value the device does not have, so this side
		 * changes at the midpoint with the words.
		 */
		if (morph.swapped) {
			morph_arc(&session, morph.s_to);
			morph_arc(&weekly, morph.w_to);
		}
		return;
	}

	double k = (double)t / MORPH_STEPS;

	morph_arc(&session, morph.s_from + (morph.s_to - morph.s_from) * k);
	morph_arc(&weekly, morph.w_from + (morph.w_to - morph.w_from) * k);
}

static void morph_done(lv_anim_t *a)
{
	ARG_UNUSED(a);

	/*
	 * Land on the real values rather than on the last interpolated frame.
	 *
	 * The animation ends at t = MORPH_STEPS, so k is exactly 1 and the
	 * arithmetic already agrees -- but only in exact arithmetic. Calling
	 * the normal render path is what guarantees the screen is showing the
	 * page's own numbers afterwards, and it also restores anything the
	 * morph did not touch. The text opacity has to be put back by hand:
	 * it is a style, not a value, and nothing else in this file sets it.
	 */
	lv_obj_set_style_text_opa(provider_lbl, LV_OPA_COVER, 0);
	lv_obj_set_style_text_opa(session.countdown, LV_OPA_COVER, 0);
	lv_obj_set_style_text_opa(weekly.countdown, LV_OPA_COVER, 0);
	render_gauges();
	/*
	 * And only NOW re-ask the status question. page_step() asks it too,
	 * but at that moment last_s_pct/last_w_pct still describe the page
	 * being left -- they move here, in render_gauges() -- so the "almost
	 * used up" hint it produced belonged to the wrong page and stayed
	 * wrong until the next usage message, up to a poll interval later.
	 */
	usage_view_set_status(last_status);
}

/*
 * Drive the handover from an animation, once the finger has stopped supplying
 * it. See RAIL_SETTLE_MS.
 */
static void rail_settle_exec(void *unused, int32_t pct)
{
	ARG_UNUSED(unused);
	rail_pct = pct;
	refresh_rail();
}

static void rail_settled(lv_anim_t *a)
{
	ARG_UNUSED(a);
	/* The mark being left stops being a thing, which is what makes the
	 * next stroke's preview start from a settled rail rather than from the
	 * tail of this one. */
	rail_a = -1;
	rail_pct = 100;
	refresh_rail();
}

void usage_view_page_preview(int delta, int pct)
{
	int to = delta == 0 ? -1 : cur_page + delta;

	if (!built) {
		return;
	}
	/*
	 * Never while the rail is landing under its own power. The stroke that
	 * caused it has usually not been lifted yet, so its progress is still
	 * being published -- and honouring it would drag the rail back towards
	 * a handover that has already happened.
	 */
	if (lv_anim_get(NULL, rail_settle_exec) != NULL) {
		return;
	}
	if (to < 0 || to >= page_count() || pct <= 0) {
		/* Let go without committing: back to settled, at once. There is
		 * nothing to animate towards -- the page did not change -- and
		 * an eased retreat would read as a page change being undone
		 * rather than as one that never started. */
		to = cur_page;
		pct = 100;
		if (rail_a == -1 && rail_b == to && rail_pct == 100) {
			return;
		}
		rail_a = -1;
	} else {
		rail_a = cur_page;
	}
	/*
	 * Dropped when nothing changed, because this arrives every drain tick
	 * whether or not the finger moved -- ui_swipe publishes a state, not
	 * an event. Without this the rail would be re-laid-out a hundred times
	 * a second for a picture that is already correct.
	 */
	if (to == rail_b && pct == rail_pct) {
		return;
	}
	rail_b = to;
	rail_pct = pct;
	refresh_rail();
}

void usage_view_page_step(int delta)
{
	if (!usage_view_can_page(delta)) {
		/* A stroke that asked for a page that is not there still has to
		 * put the rail back; its preview is on screen. */
		usage_view_page_preview(0, 0);
		return;
	}

	/*
	 * Where the rings are NOW, not where the page being left says they
	 * should be. Paging twice quickly is the case that separates the two:
	 * the second swipe arrives mid-travel, and starting its animation from
	 * the page's stored value would snap the ring backwards before setting
	 * off again.
	 */
	morph.s_from = last_s_pct;
	morph.w_from = last_w_pct;

	cur_page += delta;

	morph.s_to = pg[cur_page].s_pct;
	morph.w_to = pg[cur_page].w_pct;
	morph.jump = morph.s_from < 0 || morph.s_to < 0 ||
		     morph.w_from < 0 || morph.w_to < 0;
	morph.swapped = false;

	/*
	 * The rail leads, and it FINISHES the handover the finger started.
	 *
	 * Carrying on from wherever the preview reached, rather than jumping,
	 * is what makes the swipe visible at all. A stroke gives the panel 2
	 * to 5 reports before it crosses the threshold and can jump 149 px
	 * between two of them, so the preview often never gets a frame -- the
	 * rail would go from idle to settled with nothing drawn in between.
	 * This part is drawn rather than sampled, so it does not care how few
	 * reports there were.
	 *
	 * rail_a is where cur_page was a moment ago; cur_page has already
	 * advanced above.
	 */
	/*
	 * Re-ask the status question for the page now in front.
	 *
	 * "Reading is old" is per page (see stale_here), so changing page can
	 * change the answer -- and nothing else would re-run it until the next
	 * usage message, up to a poll interval later. Re-applying the status
	 * already held recomputes the hint and the dot from the new page
	 * without inventing a reading.
	 */
	usage_view_set_status(last_status);

	rail_a = cur_page - delta;
	rail_b = cur_page;

	lv_anim_t ra;

	lv_anim_init(&ra);
	lv_anim_set_var(&ra, NULL);
	lv_anim_set_exec_cb(&ra, rail_settle_exec);
	lv_anim_set_values(&ra, rail_pct, 100);
	/* Time what is LEFT, so a stroke that had already dragged the rail
	 * most of the way finishes quickly instead of crawling the last few
	 * percent over a fifth of a second. */
	/* Floored, not just scaled: a stroke that dragged the rail all the way
	 * to 100 before firing would otherwise get a zero-length animation,
	 * whose completion callback is what puts rail_a back to -1. */
	lv_anim_set_time(&ra, MAX(30, RAIL_SETTLE_MS * (100 - rail_pct) / 100));
	lv_anim_set_path_cb(&ra, lv_anim_path_ease_out);
	lv_anim_set_completed_cb(&ra, rail_settled);
	lv_anim_delete(NULL, rail_settle_exec);
	lv_anim_start(&ra);

	refresh_rail();

	lv_anim_t a;

	lv_anim_init(&a);
	lv_anim_set_var(&a, NULL);
	lv_anim_set_exec_cb(&a, morph_exec);
	lv_anim_set_values(&a, 0, MORPH_STEPS);
	lv_anim_set_time(&a, PAGE_MORPH_MS);
	/*
	 * Eased at both ends. A needle that starts and stops abruptly reads as
	 * a jump with a delay in the middle; this is the one place on the panel
	 * where something is meant to look like it has mass.
	 */
	lv_anim_set_path_cb(&a, lv_anim_path_ease_in_out);
	lv_anim_set_completed_cb(&a, morph_done);
	/* Restarting replaces the running one rather than queueing a second
	 * animation against the same callback, so a fast double swipe travels
	 * once, to the page it ends on. */
	lv_anim_delete(NULL, morph_exec);
	lv_anim_start(&a);
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

	pg[0].s_pct = session_pct;
	pg[0].w_pct = weekly_pct;
	pg[0].s_in_s = session_resets_in_s;
	pg[0].w_in_s = weekly_resets_in_s;
	pg[0].have = true;

	have_data = true;
	age_s = 0;
	refresh_rail();		/* page 0's mark, even while page 1 is showing */
	if (cur_page == 0) {
		render_gauges();
	}
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
	pg[cur_page].s_in_s = -1;
	pg[cur_page].s_pct = 0;
	session.resets_in_s = -1;
	render_countdown(&session);
	last_s_pct = 0;
	lv_label_set_text(session.pct, "0%");
	lv_arc_set_value(session.arc, 0);
	lv_obj_set_style_arc_color(session.arc, severity(0), LV_PART_INDICATOR);
}

static void expire_weekly(void)
{
	pg[cur_page].w_in_s = -1;
	pg[cur_page].w_pct = 0;
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

	/*
	 * EVERY page ticks, not just the one on screen. A window does not stop
	 * closing because nobody is looking at it, and a page that caught up
	 * only on arrival would show a stale figure at the moment it matters
	 * most -- the glance you take precisely because the rail went red.
	 */
	for (int i = 0; i < RAIL_PAGES_MAX; i++) {
		if (!pg[i].have) {
			continue;
		}
		if (pg[i].s_in_s > 0 && --pg[i].s_in_s == 0) {
			pg[i].s_in_s = -1;
			pg[i].s_pct = 0;
			if (i == cur_page) {
				expire_session();
			}
		}
		if (pg[i].w_in_s > 0 && --pg[i].w_in_s == 0) {
			pg[i].w_in_s = -1;
			pg[i].w_pct = 0;
			if (i == cur_page) {
				expire_weekly();
			}
		}
	}
	if (pg[cur_page].have) {
		session.resets_in_s = pg[cur_page].s_in_s;
		session.burn = pg[cur_page].burn;
		weekly.resets_in_s = pg[cur_page].w_in_s;
		render_countdown(&session);
		render_countdown(&weekly);
	}
	refresh_rail();

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

/*
 * Paint the one indicator from the worse of its two inputs.
 *
 * Data health outranks execution state when both have something to say: a
 * reading we cannot vouch for makes the execution state moot, because the
 * numbers beside it are the thing in doubt.
 */
/*
 * The colour the execution state asks for, and whether it pulses.
 *
 * Its own function because TWO status cases need it: data that is sound, and
 * data flagged stale by the OTHER provider's age while this page is fine.
 * Inlined in one of them, it was simply missing from the other.
 */
static lv_color_t activity_color(bool *pulse)
{
	switch (activity) {
	case USAGE_ACTIVITY_STUCK:
	case USAGE_ACTIVITY_FAILED:
		return COL_RED;
	case USAGE_ACTIVITY_WAITING:
		return COL_AMBER;
	case USAGE_ACTIVITY_RUNNING:
		*pulse = true;
		return COL_GREEN;
	default:
		return COL_GREEN;
	}
}

static void refresh_dot(void)
{
	lv_color_t c = COL_GREY;
	bool pulse = false;

	if (!dot) {
		return;
	}
	switch (data_health) {
	case USAGE_STATUS_ERROR:
		c = COL_RED;
		break;
	case USAGE_STATUS_STALE:
		/*
		 * Only the page in front of you can be stale-amber. If THIS
		 * page is fresh, the dot reports what the tool is doing, the
		 * same as USAGE_STATUS_OK.
		 *
		 * It used to paint plain green here, which quietly disabled
		 * the activity indicator on any two-provider desk: proto.c
		 * arms STALE when EITHER provider is old (`stale = stale ||
		 * p2stale`), so a Codex reading last touched this morning made
		 * a WEDGED Claude session show a healthy green pip. The red
		 * stuck/failed warning is the entire reason the state hooks
		 * exist.
		 */
		c = stale_here() ? COL_AMBER : activity_color(&pulse);
		break;
	case USAGE_STATUS_DISCONNECTED:
		c = COL_GREY;
		break;
	case USAGE_STATUS_OK:
	default:
		/* Data is sound, so the dot is free to report what the tool is
		 * doing. Never above ERROR -- an amber "waiting" must not mask
		 * a red "the host is gone". */
		c = activity_color(&pulse);
		break;
	}

	lv_anim_delete(dot, act_pulse_cb);
	lv_obj_set_style_bg_opa(dot, LV_OPA_COVER, 0);
	lv_obj_set_style_bg_color(dot, c, 0);

	if (pulse) {
		lv_anim_t an;

		lv_anim_init(&an);
		lv_anim_set_var(&an, dot);
		lv_anim_set_exec_cb(&an, act_pulse_cb);
		lv_anim_set_values(&an, LV_OPA_40, LV_OPA_COVER);
		lv_anim_set_duration(&an, 900);
		lv_anim_set_playback_duration(&an, 900);
		lv_anim_set_repeat_count(&an, LV_ANIM_REPEAT_INFINITE);
		lv_anim_start(&an);
	}
}

void usage_view_set_activity(enum usage_activity a)
{
	activity = a;
	refresh_dot();
}

/*
 * Session and agent counts are no longer drawn.
 *
 * They shared the bottom line with the second provider's name -- "inner ring:
 * codex   3 sessions" -- which described the panel's own construction rather
 * than anything about the work. The provider names have a better home now,
 * beside the numbers they belong to. The counts still arrive on the wire
 * (n_sess, n_agents), so nothing has to be re-plumbed to bring them back.
 */
static int sess_n, agent_n;

/*
 * Repaint the primary provider's ball and countdown.
 *
 * COLOUR APPEARS ONLY WHERE IT SEPARATES TWO THINGS THAT BOTH EXIST. On the
 * default screen there is one provider, so "this is Claude" distinguishes it
 * from nothing -- and spending a warm hue on it puts an element that means
 * nothing directly beside the amber and red that mean a great deal. The eye
 * reads it as part of the severity story, because on that screen there is no
 * other story for it to belong to.
 *
 * So with one provider these go neutral, and severity is the only coloured
 * thing on the panel. The identity hue appears the moment a second provider
 * does -- which is the moment it starts carrying information.
 */
static void refresh_provider1(void)
{
	const char *tag = page_tag(cur_page);

	if (!session.arc) {
		return;
	}
	/*
	 * The ball is NEUTRAL, on every page.
	 *
	 * It used to wear the provider's hue whenever a second provider
	 * existed, because both sat on one gauge and something had to tell
	 * them apart. One provider per page answers that question before it is
	 * asked -- the name is under the brand and the rail says which page
	 * you are on -- so an identity hue here would only put a third colour
	 * next to the amber and red that mean something.
	 */
	lv_obj_set_style_bg_color(session.arc, COL_TEXT, LV_PART_KNOB);
	lv_obj_set_style_bg_color(weekly.arc, COL_TEXT, LV_PART_KNOB);
	/* The countdown is a bare duration now, so there is no name on that
	 * line for an identity hue to mark. It stays quiet. */
	lv_obj_set_style_text_color(session.countdown, COL_DIM, 0);
	lv_obj_set_style_text_color(weekly.countdown, COL_DIM, 0);
	if (provider_lbl) {
		char t[sizeof(provider1_tag)];

		tag_cased(t, sizeof(t), tag);
		lv_label_set_text(provider_lbl, t);
		/*
		 * The fill is what says "you can press this", so it is there
		 * only when pressing does something. A single-provider desk
		 * has nowhere to go, and a control that looks live and answers
		 * nothing is worse than a plain label -- the same rule the
		 * rail follows by hiding its dots below two pages.
		 */
		lv_obj_set_style_bg_opa(provider_lbl,
					page_count() >= 2 ? LV_OPA_COVER
							  : LV_OPA_TRANSP, 0);
		/*
		 * Hugs its text. A fixed width made a pill wide enough for
		 * "claude code" around the word "Claude", which reads as a
		 * bar with a word in it rather than a button.
		 *
		 * PILL_MAX_W stops being the width and becomes the CEILING --
		 * the tag buffer holds eleven characters and the layout test
		 * pins that against it, so content-sizing can never exceed
		 * what the band was checked for.
		 */
		lv_obj_set_width(provider_lbl, LV_SIZE_CONTENT);
		lv_obj_set_style_max_width(provider_lbl, PILL_MAX_W, 0);
	}
}

void usage_view_set_provider1_stale(bool stale)
{
	pg[0].stale = stale;
	if (built) {
		refresh_dot();
	}
}

void usage_view_set_burn(double pph)
{
	/*
	 * Page 0 only. The rate comes from provider 1's history and describes
	 * provider 1's window; the second page has real reset times, which is
	 * why it never needs one.
	 */
	pg[0].burn = pph;
	if (built && cur_page == 0) {
		session.burn = pph;
		render_countdown(&session);
	}
}

void usage_view_set_provider1(const char *tag)
{
	/* The outer ring is whichever provider the daemon made primary, which
	 * on a codex-only machine is codex -- so the colour follows the NAME,
	 * not the ring position. */
	snprintf(provider1_tag, sizeof(provider1_tag), "%s", tag ? tag : "");
	refresh_provider1();
}

void usage_view_set_provider2(const char *tag, double session_pct,
			      double weekly_pct, int32_t session_resets_in_s,
			      int32_t weekly_resets_in_s, bool stale)
{
	if (!built) {
		return;
	}
	if (!tag || !tag[0]) {
		/*
		 * No second provider. The page goes away entirely rather than
		 * sitting at zero, which would read as a provider that exists
		 * and has used nothing -- and if we were standing on it, we
		 * fall back to page 0 rather than showing an empty screen.
		 */
		provider2_tag[0] = '\0';
		pg[1].have = false;
		if (cur_page != 0) {
			cur_page = 0;
			render_gauges();
		}
		refresh_rail();
		return;
	}

	snprintf(provider2_tag, sizeof(provider2_tag), "%s", tag);
	if (session_resets_in_s == 0) {
		session_pct = 0;
		session_resets_in_s = -1;
	}
	if (weekly_resets_in_s == 0) {
		weekly_pct = 0;
		weekly_resets_in_s = -1;
	}
	pg[1].s_pct = session_pct;
	pg[1].w_pct = weekly_pct;
	pg[1].s_in_s = session_resets_in_s;
	pg[1].w_in_s = weekly_resets_in_s;
	pg[1].stale = stale;
	pg[1].have = true;

	refresh_rail();
	if (cur_page == 1) {
		render_gauges();
	}
}

void usage_view_set_sessions(int n_sessions, int n_agents)
{
	/* Recorded, not drawn -- see above. */
	sess_n = n_sessions;
	agent_n = n_agents;
	(void)sess_n;
	(void)agent_n;
}


void usage_view_set_status(enum usage_status status)
{
	if (!built) {
		return;
	}
	last_status = status;
	data_health = status;

	lv_color_t tc = COL_DIM;
	const char *text;

	switch (status) {
	case USAGE_STATUS_OK:
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
		tc = COL_AMBER;
		/* Not "rate-limited": this state means the daemon has no fresh
		 * reading, which is usually its owner being away from Claude Code.
		 * The old wording is from when a 429 from the usage endpoint was
		 * the only way to get here; that endpoint is gone, and telling
		 * someone they are rate-limited when they are not is worse than
		 * saying nothing. */
		/* Only when it is true of THIS page -- see stale_here(). The
		 * other page's silence is not this page's problem, and saying
		 * so over live numbers is the panel contradicting itself. */
		text = stale_here() ? "Reading is old - showing last known" : "";
		break;
	case USAGE_STATUS_ERROR:
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
			tc = COL_RED;
			text = "HOST LOST - numbers are frozen";
		} else {
			text = "";
		}
		break;
	}
	lv_obj_set_style_text_color(hint, tc, 0);
	lv_label_set_text(hint, text);

	/* One owner for the indicator's colour. The hint says WHICH condition
	 * fired; the dot says only how bad it is. */
	refresh_dot();
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
