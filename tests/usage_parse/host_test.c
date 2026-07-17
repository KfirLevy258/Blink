/*
 * Standalone host test for usage_parse (macOS-friendly; native_sim is Linux-only).
 * Build & run:
 *   cc -I ../../firmware/src host_test.c ../../firmware/src/usage_parse.c -o /tmp/uptest_host && /tmp/uptest_host
 * The ztest version (src/main.c) covers the same cases on Linux/native_sim.
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "usage_parse.h"

static const char SAMPLE[] =
	"{\"five_hour\":{\"utilization\":61.0,\"resets_at\":\"2026-06-08T21:50:01Z\"},"
	"\"seven_day\":{\"utilization\":26.0,\"resets_at\":\"2026-06-10T06:00:01Z\"},"
	"\"seven_day_fable\":{\"utilization\":12.5,\"resets_at\":\"2026-06-10T06:00:01Z\"},"
	"\"seven_day_sonnet\":{\"utilization\":2.0,\"resets_at\":\"2026-06-10T06:00:01Z\"},"
	"\"seven_day_opus\":null,"
	"\"extra_usage\":{\"used_credits\":0.0,\"monthly_limit\":8000}}";

#define CHECK(cond, msg) do { \
	if (!(cond)) { printf("FAIL: %s\n", msg); failures++; } \
	else { printf("PASS: %s\n", msg); } \
} while (0)

int main(void)
{
	int failures = 0;
	struct usage_data d;

	memset(&d, 0, sizeof(d));
	int ret = usage_parse(SAMPLE, strlen(SAMPLE), &d);
	CHECK(ret == 0, "parse mandatory windows returns 0");
	CHECK(d.five_hour.present, "five_hour present");
	CHECK(fabs(d.five_hour.utilization - 61.0) < 0.01, "5h utilization == 61.0");
	CHECK(strcmp(d.five_hour.resets_at, "2026-06-08T21:50:01Z") == 0, "5h resets_at");
	CHECK(d.seven_day.present, "seven_day present");
	CHECK(fabs(d.seven_day.utilization - 26.0) < 0.01, "7d utilization == 26.0");
	CHECK(strcmp(d.seven_day.resets_at, "2026-06-10T06:00:01Z") == 0, "7d resets_at");
	CHECK(d.seven_day_fable.present, "fable present");
	CHECK(fabs(d.seven_day_fable.utilization - 12.5) < 0.01, "fable util == 12.5");
	CHECK(d.seven_day_sonnet.present, "sonnet present");
	CHECK(fabs(d.seven_day_sonnet.utilization - 2.0) < 0.01, "sonnet util == 2.0");
	CHECK(!d.seven_day_opus.present, "null opus window reads absent");
	CHECK(fabs(d.seven_day.utilization - 26.0) < 0.01,
	      "7d not confused with seven_day_fable/sonnet/opus siblings");

	memset(&d, 0, sizeof(d));
	CHECK(usage_parse("not json", 8, &d) < 0, "garbage rejected");

	/* whitespace + reordered fields tolerance */
	memset(&d, 0, sizeof(d));
	const char *ws =
		"{ \"seven_day\" : { \"resets_at\" : \"2026-01-02T03:04:05Z\" , \"utilization\" : 7.5 },"
		" \"five_hour\" : { \"utilization\" : 0.0 } }";
	ret = usage_parse(ws, strlen(ws), &d);
	CHECK(ret == 0, "whitespace/reordered parse returns 0");
	CHECK(fabs(d.seven_day.utilization - 7.5) < 0.01, "7d fractional 7.5 parsed");
	CHECK(strcmp(d.seven_day.resets_at, "2026-01-02T03:04:05Z") == 0, "7d resets_at (reordered)");
	CHECK(d.five_hour.present && fabs(d.five_hour.utilization) < 0.01, "5h 0.0 parsed");

	/* Current account shape: the flat model windows are null and fable
	 * rides in limits[] as a weekly_scoped entry (live shape 2026-07-17). */
	memset(&d, 0, sizeof(d));
	const char *lim =
		"{\"five_hour\":{\"utilization\":12.0,\"resets_at\":\"2026-07-18T00:40:00Z\"},"
		"\"seven_day\":{\"utilization\":23.0,\"resets_at\":\"2026-07-22T06:00:00Z\"},"
		"\"seven_day_fable\":null,\"seven_day_sonnet\":null,"
		"\"limits\":[{\"kind\":\"session\",\"percent\":12,\"resets_at\":\"A\"},"
		"{\"kind\":\"weekly_all\",\"percent\":23,\"resets_at\":\"B\"},"
		"{\"kind\":\"weekly_scoped\",\"percent\":43,"
		"\"resets_at\":\"2026-07-22T06:00:00Z\","
		"\"scope\":{\"model\":{\"display_name\":\"Fable\"}},\"is_active\":true}]}";
	ret = usage_parse(lim, strlen(lim), &d);
	CHECK(ret == 0, "scoped-limits sample parses");
	CHECK(d.seven_day_fable.present, "fable filled from weekly_scoped limit");
	CHECK(fabs(d.seven_day_fable.utilization - 43.0) < 0.01, "fable == 43");
	CHECK(strcmp(d.seven_day_fable.resets_at, "2026-07-22T06:00:00Z") == 0,
	      "fable resets_at from its own entry");
	CHECK(!d.seven_day_sonnet.present, "null sonnet stays absent");
	CHECK(fabs(d.seven_day.utilization - 23.0) < 0.01, "7d unaffected by limits scan");

	printf("\n%s (%d failure(s))\n", failures ? "TESTS FAILED" : "ALL TESTS PASSED", failures);
	return failures ? 1 : 0;
}
