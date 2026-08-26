/* Standalone host test for the state-string -> activity mapping.
 *
 * Build & run:
 *   cc -I ../../firmware/src host_test.c ../../firmware/src/usage_state.c \
 *      -o /tmp/statetest && /tmp/statetest
 *
 * The branch worth having a test for is the last one. Every named state is
 * obvious and would be noticed on a desk within a minute; an UNRECOGNISED
 * state is the one that only shows up when a newer daemon meets older
 * firmware, which is the pairing nobody has on their desk and the one the
 * additive wire contract makes inevitable.
 */
#include <stdio.h>
#include "usage_state.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

int main(void)
{
	CHECK(usage_activity_from_state("running") == USAGE_ACTIVITY_RUNNING,
	      "running -> RUNNING");
	CHECK(usage_activity_from_state("idle") == USAGE_ACTIVITY_IDLE,
	      "idle -> IDLE");
	CHECK(usage_activity_from_state("waiting") == USAGE_ACTIVITY_WAITING,
	      "waiting -> WAITING");
	CHECK(usage_activity_from_state("stuck") == USAGE_ACTIVITY_STUCK,
	      "stuck -> STUCK");

	/* The cases that cannot be seen on a desk. */
	CHECK(usage_activity_from_state("compacting") == USAGE_ACTIVITY_NONE,
	      "a state from a NEWER daemon goes dark, not amber");
	CHECK(usage_activity_from_state("") == USAGE_ACTIVITY_NONE,
	      "empty string goes dark");
	CHECK(usage_activity_from_state(NULL) == USAGE_ACTIVITY_NONE,
	      "NULL goes dark rather than dereferencing");

	/* Case and whitespace are NOT normalised, deliberately: the daemon
	 * emits these four strings from one place (pc/providers/base.py) and
	 * accepting "Running" would be inventing tolerance for a bug rather
	 * than fixing it at the source. Pinned so the choice is explicit. */
	CHECK(usage_activity_from_state("Running") == USAGE_ACTIVITY_NONE,
	      "case is not normalised (daemon emits one canonical spelling)");
	CHECK(usage_activity_from_state(" running") == USAGE_ACTIVITY_NONE,
	      "leading space is not trimmed");

	/* NONE must stay the zero value: proto.c initialises `act` to it
	 * before the msg_get_str, and relies on that when the key is absent. */
	CHECK(USAGE_ACTIVITY_NONE == 0, "NONE is the zero value");

	printf(failures ? "\n%d FAILED\n" : "\nall state checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
