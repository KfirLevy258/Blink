/* Standalone host test for the wipe transition's strip arithmetic.
 *
 * Build & run:
 *   cc -I ../../firmware/src host_test.c -o /tmp/slidetest && /tmp/slidetest
 *
 * A transition is one full render chopped into strips, and the only thing
 * deciding whether it reads as motion is WHICH strip is painted when. Get the
 * direction mapping wrong and the incoming screen assembles itself from the
 * edge it is supposed to be leaving through -- which looks like the gesture
 * being backwards, not like a coordinate being backwards, so it is the kind of
 * mistake that gets debugged in the wrong file.
 *
 * None of this needs a board, LVGL or Zephyr, which is why the arithmetic was
 * pulled out into ui_slide_geom.h in the first place.
 */
#include <stdio.h>
#include "ui_slide_geom.h"

#define HOR	320
#define VER	240
#define STEP	4
#define PAGE_STEP	8	/* what the page change uses */

static int step_override = STEP;
static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

/* The loop in ui_slide_run: j runs STEP..travel in steps of STEP. */
static int travel_of(int dir) { return ui_slide_travel(dir, HOR, VER); }

/* Every pixel along the axis painted exactly once, and nothing off-screen. */
static void covers_the_screen_once(int dir, const char *name)
{
	static char seen[HOR > VER ? HOR : VER];
	const int st = step_override;
	const int travel = travel_of(dir);
	const int vertical = ui_slide_is_vertical(dir);
	int i, j, bad_bounds = 0, bad_span = 0, twice = 0, missed = 0;

	for (i = 0; i < travel; i++) {
		seen[i] = 0;
	}
	for (j = st; j <= travel; j += st) {
		struct ui_slide_strip s = ui_slide_strip_at(dir, j, st,
							    HOR, VER);
		int lo = vertical ? s.y1 : s.x1;
		int hi = vertical ? s.y2 : s.x2;
		int other_lo = vertical ? s.x1 : s.y1;
		int other_hi = vertical ? s.x2 : s.y2;

		if (lo < 0 || hi >= travel) {
			bad_bounds++;
		}
		/* The strip must span the whole of the OTHER axis: a wipe that
		 * left a margin would reveal the new screen through a slot. */
		if (other_lo != 0 ||
		    other_hi != (vertical ? HOR : VER) - 1) {
			bad_span++;
		}
		if (hi - lo + 1 != st) {
			bad_span++;
		}
		for (i = lo; i <= hi && i < travel; i++) {
			if (i >= 0) {
				if (seen[i]) {
					twice++;
				}
				seen[i]++;
			}
		}
	}
	for (i = 0; i < travel; i++) {
		if (!seen[i]) {
			missed++;
		}
	}
	printf("-- %s (travel %d, %d steps of %d px)\n", name, travel,
	       travel / st, st);
	CHECK(bad_bounds == 0, "every strip is inside the screen");
	CHECK(bad_span == 0, "every strip spans the full width of its axis");
	CHECK(twice == 0, "no pixel is painted twice");
	CHECK(missed == 0, "no pixel is left unpainted");
}

/* Which edge the incoming screen arrives from. This is the bit a user sees. */
static void arrives_from(int dir, const char *name, int expect_far_edge)
{
	const int travel = travel_of(dir);
	const int vertical = ui_slide_is_vertical(dir);
	struct ui_slide_strip first = ui_slide_strip_at(dir, STEP, STEP,
							HOR, VER);
	struct ui_slide_strip last = ui_slide_strip_at(dir, travel, STEP,
						       HOR, VER);
	int first_lo = vertical ? first.y1 : first.x1;
	int last_lo = vertical ? last.y1 : last.x1;

	printf("-- %s: first strip at %d, last at %d\n", name,
	       first_lo, last_lo);
	if (expect_far_edge) {
		CHECK(first_lo == travel - STEP,
		      "the first strip lands at the far edge");
		CHECK(last_lo == 0, "the last strip lands at the near edge");
	} else {
		CHECK(first_lo == 0, "the first strip lands at the near edge");
		CHECK(last_lo == travel - STEP,
		      "the last strip lands at the far edge");
	}
}

