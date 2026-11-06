/* Standalone host test for the gauge screen's geometry.
 *
 * Build & run:
 *   cc -I ../../firmware/src host_test.c -o /tmp/laytest && /tmp/laytest
 *
 * This exists because "the boxes do not overlap" and "the screen reads well"
 * are different claims, and only the first one is checkable without pixels.
 * tools/panel_render/render.sh covers the second; this covers the first, and
 * has caught a hint-line collision, a bar butting flush against its own
 * readout, and a countdown sliced by its own inner ring -- none of which were
 * visible in the source.
 *
 * It asserts from usage_layout.h, the same header the screen is built from, so
 * the check cannot drift away from the code.
 *
 * Vertical geometry is exact: every unlabelled label on this screen is
 * FONT_LINE_H tall and that is a constant. Horizontal extents for text use the
 * declared *_MAX_W budgets, and the real strings are checked against them.
 */
#include <stdio.h>
#include <string.h>
#include "usage_layout.h"
#include "theme.h"
#include "fmt.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

/* Same PASS/FAIL-plus-failures-counter shape as CHECK above, spelled for a
 * pair of numbers so a mismatch prints both sides instead of just the
 * condition that failed. */
#define EXPECT_EQ(got, want) do { \
	long g_ = (long)(got), w_ = (long)(want); \
	if (g_ == w_) { \
		printf("PASS: %-28s -> %ld\n", #got, g_); \
	} else { \
		printf("FAIL: %-28s -> %ld (want %ld)\n", #got, g_, w_); \
		failures++; \
	} \
} while (0)

struct box { const char *name; int x0, y0, x1, y1; };

/* TOP_MID: x is an offset of the object's CENTRE from the screen centre. */
static struct box top_mid(const char *name, int xoff, int y, int w, int h)
{
	struct box b;
	b.name = name;
	b.x0 = SCR_MID_X + xoff - w / 2;
	b.x1 = b.x0 + w;
	b.y0 = y;
	b.y1 = y + h;
	return b;
}

/* BOTTOM_MID with a positive `up` offset from the bottom edge. */
static struct box bottom_mid(const char *name, int up, int w, int h)
{
	struct box b;
	b.name = name;
	b.x0 = SCR_MID_X - w / 2;
	b.x1 = b.x0 + w;
	b.y1 = SCR_H - up;
	b.y0 = b.y1 - h;
	return b;
}

static int overlaps(struct box a, struct box b)
{
	return a.x0 < b.x1 && b.x0 < a.x1 && a.y0 < b.y1 && b.y0 < a.y1;
}

static int on_screen(struct box b)
{
	return b.x0 >= 0 && b.y0 >= 0 && b.x1 <= SCR_W && b.y1 <= SCR_H;
}

/* Per-character width bounds for montserrat_14. A declared budget has to be
 * wide enough for its string and not absurdly wider -- a budget nobody sized
 * is how two widgets end up "clearing" each other on paper while touching on
 * the panel. */
#define CHAR_W_MIN 8
#define CHAR_W_MAX 14

#define BUDGET_FITS(budget, str) \
	((budget) >= (int)strlen(str) * CHAR_W_MIN && \
	 (budget) <= (int)strlen(str) * CHAR_W_MAX)

/*
 * Does pixel (x, y) belong to Focus mode's big ring, drawn at its thickest?
 *
 * Integer arithmetic on squared distances, so no libm. The stroke is the
 * annulus FOCUS_ARC_SZ / 2 wide by FOCUS_ARC_W_MAX, minus the open quarter at
 * the bottom; both ends are rounded; and the ball rides anywhere on the
 * stroke's centre line, GAUGE_BALL_PAD beyond the stroke on every side, so
 * the whole annulus and both caps are grown by that much. Pessimistic on
 * purpose: a theme with flat ends or no ball only draws less.
 */
static int on_focus_ring(int x, int y)
{
	const int r_out = FOCUS_ARC_SZ / 2 + GAUGE_BALL_PAD;
	const int w = FOCUS_ARC_W_MAX;
	const int r_in = FOCUS_ARC_SZ / 2 - w - GAUGE_BALL_PAD;
	const int cap = w / 2 + GAUGE_BALL_PAD;
	/* The caps' centres: on the stroke's centre line, 45 degrees down,
	 * at (+-m, m) with m = r_mid / sqrt 2, rounded outwards. */
	const int r_mid = FOCUS_ARC_SZ / 2 - w / 2;
	const int m = (r_mid * 7072 + 9999) / 10000;
	int dx = x - SCR_MID_X, dy = y - FOCUS_CY;
	int d2 = dx * dx + dy * dy;
	int adx = dx < 0 ? -dx : dx;
	int in_gap = dy > 0 && adx < dy;

	if (!in_gap && d2 >= r_in * r_in && d2 <= r_out * r_out) {
		return 1;
	}
	for (int s = -1; s <= 1; s += 2) {
		int cx = dx - s * m, cy = dy - m;

		if (cx * cx + cy * cy <= cap * cap) {
			return 1;
		}
	}
	return 0;
}

static int clear_of_focus_ring(struct box b)
{
	for (int y = b.y0; y < b.y1; y++) {
		for (int x = b.x0; x < b.x1; x++) {
			if (on_focus_ring(x, y)) {
				return 0;
			}
		}
	}
	return 1;
}

static void check_focus(void)
{
	/* Each text box at the width it actually draws, measured with
	 * lv_text_get_size on host LVGL -- the hollow is round, so a budget
	 * wider than the text would fail where the text itself does not. */
	struct box pct = top_mid("focus figure", 0, FOCUS_PCT_Y, 103,
				 FOCUS_PCT_FONT_H);
	struct box name = top_mid("focus caption", 0, FOCUS_NAME_Y,
				  FOCUS_NAME_W, FONT_LINE_H);
	struct box cd = top_mid("focus countdown", 0, FOCUS_CD_Y, FOCUS_CD_W,
				FONT_LINE_H);
	struct box other = top_mid("focus other line", 0, FOCUS_OTHER_Y,
				   FOCUS_OTHER_MAX_W, FONT_LINE_H);
	struct box arc = top_mid("focus ring", 0, FOCUS_ARC_Y, FOCUS_ARC_SZ,
				 FOCUS_ARC_SZ);
	struct box pill = bottom_mid("provider pill", PILL_BOTTOM_OFF,
				     PILL_MAX_W, PILL_H);
	struct box status = top_mid("hint/clock row", 0, STATUS_Y,
				    STATUS_MAX_W, FONT_LINE_H);
	int ring_bottom = 0;

	CHECK(FOCUS_ARC_W_MAX >= GAUGE_ARC_W && FOCUS_ARC_W_MAX == 22,
	      "focus: clearances are taken at the thickest theme arc (22 px)");
	CHECK(on_screen(arc) && on_screen(other),
	      "focus: the ring and the other line are on the panel");
	CHECK(status.y1 <= FOCUS_ARC_Y,
	      "focus: the ring starts under the hint/clock row");
	CHECK(FOCUS_PCT_MAX_W >= 103,
	      "focus: the figure's box holds \"100%\" (103 px)");
	CHECK(clear_of_focus_ring(pct),
	      "focus: \"100%\" sits inside the thick ring's hollow");
	CHECK(clear_of_focus_ring(name),
	      "focus: the caption sits inside the thick ring's hollow");
	CHECK(clear_of_focus_ring(cd),
	      "focus: the countdown sits in the ring's open quarter");
	CHECK(clear_of_focus_ring(other),
	      "focus: the other window's line is clear of the ring");
	CHECK(!overlaps(pct, name) && !overlaps(name, cd) &&
	      !overlaps(cd, other),
	      "focus: figure, caption, countdown and line do not touch");
	/* Not overlapping is not enough: at 2 px the figure read as sitting
	 * on the caption (audit, 2026-10-28). */
	CHECK(name.y0 - pct.y1 >= 6,
	      "focus: at least 6 px between the figure and its caption");
	CHECK(FOCUS_OTHER_MAX_W >= FOCUS_OTHER_W_LONGEST,
	      "focus: the line's box holds fmt_focus_line()'s longest");
	CHECK(!overlaps(other, pill),
	      "focus: the other line clears the provider pill");

	/* Nothing of the ring reaches the line's row: the open quarter is
	 * why the line can be as wide as it is. */
	for (int y = 0; y < SCR_H; y++) {
		for (int x = 0; x < SCR_W; x++) {
			if (on_focus_ring(x, y) && y > ring_bottom) {
				ring_bottom = y;
			}
		}
	}
	CHECK(ring_bottom < FOCUS_OTHER_Y,
	      "focus: the lowest pixel of the ring is above the other line");
	/* And the test can fail: a caption a few px lower than this lands on
	 * the thick ring, so the sampler is not passing by accident. */
	CHECK(!clear_of_focus_ring(top_mid("caption, too low", 0,
					   FOCUS_NAME_Y + 12, FOCUS_NAME_W,
					   FONT_LINE_H)),
	      "focus: the ring sampler catches a caption pushed into the arc");
}

/*
 * THE SPEND PAGE (docs/plans/enterprise-spend.md section 3), in pixels.
 *
 * Every width here is lv_text_get_size() on host LVGL -- the Zephyr 4.3.1
 * checkout's LVGL 9, the one the firmware links -- measured on 2026-11-04,
 * kerning included. Not the glyph advances summed by hand: Montserrat kerns
 * digit pairs, and it is not always narrower -- '4' then '8' is a pixel
 * wider at size 20, so "$44.8M" measures 77 where the advances say 76. The
 * table holds the widest spellings: every shape fmt_usd() has with each
 * digit a '4' (the widest digit at all three sizes), with and without the
 * estimate's "~", and the worst cases a sweep found that the '4's miss.
 * The sweep ran fmt_usd() from 0 to $1e9 and fmt_countdown() over every
 * second up to the daemon's 32-day ceiling (SPEND_COUNTDOWN_MAX_S),
 * through lv_text_get_size(). A string fmt_usd() starts producing that is
 * not in the table fails the lookup below, which is the prompt to measure
 * it.
 */
static const struct measured {
	const char *s;
	int m14, m16, m20;
} spend_w[] = {
	{ "$4.44", 40, 47, 57 },	{ "~$4.44", 48, 56, 69 },
	{ "$44", 27, 32, 39 },		{ "~$44", 35, 41, 51 },
	{ "$444", 36, 43, 52 },		{ "~$444", 44, 52, 64 },
	{ "$4444", 45, 54, 65 },	{ "~$4444", 53, 63, 77 },
	{ "$44.4k", 49, 57, 69 },	{ "~$44.4k", 57, 66, 81 },
	{ "$444k", 45, 53, 64 },	{ "~$444k", 53, 62, 76 },
	{ "$4.4M", 44, 51, 63 },	{ "~$4.4M", 52, 60, 75 },
	{ "$44.4M", 53, 62, 76 },	{ "~$44.4M", 61, 71, 88 },
	{ "$444M", 49, 58, 71 },	{ "~$444M", 57, 67, 83 },
	/* The sweep's widest, past the '4's: kerning. */
	{ "$44.8M", 0, 0, 77 },		{ "~$40.4M", 0, 71, 0 },
};

static const struct measured *spend_measured(const char *s)
{
	for (size_t i = 0; i < sizeof(spend_w) / sizeof(spend_w[0]); i++) {
		if (strcmp(spend_w[i].s, s) == 0) {
			return &spend_w[i];
		}
	}
	return NULL;
}

/*
 * Whole lines, measured the same way, by the font they are drawn in. The
 * sweep's widest line under the ring is a cap in the tens of millions on the
 * last day of its period -- hours and minutes are wider than days and hours
 * -- and under $100k it is 144. The checks below never type a width in:
 * they run the formatter on its worst inputs and look the output up here,
 * so a changed spelling fails the lookup rather than passing on a stale
 * number.
 */
static const struct line_w {
	int font;			/* 14 or 20: montserrat_N */
	const char *s;
	int w;
} line_w[] = {
	{ 14, "of $44.4M " FMT_BULLET " 32d 23h", 139 },
	{ 14, "of $44.0M " FMT_BULLET " 20h 48m", 148 },
	{ 14, "Spent ~$44.0M of $44.0M", 184 },
	{ 14, "+$44.0k/h " FMT_BULLET " steady", 131 },
	{ 14, "Run /usage in Claude Code", 198 },
	{ 14, "Spend limit almost used up", 202 },
	{ 14, "SPEND", 51 },
	{ 14, "SPEND month", 105 },
	/* An unreported page's words (lv_text_get_size on host LVGL,
	 * 2026-11-04). */
	{ 14, "Not reported", 95 },
	{ 14, "ENTERPRISE", 89 },
	/* The burn view's total: twenty buckets at the ceiling,
	 * $19,999,999.80, the sweep's widest under $20M. */
	{ 20, "$20.0M", 72 },
};

/* The measured width of `s` at montserrat_`font`; a FAIL and a width no
 * box holds when the spelling has not been measured. */
static int measured_w(int font, const char *s)
{
	char msg[96];

	for (size_t i = 0; i < sizeof(line_w) / sizeof(line_w[0]); i++) {
		if (line_w[i].font == font && strcmp(line_w[i].s, s) == 0) {
			return line_w[i].w;
		}
	}
	snprintf(msg, sizeof(msg), "\"%s\" at montserrat_%d is measured",
		 s, font);
	CHECK(0, msg);
	return 9999;
}

static void test_spend_page(void)
{
	/* The hollow at the THICKEST stroke any theme draws. GAUGE_ARC_W is
	 * the default's 12; Bold is 22, and the dollars must clear it too. */
	const int hollow = GAUGE_ARC_SZ - 2 * THEME_ARC_W_MAX;
	static const double fours[] = {
		4.44, 44.4, 444, 4444, 44444, 444444, 4444444, 44444444,
		444444444,
	};
	char msg[96];

	/* The widths below come from the formatters' own output on their
	 * worst inputs, looked up in line_w. */
	char line[FMT_FOCUS_MAX];
	int w_under_32d, w_under_widest, w_focus_line, w_rate, w_total;
	int w_usage_hint, w_spend_hint, w_focus_caption, w_spend_month;

	printf("\n-- the spend page --\n");
	EXPECT_EQ(hollow, 76);
	fmt_spend_under(44.4e6, 32 * 86400 + 23 * 3600, line, sizeof(line));
	w_under_32d = measured_w(14, line);
	fmt_spend_under(44.0e6, 20 * 3600 + 48 * 60, line, sizeof(line));
	w_under_widest = measured_w(14, line);
	fmt_focus_spend_line(44.0e6, true, 44.0e6, line, sizeof(line));
	w_focus_line = measured_w(14, line);
	fmt_usd_burn_line(4400000u, "steady", line, sizeof(line));
	w_rate = measured_w(14, line);
	fmt_usd(20u * 99999999u / 100.0, line, sizeof(line));
	w_total = measured_w(20, line);
	w_usage_hint = measured_w(14, FMT_SPEND_USAGE_HINT);
	w_spend_hint = measured_w(14, fmt_spend_hint(96));
	w_focus_caption = measured_w(14, fmt_spend_caption(NULL));
	w_spend_month = measured_w(14, fmt_spend_caption("monthly"));
	for (size_t i = 0; i < sizeof(fours) / sizeof(fours[0]); i++) {
		for (int est = 0; est <= 1; est++) {
			char b[FMT_USD_MAX];
			const struct measured *m;

			fmt_spend_amount(fours[i], est, b, sizeof(b));
			m = spend_measured(b);
			snprintf(msg, sizeof(msg),
				 "\"%s\" is a spelling this test has measured",
				 b);
			CHECK(m != NULL, msg);
			if (m == NULL) {
				continue;
			}
			snprintf(msg, sizeof(msg),
				 "\"%s\" fits the hollow at montserrat_16 "
				 "(%d <= %d)", b, m->m16, hollow);
			CHECK(m->m16 <= hollow, msg);
			/*
			 * usage_view.c draws it at 20 when lv_text_get_size()
			 * says it fits and at 16 otherwise, so only the ones
			 * that fit are drawn at 20. An exact '4' figure always
			 * does: "$44.4M" is exactly the hollow.
			 */
			if (!est) {
				snprintf(msg, sizeof(msg),
					 "the exact \"%s\" stays at "
					 "montserrat_20 (%d <= %d)",
					 b, m->m20, hollow);
				CHECK(m->m20 <= hollow, msg);
			}
		}
	}
	CHECK(spend_measured("~$40.4M")->m16 <= hollow,
	      "the sweep's widest estimate fits the hollow at montserrat_16");
	CHECK(spend_measured("$44.8M")->m20 > hollow,
	      "a kerned exact figure can be too wide for 20 (so 16 decides)");
	CHECK(hollow <= GAUGE_PCT_MAX_W,
	      "the hollow's text sits inside the label's own box");

	/* Under the ring: the cap and the period's reset, in the right
	 * countdown label widened on a spend page and still centred at 240. */
	{
		struct box under = top_mid("spend line", GAUGE_CX, GAUGE_CD_Y,
					   SPEND_CD_MAX_W, FONT_LINE_H);
		struct box cd_l = top_mid("left countdown", -GAUGE_CX,
					  GAUGE_CD_Y, GAUGE_CD_MAX_W,
					  FONT_LINE_H);
		struct box rate = top_mid("burn rate line", -GAUGE_CX,
					  GAUGE_CD_Y, BURN_TEXT_W,
					  FONT_LINE_H);
		struct box pill = bottom_mid("provider pill", PILL_BOTTOM_OFF,
					     PILL_MAX_W, PILL_H);

		EXPECT_EQ(SCR_MID_X + GAUGE_CX, 240);
		CHECK(w_under_32d <= SPEND_CD_MAX_W,
		      "\"of $44.4M <b> 32d 23h\" fits under the ring");
		CHECK(w_under_widest <= SPEND_CD_MAX_W,
		      "the widest line under the ring, measured, fits it");
		CHECK(under.x1 <= SCR_W - SCR_RIGHT_MARGIN_MIN,
		      "the line under the ring clears the right bezel");
		CHECK(!overlaps(under, cd_l),
		      "it clears the left gauge's countdown");
		CHECK(!overlaps(under, rate),
		      "it clears the dollar burn view's rate line");
		CHECK(!overlaps(under, pill), "it clears the provider pill");
		CHECK(w_rate <= BURN_TEXT_W,
		      "the widest dollar rate line fits the burn view's row");
		CHECK(w_total + BURN_GLYPH_GAP + BURN_GLYPH_FONT_H <=
			      BURN_TEXT_W,
		      "the widest dollar total and its arrow fit the half");
	}

	/* Focus mode's caption in the big ring: "SPEND month" (105 px),
	 * "SPEND today" (96) and "SPEND week" (94) all overrun the 85 px the
	 * thick ring's hollow holds there, so Focus writes "SPEND" (51). */
	CHECK(w_focus_caption <= FOCUS_NAME_W,
	      "Focus's spend caption fits inside the thick ring");
	CHECK(w_spend_month > FOCUS_NAME_W,
	      "and the period's caption would not, which is why it is cut");
	/*
	 * And an unreported page's. "Not reported" overran the 67 px open
	 * quarter under the big ring and "ENTERPRISE" the 85 px caption
	 * (review of feat/enterprise-spend, 2026-11-04, FW2), so in Focus
	 * the left gauge is always the big one, captioned "SPEND", and the
	 * words go on the dim line, which holds them.
	 */
	CHECK(measured_w(14, "Not reported") > FOCUS_CD_W,
	      "\"Not reported\" does not fit Focus's countdown slot");
	CHECK(measured_w(14, "Not reported") <= FOCUS_OTHER_MAX_W,
	      "\"Not reported\" fits Focus mode's dim line");
	CHECK(measured_w(14, "ENTERPRISE") > FOCUS_NAME_W,
	      "\"ENTERPRISE\" would not fit the big ring's caption");
	CHECK(measured_w(14, "Not reported") <= GAUGE_CD_MAX_W,
	      "\"Not reported\", measured, fits the two-gauge countdown");

	/* Focus mode's dim line, and the status row's two spend sentences. */
	CHECK(w_focus_line <= FOCUS_OTHER_MAX_W,
	      "\"Spent ~$44.0M of $44.0M\" fits Focus mode's dim line");
	CHECK(w_usage_hint <= STATUS_MAX_W,
	      "\"Run /usage in Claude Code\" fits the status row");
	CHECK(strlen(FMT_SPEND_USAGE_HINT) <= 47,
	      "\"Run /usage in Claude Code\" fits the 47 characters kept");
	CHECK(w_spend_hint <= STATUS_MAX_W,
	      "\"Spend limit almost used up\" fits the status row");
}

int main(void)
{
	/* The gauges. */
	struct box arc_l = top_mid("arc L", -GAUGE_CX, GAUGE_ARC_Y,
				   GAUGE_ARC_SZ, GAUGE_ARC_SZ);
	struct box arc_r = top_mid("arc R", GAUGE_CX, GAUGE_ARC_Y,
				   GAUGE_ARC_SZ, GAUGE_ARC_SZ);
	/* Height is GAUGE_PCT_FONT_H, not FONT_LINE_H: this label is drawn at
	 * montserrat_20, not the screen's default 14. */
	struct box pct_l = top_mid("percentage L", -GAUGE_CX, GAUGE_PCT_Y,
				   80, GAUGE_PCT_FONT_H);
	struct box name_l = top_mid("SESSION caption", -GAUGE_CX,
				    GAUGE_NAME_Y, 110, FONT_LINE_H);
	struct box name_r = top_mid("WEEKLY caption", GAUGE_CX, GAUGE_NAME_Y,
				    110, FONT_LINE_H);

	/* ONE countdown per gauge. The second provider is a page now, not a
	 * second line, so the only thing under a gauge is its own duration. */
	struct box cd_l = top_mid("countdown L", -GAUGE_CX,
				  GAUGE_CD_Y, GAUGE_CD_MAX_W, FONT_LINE_H);
	struct box cd_r = top_mid("countdown R", GAUGE_CX,
				  GAUGE_CD_Y, GAUGE_CD_MAX_W, FONT_LINE_H);

	/* The brand, and the status line that took the space under it when the
	 * provider's name moved to the bottom. */
	struct box brand = top_mid("brand", 0, TITLE_Y, BRAND_W, FONT_LINE_H);
	struct box status = top_mid("status", 0, STATUS_Y,
				    STATUS_MAX_W, FONT_LINE_H);

	/* The clock shares STATUS_Y with the hint -- one of them is visible at
	 * a time, so they may overlap each other and must not overlap anything
	 * else. Width is the worst case the format can produce, not "12:04":
	 * 0, 4, 6, 8 and 9 all round to 9 px, so four digits and a 3 px colon. */
	struct box clock = top_mid("clock", 0, STATUS_Y, 4 * 9 + 3, FONT_LINE_H);

	/* The pip row's own bounding box, built from the same constants the
	 * checks below already asserted individually -- so the clock/pip
	 * overlap check and the row's own left/right clearance share one
	 * definition instead of two copies of the same arithmetic. */
	struct box pips;
	pips.name = "pip row";
	pips.x0 = PIP_X0;
	pips.y0 = PIP_Y;
	pips.x1 = PIP_X0 + PIP_MAX * PIP_PITCH - (PIP_PITCH - PIP_SZ);
	pips.y1 = PIP_Y + PIP_SZ;

	/* The provider pill: whose numbers these are, and the button that
	 * changes it. Padded, so it is taller than a bare line. */
	struct box who = bottom_mid("provider pill", PILL_BOTTOM_OFF,
				    PILL_MAX_W, PILL_H);

	/* The page rail, below everything. */
	struct box rail = bottom_mid("page rail", RAIL_BOTTOM_OFF,
				     RAIL_PAGES_MAX * RAIL_PITCH -
					     (RAIL_PITCH - RAIL_DOT_W),
				     RAIL_H);

	struct box all[] = { arc_l, arc_r, pct_l, name_l, name_r,
			     cd_l, cd_r, brand, status, clock, pips,
			     who, rail };

	for (unsigned i = 0; i < sizeof(all) / sizeof(all[0]); i++) {
		char msg[64];

		snprintf(msg, sizeof(msg), "%s fits on the panel", all[i].name);
		CHECK(on_screen(all[i]), msg);
	}

	/* --- the gauges do not meet --- */
	CHECK(!overlaps(arc_l, arc_r), "the two gauges do not overlap");
	CHECK(arc_l.y1 <= name_l.y0, "the caption sits below its ring");

	/* --- the ring hollow --- */
	/*
	 * The percentage must clear the arc's own stroke, not merely fit
	 * inside the ring's outer bounding box -- text drawn as wide as the
	 * ring would paint over the coloured track. "1000%" is the longest
	 * string this label is ever asked to hold: pct_int() in usage_view.c
	 * clamps at PCT_DISPLAY_MAX (1000). Measured straight from
	 * lv_font_montserrat_20.c's own glyph_dsc table, not guessed: '1' is
	 * 7 px, '0' is 13 px, '%' is 17 px, so "1000%" is 7+13*3+17 = 63 px.
	 */
	CHECK(63 <= GAUGE_ARC_SZ - 2 * GAUGE_ARC_W,
	      "the widest percentage this label ever shows clears the arc's stroke");
	/* The spend page's hollow: test_spend_page() below. */
	/* --- one countdown per gauge --- */
	CHECK(cd_l.x1 <= cd_r.x0,
	      "the two gauges' countdowns do not meet in the middle");
	CHECK(cd_l.y0 >= GAUGE_ARC_Y + GAUGE_ARC_SZ,
	      "countdowns sit below the rings, not inside them");
	CHECK(cd_l.y0 >= name_l.y1, "countdowns sit below the caption");
	CHECK(!overlaps(cd_l, who), "countdowns clear the provider pill");
	CHECK(!overlaps(cd_r, who), "countdowns clear the provider pill");

	/* --- the header block stacks without touching --- */
	CHECK(status.y0 >= brand.y1,
	      "the status line sits below the brand, not on it");
	CHECK(arc_l.y0 >= status.y1,
	      "the gauges start below the status line");

	/* The clock shares STATUS_Y with the status/hint line rather than
	 * owning a row, so its own box is checked against the same neighbours:
	 * it sits under the brand, clears the arcs below, and -- since it left
	 * the pip row's corner for this shared row -- no longer collides with
	 * the pips it used to sit beside.
	 *
	 * Against status.y0, not the STATUS_Y macro: clock is built two lines
	 * above from that same macro, so comparing it to the macro can never
	 * fail -- it would still pass with the clock's own top_mid() call
	 * edited to some other row. Comparing to status.y0 instead asserts the
	 * actual invariant this block is about: clock and status occupy the
	 * same row as each other, and a future edit that moves either one off
	 * that row fails here. */
	EXPECT_EQ(clock.y0, status.y0);
	CHECK(clock.y1 <= GAUGE_ARC_Y,
	      "the clock's line box clears the arcs below it");
	CHECK(clock.y0 >= brand.y1,
	      "the clock sits under the brand, not on it");
	CHECK(!overlaps(clock, pips),
	      "the clock no longer collides with the pip row it vacated");

	/*
	 * Two header rows again, not three. The clock shares STATUS_Y with the
	 * status/hint line instead of owning a corner, so the arcs get their
	 * 20 px back.
	 */
	EXPECT_EQ(STATUS_Y, TITLE_Y + FONT_LINE_H + 2);
	EXPECT_EQ(GAUGE_ARC_Y, STATUS_Y + FONT_LINE_H + 4);
	EXPECT_EQ(GAUGE_ARC_Y + GAUGE_ARC_SZ + 4, GAUGE_NAME_Y);

	/* The percentage stays centre-derived rather than a literal: its
	 * middle sits on the ring's middle, whatever the ring's size or the
	 * font's line height. The old literal 90 was 3 px high, which is what
	 * e7df2f2 fixed and this must not un-fix. */
	EXPECT_EQ(GAUGE_PCT_Y + GAUGE_PCT_FONT_H / 2, GAUGE_ARC_Y + GAUGE_ARC_SZ / 2);

	/*
	 * The pip row runs from the bezel to the brand, not from the clock to
	 * the brand: the clock shares STATUS_Y with the status/hint line now
	 * and is asserted against this row separately, above. Only the right
	 * edge here is still a measurement -- the wall derived from "BLINK"
	 * below -- and a font bump must fail here rather than slide pips under
	 * the logo.
	 */
	/*
	 * The clock has left this corner for the row under the brand, so the
	 * row's left edge is now the bezel, not a time string. Its right edge
	 * is still the wordmark, and that IS a measurement -- so it is derived
	 * here rather than typed in, because the number that was typed in was
	 * wrong by 2 px in the direction that matters.
	 *
	 * "BLINK" at lv_font_montserrat_14, whose advances LVGL rounds to
	 * whole pixels as (adv_w + 8) >> 4: B 11 + L 8 + I 4 + N 11 + K 10 =
	 * 44, plus the letter_space of 2 that usage_view.c sets, in each of
	 * the four gaps = 52. Centred on SCR_MID_X, so it begins at 134 -- not
	 * the 136 an earlier comment claimed from a tracking value the code
	 * does not use.
	 */
	CHECK(pips.x0 >= SCR_RIGHT_MARGIN_MIN,
	      "the pip row is not flush against the left bezel");
	CHECK(pips.x1 <= PIP_WALL_X,
	      "a full pip row clears the wall");
	CHECK(PIP_WALL_X <= brand.x0,
	      "the wall is left of the brand's real left edge");
	EXPECT_EQ(brand.x0, 134);
	EXPECT_EQ(PIP_MAX, 11);
	/*
	 * Counts mode: three groups of pip + gap + one digit, with a gap
	 * between them. Asserted for the SINGLE-digit case only, which is the
	 * one the metrics are sized for -- a wider tally is measured at draw
	 * time and stopped at the wall (refresh_dots), because no constant
	 * here can know how many digits a tally has. PIP_NUM_ADV is the
	 * measured advance of the widest digit; if a font bump breaks that,
	 * this fails rather than the numerals creeping onto the brand.
	 */
	CHECK(PIP_X0 + 4 * (PIP_SZ + PIP_NUM_GAP + PIP_NUM_ADV)
	      + 3 * PIP_GROUP_GAP <= PIP_WALL_X,
	      "four counted groups clear the brand");
	/*
	 * And the row's Y, which had no assertion at all -- which is why a
	 * tally whose line box ended exactly on STATUS_Y got as far as a
	 * review. The label is FONT_LINE_H tall whatever is written in it, so
	 * this is the check the numeral needs and the pip does not.
	 */
	CHECK(PIP_NUM_Y + FONT_LINE_H <= STATUS_Y,
	      "the tally's line box clears the hint line");
	/*
	 * One centre line for the whole header: the tally's box, the pip and
	 * the health dot, at three different heights.
	 *
	 * Neither of these bites the way the clearance above does, and saying
	 * so is the point. PIP_NUM_Y subtracts FONT_LINE_H / 2 and the first
	 * check adds it straight back, so the two cancel exactly -- no font
	 * change can fail it, and it reduces to the second. What it does still
	 * catch is PIP_NUM_Y being rewritten with the wrong sign or the wrong
	 * operand, which is the mistake this row has already made once. The
	 * second fails only on an even DOT_SZ against an odd PIP_SZ.
	 *
	 * Both are written down anyway, because the shared centre line is the
	 * rule this row is built on, and a rule with no assertion is exactly
	 * how the Y above went unchecked until a tally landed on the hint line.
	 */
	EXPECT_EQ(PIP_NUM_Y + FONT_LINE_H / 2, HDR_ROW_Y + DOT_SZ / 2);
	EXPECT_EQ(PIP_Y + PIP_SZ / 2, HDR_ROW_Y + DOT_SZ / 2);

	/* --- the bottom stacks: countdowns, pill, rail --- */
	CHECK(who.y0 >= cd_l.y1,
	      "the provider pill sits below the countdowns");
	CHECK(!overlaps(rail, who),
	      "the page rail clears the provider pill above it");
	CHECK(!overlaps(rail, cd_l) && !overlaps(rail, cd_r),
	      "the page rail clears both countdowns");
	CHECK(rail.y1 <= SCR_H - 2,
	      "the page rail is not flush against the bottom bezel");

	/* --- the text budgets are real --- */
	/* A bare duration. The provider's name moved under the brand, which is
	 * what let this budget shrink -- and shrinking it is the check that
	 * the name really did leave, rather than merely being hidden. */
	CHECK(BUDGET_FITS(GAUGE_CD_MAX_W, "00m 00s"),
	      "GAUGE_CD_MAX_W is sized for a bare countdown, not guessed");
	CHECK(!BUDGET_FITS(GAUGE_CD_MAX_W, "claude  00m 00s"),
	      "GAUGE_CD_MAX_W no longer has room for a named one");
	/*
	 * GAUGE_CD_MAX_W was set by "00m 00s", and plan pages reuse that box
	 * for text of their own, which nobody widened it for. Each string is
	 * therefore summed from montserrat_14's adv_w table here: "Not
	 * reported" takes 92.2 of the 96 px -- the tightest fit anywhere on
	 * those pages, which is why it stops at two words -- and "of $99.9M",
	 * the longest cap line fmt_usd_limit() can make, takes 68.7.
	 */
	CHECK(BUDGET_FITS(GAUGE_CD_MAX_W, "Not reported"),
	      "\"Not reported\" is inside the countdown budget's range");
	CHECK(93 <= GAUGE_CD_MAX_W,
	      "\"Not reported\", measured, fits the countdown box");
	/*
	 * The captions are auto-sized and centred on each gauge, GAUGE_CX
	 * either side of the middle, so the widest pair must not meet across
	 * it: "SPEND month" is 103.5 px and "ENTERPRISE" 91.2 at montserrat_14
	 * -- each well inside the 2 * GAUGE_CX between the two centres.
	 */
	CHECK((104 + 92) / 2 < 2 * GAUGE_CX,
	      "the spend page's two captions do not meet in the middle");
	CHECK(104 / 2 < SCR_W / 2 - GAUGE_CX,
	      "\"SPEND month\" clears the left bezel");
	/* Sized for the LONGEST tag the pill can hold, not the one we happen
	 * to ship: usage_view.c PILL_TAG_BUF is 12, so eleven characters is
	 * what has to fit without reaching the clock and the status dot in the
	 * corners. (The page tags themselves are USAGE_TAG_MAX, for matching;
	 * the pill shows the first eleven.) */
	CHECK(BUDGET_FITS(PILL_MAX_W, "claude code"),
	      "PILL_MAX_W is sized for the longest tag, not guessed");
	/* The status line replaced a 140 px name with a whole sentence, so it
	 * needs the width the name never did -- and it must still clear the
	 * bezel. */
	CHECK(BUDGET_FITS(STATUS_MAX_W, "Reading is old - showing last known"),
	      "STATUS_MAX_W holds the longest thing the status says");
	CHECK(STATUS_MAX_W + 2 * SCR_RIGHT_MARGIN_MIN <= SCR_W,
	      "the status line still clears both bezels");

	/* --- the font assumption these clearances rest on --- */
	CHECK(FONT_LINE_H == 16,
	      "FONT_LINE_H still matches LV_FONT_DEFAULT_MONTSERRAT_14");

	/*
	 * A two-line hint lands on the gauges. The label must ellipsize; this
	 * asserts the clearance that makes that non-negotiable.
	 */
	CHECK(STATUS_Y + FONT_LINE_H < GAUGE_ARC_Y + 4,
	      "a one-line hint clears the gauges");
	CHECK(STATUS_Y + 2 * FONT_LINE_H > GAUGE_ARC_Y,
	      "a two-line hint would land on the gauges, so it must ellipsize");

	/* --- the status-change popup --- */
	/*
	 * A popup on a full 320x240 panel is always a card ON TOP of
	 * something, so WHAT IT MAY COVER is the design, and these are it:
	 * not the percentage, and not the pip row above it. Those two are
	 * what someone crossing the room reads without stopping, and covering
	 * either of them for five seconds makes the panel worse in exchange
	 * for a sentence nobody asked to read right then.
	 *
	 * Clearing the whole arc is the strictly stronger claim and is what
	 * is asserted -- the percentage sits inside the ring, and the ring's
	 * bottom is the last thing above the band the card takes.
	 */
	CHECK(TOAST_TOP_Y >= GAUGE_ARC_Y + GAUGE_ARC_SZ,
	      "the popup clears the gauges, and so the percentages inside them");
	/*
	 * The page rail stays visible. It is the only thing on the screen
	 * that says WHICH PROVIDER you are looking at, and a card that hid it
	 * would leave the reader unable to tell whose session the sentence is
	 * about -- which is most of the sentence's value on a two-page desk.
	 */
	CHECK(SCR_H - TOAST_BOTTOM_OFF <= SCR_H - RAIL_BOTTOM_OFF - RAIL_H,
	      "the popup clears the page rail, which names the provider");
	CHECK(TOAST_MAX_W + 2 * SCR_RIGHT_MARGIN_MIN <= SCR_W,
	      "the popup clears both bezels");
	/*
	 * One line, with its padding, and the padding is real. TOAST_H is
	 * derived from the band the card is allowed, so a rail or countdown
	 * that moved could squeeze it below a line's height without anybody
	 * noticing until the text was clipped on a desk.
	 */
	CHECK(TOAST_H >= FONT_LINE_H && TOAST_PAD_V > 0,
	      "the popup is tall enough for its line, and pads it");
	/*
	 * The sentences that carry NO project name must fit whole: there is
	 * nothing shorter for them to fall back to. The longest is the
	 * waiting one.
	 */
	CHECK(BUDGET_FITS(TOAST_MAX_W, "A session is waiting for you"),
	      "TOAST_MAX_W is sized for the longest unnamed sentence");
	/* A real project name still fits beside it. */
	CHECK(BUDGET_FITS(TOAST_MAX_W, "LiveClaudeUi is waiting for you"),
	      "an ordinary project name fits too");
	/*
	 * And the longest label the buffer can hold does NOT, which is why
	 * the label ellipsizes rather than wraps -- the same bargain the hint
	 * line makes, asserted here so LV_LABEL_LONG_DOT is not optional.
	 */
	CHECK(!BUDGET_FITS(TOAST_MAX_W,
			   "123456789012345678901234567 is waiting for you"),
	      "a pathological label overruns the card, so it must ellipsize");

	check_focus();
	test_spend_page();

	printf(failures ? "\n%d FAILED\n" : "\nall layout checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
