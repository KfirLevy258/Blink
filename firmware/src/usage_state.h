#ifndef USAGE_STATE_H
#define USAGE_STATE_H

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

#endif /* USAGE_STATE_H */
