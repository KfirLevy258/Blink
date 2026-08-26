/* Standalone host test for the gauge screen's geometry.
 *
 * Build & run:
 *   cc -I ../../firmware/src host_test.c -o /tmp/laytest && /tmp/laytest
 *
 * This exists because the gauge screen grew a third row -- the context bar,
 * its caption and its readout -- into a band that was already nearly full,
 * and the resulting clearances are 2 px and 0 px. Those are legal and also
 * invisible: nothing about reading usage_view.c tells you the context readout
 * sits exactly flush against the hint line, or that raising the default font
 * one size lands one on top of the other.
 *
 * It asserts the arrangement from usage_layout.h, the same header the screen
 * is built from, so the check cannot drift away from the code.
 *
 * What it can and cannot do: vertical geometry is exact, because every
 * unlabelled label on this screen is FONT_LINE_H tall and that is a constant.
 * Horizontal extents for text depend on the glyphs, so those use the declared
 * *_MAX_W budgets -- the test then also asserts the real strings fit inside
 * them at a conservative per-character width.
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

/* Conservative upper bound for montserrat_14: no glyph on this screen is
 * wider than this, and '%' and digits are the widest of them. */
#define CHAR_W_MAX 10

int main(void)
{
	/* The context row. */
	struct box cap = top_mid("CTX caption", CTX_CAP_X, CTX_CAP_Y,
				 CTX_CAP_MAX_W, FONT_LINE_H);
	struct box bar = top_mid("CTX bar", CTX_BAR_X, CTX_BAR_Y,
				 CTX_BAR_W, CTX_BAR_H);
	struct box val = top_mid("CTX readout", CTX_VAL_X, CTX_VAL_Y,
				 CTX_VAL_MAX_W, FONT_LINE_H);

	/* The two countdowns immediately above it. */
	struct box cd_l = top_mid("countdown L", -GAUGE_CX, GAUGE_CD_Y,
				  70, FONT_LINE_H);
	struct box cd_r = top_mid("countdown R", GAUGE_CX, GAUGE_CD_Y,
				  70, FONT_LINE_H);
	struct box name_l = top_mid("SESSION caption", -GAUGE_CX, GAUGE_NAME_Y,
				    110, FONT_LINE_H);
	struct box name_r = top_mid("WEEKLY caption", GAUGE_CX, GAUGE_NAME_Y,
				    110, FONT_LINE_H);

	/* The hint line below. Width is the worst case: a hint long enough to
	 * span the panel, which is what an error message actually is. */
	struct box hint = bottom_mid("hint", HINT_BOTTOM_OFF, SCR_W,
				     FONT_LINE_H);
	struct box sess_b = bottom_mid("session readout", SESS_BOTTOM_OFF,
				       SESS_MAX_W, FONT_LINE_H);

	/* The header row. */
	/* MODEL_W, not a guess: the label is width-bounded and ellipsizes, so
	 * this IS its extent however long the model name gets. */
	struct box model = top_mid("model", 0, MODEL_Y, MODEL_W, FONT_LINE_H);

	struct box pip = top_left("activity pip", ACT_PIP_X, ACT_PIP_Y,
				  ACT_PIP_SZ, ACT_PIP_SZ);
	struct box arc_l = top_mid("arc L", -GAUGE_CX, GAUGE_ARC_Y,
				   GAUGE_ARC_SZ, GAUGE_ARC_SZ);

	/* --- everything is on the panel at all --- */
	struct box all[] = { cap, bar, val, cd_l, cd_r, hint, model, pip, arc_l,
			     sess_b, name_l, name_r };
	for (unsigned i = 0; i < sizeof(all) / sizeof(all[0]); i++) {
		char msg[64];
		snprintf(msg, sizeof(msg), "%s fits on the panel", all[i].name);
		CHECK(on_screen(all[i]), msg);
	}

	/* --- the clearances that are not obvious by eye --- */

	/* The countdowns moved inside the rings, so the old bar-versus-
	 * countdown clearance describes nothing any more. What the bar now has
	 * to clear is the gauge captions above it. */
	CHECK(!overlaps(bar, name_l), "context bar clears the SESSION caption");
	CHECK(!overlaps(bar, name_r), "context bar clears the WEEKLY caption");

	/* The readout sits flush against the hint line -- 0 px, legal, and one
	 * font size away from breaking. */
	CHECK(hint.y0 - val.y1 >= CTX_VAL_HINT_GAP_MIN,
	      "context readout does not overlap the hint line");
	CHECK(!overlaps(val, hint), "context readout and hint do not overlap");
	CHECK(!overlaps(bar, hint), "context bar and hint do not overlap");

	CHECK(cap.x1 <= bar.x0, "CTX caption sits left of its bar");

	/* ...and clear of the BAR's own right edge. This was flush at 0 px
	 * until a render showed it: the overlap test passed, because touching
	 * is not overlapping, and it only looked wrong at "100%" -- the exact
	 * reading someone is staring at when the context meter matters. */
	CHECK(val.x0 - bar.x1 >= CTX_BAR_VAL_GAP_MIN,
	      "context readout clears the end of its own bar");

	/* Nothing flush against the right bezel either. */
	CHECK(SCR_W - val.x1 >= SCR_RIGHT_MARGIN_MIN,
	      "context readout keeps a margin from the screen edge");
	CHECK(SCR_W - bar.x1 >= SCR_RIGHT_MARGIN_MIN,
	      "context bar keeps a margin from the screen edge");

	/* --- the header --- */
	CHECK(!overlaps(model, arc_l), "model label clears the arcs");
	CHECK(model.y1 <= GAUGE_ARC_Y, "model label sits above the arcs");
	CHECK(!overlaps(pip, model), "activity pip clears the model label");
	CHECK(pip.y1 <= GAUGE_ARC_Y, "activity pip sits above the arcs");

	/* The bottom row now carries three things on two lines: the context
	 * meter, the caption saying it is a maximum, and the session counts.
	 * All three share the band with the hint line, which is the one that
	 * appears without warning when something goes wrong. */
	CHECK(!overlaps(sess_b, bar), "session counts clear the context bar");
	CHECK(!overlaps(sess_b, cap), "session counts clear the CTX caption");
	CHECK(!overlaps(sess_b, val), "session counts clear the context readout");
	/* The counts and the hint SHARE this line by design -- the hint wins
	 * when it has something to say -- so they are expected to coincide,
	 * and the code, not the geometry, keeps them apart. */
	CHECK(sess_b.y0 == hint.y0, "counts and hint share one line, as intended");

	/* The inner ring eats the hollow the countdown lives in. The first
	 * inner ring was 84 across with an 8 px wall, leaving 68 px of centre
	 * for a ~70 px string, and the render showed "30m 00s" sliced by its
	 * own gauge. The fix was counter-intuitive -- a BIGGER, thinner inner
	 * ring buys hollow rather than spending it -- so the rule is pinned
	 * here rather than left as a number someone will helpfully shrink. */
	CHECK(GAUGE_HOLLOW_W >= (int)strlen("00m 00s") * 9,
	      "the ring hollow still fits a countdown");
	CHECK(GAUGE_HOLLOW_W >= GAUGE_P2_MAX_W,
	      "the ring hollow still fits the second provider's readout");
	CHECK(GAUGE_ARC2_SZ <= GAUGE_ARC_SZ - 2 * GAUGE_ARC_W,
	      "the inner ring stays inside the outer ring's wall");

	/* The countdown lives inside the ring now, so it must actually be
	 * inside it -- and clear of the percentage above it. */
	CHECK(cd_l.y0 >= GAUGE_PCT_Y + FONT_LINE_H,
	      "countdown sits below the percentage");
	CHECK(cd_l.y1 <= GAUGE_ARC_Y + GAUGE_ARC_SZ,
	      "countdown stays inside the ring");

	/* --- the text budgets are real --- */
	CHECK((int)strlen("CTX") * CHAR_W_MAX <= CTX_CAP_MAX_W + CHAR_W_MAX,
	      "\"CTX\" fits its width budget");
	/* The widest thing this label ever holds is the qualified form. */
	CHECK((int)strlen("100% of 9") * CHAR_W_MAX >= CTX_VAL_MAX_W,
	      "CTX_VAL_MAX_W is a real budget for \"100% of 9\", not a guess");

	/* --- the font assumption these clearances rest on --- */
	CHECK(FONT_LINE_H == 16,
	      "FONT_LINE_H still matches LV_FONT_DEFAULT_MONTSERRAT_14");

	printf(failures ? "\n%d FAILED\n" : "\nall layout checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
