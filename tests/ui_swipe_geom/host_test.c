/*
 * The swipe rules, checked against strokes that really happened.
 *
 *   cc -I firmware/src -o /tmp/t tests/ui_swipe_geom/host_test.c && /tmp/t
 *
 * (tests/ci/check_host_tests.sh runs this; the line above is for editing it.)
 *
 * The displacements marked "trace" are from the touch-trace capture of
 * 2026-08-27 -- five deliberate vertical swipes on the board, recorded in the
 * channel frame and converted to screen coordinates. They are the reason the
 * thresholds are what they are, so they are what the thresholds are tested
 * against. A rule that stops admitting them has broken the panel it was
 * written for, whatever it does to invented numbers.
 */
#include <stdbool.h>
#include <stdio.h>

#include "ui_swipe_geom.h"

static int fails;
static int checks;

/* PASS/FAIL one line per check, which is the convention the runner counts
 * (tests/ci/check_host_tests.sh greps for lines starting with PASS). */
static void eq(const char *what, int got, int want)
{
	checks++;
	if (got != want) {
		fails++;
		printf("FAIL: %s (got %d, want %d)\n", what, got, want);
	} else {
		printf("PASS: %s\n", what);
	}
}

/*
 * A stroke's duration matters only below the distance floor, so the cases that
 * are about direction or dominance pass a time that is comfortably slow --
 * proving they were admitted on distance, not accidentally rescued by speed.
 */
#define SLOW_MS 500

static void classify(const char *what, int dx, int dy, enum ui_swipe_dir want)
{
	eq(what, (int)ui_swipe_classify(dx, dy, SLOW_MS), (int)want);
}

static void classify_ms(const char *what, int dx, int dy, int ms,
			enum ui_swipe_dir want)
{
	eq(what, (int)ui_swipe_classify(dx, dy, ms), (int)want);
}

