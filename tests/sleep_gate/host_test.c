/* The dozing rules, pinned: docs/sleep-mode-design.md.
 *
 *   cc -Wall -Werror -I firmware/src tests/sleep_gate/host_test.c \
 *      firmware/src/sleep_gate.c firmware/src/usage_freshness.c \
 *      -o /tmp/sleep_gate
 *
 * usage_freshness is linked because the last block asks the question the
 * way main.c asks it -- with the numbers a message actually carried --
 * rather than with two constants typed in beside each other.
 */
#include <stdio.h>
#include "sleep_gate.h"
#include "usage_freshness.h"

static int fails;
#define CHECK(c) do { if (!(c)) { fails++; printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); } } while (0)

int main(void)
{
	/* --- the original rule: the app went silent --- */

	/* the case it exists for: app silent, figures shown, nothing flashing */
	CHECK(sleep_should_start(true, true, false));
	/* never met the app this boot: still "connecting" */
	CHECK(!sleep_should_start(true, false, false));
	/* the app is talking, or said bye: no sleep */
	CHECK(!sleep_should_start(false, true, false));
	/* esptool has the port: silence means an update, not a nap */
	CHECK(!sleep_should_start(true, true, true));

	/* --- the second rule: the app talks, the reading does not move --- */

	/* The field case (2026-09-02): the computer slept, the daemon kept
	 * pinging all night, and the panel sat awake on a reading that had
	 * stopped moving. Silence never came, so the rule above never fired. */
	CHECK(sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, true, false,
				       USAGE_ACTIVITY_NONE));
	/* One second under the line is not old enough. */
	CHECK(!sleep_stale_should_start(SLEEP_ABSENT_AFTER_S - 1, true, false,
					USAGE_ACTIVITY_NONE));
	/* An unknown age is not a very old one. A daemon too old to send an
	 * age must not put the panel to sleep. */
	CHECK(!sleep_stale_should_start(-1, true, false, USAGE_ACTIVITY_NONE));
	/* Same three refusals the first rule has. */
	CHECK(!sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, false, false,
					USAGE_ACTIVITY_NONE));
	CHECK(!sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, true, true,
					USAGE_ACTIVITY_NONE));
	/* Something wants a person. A wedged session or an open prompt is a
	 * claim on them, and closing the eyes over it hides the one thing
	 * this panel exists to show. */
	CHECK(!sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, true, false,
					USAGE_ACTIVITY_WAITING));
	CHECK(!sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, true, false,
					USAGE_ACTIVITY_STUCK));
	CHECK(!sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, true, false,
					USAGE_ACTIVITY_FAILED));
	CHECK(!sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, true, false,
					USAGE_ACTIVITY_RUNNING));
	/* A finished turn is amber, not a summons: the pip already carries
	 * it and it does not keep the panel lit for four hours. */
	CHECK(sleep_stale_should_start(SLEEP_ABSENT_AFTER_S, true, false,
				       USAGE_ACTIVITY_IDLE));

	/* --- start and wake are exact complements --- */

	/*
	 * The one property that matters more than any single case. The wake
	 * rule runs INSIDE ui_sleep_run while the start rule stays outside
	 * it, so a threshold or an activity that drifted between them would
	 * make the board close its eyes and open them again in a loop,
	 * forever, on a real desk. Pinned as a grid rather than as prose.
	 *
	 * had_usage is fixed at true, and the grid is only a complement proof
	 * under that. It has to be: with had_usage false and a genuinely old
	 * reading, start is false because it demands had_usage and wake is
	 * false because the age is old -- a doze with no way out of it. Two
	 * separate things keep that off a desk. sleep_stale_should_wake is
	 * only ever asked from inside ui_sleep_run, which is only entered
	 * after a start that already required had_usage; and had_usage is
	 * usage_view_have_data(), a latch (usage_view.c:1567) that never goes
	 * back to false once set, while every non-negative age reaches the
	 * board from proto.c:427 -- the same handler that has just called
	 * usage_view_update() and thrown that latch. Break either of those
	 * and the empty corner of this grid becomes a board that hangs
	 * asleep.
	 */
	{
		static const enum usage_activity acts[] = {
			USAGE_ACTIVITY_NONE, USAGE_ACTIVITY_IDLE,
			USAGE_ACTIVITY_RUNNING, USAGE_ACTIVITY_WAITING,
			USAGE_ACTIVITY_STUCK, USAGE_ACTIVITY_FAILED,
		};
		static const int32_t ages[] = {
			-1, 0, 600, 1800, SLEEP_ABSENT_AFTER_S - 1,
			SLEEP_ABSENT_AFTER_S, SLEEP_ABSENT_AFTER_S + 1, 205200,
		};
		unsigned int a, g, o;

		for (a = 0; a < sizeof(acts) / sizeof(acts[0]); a++) {
			for (g = 0; g < sizeof(ages) / sizeof(ages[0]); g++) {
				for (o = 0; o < 2; o++) {
					bool start = sleep_stale_should_start(
						ages[g], true, o == 1, acts[a]);
					bool wake = sleep_stale_should_wake(
						ages[g], o == 1, acts[a]);

					CHECK(start != wake);
				}
			}
		}
	}

	/* --- the age predicate both dozing rules share --- */
	CHECK(!sleep_nobody_is_here(-1));
	CHECK(!sleep_nobody_is_here(0));
	CHECK(!sleep_nobody_is_here(SLEEP_ABSENT_AFTER_S - 1));
	CHECK(sleep_nobody_is_here(SLEEP_ABSENT_AFTER_S));

	/* --- and the one the wake-time status stamp asks instead --- */

	/* Half an hour, because this one is about the number on the screen
	 * rather than about the person in front of it. */
	CHECK(!sleep_reading_is_stale(-1));
	CHECK(!sleep_reading_is_stale(0));
	CHECK(!sleep_reading_is_stale(SLEEP_READING_STALE_AFTER_S - 1));
	CHECK(sleep_reading_is_stale(SLEEP_READING_STALE_AFTER_S));
	CHECK(sleep_reading_is_stale(SLEEP_READING_STALE_AFTER_S + 1));

	/* The gap between the two questions, which is the bug this predicate
	 * exists for: a board that dozed for an hour used to wake with a green
	 * dot over an hour-old reading, because an hour is not four hours. It
	 * IS half an hour. */
	CHECK(sleep_reading_is_stale(3600) && !sleep_nobody_is_here(3600));
	CHECK(sleep_reading_is_stale(SLEEP_ABSENT_AFTER_S));

	/* Both numbers, not just their symbols -- see the note below. 1800 s
	 * is not a free choice: it is pc/statusline_source's STALE_AFTER_S,
	 * the bound the daemon computes the wire's `stale` flag by, and the
	 * board answering the same question differently is the disagreement
	 * this whole predicate exists to prevent. */
	CHECK(SLEEP_READING_STALE_AFTER_S == 30 * 60);

	/* The number, not just the symbol. Everything above is written in
	 * terms of SLEEP_ABSENT_AFTER_S and would keep passing if somebody
	 * changed it to 30 minutes -- which would doze on a person sitting
	 * at the desk between renders. Four hours is argued for in
	 * sleep_gate.h; changing it means changing that argument too. */
	CHECK(SLEEP_ABSENT_AFTER_S == 4 * 60 * 60);

	/*
	 * --- and the desk this gate closed its eyes on ---
	 *
	 * The two fixes on this branch, fighting. The daemon remembers the
	 * last status line that carried a five-hour percentage and re-offers
	 * it at its ORIGINAL time, because an expired window does not make
	 * the last real reading untrue. Claude Code is open and rewriting
	 * that file every minute; only the seven-day figure survives the
	 * rewrite. So one message carries a dial twelve hours old and a
	 * machine that spoke five seconds ago -- and the gate used to be
	 * given the first number.
	 *
	 * Driven through usage_freshness rather than by passing 43200 and 5
	 * to the predicates directly, because the fix is which of the two
	 * numbers the board keeps and hands over, not what the predicate
	 * does with the number it gets.
	 */
	usage_freshness_note(43200, 5, USAGE_ACTIVITY_NONE, 1000000);

	/* What the panel used to do, and it is not a near miss: the dial's
	 * age is three times the threshold. */
	CHECK(sleep_stale_should_start(usage_freshness_age_s(1000000), true,
				       false, USAGE_ACTIVITY_NONE));

	/* What it does now. Somebody is at this desk. */
	CHECK(!sleep_stale_should_start(usage_freshness_active_age_s(1000000),
					true, false, USAGE_ACTIVITY_NONE));

	/* An hour of dozing later it is still awake, because the active age
	 * grows from five seconds and not from twelve hours. */
	CHECK(!sleep_stale_should_start(usage_freshness_active_age_s(4600000),
					true, false, USAGE_ACTIVITY_NONE));

	/* The desk really does go quiet eventually, and then it dozes -- the
	 * fix must not have replaced the gate with an open door. Four hours
	 * and five seconds after that message, with nothing since. */
	CHECK(sleep_stale_should_start(
		      usage_freshness_active_age_s(1000000 +
						   SLEEP_ABSENT_AFTER_S *
						   1000LL),
		      true, false, USAGE_ACTIVITY_NONE));

	/* And a daemon too old to send the second number still dozes on the
	 * first, exactly as it did before the field existed. */
	usage_freshness_note(SLEEP_ABSENT_AFTER_S, -1, USAGE_ACTIVITY_NONE,
			     2000000);
	CHECK(sleep_stale_should_start(usage_freshness_active_age_s(2000000),
				       true, false, USAGE_ACTIVITY_NONE));

	printf("%s\n", fails ? "FAIL" : "ok   sleep_gate");
	return fails ? 1 : 0;
}