int main(void)
{
	printf("== the axis is chosen by the direction ==\n");
	CHECK(!ui_slide_is_vertical(UI_SLIDE_LEFT), "LEFT is horizontal");
	CHECK(!ui_slide_is_vertical(UI_SLIDE_RIGHT), "RIGHT is horizontal");
	CHECK(ui_slide_is_vertical(UI_SLIDE_UP), "UP is vertical");
	CHECK(ui_slide_is_vertical(UI_SLIDE_DOWN), "DOWN is vertical");

	printf("\n== travel is the screen's extent along its own axis ==\n");
	CHECK(travel_of(UI_SLIDE_LEFT) == HOR, "horizontal travels 320");
	CHECK(travel_of(UI_SLIDE_UP) == VER, "vertical travels 240");
	/* Not a rounding trap: both are multiples of STEP_COLS, so the loop
	 * lands exactly on travel and the last strip is full width. */
	CHECK(HOR % STEP == 0 && VER % STEP == 0,
	      "both axes divide evenly into steps");

	printf("\n== a wipe covers the screen exactly once, each way ==\n");
	covers_the_screen_once(UI_SLIDE_LEFT, "LEFT");
	covers_the_screen_once(UI_SLIDE_RIGHT, "RIGHT");
	covers_the_screen_once(UI_SLIDE_UP, "UP");
	covers_the_screen_once(UI_SLIDE_DOWN, "DOWN");

	printf("\n== and at the wider step the page change uses ==\n");
	/* The strip width became a parameter when 60 steps was judged too slow,
	 * and a width that does not divide the travel leaves the last strip
	 * short -- the new screen with a stripe of the old one still on it. The
	 * code falls back rather than ship that; these are the widths it must
	 * never have to. */
	CHECK(HOR % PAGE_STEP == 0 && VER % PAGE_STEP == 0,
	      "the page step divides both axes exactly");
	step_override = PAGE_STEP;
	covers_the_screen_once(UI_SLIDE_UP, "UP at 8 px");
	covers_the_screen_once(UI_SLIDE_DOWN, "DOWN at 8 px");
	step_override = STEP;

	printf("\n== the incoming screen enters from the right edge ==\n");
	/* LEFT means the OLD screen exits left, so the new one comes in from
	 * the right: the wipe starts at the far edge and works back. UP is the
	 * same statement on the other axis -- the old screen goes up, so the
	 * new one comes up from below. That symmetry is the whole design, and
	 * breaking it is the most likely way to get this wrong. */
	arrives_from(UI_SLIDE_LEFT, "LEFT (new enters from the right)", 1);
	arrives_from(UI_SLIDE_UP, "UP (new enters from below)", 1);
	arrives_from(UI_SLIDE_RIGHT, "RIGHT (new enters from the left)", 0);
	arrives_from(UI_SLIDE_DOWN, "DOWN (new enters from above)", 0);

	printf("\n== UP is to DOWN as LEFT is to RIGHT ==\n");
	{
		int j, mismatched = 0;

		for (j = STEP; j <= VER; j += STEP) {
			struct ui_slide_strip u =
				ui_slide_strip_at(UI_SLIDE_UP, j, STEP, HOR, VER);
			struct ui_slide_strip l =
				ui_slide_strip_at(UI_SLIDE_LEFT, j, STEP, HOR, VER);

			/* Same index rule, different axis: at the same j, the
			 * vertical lead equals the horizontal lead measured
			 * from its own travel. */
			if (u.y1 != VER - j || l.x1 != HOR - j) {
				mismatched++;
			}
		}
		CHECK(mismatched == 0,
		      "the positive directions use one rule on both axes");
	}

	printf("\n%s\n", failures ? "FAILURES" : "all checks passed");
	return failures ? 1 : 0;
}