int main(void)
{
	/* --- the real strokes, which must all be admitted --------------- */
	/* Vertical swipes, screen dy, from the trace. Up is negative. */
	classify("trace up 174", 44, -174, UI_SWIPE_UP);
	classify("trace up 144", 24, -144, UI_SWIPE_UP);
	classify("trace up 120", -1, -120, UI_SWIPE_UP);
	classify("trace up 110", 26, -110, UI_SWIPE_UP);
	classify("trace down 178", -20, 178, UI_SWIPE_DOWN);
	classify("trace down 177", 3, 177, UI_SWIPE_DOWN);

	/*
	 * The stroke that opened settings mid-swipe. It measured 2.9x
	 * horizontal, so it WAS a leftward drag and is classified as one --
	 * the dominance rule is not what fixes that case, the stitching is
	 * (it was one fragment of a longer stroke). Pinned so nobody "fixes"
	 * it by tightening dominance until real horizontal swipes stop
	 * working.
	 */
	classify("trace diagonal-ish left", -154, 53, UI_SWIPE_LEFT);

	/* --- direction and sign ----------------------------------------- */
	classify("right", 100, 0, UI_SWIPE_RIGHT);
	classify("left", -100, 0, UI_SWIPE_LEFT);
	classify("down", 0, 100, UI_SWIPE_DOWN);
	classify("up", 0, -100, UI_SWIPE_UP);

	/* Screen y grows downward, so "up" is the negative one. Getting this
	 * backwards pages the wrong way and reads as the panel fighting you. */
	eq("up is negative dy", (int)ui_swipe_classify(0, -60, SLOW_MS),
	   UI_SWIPE_UP);
	eq("down is positive dy", (int)ui_swipe_classify(0, 60, SLOW_MS),
	   UI_SWIPE_DOWN);

	/* --- the travel floor ------------------------------------------- */
	classify("still", 0, 0, UI_SWIPE_NONE);
	classify("tap jitter", 6, -4, UI_SWIPE_NONE);
	classify("just under the floor", 0, UI_SWIPE_MIN_PX - 1, UI_SWIPE_NONE);
	classify("exactly the floor", 0, UI_SWIPE_MIN_PX, UI_SWIPE_DOWN);
	classify("floor, negative", 0, -UI_SWIPE_MIN_PX, UI_SWIPE_UP);
	classify("floor, horizontal", UI_SWIPE_MIN_PX, 0, UI_SWIPE_RIGHT);

	/* --- dominance --------------------------------------------------- */
	/* 1.5x is the line. 100 needs a minor of 66 or less. */
	classify("100 by 66 passes", 0 + 66, 100, UI_SWIPE_DOWN);
	classify("100 by 67 refused", 0 + 67, 100, UI_SWIPE_NONE);
	classify("perfect diagonal refused", 100, 100, UI_SWIPE_NONE);
	classify("near diagonal refused", 100, -90, UI_SWIPE_NONE);
	/* Refusal is symmetric: no axis, no sign, gets a private exemption. */
	classify("diagonal refused, mirrored x", -100, 100, UI_SWIPE_NONE);
	classify("diagonal refused, mirrored y", 100, -100, UI_SWIPE_NONE);
	classify("diagonal refused, both", -100, -100, UI_SWIPE_NONE);

	/*
	 * A stroke that is long but too diagonal is refused even though it is
	 * far over the travel floor. Both rules apply; passing one is not
	 * enough. This is the case that used to open settings during a
	 * vertical swipe.
	 */
	classify("long and diagonal", 200, 190, UI_SWIPE_NONE);

	/* --- the flick rule: short strokes, admitted on speed ----------- */
	/*
	 * The two real short swipes the log refused, which is what this rule
	 * was added for. Both are well under the 36 px floor and both were
	 * quick. Pinned with the displacements exactly as they were logged.
	 */
	classify_ms("logged short swipe, 18 px flicked", 5, 18, 80,
		    UI_SWIPE_DOWN);
	classify_ms("logged short swipe, 35 px flicked", 2, -35, 150,
		    UI_SWIPE_UP);

	/* The tap in the same log stays refused however fast it was: 6 px is
	 * under the drag line, and no speed rescues something that did not
	 * move. */
	classify_ms("logged tap stays refused, however quick", -3, -6, 20,
		    UI_SWIPE_NONE);

	/* The same short stroke, done slowly, is a drifting tap and is not a
	 * swipe. This is the whole distinction the rule rests on: identical
	 * displacement, opposite verdicts, decided by speed. */
	classify_ms("35 px slowly is not a swipe", 2, -35, 400, UI_SWIPE_NONE);
	classify_ms("35 px quickly is a swipe", 2, -35, 200, UI_SWIPE_UP);

	/* 150 px/s exactly, either side of it. 30 px in 200 ms is exactly the
	 * line and passes; one millisecond slower does not. */
	classify_ms("exactly 150 px/s passes", 0, 30, 200, UI_SWIPE_DOWN);
	classify_ms("just under 150 px/s refused", 0, 30, 201, UI_SWIPE_NONE);

	/* Above the distance floor, speed is not asked at all -- a long,
	 * deliberate, slow drag is still a swipe. */
	classify_ms("long and slow is still a swipe", 0, 180, 2000,
		    UI_SWIPE_DOWN);

	/* A flick still has to pick an axis. Speed does not buy past the
	 * dominance rule, or a fast diagonal becomes a coin toss again. */
	classify_ms("fast diagonal still refused", 25, 24, 50, UI_SWIPE_NONE);

	/* A whole stroke inside one poll has no measurable duration. Treated
	 * as fast, because nothing that slow can complete in under 10 ms. */
	classify_ms("zero duration counts as fast", 0, 20, 0, UI_SWIPE_DOWN);

	/* --- the tap/drag line ------------------------------------------ */
	eq("still is not a drag", ui_swipe_is_drag(0, 0), false);
	eq("tap jitter is not a drag", ui_swipe_is_drag(8, 8), false);
	eq("one under is not a drag",
	   ui_swipe_is_drag(UI_SWIPE_DRAG_PX - 1, 0), false);
	eq("exactly the line is a drag",
	   ui_swipe_is_drag(UI_SWIPE_DRAG_PX, 0), true);
	eq("vertical drag counts", ui_swipe_is_drag(0, UI_SWIPE_DRAG_PX), true);
	eq("negative drag counts",
	   ui_swipe_is_drag(-UI_SWIPE_DRAG_PX, 0), true);

	/*
	 * Every real swipe is also a drag, which is what lets the edge zones
	 * disown the click at the end of one. If this ever stops holding, a
	 * swipe that lands on a zone opens the thing it was swiping away from.
	 */
	eq("swipes are drags: min travel", ui_swipe_is_drag(0, UI_SWIPE_MIN_PX),
	   true);
	eq("the drag line sits below the swipe floor",
	   UI_SWIPE_DRAG_PX <= UI_SWIPE_MIN_PX, true);
	/* And the shortest thing the flick rule can admit is exactly the drag
	 * line, so a flick is a drag too -- the edge zones disown its click. */
	eq("the shortest flick is a drag",
	   ui_swipe_is_drag(0, UI_SWIPE_DRAG_PX), true);
	classify_ms("the shortest possible flick", 0, UI_SWIPE_DRAG_PX, 10,
		    UI_SWIPE_DOWN);
	classify_ms("one pixel under the drag line is never a swipe",
		    0, UI_SWIPE_DRAG_PX - 1, 1, UI_SWIPE_NONE);

	printf("ui_swipe_geom: %d checks, %d failed\n", checks, fails);
	return fails != 0;
}
