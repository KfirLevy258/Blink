/* What the board knows about the age of the figures on its own screen, and
 * about how long since the machine behind them said anything at all.
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

	/*
	 * The second age, and the desk it is about.
	 *
	 * The daemon re-offers the last status line that carried a five-hour
	 * percentage, at its ORIGINAL time -- so the dial is honestly twelve
	 * hours old at the same instant the file under it was rewritten five
	 * seconds ago. Both facts arrive in one message and the board has to
	 * keep them apart: main.c dozes on the second, ui_sleep stamps the
	 * dot from the first.
	 */
	usage_freshness_note(43200, 5, USAGE_ACTIVITY_NONE, 400000);
	CHECK(usage_freshness_age_s(400000) == 43200);
	CHECK(usage_freshness_active_age_s(400000) == 5);

	/* Both grow with uptime, and they keep their distance while they do:
	 * a minute in the gap between messages ages the reading and the desk
	 * by the same minute. */
	CHECK(usage_freshness_age_s(460000) == 43260);
	CHECK(usage_freshness_active_age_s(460000) == 65);

	/* A daemon older than the field sends no active age. The reading's
	 * own age is then not an approximation of it, it IS it: the two can
	 * only differ because of a memory that daemon does not have. Getting
	 * -1 back here instead would be read as "cannot say" by the sleep
	 * gate, which refuses to doze on that -- and the board would sit lit
	 * all night against every daemon of that vintage, which is the bug
	 * the gate was added to fix. */
	usage_freshness_note(43200, -1, USAGE_ACTIVITY_IDLE, 500000);
	CHECK(usage_freshness_active_age_s(500000) == 43200);
	CHECK(usage_freshness_active_age_s(560000) == 43260);

	/* And it is a fallback, not a floor: the next message that does carry
	 * an active age replaces it rather than keeping the larger one. */
	usage_freshness_note(43200, 5, USAGE_ACTIVITY_IDLE, 600000);
	CHECK(usage_freshness_active_age_s(600000) == 5);

	/* Nothing known at all stays nothing known, on both. */
	usage_freshness_note(-1, -1, USAGE_ACTIVITY_NONE, 700000);
	CHECK(usage_freshness_age_s(760000) == -1);
	CHECK(usage_freshness_active_age_s(760000) == -1);

	/* An active age without a reading age. The daemon cannot produce
	 * this today -- both come from the same absent timestamp -- but the
	 * parser has two independent keys and a board must not invent an age
	 * for a reading it was not given. */
	usage_freshness_note(-1, 5, USAGE_ACTIVITY_NONE, 800000);
	CHECK(usage_freshness_age_s(800000) == -1);
	CHECK(usage_freshness_active_age_s(800000) == 5);

	/* The active age saturates like the reading age, and for the same
	 * reason: wrapping past INT32_MAX hands back a negative, the sleep
	 * gate reads a negative as "cannot say" and refuses to doze, and the
	 * quietest desk the board has ever seen would be the one that keeps
	 * it lit. */
	usage_freshness_note(INT32_MAX - 5, INT32_MAX - 5,
			     USAGE_ACTIVITY_IDLE, 900000);
	CHECK(usage_freshness_active_age_s(960000) == INT32_MAX);
	/* Not `> 0` beside the line above -- that cannot fail while the
	 * equality holds, and an assertion that cannot fail reads as coverage
	 * without being any. What is worth pinning is the direction the clamp
	 * saves us from: the addition must not wrap into a negative, because a
	 * negative age is indistinguishable from the -1 that means "no reading
	 * yet", and the quietest desk the board has ever seen would then be
	 * the one thing that keeps it awake. Ask it of the unclamped sum. */
	usage_freshness_note(INT32_MAX - 5, INT32_MAX - 5,
			     USAGE_ACTIVITY_IDLE, 900000);
	CHECK(usage_freshness_active_age_s(1200000) >= 0);
	CHECK(usage_freshness_age_s(1200000) >= 0);

	printf("%s\n", fails ? "FAIL" : "ok   usage_freshness");
	return fails ? 1 : 0;
}
