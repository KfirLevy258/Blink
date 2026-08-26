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

static struct box top_left(const char *name, int x, int y, int w, int h)
{
	struct box b = { name, x, y, x + w, y + h };
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
	/* Header. */
	struct box pip = top_left("activity pip", ACT_PIP_X, ACT_PIP_Y,
				  ACT_PIP_SZ, ACT_PIP_SZ);

	/* The gauges. */
	struct box arc_l = top_mid("arc L", -GAUGE_CX, GAUGE_ARC_Y,
				   GAUGE_ARC_SZ, GAUGE_ARC_SZ);
	struct box arc_r = top_mid("arc R", GAUGE_CX, GAUGE_ARC_Y,
				   GAUGE_ARC_SZ, GAUGE_ARC_SZ);
	struct box pct_l = top_mid("percentage L", -GAUGE_CX, GAUGE_PCT_Y,
				   80, FONT_LINE_H);
	struct box p2_l = top_mid("second percentage L", -GAUGE_CX,
				  GAUGE_P2PCT_Y, GAUGE_P2_MAX_W, FONT_LINE_H);
	struct box name_l = top_mid("SESSION caption", -GAUGE_CX,
				    GAUGE_NAME_Y, 110, FONT_LINE_H);
	struct box name_r = top_mid("WEEKLY caption", GAUGE_CX, GAUGE_NAME_Y,
				    110, FONT_LINE_H);

	/* Four countdowns when a second provider reports: two per gauge,
	 * pushed apart by GAUGE_CD_DX. */
	struct box cd_l = top_mid("countdown L (primary)",
				  -GAUGE_CX - GAUGE_CD_DX, GAUGE_CD_Y,
				  GAUGE_CD_MAX_W, FONT_LINE_H);
	struct box cd_l2 = top_mid("countdown L (second)",
				   -GAUGE_CX + GAUGE_CD_DX, GAUGE_CD_Y,
				   GAUGE_CD_MAX_W, FONT_LINE_H);
	struct box cd_r = top_mid("countdown R (primary)",
				  GAUGE_CX - GAUGE_CD_DX, GAUGE_CD_Y,
				  GAUGE_CD_MAX_W, FONT_LINE_H);
	struct box cd_r2 = top_mid("countdown R (second)",
				   GAUGE_CX + GAUGE_CD_DX, GAUGE_CD_Y,
				   GAUGE_CD_MAX_W, FONT_LINE_H);

	/* The bottom line, shared between the counts and the hint. */
	struct box hint = bottom_mid("hint", HINT_BOTTOM_OFF, SCR_W,
				     FONT_LINE_H);
	struct box sess = bottom_mid("session readout", SESS_BOTTOM_OFF,
				     SESS_MAX_W, FONT_LINE_H);

	struct box all[] = { pip, arc_l, arc_r, pct_l, p2_l, name_l, name_r,
			     cd_l, cd_l2, cd_r, cd_r2, hint, sess };

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
	 * The inner ring eats the hollow the percentages live in. An inner
	 * ring of 84 with an 8 px wall left 68 px of centre and the render
	 * showed text sliced by its own gauge; the fix is counter-intuitive,
	 * because the usable hollow is the ring's diameter minus TWO walls, so
	 * a bigger, thinner inner ring buys space rather than spending it.
	 */
	CHECK(GAUGE_HOLLOW_W >= GAUGE_P2_MAX_W,
	      "the ring hollow fits the second provider's readout");
	CHECK(GAUGE_ARC2_SZ <= GAUGE_ARC_SZ - 2 * GAUGE_ARC_W,
	      "the inner ring stays inside the outer ring's wall");
	CHECK(!overlaps(pct_l, p2_l),
	      "the two percentages in the hollow do not overlap");

	/* --- four countdowns on one line --- */
	CHECK(!overlaps(cd_l, cd_l2), "the left gauge's two countdowns clear");
	CHECK(!overlaps(cd_r, cd_r2), "the right gauge's two countdowns clear");
	CHECK(cd_l2.x1 <= cd_r.x0,
	      "the two gauges' countdowns do not meet in the middle");
	CHECK(cd_l.y0 >= GAUGE_ARC_Y + GAUGE_ARC_SZ,
	      "countdowns sit below the rings, not inside them");
	CHECK(cd_l.y0 >= name_l.y1, "countdowns sit below the caption");
	CHECK(!overlaps(cd_l, hint), "countdowns clear the bottom line");
	CHECK(!overlaps(cd_r2, hint), "countdowns clear the bottom line");

	/* --- the bottom line --- */
	/* The counts and the hint SHARE this line by design -- the hint wins
	 * when it has something to say -- so they are expected to coincide,
	 * and the code, not the geometry, keeps them apart. */
	CHECK(sess.y0 == hint.y0, "counts and hint share one line, as intended");

	/* --- the header --- */
	CHECK(pip.y1 <= GAUGE_ARC_Y, "activity pip sits above the arcs");
	CHECK(!overlaps(pip, arc_l), "activity pip clears the left ring");

	/* --- the text budgets are real --- */
	CHECK(BUDGET_FITS(GAUGE_P2_MAX_W, "100%"),
	      "GAUGE_P2_MAX_W is sized for \"100%\", not guessed");
	CHECK(BUDGET_FITS(GAUGE_CD_MAX_W, "00m 00s"),
	      "GAUGE_CD_MAX_W is sized for a countdown, not guessed");

	/* --- the font assumption these clearances rest on --- */
	CHECK(FONT_LINE_H == 16,
	      "FONT_LINE_H still matches LV_FONT_DEFAULT_MONTSERRAT_14");

	printf(failures ? "\n%d FAILED\n" : "\nall layout checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
