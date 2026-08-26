#include <string.h>

#include "usage_state.h"

enum usage_activity usage_activity_from_state(const char *state)
{
	if (state == NULL) {
		return USAGE_ACTIVITY_NONE;
	}
	if (strcmp(state, "running") == 0) {
		return USAGE_ACTIVITY_RUNNING;
	}
	if (strcmp(state, "idle") == 0) {
		return USAGE_ACTIVITY_IDLE;
	}
	if (strcmp(state, "waiting") == 0) {
		return USAGE_ACTIVITY_WAITING;
	}
	if (strcmp(state, "stuck") == 0) {
		return USAGE_ACTIVITY_STUCK;
	}
	if (strcmp(state, "failed") == 0) {
		return USAGE_ACTIVITY_FAILED;
	}
	/*
	 * Anything else goes dark rather than landing on whichever branch
	 * happened to be last. A newer daemon naming a state this firmware has
	 * never heard of is the expected case here -- the wire contract is
	 * additive by design -- and lighting the pip amber because the string
	 * failed four comparisons would be inventing a claim from a parse
	 * failure.
	 */
	return USAGE_ACTIVITY_NONE;
}
