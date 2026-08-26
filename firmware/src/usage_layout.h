#ifndef USAGE_LAYOUT_H
#define USAGE_LAYOUT_H

/*
 * Every coordinate on the gauge screen, in one place.
 *
 * These were inline magic numbers until a third row of widgets went in below
 * the gauges and the clearances stopped being obvious by eye. Two of them are
 * 2 px and 0 px, which is fine and also exactly the kind of thing that breaks
 * silently: bump the default font one size and the context readout lands on
 * top of the hint line. tests/usage_layout/host_test.c asserts the whole
 * arrangement from THIS header, so the check cannot drift from the code.
 *
 * Coordinates follow LVGL's alignment convention: X is an offset from the
 * named anchor (TOP_MID unless stated), Y is an offset from the top edge.
 */

/* The CYD panel, landscape. */
#define SCR_W			320
#define SCR_H			240
#define SCR_MID_X		(SCR_W / 2)

/*
 * Line height of LV_FONT_DEFAULT. Pinned to CONFIG_LV_FONT_DEFAULT_MONTSERRAT_14
 * -- lv_font_montserrat_14.c declares .line_height = 16. Every unlabelled
 * label on this screen is this tall, and the clearances below are only true
 * for this value.
 */
#define FONT_LINE_H		16

/* Header: brand centred, clock left, age + dot right. */
#define TITLE_Y			6
#define HDR_ROW_Y		8
#define DOT_SZ			12
#define BRAND_TEXT		"BLINK"

/*
 * Which model is in use, under the brand.
 *
 * BOUNDED, not auto-sized. A label left to size itself grows with its text,
 * and the daemon's cap is 24 characters -- about 190 px centred, which reaches
 * from x=65 to x=255 and leaves no left column for anything else. Fixing the
 * width and letting a long name ellipsize is what makes room for the session
 * readout beside it.
 */

/* Execution-state pip, left column under the clock. */
#define ACT_PIP_X		12
#define ACT_PIP_Y		26
#define ACT_PIP_SZ		8

/* The two arcs. */
#define GAUGE_CX		78
#define GAUGE_ARC_Y		34
#define GAUGE_ARC_SZ		128
#define GAUGE_PCT_Y		84

/*
 * The countdown moved INSIDE the ring, under the percentage.
 *
 * An arc's hollow centre was carrying one number and about sixty percent
 * nothing, while the row below the gauges was trying to fit a caption, a
 * countdown, a context meter and a hint into the last thirty pixels of the
 * panel. Putting the countdown where its own percentage already is costs no
 * new space, pairs the two numbers that belong together -- how much is gone,
 * how long until it comes back -- and frees a whole row underneath.
 */
/*
 * The countdowns are back OUT of the ring, under the caption.
 *
 * They lived in the hollow while there was one of them. With two providers
 * there are two, the hollow is 76 px across, and stacking them there would put
 * four numbers inside a ring 116 px wide. Under the gauge they get a line of
 * their own and can sit side by side, each in its provider's colour, which is
 * what makes "how long has each of them got" answerable at a glance.
 *
 * The hollow keeps both PERCENTAGES -- the primary large, the second small --
 * so it is still carrying its weight rather than going back to one number and
 * a lot of nothing.
 */
#define GAUGE_P2PCT_Y		112
#define GAUGE_NAME_Y		170
#define GAUGE_CD_Y		190
#define GAUGE_CD_MAX_W		72	/* "6d 22h" per provider */
/* Side by side when there are two, centred on the gauge when there is one. */
#define GAUGE_CD_DX		38

/*
 * A second provider, as an inner ring inside the same gauge.
 *
 * Provider is encoded by GEOMETRY, severity by COLOUR, and that split is the
 * whole design. Colour is what the eye resolves from across a desk -- green
 * means room, red means stop -- so it cannot also be spent on saying which
 * tool a number belongs to. Ring position carries that instead, and it is
 * legible on a second look without costing anything on the first.
 *
 * Thinner than the outer ring as well as smaller, so the primary provider
 * stays the thing you read and the second is peripheral awareness rather than
 * a competing headline.
 */
/*
 * 88 across with a 6 px wall, and both numbers matter.
 *
 * The first attempt used 84/8, which put the inner ring's wall right where the
 * countdown text runs and left a 68 px hollow for a 70 px string -- the render
 * showed "30m 00s" sliced by its own gauge. Counter-intuitively the fix is to
 * make the inner ring BIGGER: the usable hollow is the ring's diameter minus
 * two walls, so moving it outwards and thinning it buys space rather than
 * spending it. 88 - 12 = 76 px of clear centre, against a ~70 px countdown.
 *
 * The outer ring's inner edge sits at 116 - 2*12 = 92, so 88 leaves 4 px of
 * dark between the two rings -- enough to read them as separate without a
 * divider.
 */
#define GAUGE_ARC_W		12
#define GAUGE_ARC2_SZ		100
#define GAUGE_ARC2_W		6
#define GAUGE_ARC2_Y		(GAUGE_ARC_Y + (GAUGE_ARC_SZ - GAUGE_ARC2_SZ) / 2)
#define GAUGE_HOLLOW_W		(GAUGE_ARC2_SZ - 2 * GAUGE_ARC2_W)

/*
 * The provider ball: a small disc at the end of each arc, in that provider's
 * colour.
 *
 * This is what lets the ARC go back to green-amber-red. Severity is the thing
 * worth reading from across a desk and it belongs on the biggest element on
 * the panel; identity is a second-look question, and a dot the size of a
 * fingernail answers it without spending the ramp. It rides the end of the
 * filled arc, so it also marks the value.
 *
 * LVGL draws this as the arc's KNOB part, which the gauges used to delete
 * outright on the grounds that a readout is not a control. It still is not --
 * the arc stays unclickable -- but the knob is the only thing that tracks the
 * indicator's end, so it is styled rather than removed.
 */
#define GAUGE_BALL_PAD		2
#define GAUGE_P2_MAX_W		44	/* "100%" -- the tag is named once, below */

/* Context row, in the band between the countdowns and the hint. */
/*
 * No context row.
 *
 * It showed one context window, and with several agents running there are
 * several -- at different levels, refilling at different times, belonging to
 * conversations the panel cannot name. "88% of 4" was an attempt to qualify
 * one number into honesty and it did not earn its line: knowing that the
 * fullest of four contexts is at 88% does not tell you which one, and there is
 * nothing to do about it from across the room.
 *
 * The two things a desk gauge can answer -- how much quota is left, and how
 * long until it refills -- get the space instead.
 */

/* Session and agent counts, in the bottom line the hint also uses. */
#define SESS_BOTTOM_OFF		6
#define SESS_MAX_W		200	/* "9 sessions  9 agents" */

/* Hint line, bottom-centred; carries the amber/red explanation. */
#define HINT_BOTTOM_OFF		6

/*
 * Clearances the layout must keep. Named so a failure says which rule broke
 * rather than printing two numbers.
 */
#define SCR_RIGHT_MARGIN_MIN	4	/* nothing flush against the bezel */

#endif /* USAGE_LAYOUT_H */
