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

bool usage_activity_needs_row(enum usage_activity a)
{
	switch (a) {
	case USAGE_ACTIVITY_FAILED:
	case USAGE_ACTIVITY_STUCK:
	case USAGE_ACTIVITY_WAITING:
		return true;
	default:
		/*
		 * RUNNING, IDLE and NONE all leave the clock alone. Written as
		 * a default rather than three more cases because a state from
		 * a NEWER daemon lands here too, and the safe answer for a
		 * state this firmware cannot name is to say nothing rather
		 * than to take the row and print an empty line where the time
		 * used to be.
		 */
		return false;
	}
}
