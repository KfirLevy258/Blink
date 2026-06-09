#include <zephyr/ztest.h>
#include <string.h>
#include "usage_parse.h"

static const char SAMPLE[] =
	"{\"five_hour\":{\"utilization\":61.0,\"resets_at\":\"2026-06-08T21:50:01Z\"},"
	"\"seven_day\":{\"utilization\":26.0,\"resets_at\":\"2026-06-10T06:00:01Z\"},"
	"\"seven_day_sonnet\":{\"utilization\":2.0,\"resets_at\":\"2026-06-10T06:00:01Z\"},"
	"\"seven_day_opus\":null,"
	"\"extra_usage\":{\"used_credits\":0.0,\"monthly_limit\":8000}}";

ZTEST(usage_parse, test_parses_mandatory_windows)
{
	struct usage_data d = {0};
	int ret = usage_parse(SAMPLE, strlen(SAMPLE), &d);

	zassert_equal(ret, 0, "parse should succeed");
	zassert_true(d.five_hour.present, "five_hour present");
	zassert_within(d.five_hour.utilization, 61.0, 0.01, "5h util");
	zassert_mem_equal(d.five_hour.resets_at, "2026-06-08T21:50:01Z", 20, "5h reset");
	zassert_true(d.seven_day.present, "seven_day present");
	zassert_within(d.seven_day.utilization, 26.0, 0.01, "7d util");
	zassert_mem_equal(d.seven_day.resets_at, "2026-06-10T06:00:01Z", 20, "7d reset");
	zassert_true(d.seven_day_sonnet.present, "sonnet present");
	zassert_within(d.seven_day_sonnet.utilization, 2.0, 0.01, "sonnet util");
}

ZTEST(usage_parse, test_rejects_garbage)
{
	struct usage_data d = {0};
	zassert_true(usage_parse("not json", 8, &d) < 0, "garbage should fail");
}

ZTEST(usage_parse, test_opus_null_not_present)
{
	struct usage_data d = {0};
	(void)usage_parse(SAMPLE, strlen(SAMPLE), &d);
	/* seven_day_opus is null in the sample; we never set a window for it,
	 * and seven_day must not be mis-parsed from the opus/sonnet siblings. */
	zassert_within(d.seven_day.utilization, 26.0, 0.01, "7d not confused with siblings");
}

ZTEST_SUITE(usage_parse, NULL, NULL, NULL, NULL, NULL);
