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
#define MODEL_Y			23
#define MODEL_W			170

/* Execution-state pip, left column under the clock. */
#define ACT_PIP_X		12
#define ACT_PIP_Y		26
#define ACT_PIP_SZ		8

/* The two arcs. */
#define GAUGE_CX		78
#define GAUGE_ARC_Y		44
#define GAUGE_ARC_SZ		116
#define GAUGE_PCT_Y		86

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
#define GAUGE_CD_Y		112
#define GAUGE_NAME_Y		166

/* Context row, in the band between the countdowns and the hint. */
/*
 * The freed row, laid out as ONE line rather than two.
 *
 * The first attempt put the context meter on one line and its captions on
 * another, which collided with the hint -- the label that appears without
 * warning when something goes wrong, and therefore the one nothing else may
 * sit on top of. The layout test caught all five overlaps before any of it
 * was rendered.
 *
 * So: context gets a line of its own, and the bottom line is SHARED between
 * the hint and the session counts, with the hint winning. That mirrors what
 * the hint already does -- it is empty when all is well, which is exactly
 * when the counts are worth reading.
 */
#define CTX_CAP_X		(-134)
#define CTX_CAP_Y		194
#define CTX_CAP_MAX_W		30	/* "CTX" */
#define CTX_BAR_X		(-21)
#define CTX_BAR_Y		199
#define CTX_BAR_W		182
#define CTX_BAR_H		8	/* thicker, now that there is room */

/*
 * The value carries its own qualifier: "88%" alone, or "88% of 4" when
 * several sessions have a context and this is the worst of them.
 *
 * One label rather than two. With several agents running there are several
 * context windows and one number cannot be all of them; the bar shows the
 * fullest, because that is the one about to end somebody's turn, and the
 * suffix stops it reading as though it were the only one.
 */
#define CTX_VAL_X		114
#define CTX_VAL_Y		194
#define CTX_VAL_MAX_W		76	/* "100% of 9" */

/* Session and agent counts, in the bottom line the hint also uses. */
#define SESS_BOTTOM_OFF		6
#define SESS_MAX_W		200	/* "9 sessions  9 agents" */

/* Hint line, bottom-centred; carries the amber/red explanation. */
#define HINT_BOTTOM_OFF		6

/*
 * Clearances the layout must keep. Named so a failure says which rule broke
 * rather than printing two numbers.
 */
#define CTX_VAL_HINT_GAP_MIN	0	/* readout above the hint line */
#define CTX_BAR_VAL_GAP_MIN	4	/* bar's end to its own readout */
#define SCR_RIGHT_MARGIN_MIN	4	/* nothing flush against the bezel */

#endif /* USAGE_LAYOUT_H */
