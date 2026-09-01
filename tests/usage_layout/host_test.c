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
	struct box pct_l = top_mid("percentage L", -GAUGE_CX, GAUGE_PCT_Y,
				   80, FONT_LINE_H);
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
	struct box brand = top_mid("brand", 0, TITLE_Y, 90, FONT_LINE_H);
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
