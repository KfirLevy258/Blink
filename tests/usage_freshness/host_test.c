/* What the board knows about the age of the figures on its own screen.
 *
 *   cc -Wall -Werror -I firmware/src tests/usage_freshness/host_test.c \
 *      firmware/src/usage_freshness.c -o /tmp/usage_freshness
 */
#include <stdio.h>
#include "usage_freshness.h"

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

int main(void)
{
	/* Nothing has arrived yet: not "brand new", unknown. */
	CHECK(usage_freshness_age_s(1000) == -1);
	CHECK(usage_freshness_activity() == USAGE_ACTIVITY_NONE);

	/* A daemon older than this firmware sends no age at all. That must
	 * stay unknown rather than becoming a very fresh zero, which would
	 * hold the panel awake forever against every such daemon. */
	usage_freshness_note(-1, -1, USAGE_ACTIVITY_NONE, 10000);
	CHECK(usage_freshness_age_s(70000) == -1);

	/* The ordinary case, and the one the sleep gate reads: the daemon
	 * says the reading is an hour old, and a minute later it is an hour
	 * and a minute old even though nothing new arrived. */
	usage_freshness_note(3600, 3600, USAGE_ACTIVITY_IDLE, 100000);
	CHECK(usage_freshness_age_s(100000) == 3600);
	CHECK(usage_freshness_age_s(160000) == 3660);
	CHECK(usage_freshness_activity() == USAGE_ACTIVITY_IDLE);

	/* A fresh reading resets the clock it grows from. */
	usage_freshness_note(1, 1, USAGE_ACTIVITY_RUNNING, 200000);
	CHECK(usage_freshness_age_s(260000) == 61);
	CHECK(usage_freshness_activity() == USAGE_ACTIVITY_RUNNING);

	/* k_uptime_get is monotonic, but a caller that passes a stale
	 * timestamp must not produce an age that runs backwards past the
	 * figure the daemon actually gave us. */
	CHECK(usage_freshness_age_s(199000) == 1);

	/* The far end of the range the wire actually permits. proto.c bounds
	 * age_s at SECS_MAX, which is INT32_MAX itself, so a daemon may send a
	 * figure with no room left above it -- and this age keeps growing
	 * between messages. Unclamped, the sum runs past INT32_MAX and the
	 * narrowing cast hands back a large negative, which every caller reads
	 * as "cannot say": the oldest reading the board has ever held would
	 * become an undatable one, and the sleep gate, which refuses to doze on
	 * an unknown age, would hold the panel lit on it forever. Saturating
	 * keeps it merely old. */
	usage_freshness_note(INT32_MAX - 5, INT32_MAX - 5, USAGE_ACTIVITY_IDLE,
			     300000);
	CHECK(usage_freshness_age_s(300000) == INT32_MAX - 5);
	CHECK(usage_freshness_age_s(360000) == INT32_MAX);
	CHECK(usage_freshness_age_s(360000) > 0);

	printf("%s\n", fails ? "FAIL" : "ok   usage_freshness");
	return fails ? 1 : 0;
}
