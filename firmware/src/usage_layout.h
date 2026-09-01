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

/*
 * Every Y below sits on a 4 px rhythm, and the gaps between rows are 4 or 8.
 *
 * They were arbitrary -- 34, 84, 112, 170, 190 -- chosen one at a time to make
 * whatever had just been added fit. Individually reasonable, collectively the
 * reason a screen reads as assembled rather than designed: nothing lines up
 * with anything, and the eye notices even when it cannot say why.
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

/* Header: brand centred, clock stacked under it, age + dot right. */
#define TITLE_Y			4
#define HDR_ROW_Y		8
#define DOT_SZ			12
#define BRAND_TEXT		"BLINK"

/*
 * The clock, centred UNDER the brand rather than in the top-left corner.
 *
 * It moved for the corner, not for itself: an execution-state indicator needs
 * a place the eye already goes, and the top-left is the only one left once the
 * age and the status dot have the right. Stacking brand over time also reads
 * as one header block instead of three things sharing a strip.
 */
#define CLOCK_Y			24

/*
 * Whose numbers these are, directly under the brand.
 *
 * ONE label for the whole screen, not a name beside each countdown. With one
 * provider per page there is exactly one answer, and writing it twice under
 * two gauges spent a line each time to repeat the same word -- which is also
 * what pushed those countdowns to a width that had to carry "claude  6d 22h"
 * instead of just a duration.
 *
 * It belongs under BLINK because that is the header's job: what you are
 * looking at. The gauges then say only how much and how long.
 *
 * BOUNDED, not auto-sized. A label left to size itself grows with its text,
 * and the daemon's tag cap is 11 characters; fixing the width means an
 * unexpected tag ellipsizes instead of reaching into the corners where the
 * clock and the status dot live.
 */
/*
 * The line under the brand carries the STATUS now, not the provider's name.
 *
 * The name moved to the bottom, where it became the control that changes it
 * (see the pill in usage_view and mk_page_zone in ui_settings). Having it
 * here as well was the same fact twice on one screen -- the header said
 * "Codex" while the pill said "Claude", which is two names and one page and
 * the reader has to work out which is which.
 *
 * The status took the freed line rather than the layout keeping a hole. It is
 * the better home for it: a warning belongs where the eye lands first, and it
 * had been sharing the bottom line with whatever else wanted it. Full width
 * here, because "Reading is old - showing last known" is 35 characters and
 * the corners above it are the clock and the dot on their own row.
 */
#define STATUS_Y		44
#define STATUS_MAX_W		300


/*
 * No execution-state pip. It sat top-left under the clock while the status dot
 * sat top-right -- two unlabelled circles in the same colour vocabulary saying
 * unrelated things. They are one indicator now, top-right; see refresh_dot().
 */

/* The two arcs. */
#define GAUGE_CX		80
#define GAUGE_ARC_Y		64
/*
 * 100, down from 120.
 *
 * The header grew a row and every pixel of it came from here, because nothing
 * below the arcs can move: the rail is pinned by RAIL_BOTTOM_OFF, the pill
 * must clear it by 2, the countdowns must clear the pill by 2, and the caption
 * must clear the countdowns by 2. GAUGE_NAME_Y at 168 is therefore a ceiling,
 * not a preference -- see the pill's own comment about an earlier 3 px padding
 * that went straight through the countdowns.
 *
 * 100 rather than 104 so every seam stays on the 4 px rhythm; 104 lands the
 * ring exactly on the caption with no gap at all.
 */
#define GAUGE_ARC_SZ		100
#define GAUGE_PCT_Y		110	/* same 46 px inside the ring as before */
#define GAUGE_PCT_MAX_W		96	/* "100%" at montserrat_20 */

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
#define GAUGE_NAME_Y		168
/*
 * The countdowns STACK, one per provider, rather than sitting side by side.
 *
 * Side by side, each got 72 px and could hold a duration and nothing else --
 * so which provider a countdown belonged to had to be inferred from its
 * colour, and the provider names ended up exiled to a line at the bottom of
 * the panel that said "inner ring: codex" and helped nobody.
 *
 * Stacked, each line has the full width of its gauge, which is enough to write
 * the provider's name next to its own number. The label is the thing that was
 * missing; the stack is what made room for it.
 */
#define GAUGE_CD_Y		186	/* first line: the primary provider */
#define GAUGE_CD_MAX_W		96	/* "00m 00s" -- named once, up top */

/* The ring itself. */
#define GAUGE_ARC_W		12

#define GAUGE_BALL_PAD		2
/* A ring of panel ground around each ball, so it separates from the arc it
 * rides regardless of that arc's colour. */
#define GAUGE_BALL_RING		2

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
#define SESS_BOTTOM_OFF		20
#define SESS_MAX_W		200	/* "9 sessions  9 agents" */

/*
 * The provider pill, bottom-centred above the rail: whose numbers these are,
 * and the control that changes it.
 *
 * State and control as one object, which is the idiom the settings panel's
 * old "Main source" row used before one provider per page replaced it. It
 * always shows, because it is the only thing on the screen that names the
 * page -- unlike the hint it used to share this line with, which is why the
 * hint moved up to STATUS_Y.
 */
/*
 * 16 px up, and the height is 20: the line plus 2 px of padding each side.
 *
 * The band it has to live in is exactly 24 px -- the countdowns end at 202
 * (GAUGE_CD_Y + FONT_LINE_H) and the rail begins at 226 -- so the padding is
 * what it can afford rather than what would look most generous. It lands at
 * 204..224, clear of both by 2. The first attempt used 3 px of padding and a
 * 20 px offset, which put it through the countdowns; the layout test caught
 * it before the board did.
 */
#define PILL_BOTTOM_OFF		16
#define PILL_PAD_V		2
#define PILL_H			(FONT_LINE_H + 2 * PILL_PAD_V)
#define PILL_MAX_W		140

/*
 * Clearances the layout must keep. Named so a failure says which rule broke
 * rather than printing two numbers.
 */
/*
 * The page rail: one mark per provider, along the bottom edge.
 *
 * BOTTOM rather than a side edge because both sides are already spoken for --
 * the chevrons there mean settings and the boot clip, and a dot column beside
 * them would be a third meaning on an edge that already carries one.
 *
 * Each mark is coloured by ITS OWN page's severity, which is what buys back
 * the only thing splitting the providers costs: with both on one gauge you
 * could see the second one going red without looking for it. Now the rail
 * says that instead, and says it from whichever page you happen to be on.
 *
 * Position is carried by WIDTH, never by colour, so the two channels never
 * compete for the same pixels.
 */
#define RAIL_PAGES_MAX		2
#define RAIL_H			6
#define RAIL_DOT_W		6
#define RAIL_ACT_W		16
#define RAIL_PITCH		14
#define RAIL_BOTTOM_OFF		8

#define SCR_RIGHT_MARGIN_MIN	4	/* nothing flush against the bezel */

#endif /* USAGE_LAYOUT_H */
