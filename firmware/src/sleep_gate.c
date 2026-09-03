#include "sleep_gate.h"

bool sleep_should_start(bool host_lost, bool had_usage, bool ota_busy)
{
	return host_lost && had_usage && !ota_busy;
}

/* Nothing on screen is asking for a person. RUNNING is excluded as well as
 * the three alarming ones: work in flight is work somebody may want to watch
 * land, and it will be over long before four hours are out. IDLE is allowed
 * because a finished turn is an amber pip, not a summons -- and it is the
 * state a desk sits in overnight. */
static bool nothing_wants_a_person(enum usage_activity act)
{
	return act == USAGE_ACTIVITY_NONE || act == USAGE_ACTIVITY_IDLE;
}

bool sleep_reading_is_old(int32_t age_s)
{
	return age_s >= 0 && age_s >= SLEEP_STALE_AFTER_S;
}

bool sleep_stale_should_start(int32_t age_s, bool had_usage, bool ota_busy,
			      enum usage_activity act)
{
	return had_usage && !ota_busy && nothing_wants_a_person(act) &&
	       sleep_reading_is_old(age_s);
}

bool sleep_stale_should_wake(int32_t age_s, bool ota_busy,
			     enum usage_activity act)
{
	return ota_busy || !nothing_wants_a_person(act) ||
	       !sleep_reading_is_old(age_s);
}
