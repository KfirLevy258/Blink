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

static void check_age(const char *what, int32_t secs, const char *want)
{
	char got[FMT_COUNTDOWN_MAX];

	fmt_age(secs, got, sizeof(got));
	if (strcmp(got, want) == 0) {
		printf("PASS: %-28s %6d -> \"%s\"\n", what, secs, got);
	} else {
		printf("FAIL: %-28s %6d -> \"%s\" (want \"%s\")\n", what, secs, got, want);
		failures++;
	}
}

static void burn(double pph, const char *want)
{
	char got[FMT_COUNTDOWN_MAX];

	fmt_burn(pph, got, sizeof(got));
	if (strcmp(got, want) == 0) {
		printf("PASS: %-28s %6.2f -> \"%s\"\n", "burn", pph, got);
	} else {
		printf("FAIL: %-28s %6.2f -> \"%s\" (want \"%s\")\n",
		       "burn", pph, got, want);
		failures++;
	}
}

static void check_true(const char *what, int ok)
{
	if (ok) {
		printf("PASS: %s\n", what);
	} else {
		printf("FAIL: %s\n", what);
		failures++;
	}
}

/* EXPECT_STR/EXPECT_EQ: same PASS/FAIL-plus-failures-counter shape as
 * check()/check_true() above, just spelled as a one-line macro so
 * test_fmt_hint's table of cases reads as a table, not a wall of calls. */
#define EXPECT_STR(got, want) do { \
	const char *g_ = (got), *w_ = (want); \
	if (strcmp(g_, w_) == 0) { \
		printf("PASS: %-28s -> \"%s\"\n", #got, g_); \
	} else { \
		printf("FAIL: %-28s -> \"%s\" (want \"%s\")\n", #got, g_, w_); \
		failures++; \
	} \
} while (0)

#define EXPECT_EQ(got, want) do { \
	long g_ = (long)(got), w_ = (long)(want); \
	if (g_ == w_) { \
		printf("PASS: %-28s -> %ld\n", #got, g_); \
	} else { \
		printf("FAIL: %-28s -> %ld (want %ld)\n", #got, g_, w_); \
		failures++; \
	} \
} while (0)

static void test_fmt_hint(void)
{
	char b[FMT_HINT_MAX];

	/* Nothing to say stays empty -- the caller draws no line. */
	fmt_hint("", NULL, 0, b, sizeof(b));
	EXPECT_STR(b, "");

	/* A status alone, when there is no name and one session. */
	fmt_hint("Working", NULL, 1, b, sizeof(b));
	EXPECT_STR(b, "Working");

	/* A name when exactly one session is named. */
	fmt_hint("Waiting for you", "LiveClaudeUi", 1, b, sizeof(b));
	EXPECT_STR(b, "Waiting for you - LiveClaudeUi");

	/* A count when several share the state and no name was sent. */
	fmt_hint("Waiting for you", NULL, 3, b, sizeof(b));
	EXPECT_STR(b, "Waiting for you - 3 sessions");

	/* One session is never "1 sessions"; it is just the status. */
	fmt_hint("Finished", NULL, 1, b, sizeof(b));
	EXPECT_STR(b, "Finished");

	/* A label wins over a count if both arrive. */
	fmt_hint("Working", "Blink", 2, b, sizeof(b));
	EXPECT_STR(b, "Working - Blink");

	/* Non-ASCII is transliterated, never drawn as boxes. */
	fmt_hint("Working", "caf\xc3\xa9", 1, b, sizeof(b));
	EXPECT_STR(b, "Working - caf?");

	/* An empty label is the same as no label. */
	fmt_hint("Working", "", 1, b, sizeof(b));
	EXPECT_STR(b, "Working");

	/* Truncation never overruns and always NUL-terminates. */
	char small[12];
	fmt_hint("Waiting for you", "LiveClaudeUi", 1, small, sizeof(small));
	EXPECT_EQ((int)strlen(small), 11);
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

	check_age("never updated", -1, "never");
	check_age("just now", 0, "0s ago");
	check_age("seconds", 12, "12s ago");
	check_age("just under a minute", 59, "59s ago");
	check_age("one minute", 60, "1m ago");
	check_age("minutes", 5 * 60 + 30, "5m ago");
	check_age("just under an hour", 3599, "59m ago");
	check_age("hours", 3600 + 20 * 60, "1h 20m ago");

	/* --- burn rate ------------------------------------------------ */
	/*
	 * Empty, not "--", when there is nothing to say. The caller draws its
	 * own blank; a second spelling of nothing on one screen is a bug.
	 */
	burn(0.0, "");
	burn(-1.0, "");
	/* Below ten a tenth is the difference between "barely moving" and
	 * "moving", so it is kept. */
	burn(2.4, "+2.4%/h");
	burn(0.5, "+0.5%/h");
	burn(9.94, "+9.9%/h");
	/* At and above ten it is noise on a five-minute sample, and the width
	 * is worth more -- GAUGE_CD_MAX_W is sized for "00m 00s". */
	burn(10.0, "+10%/h");
	burn(14.2, "+14%/h");
	burn(14.6, "+15%/h");		/* rounds, does not truncate */
	burn(99.4, "+99%/h");
	/* A rate this high says "about to be full" whatever the digits, and
	 * four of them would run past the countdown's budget. */
	burn(1000.0, "+999%/h");
	burn(50000.0, "+999%/h");

	/*
	 * The rule the panel depends on: a burn rate can never be mistaken for
	 * a countdown, because a countdown never contains a percent sign. If
	 * this ever fails, the two readings have become confusable and the
	 * unlabelled design is no longer safe.
	 */
	{
		const int32_t secs[] = { -1, 0, 59, 60, 3599, 3600, 86399,
					 86400, 999 * 86400 };
		char b[FMT_COUNTDOWN_MAX];
		int clash = 0;

		for (unsigned i = 0; i < sizeof(secs) / sizeof(secs[0]); i++) {
			fmt_countdown(secs[i], b, sizeof(b));
			if (strchr(b, '%') != NULL) {
				clash = 1;
			}
		}
		check_true("no countdown contains a percent sign", clash == 0);
	}

	test_fmt_hint();

	printf("\n%s (%d failure%s)\n", failures ? "FAILURES" : "ALL TESTS PASSED",
	       failures, failures == 1 ? "" : "s");
	return failures != 0;
}
