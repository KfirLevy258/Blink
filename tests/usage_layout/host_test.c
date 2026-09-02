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
			     cd_l, cd_r, brand, status, who, rail };

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

	/*
	 * Two header rows again, not three. The clock is back in the corner
	 * it briefly vacated, so the arcs get their 20 px back.
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
	 * The pip row lives between the clock and the brand. Both edges are
	 * asserted because both are measurements, not constants the code
	 * knows: the clock's width comes from "12:04" at montserrat_14, and
	 * the wall from "BLINK" centred with .09em tracking. A font bump
	 * must fail here rather than slide pips under the logo.
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
	CHECK(PIP_X0 >= SCR_RIGHT_MARGIN_MIN,
	      "the pip row is not flush against the left bezel");
	CHECK(PIP_X0 + PIP_MAX * PIP_PITCH - (PIP_PITCH - PIP_SZ) <= PIP_WALL_X,
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
	/* Sized for the LONGEST tag the buffer can hold, not the one we happen
	 * to ship: provider1_tag is char[12], so eleven characters is what has
	 * to fit without reaching the clock and the status dot in the corners. */
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

	printf(failures ? "\n%d FAILED\n" : "\nall layout checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
