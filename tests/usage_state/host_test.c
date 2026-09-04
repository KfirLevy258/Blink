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

/*
 * The status-change popup's trigger.
 *
 * Everything about this feature that can be got wrong quietly lives here: it
 * fires on someone's desk, once, and then the moment is gone. Nobody can sit
 * and watch for a popup that should not have appeared at boot, which is
 * exactly why the decision is in a pure function instead of in usage_view.c
 * with the card it raises.
 */
static struct usage_counts C(int sess, int run, int wait, int stuck)
{
	struct usage_counts c = { sess, run, wait, stuck };

	return c;
}

static void test_toast_change(void)
{
	struct usage_toast t;
	struct usage_counts a, b;

	/*
	 * NOTHING AT BOOT, and nothing on the first message after a
	 * reconnect. Every count is a change from nothing at both, and a
	 * panel that announced "2 sessions are waiting" every time the daemon
	 * restarted would teach its owner to stop reading it inside a week.
	 */
	b = C(3, 1, 1, 1);
	CHECK(!usage_toast_change(NULL, &b, USAGE_ACTIVITY_WAITING, &t),
	      "no popup without a previous state (boot, reconnect)");

	/* A poll that changed nothing. The daemon sends these every 60 s
	 * whether or not anything moved, so this is the common case by far. */
	a = C(3, 1, 1, 1);
	CHECK(!usage_toast_change(&a, &b, USAGE_ACTIVITY_WAITING, &t),
	      "an unchanged poll says nothing");

	/* A prompt opened: the thing the owner asked for. */
	a = C(1, 1, 0, 0);
	b = C(1, 0, 1, 0);
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_WAITING, &t) &&
	      t.kind == FMT_PIP_WAITING && t.count == 1 && t.nameable,
	      "a session starting to wait fires, and may be named");

	/* A turn ended. FINISHED is n_sess minus the other three -- the wire
	 * has no room for a fifth count and never will. */
	a = C(2, 2, 0, 0);
	b = C(2, 1, 0, 0);
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_IDLE, &t) &&
	      t.kind == FMT_PIP_FINISHED && t.count == 1,
	      "a finished turn fires, derived from n_sess");

	/* A turn died. `n_stuck` is the wire's name and carries FAILED. */
	a = C(1, 1, 0, 0);
	b = C(1, 0, 0, 1);
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_FAILED, &t) &&
	      t.kind == FMT_PIP_FAILED,
	      "a failed turn fires");

	/*
	 * SEVERITY, not arrival order. Two states can rise on one poll -- a
	 * minute is long enough for both -- and a queue of cards on a 320x240
	 * panel is worse than one true sentence.
	 */
	a = C(4, 4, 0, 0);
	b = C(4, 2, 1, 0);	/* one now waiting, one now finished */
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_WAITING, &t) &&
	      t.kind == FMT_PIP_WAITING,
	      "waiting outranks finished when both rise at once");
	b = C(4, 2, 1, 1);	/* and a failure on top of both */
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_FAILED, &t) &&
	      t.kind == FMT_PIP_FAILED,
	      "failed outranks waiting when both rise at once");

	/*
	 * A state LOSING sessions is the good direction: somebody answered
	 * the prompt, or read the result. Announcing it would interrupt a
	 * person to tell them about what they just did.
	 */
	a = C(3, 0, 3, 0);
	b = C(3, 3, 0, 0);
	CHECK(!usage_toast_change(&a, &b, USAGE_ACTIVITY_RUNNING, &t),
	      "sessions leaving waiting is not news");

	/*
	 * RUNNING is never announced, however it rises. A session starting
	 * work is something the person at the desk just did on purpose.
	 */
	a = C(1, 0, 0, 0);
	b = C(3, 3, 0, 0);
	CHECK(!usage_toast_change(&a, &b, USAGE_ACTIVITY_RUNNING, &t),
	      "sessions starting to run is never announced");

	/*
	 * THE COUNT, when several share the state -- the owner's call: "if
	 * there is multi sessions waiting, you can just say x session are
	 * waiting". A name picked from three says something true about one
	 * and implies it about the other two.
	 */
	a = C(4, 4, 0, 0);
	b = C(4, 1, 3, 0);
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_WAITING, &t) &&
	      t.count == 3 && !t.nameable,
	      "three waiting carries the count and refuses the label");

	/*
	 * The label belongs to the AGGREGATE state, and only that one.
	 * claude_state.py picks it from the WORST state held by exactly one
	 * session, so on a desk with one session waiting and one that just
	 * finished, the label names the WAITING one -- and captioning the
	 * finished card with it would be the panel inventing a fact.
	 */
	a = C(2, 2, 0, 0);
	b = C(2, 0, 1, 0);	/* one waiting, one finished, both new */
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_WAITING, &t) &&
	      t.kind == FMT_PIP_WAITING && t.nameable,
	      "the label is offered for the state the daemon chose it for");
	a = C(2, 1, 1, 0);
	b = C(2, 0, 1, 0);	/* only the finished one is new */
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_WAITING, &t) &&
	      t.kind == FMT_PIP_FINISHED && !t.nameable,
	      "the label is refused for a state it was not chosen for");

	/*
	 * An aggregate state this firmware cannot name -- a newer daemon's --
	 * refuses the label rather than matching some state by accident.
	 */
	a = C(1, 1, 0, 0);
	b = C(1, 0, 1, 0);
	CHECK(usage_toast_change(&a, &b, USAGE_ACTIVITY_NONE, &t) &&
	      !t.nameable,
	      "an unnameable aggregate state refuses the label");

	/*
	 * A TORN READ. The four numbers come from four separate JSON keys,
	 * and n_sess lagging its parts makes the derived finished count
	 * negative. Clamping at zero matters for the PREVIOUS frame most: a
	 * negative "was" turns the next perfectly ordinary poll into a rise
	 * that never happened.
	 */
	a = C(1, 2, 1, 1);	/* finished = -3 if it were trusted */
	b = C(1, 1, 0, 0);	/* finished = 0 */
	CHECK(!usage_toast_change(&a, &b, USAGE_ACTIVITY_RUNNING, &t),
	      "a torn count cannot manufacture a finished session");

	/* A NULL `out` is refused rather than written through. */
	a = C(1, 1, 0, 0);
	b = C(1, 0, 1, 0);
	CHECK(!usage_toast_change(&a, &b, USAGE_ACTIVITY_WAITING, NULL),
	      "a NULL out is refused, not dereferenced");
}

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
	CHECK(usage_activity_from_state("failed") == USAGE_ACTIVITY_FAILED,
	      "failed -> FAILED");

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

	/* FAILED and STUCK are distinct enum values even though both render
	 * red today. They are different facts -- a wedged tool versus a turn
	 * that died on an API error -- and collapsing them here would make
	 * telling them apart later a protocol change rather than a UI one. */
	CHECK(USAGE_ACTIVITY_FAILED != USAGE_ACTIVITY_STUCK,
	      "FAILED is not merely an alias for STUCK");


	/*
	 * Which states earn the row under the brand. The clock owns it by
	 * default; only a state that wants a person takes it away.
	 */
	CHECK(usage_activity_needs_row(USAGE_ACTIVITY_FAILED),
	      "a failed turn takes the row from the clock");
	CHECK(usage_activity_needs_row(USAGE_ACTIVITY_STUCK),
	      "a wedged session takes the row");
	CHECK(usage_activity_needs_row(USAGE_ACTIVITY_WAITING),
	      "an open prompt takes the row");
	/*
	 * The two that do NOT, and the reasons are different. RUNNING would
	 * spend the panel's only sentence saying what a row of green pips
	 * already says. IDLE is the owner's call: "Finished" is not an error,
	 * an amber pip carries it, and a desk that finishes a session every
	 * few minutes would never see the clock.
	 */
	CHECK(!usage_activity_needs_row(USAGE_ACTIVITY_RUNNING),
	      "working does not displace the clock -- the green pip says it");
	CHECK(!usage_activity_needs_row(USAGE_ACTIVITY_IDLE),
	      "finished does not displace the clock");
	CHECK(!usage_activity_needs_row(USAGE_ACTIVITY_NONE),
	      "nothing known does not displace the clock");

	test_toast_change();

	printf(failures ? "\n%d FAILED\n" : "\nall state checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
