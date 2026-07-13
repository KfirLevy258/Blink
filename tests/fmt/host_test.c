/* Host test for the countdown formatter. Compile natively (no Zephyr):
 *   cc -Wall -I firmware/src tests/fmt/host_test.c firmware/src/fmt.c -o /tmp/fmt && /tmp/fmt
 * native_sim is Linux-only and this is a Mac, so the pure logic is tested here.
 */
#include <stdio.h>
#include <string.h>
#include "fmt.h"

static int failures;

static void check(const char *what, int32_t secs, const char *want)
{
	char got[FMT_COUNTDOWN_MAX];

	fmt_countdown(secs, got, sizeof(got));
	if (strcmp(got, want) == 0) {
		printf("PASS: %-28s %6d -> \"%s\"\n", what, secs, got);
	} else {
		printf("FAIL: %-28s %6d -> \"%s\" (want \"%s\")\n", what, secs, got, want);
		failures++;
	}
}

int main(void)
{
	check("unknown", -1, "--");
	check("zero / resetting now", 0, "now");
	check("seconds only", 45, "0m 45s");
	check("minutes and seconds", 8 * 60 + 5, "8m 05s");
	check("just under an hour", 59 * 60 + 59, "59m 59s");
	check("exactly one hour", 3600, "1h 00m");
	check("hours and minutes", 2 * 3600 + 14 * 60, "2h 14m");
	check("just under a day", 23 * 3600 + 59 * 60, "23h 59m");
	check("exactly one day", 86400, "1d 0h");
	check("days and hours", 4 * 86400 + 3 * 3600, "4d 3h");
	check("a full week", 7 * 86400, "7d 0h");
	/* Overly large input must not overflow the buffer or wrap. */
	check("absurdly large", 999 * 86400, "999d 0h");

	printf("\n%s (%d failure%s)\n", failures ? "FAILURES" : "ALL TESTS PASSED",
	       failures, failures == 1 ? "" : "s");
	return failures != 0;
}
