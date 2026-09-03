#ifndef SLEEP_GATE_H
#define SLEEP_GATE_H

#include <stdbool.h>
#include <stdint.h>
#include "usage_view.h"

/* The one rule for dozing (docs/sleep-mode-design.md): the host has gone
 * silent without saying goodbye, this boot has shown real figures at least
 * once (so a board that never met its app keeps saying "connecting"), and no
 * firmware update is in flight (the port is closed for ~75 s while esptool
 * writes -- silence that means the opposite of sleep). Pure, so the host
 * test in tests/sleep_gate can pin it. */
bool sleep_should_start(bool host_lost, bool had_usage, bool ota_busy);

/*
 * How old a reading has to be before the board stops waiting up for it.
 *
 * Four hours, and the number is load-bearing. It has to sit above every gap
 * a person sitting at the desk can produce and below a night:
 *
 *   - The daemon's own staleness bound is 1800 s (pc/statusline_source
 *     STALE_AFTER_S). That marks "this reading is old", not "nobody is
 *     here" -- a Claude Code user reading code between renders crosses it
 *     routinely, and dozing on them would be the opposite bug.
 *   - The age caption appears at 600 s (AGE_CAPTION_MIN_S in usage_view.c),
 *     chosen against Claude Desktop's 300 s at-the-machine and 900 s
 *     away refresh schedules -- so a desktop-only user who is present sits
 *     under 900 s.
 *   - Four hours is 8x the first and 16x the second. It outlasts a lunch, a
 *     meeting, and any single stretch of work without a render, and still
 *     has a machine that sleeps at 23:00 dozing by 03:00 rather than at
 *     dawn. Eight hours would have left the board that produced this bug
 *     lit until morning, which was the complaint.
 */
#define SLEEP_ABSENT_AFTER_S 14400

/* Has the reading stopped moving for long enough that nobody can be here?
 * -1 (we cannot say) is NOT absence: a daemon too old to send an age must
 * not doze the panel. */
bool sleep_nobody_is_here(int32_t age_s);

/*
 * How old a reading has to be before it is DRAWN as old. A different
 * question from the one above, which is why it gets a different number.
 *
 * "Is anybody at this desk?" is answered in hours, because a person who
 * stepped out is still coming back and a panel that dozed on them would be
 * the worse bug. "Is this number old?" is answered in half an hour, because
 * that is the bound the rest of the system already uses: the daemon's
 * pc/statusline_source STALE_AFTER_S is 1800 s, and the `stale` flag it puts
 * on the wire is computed by it. The board almost never has to ask this
 * itself -- it is told -- except at one moment, opening its eyes, when the
 * next usage message may still be up to a minute away. Answering it there
 * with the four-hour number had a board waking from an hour's doze showing a
 * green dot over an hour-old reading until that message landed.
 */
#define SLEEP_READING_STALE_AFTER_S 1800

/* Should that reading be drawn as old? -1 keeps the same meaning it has
 * above: a board that has never had a reading has nothing to call stale. */
bool sleep_reading_is_stale(int32_t age_s);

/*
 * The second way in (field report 2026-09-02).
 *
 * The rule above waits for silence, and silence never came: proto.c clears
 * host_lost on every protocol line including the 10 s pings, so a computer
 * that slept while its daemon kept answering left the panel awake all night
 * on a reading 57 hours old. This asks the other question -- the app is
 * talking, but is it saying anything new? -- and refuses for the same two
 * reasons the first rule does, plus one of its own: nothing on screen may be
 * asking for a person.
 */
bool sleep_stale_should_start(int32_t age_s, bool had_usage, bool ota_busy,
			      enum usage_activity act);

/*
 * And back out again. The exact complement of the rule above minus
 * had_usage, which cannot become false once true.
 *
 * It has to be a separate function because it is asked from inside
 * ui_sleep_run, where the loop is waiting on something to change, and
 * complement rather than "a fresh reading arrived" because the wake
 * condition drifting from the sleep condition by so much as a second would
 * have a board on a real desk closing and opening its eyes forever.
 */
bool sleep_stale_should_wake(int32_t age_s, bool ota_busy,
			     enum usage_activity act);

#endif /* SLEEP_GATE_H */
