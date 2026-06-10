/* Standalone host test for msg_parse (macOS-friendly; native_sim is Linux-only).
 * Build & run:
 *   cc -I ../../firmware/src host_test.c ../../firmware/src/msg_parse.c -o /tmp/mptest && /tmp/mptest
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "msg_parse.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

int main(void)
{
	const char *u = "{\"t\":\"usage\",\"v\":1,\"session_pct\":61.0,"
		"\"session_resets_at\":\"2026-06-08T21:50:01Z\",\"weekly_pct\":26.0}";
	char buf[40];
	double d;

	CHECK(msg_get_str(u, "t", buf, sizeof(buf)) && strcmp(buf, "usage") == 0, "type=usage");
	CHECK(msg_get_str(u, "session_resets_at", buf, sizeof(buf)) &&
	      strcmp(buf, "2026-06-08T21:50:01Z") == 0, "session_resets_at");
	CHECK(msg_get_double(u, "session_pct", &d) && fabs(d - 61.0) < 0.01, "session_pct=61");
	CHECK(msg_get_double(u, "weekly_pct", &d) && fabs(d - 26.0) < 0.01, "weekly_pct=26");
	CHECK(!msg_get_double(u, "missing", &d), "missing key -> false");
	CHECK(!msg_get_str("not json", "t", buf, sizeof(buf)), "garbage -> false");

	printf("\n%s (%d failures)\n", failures ? "TESTS FAILED" : "ALL TESTS PASSED", failures);
	return failures ? 1 : 0;
}
