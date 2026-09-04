#include "usage_freshness.h"

static int32_t noted_age_s = -1;
static int32_t noted_active_age_s = -1;
static int64_t noted_at_ms;
static enum usage_activity noted_act = USAGE_ACTIVITY_NONE;

void usage_freshness_note(int32_t age_s, int32_t active_age_s,
			  enum usage_activity act, int64_t now_ms)
{
	noted_age_s = age_s;
	/* An absent `active_age_s` means a daemon older than the field, and
	 * the reading's own age is then the right answer rather than a
	 * near-enough one: the two only diverge because that daemon remembers
	 * a five-hour reading and re-offers it at its original time, and one
	 * without the memory has nothing to diverge from. Done here rather
	 * than at each caller so there is one rule for it. */
	noted_active_age_s = active_age_s >= 0 ? active_age_s : age_s;
	noted_at_ms = now_ms;
	noted_act = act;
}

/* Both ages grow the same way, so they share the arithmetic: unknown stays
 * unknown, a clock that ran backwards returns the daemon's own figure, and
 * the sum saturates rather than wrapping. */
static int32_t grown(int32_t noted, int64_t now_ms)
{
	int64_t out;

	if (noted < 0) {
		return -1;
	}
	if (now_ms < noted_at_ms) {
		/* Cannot happen with k_uptime_get, which is monotonic. If it
		 * ever does, the daemon's own figure is still true of the
		 * moment it arrived, and an age that ran backwards would read
		 * as a reading getting fresher on its own. */
		return noted;
	}
	out = (int64_t)noted + (now_ms - noted_at_ms) / 1000;
	if (out > INT32_MAX) {
		out = INT32_MAX;
	}
	return (int32_t)out;
}

int32_t usage_freshness_age_s(int64_t now_ms)
{
	return grown(noted_age_s, now_ms);
}

int32_t usage_freshness_active_age_s(int64_t now_ms)
{
	return grown(noted_active_age_s, now_ms);
}

enum usage_activity usage_freshness_activity(void)
{
	return noted_act;
}
