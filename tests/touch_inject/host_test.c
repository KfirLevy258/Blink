/*
 * The bench touch injector's arithmetic: where a screen point lands in the
 * touch controller's channel frame, and when each report of a stroke goes out.
 *
 *   cc -I firmware/src -o /tmp/t tests/touch_inject/host_test.c && /tmp/t
 *
 * (tests/ci/check_host_tests.sh runs this; the line above is for editing it.)
 *
 * Two things are checked here because nothing else can check them without a
 * board. The transform has to be the exact inverse of what Zephyr's
 * lvgl_pointer_input glue does on THIS devicetree, or every injected tap lands
 * on the wrong button -- and the board would still say "touch_done ok". And
 * the timing has to look like the real panel, or the injector proves only
 * that LVGL accepts a stroke no finger could make.
 *
 * The forward model, ti_chan_to_screen(), is written out step by step from
 * lvgl_pointer_process_event() rather than as the two-line shortcut the
 * inverse reduces to, so a test of the round trip is a test against the glue
 * and not against itself. ui_swipe.c's SCREEN_DX/SCREEN_DY is the third
 * witness: it reads displacements straight off the channels, and the injector
 * has to agree with it or the gauge screen's swipes would go the wrong way.
 */
#include <stdbool.h>
#include <stdio.h>
#include <stdlib.h>

#include "touch_inject_geom.h"
#include "ui_swipe_geom.h"

static int fails;

static void eq(const char *what, long got, long want)
{
	if (got != want) {
		fails++;
		printf("FAIL: %s (got %ld, want %ld)\n", what, got, want);
	} else {
		printf("PASS: %s\n", what);
	}
}

static void ok(const char *what, bool cond)
{
	eq(what, cond ? 1 : 0, 1);
}

/* ui_swipe.c's transform, copied by meaning: screen displacement from a
 * channel displacement. Kept identical so a change there fails here. */
#define SWIPE_SCREEN_DX(cx, cy) (cy)
#define SWIPE_SCREEN_DY(cx, cy) (cx)

static void transform(void)
{
	int bad = 0, out = 0;

	/* Every pixel, both ways. */
	for (int sy = 0; sy < TI_SCREEN_H; sy++) {
		for (int sx = 0; sx < TI_SCREEN_W; sx++) {
			int cx, cy, rx, ry;

			ti_screen_to_chan(sx, sy, &cx, &cy);
			if (cx < 0 || cx > TI_CHAN_X_MAX || cy < 0 ||
			    cy > TI_CHAN_Y_MAX) {
				out++;
			}
			ti_chan_to_screen(cx, cy, &rx, &ry);
			if (rx != sx || ry != sy) {
				bad++;
			}
		}
	}
	eq("every screen pixel survives the round trip", bad, 0);
	eq("every screen pixel maps inside the channel range", out, 0);

	/* The corners, by hand: ABS_X is the panel's vertical, ABS_Y its
	 * horizontal (the overlay's own description of the channel frame). */
	int cx, cy;

	ti_screen_to_chan(0, 0, &cx, &cy);
	eq("top-left -> ABS_X", cx, 0);
	eq("top-left -> ABS_Y", cy, 0);
	ti_screen_to_chan(319, 0, &cx, &cy);
	eq("top-right -> ABS_X", cx, 0);
	eq("top-right -> ABS_Y", cy, 319);
	ti_screen_to_chan(0, 239, &cx, &cy);
	eq("bottom-left -> ABS_X", cx, 239);
	eq("bottom-left -> ABS_Y", cy, 0);
	ti_screen_to_chan(298, 120, &cx, &cy);
	eq("settings edge zone -> ABS_X", cx, 120);
	eq("settings edge zone -> ABS_Y", cy, 298);

	/* Agreement with ui_swipe over a stroke: the screen displacement it
	 * reads off the injected channels is the one that was asked for. */
	int ax, ay, bx, by;

	ti_screen_to_chan(240, 120, &ax, &ay);
	ti_screen_to_chan(80, 130, &bx, &by);
	eq("ui_swipe reads the injected dx",
	   SWIPE_SCREEN_DX(bx, by) - SWIPE_SCREEN_DX(ax, ay), 80 - 240);
	eq("ui_swipe reads the injected dy",
	   SWIPE_SCREEN_DY(bx, by) - SWIPE_SCREEN_DY(ax, ay), 130 - 120);

	/* The glue clamps to the screen; the forward model must too, or the
	 * round trip above would be proving an identity it does not have. */
	int rx, ry;

	ti_chan_to_screen(-5, 400, &rx, &ry);
	eq("glue clamps x to the last column", rx, TI_SCREEN_W - 1);
	eq("glue clamps y to the first row", ry, 0);
}

