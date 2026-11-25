/* Host test for the pure brightness stepping and quiet-hours window logic.
 *   cc -I firmware/src tests/backlight/host_test.c -o /tmp/bl && /tmp/bl
 */
#include <stdio.h>
#include "backlight.h"

static int failures;
#define CK(cond, name) do{ if(cond){printf("PASS: %s\n",name);} \
	else {printf("FAIL: %s\n",name); failures++;} }while(0)

int main(void)
{
	CK(backlight_next_level(60, +1) == 80, "up from 60 -> 80");
	CK(backlight_next_level(60, -1) == 40, "down from 60 -> 40");
	CK(backlight_next_level(80, +1) == 100, "up from 80 -> 100");
	CK(backlight_next_level(100, +1) == 100, "clamp at 100");
	CK(backlight_next_level(20, -1) == 20, "clamp at 20");
	CK(backlight_next_level(100, -1) == 80, "down from 100 -> 80");

	/* Quiet hours. Times are minutes since local midnight. */
#define HM(h, m) ((h) * 60 + (m))
	CK(!backlight_quiet_now(false, HM(22, 0), HM(7, 0), true, HM(23, 0)),
	   "quiet: schedule off is never quiet");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), false, HM(23, 0)),
	   "quiet: time unknown is never quiet");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), false, 0),
	   "quiet: time unknown at 00:00 is never quiet");

	/* Same-day window, 13:00-15:00. */
	CK(backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(14, 0)),
	   "quiet: 14:00 inside 13:00-15:00");
	CK(backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(13, 0)),
	   "quiet: From inclusive (13:00 in 13:00-15:00)");
	CK(!backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(15, 0)),
	   "quiet: Until exclusive (15:00 not in 13:00-15:00)");
	CK(backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(14, 59)),
	   "quiet: 14:59 inside 13:00-15:00");
	CK(!backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(12, 59)),
	   "quiet: 12:59 before 13:00-15:00");
	CK(!backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(3, 0)),
	   "quiet: 03:00 outside 13:00-15:00 (no wrap)");

	/* Across midnight, 22:00-07:00. */
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(23, 0)),
	   "quiet: 23:00 inside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(3, 0)),
	   "quiet: 03:00 inside 22:00-07:00");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(7, 0)),
	   "quiet: 07:00 is Until, not quiet");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(6, 59)),
	   "quiet: 06:59 still inside 22:00-07:00");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(12, 0)),
	   "quiet: 12:00 outside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(22, 0)),
	   "quiet: 22:00 is From, quiet");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(21, 59)),
	   "quiet: 21:59 not yet quiet");

	/* Midnight edges. */
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(0, 0)),
	   "quiet: 00:00 inside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(23, 59)),
	   "quiet: 23:59 inside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(0, 0), HM(7, 0), true, HM(0, 0)),
	   "quiet: From 00:00 inclusive at 00:00");
	CK(!backlight_quiet_now(true, HM(0, 0), HM(7, 0), true, HM(23, 59)),
	   "quiet: 23:59 outside 00:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(0, 0), true, HM(23, 59)),
	   "quiet: 23:59 inside 22:00-00:00");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(0, 0), true, HM(0, 0)),
	   "quiet: Until 00:00 exclusive at 00:00");

	/* Equal times: an empty window, never a whole day. */
	CK(!backlight_quiet_now(true, HM(22, 0), HM(22, 0), true, HM(22, 0)),
	   "quiet: equal times never quiet (at that time)");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(22, 0), true, HM(3, 0)),
	   "quiet: equal times never quiet (elsewhere)");
	CK(!backlight_quiet_now(true, 0, 0, true, 0),
	   "quiet: equal 00:00 times never quiet");
#undef HM
	printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
	return failures ? 1 : 0;
}
