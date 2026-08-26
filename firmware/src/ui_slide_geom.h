#ifndef UI_SLIDE_GEOM_H
#define UI_SLIDE_GEOM_H

/*
 * The strip arithmetic behind a wipe transition, as a pure function.
 *
 * Extracted from ui_slide.c so it can be checked on a laptop. It is four
 * lines of index maths and it is the whole transition: get the direction
 * mapping wrong and the incoming screen assembles itself from the side it is
 * supposed to be leaving from, which looks like a bug in the gesture rather
 * than in a coordinate. tests/ui_slide_geom/host_test.c pins it.
 *
 * No includes on purpose -- not lvgl.h, not zephyr.h. A header that drags in
 * either cannot be compiled by a host test, and a geometry check nobody can
 * run is a comment.
 */

/*
 * Direction the OUTGOING image travels. The sign is the axis's positive
 * direction on screen; the magnitude picks the axis.
 *
 * Horizontal is what the panel's own scroll register can do (rotation 90 maps
 * the chip's native vertical axis onto the screen's horizontal one), so it is
 * the only direction a true hardware SLIDE can take. A wipe has no such
 * constraint -- it paints strips where they will be seen and never touches the
 * scroll register -- so vertical is available to the wipe and to nothing else.
 * ui_slide_run() enforces that.
 */
#define UI_SLIDE_LEFT	1	/* old exits left, new enters from the right */
#define UI_SLIDE_RIGHT	(-1)	/* old exits right, new enters from the left */
#define UI_SLIDE_UP	2	/* old exits up, new enters from below */
#define UI_SLIDE_DOWN	(-2)	/* old exits down, new enters from above */

struct ui_slide_strip {
	int x1, y1, x2, y2;
};

static inline int ui_slide_is_vertical(int dir)
{
	return dir == UI_SLIDE_UP || dir == UI_SLIDE_DOWN;
}

/*
 * How far the transition travels: the screen's extent along its own axis.
 *
 * Not a single constant, because the two axes of this panel are 320 and 240.
 * The step count follows from it, so a vertical transition is 60 steps where a
 * horizontal one is 80 -- and both render the screen exactly once, which is
 * the property that makes either affordable at all.
 */
static inline int ui_slide_travel(int dir, int hor_res, int ver_res)
{
	return ui_slide_is_vertical(dir) ? ver_res : hor_res;
}

/*
 * The strip arriving this step, in screen coordinates.
 *
 * `j` counts painted pixels along the axis and runs step..travel in steps of
 * `step`. The strip spans the whole of the other axis.
 *
 * The mapping is one rule applied twice: a POSITIVE direction (LEFT, UP) means
 * the incoming screen enters from the far edge, so the wipe runs backwards
 * along the axis; a negative one (RIGHT, DOWN) runs forwards. LEFT is to
 * RIGHT exactly as UP is to DOWN, which is the invariant worth testing.
 */
static inline struct ui_slide_strip ui_slide_strip_at(int dir, int j, int step,
						      int hor_res, int ver_res)
{
	const int travel = ui_slide_travel(dir, hor_res, ver_res);
	const int positive = dir > 0;
	const int lead = positive ? travel - j : j - step;
	struct ui_slide_strip s;

	if (ui_slide_is_vertical(dir)) {
		s.x1 = 0;
		s.x2 = hor_res - 1;
		s.y1 = lead;
		s.y2 = lead + step - 1;
	} else {
		s.y1 = 0;
		s.y2 = ver_res - 1;
		s.x1 = lead;
		s.x2 = lead + step - 1;
	}
	return s;
}

#endif /* UI_SLIDE_GEOM_H */
