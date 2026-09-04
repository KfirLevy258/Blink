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

/*
 * Both predicates refuse -1, and neither says so in code any more.
 *
 * They used to open with `age_s >= 0 &&`, which read as the guard that
 * enforces the sentinel rule the header argues for -- and was a no-op:
 * deleting both conjuncts left every host test green, because a threshold
 * this side of zero already refuses every negative. A guard that cannot fire
 * is not defence, it is a claim of care that no test can check, and it hides
 * where the rule is actually enforced.
 *
 * The rule is enforced by the thresholds being positive, and THAT is pinned:
 * tests/sleep_gate asserts both constants by value, not merely by symbol, so
 * a threshold moved to zero or below fails the suite rather than quietly
 * turning "we cannot say" into "nobody is here" -- which would doze the panel
 * against every daemon too old to send an age. The -1 cases in that suite are
 * not redundant with the boundary cases either: they are the only thing that
 * catches the mistake worth catching here, someone deciding an unknown age
 * means an absent person and writing `age_s < 0 ||` in front of the compare.
 */
bool sleep_nobody_is_here(int32_t age_s)
{
	return age_s >= SLEEP_ABSENT_AFTER_S;
}

bool sleep_reading_is_stale(int32_t age_s)
{
	return age_s >= SLEEP_READING_STALE_AFTER_S;
}

bool sleep_stale_should_start(int32_t age_s, bool had_usage, bool ota_busy,
			      enum usage_activity act)
{
	return had_usage && !ota_busy && nothing_wants_a_person(act) &&
	       sleep_nobody_is_here(age_s);
}

bool sleep_stale_should_wake(int32_t age_s, bool ota_busy,
			     enum usage_activity act)
{
	return ota_busy || !nothing_wants_a_person(act) ||
	       !sleep_nobody_is_here(age_s);
}
