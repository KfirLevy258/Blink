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
#define GAUGE_PCT_Y		92
#define GAUGE_NAME_Y		166
#define GAUGE_CD_Y		188

/* Context row, in the band between the countdowns and the hint. */
#define CTX_CAP_X		(-116)
#define CTX_CAP_Y		202
#define CTX_CAP_MAX_W		30	/* "CTX" */
#define CTX_BAR_X		16
#define CTX_BAR_Y		206
#define CTX_BAR_W		200
#define CTX_BAR_H		6
#define CTX_VAL_X		134
#define CTX_VAL_Y		202
#define CTX_VAL_MAX_W		36	/* "100%" */

/*
 * Session/agent readout, in the header's left column beneath the activity
 * pip. Left rather than centre because the centre is the brand and the model
 * name, and right is the age and the status dot -- the left column has the
 * clock and the pip and room under both.
 */
#define SESS_X			22
#define SESS_Y			24
#define SESS_MAX_W		44	/* "9s 9a" -- compact on purpose, see MODEL_W */

/* Hint line, bottom-centred; carries the amber/red explanation. */
#define HINT_BOTTOM_OFF		6

/*
 * Clearances the layout must keep. Named so a failure says which rule broke
 * rather than printing two numbers.
 */
#define CTX_BAR_CD_GAP_MIN	2	/* bar below the countdown text */
#define CTX_VAL_HINT_GAP_MIN	0	/* readout above the hint line */

#endif /* USAGE_LAYOUT_H */