static void tap(void)
{
	struct ti_stroke s = { .op = TI_OP_TAP, .x0 = 36, .y0 = 20, .ms = 120 };
	struct ti_sample p;
	int n = ti_samples(&s);

	ok("a 120 ms tap is valid", ti_valid(&s));
	/* 0, 13, ..., 117: ten reports while the finger is down. */
	eq("a 120 ms tap carries ten reports", n, 10);
	for (int i = 0; i < n; i++) {
		ti_sample(&s, i, &p);
		if (p.x != 36 || p.y != 20 || p.t_ms != i * TI_STEP_MS) {
			eq("a tap stays put, one report per step", i, -1);
			return;
		}
	}
	ok("a tap stays put, one report per step", true);
	/* The XPT2046 driver reports the lift 100 ms after the last report
	 * that still saw the finger, from its delayed re-check. */
	eq("the tap's release comes one driver re-check after the last report",
	   ti_release_ms(&s), 9 * TI_STEP_MS + TI_RELEASE_LAG_MS);

	struct ti_stroke z = { .op = TI_OP_TAP, .x0 = 1, .y0 = 1, .ms = 0 };

	eq("a zero-hold tap is still one report", ti_samples(&z), 1);
	eq("...and a release", ti_release_ms(&z), TI_RELEASE_LAG_MS);
}

/* Every swipe the bench script makes, played and checked the way the board
 * will read it: by ui_swipe's own rules, and by LVGL's gesture floor. */
static void swipe_one(const char *name, int x0, int y0, int x1, int y1,
		      int ms, enum ui_swipe_dir want)
{
	struct ti_stroke s = { .op = TI_OP_SWIPE, .x0 = x0, .y0 = y0,
			       .x1 = x1, .y1 = y1, .ms = ms };
	struct ti_sample p, prev = { 0 };
	int n = ti_samples(&s);
	int worst_gap = 0, still = 0, fired_at = -1;
	char what[96];

	snprintf(what, sizeof(what), "%s is valid", name);
	ok(what, ti_valid(&s));
	for (int i = 0; i < n; i++) {
		ti_sample(&s, i, &p);
		if (i > 0) {
			int gap = p.t_ms - prev.t_ms;

			if (gap > worst_gap) {
				worst_gap = gap;
			}
			/* LVGL's gesture_min_velocity is 1 on this board
			 * (ui_settings.c): a sample that moved less than that
			 * in both axes zeroes its accumulator. */
			if (abs(p.x - prev.x) < 1 && abs(p.y - prev.y) < 1) {
				still++;
			}
		}
		if (fired_at < 0 &&
		    ui_swipe_classify(p.x - x0, p.y - y0) == want) {
			fired_at = p.t_ms;
		}
		prev = p;
	}
	snprintf(what, sizeof(what), "%s starts where it was asked", name);
	ti_sample(&s, 0, &p);
	ok(what, p.x == x0 && p.y == y0 && p.t_ms == 0);
	snprintf(what, sizeof(what), "%s ends where it was asked, on time",
		 name);
	ti_sample(&s, n - 1, &p);
	ok(what, p.x == x1 && p.y == y1 && p.t_ms == ms);
	/* The real panel's median is 13 ms; integer division of the duration
	 * can stretch a step by under one step's worth. */
	snprintf(what, sizeof(what), "%s reports at the panel's pace", name);
	ok(what, worst_gap >= TI_STEP_MS && worst_gap < 2 * TI_STEP_MS);
	snprintf(what, sizeof(what), "%s moves on every report", name);
	eq(what, still, 0);
	snprintf(what, sizeof(what), "%s fires ui_swipe before the lift", name);
	ok(what, fired_at >= 0 && fired_at < ti_release_ms(&s));
}

