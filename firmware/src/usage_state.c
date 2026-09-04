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

/*
 * FINISHED, which the wire does not send. Clamped at zero rather than trusted:
 * the four numbers are read from separate JSON keys and a daemon that sent a
 * torn set -- n_sess from before a session appeared, n_run from after -- would
 * otherwise make this subtraction negative, and a negative "was" turns the
 * next ordinary poll into a rise that never happened.
 */
static int finished_in(const struct usage_counts *c)
{
	int fin = c->n_sess - c->n_run - c->n_wait - c->n_stuck;

	return fin > 0 ? fin : 0;
}

/*
 * The aggregate `state` string's pip, so a label can be matched to the state
 * it was chosen for. -1 for the states with no pip of their own: NONE is the
 * absence of a state and nothing is ever named after it.
 */
static int aggregate_pip(enum usage_activity a)
{
	switch (a) {
	case USAGE_ACTIVITY_FAILED:
	case USAGE_ACTIVITY_STUCK:
		return FMT_PIP_FAILED;
	case USAGE_ACTIVITY_WAITING:
		return FMT_PIP_WAITING;
	case USAGE_ACTIVITY_IDLE:
		return FMT_PIP_FINISHED;
	case USAGE_ACTIVITY_RUNNING:
		return FMT_PIP_RUNNING;
	default:
		return -1;
	}
}

bool usage_toast_change(const struct usage_counts *prev,
			const struct usage_counts *now,
			enum usage_activity aggregate,
			struct usage_toast *out)
{
	/* Most severe first, which is fmt_pips()' order and for the same
	 * reason: whichever of these fired is the one a person should deal
	 * with first. RUNNING is absent on purpose -- see the header. */
	static const enum fmt_pip_kind kinds[3] = {
		FMT_PIP_FAILED, FMT_PIP_WAITING, FMT_PIP_FINISHED
	};

	if (out == NULL || now == NULL) {
		return false;
	}
	/* Boot, and every reconnect. Everything is a change from nothing. */
	if (prev == NULL) {
		return false;
	}

	const int was[3] = { prev->n_stuck, prev->n_wait, finished_in(prev) };
	const int is[3] = { now->n_stuck, now->n_wait, finished_in(now) };

	for (int i = 0; i < 3; i++) {
		/*
		 * A RISE, not a difference. A state LOSING a session is the
		 * good direction -- somebody dealt with it, or the session
		 * closed -- and announcing that would interrupt a person to
		 * tell them about work they just did themselves.
		 */
		if (is[i] <= was[i]) {
			continue;
		}
		out->kind = kinds[i];
		out->count = is[i];
		out->nameable = (is[i] == 1 &&
				 aggregate_pip(aggregate) == (int)kinds[i]);
		return true;
	}
	return false;
}
