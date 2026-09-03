#include "usage_freshness.h"

static int32_t noted_age_s = -1;
static int64_t noted_at_ms;
static enum usage_activity noted_act = USAGE_ACTIVITY_NONE;

void usage_freshness_note(int32_t age_s, enum usage_activity act,
			  int64_t now_ms)
{
	noted_age_s = age_s;
	noted_at_ms = now_ms;
	noted_act = act;
}

int32_t usage_freshness_age_s(int64_t now_ms)
{
	int64_t grown;

	if (noted_age_s < 0) {
		return -1;
	}
	if (now_ms < noted_at_ms) {
		/* Cannot happen with k_uptime_get, which is monotonic. If it
		 * ever does, the daemon's own figure is still true of the
		 * moment it arrived, and an age that ran backwards would read
		 * as a reading getting fresher on its own. */
		return noted_age_s;
	}
	grown = (int64_t)noted_age_s + (now_ms - noted_at_ms) / 1000;
	if (grown > INT32_MAX) {
		grown = INT32_MAX;
	}
	return (int32_t)grown;
}

enum usage_activity usage_freshness_activity(void)
{
	return noted_act;
}