static void swipes(void)
{
	/* The bench targets in tools/board_touch.py, by the same numbers. */
	swipe_one("settings swipe (left)", 260, 120, 100, 120, 260,
		  UI_SWIPE_LEFT);
	swipe_one("eye swipe (right)", 60, 120, 220, 120, 260, UI_SWIPE_RIGHT);
	swipe_one("back swipe (right)", 60, 150, 220, 150, 260,
		  UI_SWIPE_RIGHT);
	swipe_one("eye exit (left)", 260, 120, 100, 120, 260, UI_SWIPE_LEFT);
	swipe_one("page swipe (up)", 160, 200, 160, 60, 260, UI_SWIPE_UP);
	/* eye.exit_short: 30 px, which LVGL's 24 px gesture floor takes and
	 * ui_swipe's 36 px one never does, at any point of the stroke. */
	{
		struct ti_stroke s = { .op = TI_OP_SWIPE, .x0 = 200, .y0 = 120,
				       .x1 = 170, .y1 = 120, .ms = 130 };
		struct ti_sample p;
		int fired = 0;

		for (int i = 0; i < ti_samples(&s); i++) {
			ti_sample(&s, i, &p);
			fired |= ui_swipe_classify(p.x - 200, p.y - 120) !=
				 UI_SWIPE_NONE;
		}
		ok("the short eye exit never fires ui_swipe", !fired);
		ok("...and travels past LVGL's 24 px gesture floor",
		   200 - p.x > 24);
	}
	/* The shortest duration a swipe may have is one step. */
	swipe_one("a one-step swipe", 100, 100, 200, 100, TI_STEP_MS,
		  UI_SWIPE_RIGHT);
}

static void refusals(void)
{
	struct ti_stroke s = { .op = TI_OP_TAP, .x0 = 320, .y0 = 0, .ms = 50 };

	ok("x past the last column is refused", !ti_valid(&s));
	s.x0 = 0;
	s.y0 = 240;
	ok("y past the last row is refused", !ti_valid(&s));
	s.y0 = -1;
	ok("a negative y is refused", !ti_valid(&s));
	s.y0 = 0;
	s.ms = TI_MAX_MS + 1;
	ok("a hold longer than the cap is refused", !ti_valid(&s));
	s.ms = -1;
	ok("a negative hold is refused", !ti_valid(&s));

	struct ti_stroke w = { .op = TI_OP_SWIPE, .x0 = 10, .y0 = 10,
			       .x1 = 300, .y1 = 10, .ms = TI_STEP_MS - 1 };

	ok("a swipe shorter than one report is refused", !ti_valid(&w));
	w.ms = 200;
	w.y1 = 240;
	ok("a swipe ending off the panel is refused", !ti_valid(&w));

	struct ti_stroke pr = { .op = TI_OP_PRESS, .x0 = 5, .y0 = 5 };

	ok("a press needs no duration", ti_valid(&pr));

	/* The two that are not strokes: no finger, no samples, always valid. */
	struct ti_stroke dm = { .op = TI_OP_DISMISS }, wk = { .op = TI_OP_WAKE };

	ok("dismiss needs no arguments", ti_valid(&dm));
	ok("wake needs no arguments", ti_valid(&wk));
	eq("dismiss plays no reports", ti_samples(&dm), 0);
	eq("wake plays no reports", ti_samples(&wk), 0);
	eq("a press is held by re-reporting, one report per step",
	   ti_samples(&pr), TI_HOLD_MAX_MS / TI_STEP_MS + 1);
}

static void parse(void)
{
	eq("tap", ti_op_from_name("tap"), TI_OP_TAP);
	eq("swipe", ti_op_from_name("swipe"), TI_OP_SWIPE);
	eq("press", ti_op_from_name("press"), TI_OP_PRESS);
	eq("release", ti_op_from_name("release"), TI_OP_RELEASE);
	eq("dismiss", ti_op_from_name("dismiss"), TI_OP_DISMISS);
	eq("wake", ti_op_from_name("wake"), TI_OP_WAKE);
	eq("anything else", ti_op_from_name("pinch"), TI_OP_NONE);
	eq("a prefix is not a name", ti_op_from_name("ta"), TI_OP_NONE);
}

int main(void)
{
	transform();
	tap();
	swipes();
	refusals();
	parse();
	if (fails) {
		printf("%d check(s) failed\n", fails);
		return 1;
	}
	return 0;
}
