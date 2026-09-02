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

/*
 * Line height of lv_font_montserrat_20, the percentage label's own font --
 * NOT FONT_LINE_H, which is the DEFAULT font's height and belongs to every
 * other label on this screen. lv_font_montserrat_20.c declares
 * .line_height = 22. Needed to centre GAUGE_PCT_Y inside the ring rather than
 * offsetting it from a moving edge.
 */
#define GAUGE_PCT_FONT_H	22

/* Header: brand centred, clock left, age + dot right. */
#define TITLE_Y			6
#define HDR_ROW_Y		8
#define DOT_SZ			12
#define BRAND_TEXT		"BLINK"

/*
 * The clock is back in the top-left corner, and the rings are back to 120.
 *
 * It moved to a row of its own to free that corner for an execution-state
 * indicator, and the whole cost of the extra row came out of the ring --
 * 120 down to 100 -- because everything below the arcs is pinned. The
 * indicator is now a row of pips in the gap between the clock and the brand,
 * which was empty the entire time, so the row was never needed and the ring
 * gets its 20 px back.
 */

/*
 * The pip row: one mark per live session, in the gap between the clock and
 * the brand.
 *
 * Both bounds are MEASUREMENTS, which is why the layout test asserts them
 * rather than trusting them. "12:04" at montserrat_14 ends near x=47, and
 * "BLINK" is centred at 160 with .09em tracking and about 47 px wide, so it
 * begins near x=136. An 8 px gap either side leaves 75 px.
 *
 * PIP_MAX is geometry, not policy: seven fit. The display switches to counts
 * at SIX, which is where a row stops being read and starts being counted --
 * see fmt_pips(). The extra slot is headroom, not a target.
 */
#define PIP_SZ			8
#define PIP_PITCH		11
#define PIP_X0			56
#define PIP_WALL_X		130
#define PIP_MAX			7

/*
 * An 8 px pip on the 12 px health dot's centre line, written as the
 * difference rather than as the 2 it evaluates to: the header carries marks of
 * two sizes, and what makes it read as ONE row is that their centres agree.
 * Spelling it this way means it still agrees if either size changes.
 */
#define PIP_Y			(HDR_ROW_Y + (DOT_SZ - PIP_SZ) / 2)

/*
 * Counts mode's metrics: pip, gap, numeral, then the gap to the next group.
 *
 * PIP_NUM_ADV is a MEASUREMENT of the widest digit, like the two bounds above
 * are measurements of the clock and the brand. lv_font_montserrat_14 -- the
 * font this build links, pinned by CONFIG_LV_FONT_DEFAULT_MONTSERRAT_14 --
 * gives '4' an adv_w of 150 in 1/16 px, which is 9.375 px, in a 10 px ink box.
 * 9 truncates both, and a two-digit tally drawn 9 px per digit creeps right
 * with every digit until it reaches the brand. 10 covers every digit with the
 * ink box to spare.
 *
 * The pixel comes back out of PIP_GROUP_GAP, so a single-digit group is still
 * 26 px wide and the three groups counts mode can hold still start at
 * x = 56 / 82 / 108 -- the last numeral ending at 128, inside PIP_WALL_X.
 *
 * PIP_NUM_ADV is a BUDGET, not what the drawing uses. refresh_dots() measures
 * the string it actually wrote (lv_text_get_size) and stops at the wall,
 * because billing every digit the widest one's advance drops groups that had
 * room -- '1' advances 5.2 px, not 10. What this constant is for is choosing
 * the constants around it, and asserting in the layout test that the
 * single-digit case those constants promise still fits.
 */
#define PIP_NUM_GAP		2
#define PIP_NUM_ADV		10
#define PIP_GROUP_GAP		6

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
 * here, because "Reading is old - showing last known" is 35 characters, and
 * full width is safe because the row above carries only the clock and the
 * dot, each confined to its own corner.
 */
#define STATUS_Y		24
#define STATUS_MAX_W		300


/*
 * An execution-state indicator is BACK, and this is not a revert of the
 * decision that removed it.
 *
 * Commit 6540287 ("colour meant three different things, and two dots said
 * the same one") pulled a top-left activity pip and a top-right status dot
 * into one indicator, for two reasons: the top-left corner was occupied by
 * the clock, and two unlabelled circles in the same green/amber/red
 * vocabulary said unrelated things with no way to tell which.
 *
 * Both reasons are answered now, not ignored -- and the corner is not part of
 * the answer. What came back is a ROW, at PIP_X0 above, in the gap between
 * the clock and the brand that was empty either way; the clock keeps the
 * corner it has always had. And the hint line at STATUS_Y now names whichever
 * condition fired, so the marks up there are no longer unlabelled: colour
 * gets you "something is wrong", the sentence under it gets you which. That
 * is the arrangement commit 6540287 could not have had, because the label did
 * not exist yet.
 *
 * The row also answers something that commit did not raise and the single pip
 * it removed could not have fixed: one mark cannot speak for several
 * sessions. One per session can.
 *
 * See refresh_dots() in usage_view.c -- with the s, one call refreshing both
 * the health dot and the row.
 */

/* The two arcs. */
#define GAUGE_CX		80
#define GAUGE_ARC_Y		44
#define GAUGE_ARC_SZ		120
/*
 * Centred on the ring, not offset from its top.
 *
 * The ring shrank from the top -- its bottom stayed pinned at GAUGE_NAME_Y -
 * 4 -- so an earlier version of this that preserved "46 px from the arc's
 * top" preserved an offset from an edge that had moved, not where the number
 * actually sits inside the ring. The reader saw a percentage sitting low in
 * its ring by about 20 px. Centring on the ring is the invariant a reader
 * can actually see, and writing it as this expression means the label
 * follows automatically if the ring's size or position ever changes again.
 */
#define GAUGE_PCT_Y		(GAUGE_ARC_Y + (GAUGE_ARC_SZ - GAUGE_PCT_FONT_H) / 2)
/*
 * The ring's own hollow (GAUGE_ARC_SZ minus the stroke on both sides), not a
 * guess -- this used to just equal that by coincidence (96 = 120 - 2*12
 * before the ring shrank) until the ring did and the width didn't follow. A
 * label wider than the hollow paints over the coloured track it sits on.
 */
#define GAUGE_PCT_MAX_W		(GAUGE_ARC_SZ - 2 * GAUGE_ARC_W)

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
