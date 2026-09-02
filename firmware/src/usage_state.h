#ifndef USAGE_STATE_H
#define USAGE_STATE_H

#include <stdbool.h>

#include "usage_view.h"

/*
 * The daemon's `state` string -> the pip's enum.
 *
 * Its own translation unit, with no Zephyr and no LVGL in it, so
 * tests/usage_state/host_test.c can compile it on a laptop. The mapping used
 * to be an if/else chain inline in proto.c's usage handler, where the only
 * way to exercise it was to flash a board and drive the daemon -- which meant
 * the branch that matters most, an unrecognised state, was never going to be
 * checked by anybody.
 */
enum usage_activity usage_activity_from_state(const char *state);

/*
 * Does this state deserve the row under the brand?
 *
 * That row is the CLOCK by default. A sentence takes it away only while
 * something wants a person: a turn died, a session is wedged, or a prompt is
 * open and waiting. Everything else leaves the clock alone.
 *
 * RUNNING is excluded because a green pip already says it, and a line reading
 * "Working" over a row of working pips spent the panel's only sentence
 * repeating itself. IDLE ("Finished") is excluded on the owner's call: it is
 * not an error, an amber pip already carries it, and on a busy desk a session
 * finishes often enough that including it would hide the clock most of the
 * day.
 *
 * Here rather than beside the text it pairs with in usage_view.c, for the same
 * reason the mapping above moved: usage_view.c needs LVGL and cannot be
 * compiled on a laptop, so a decision left there is a decision no test can
 * reach.
 */
bool usage_activity_needs_row(enum usage_activity a);

#endif /* USAGE_STATE_H */
