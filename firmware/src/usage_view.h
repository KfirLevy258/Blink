#ifndef USAGE_VIEW_H
#define USAGE_VIEW_H

#include <stdint.h>

/* Connection state, shown as a coloured dot. Mirrors the web UI's convention. */
enum usage_status {
	USAGE_STATUS_DISCONNECTED = 0,	/* grey  */
	USAGE_STATUS_OK,		/* green */
	USAGE_STATUS_STALE,		/* amber: rate-limited, showing last good */
	USAGE_STATUS_ERROR,		/* red   */
};

/* Build the screen. Call once, before any update. */
void usage_view_init(void);

/*
 * New numbers arrived.
 *
 * The reset times are *remaining seconds*, not absolute timestamps: tethered
 * over USB the board has no wall clock, so the daemon does the subtraction and
 * the board ticks the value down locally. -1 means unknown.
 */
void usage_view_update(double session_pct, int32_t session_resets_in_s,
		       double weekly_pct, int32_t weekly_resets_in_s);

/* Advance the countdowns one second. Driven from the main loop, independent of
 * message arrival, so the display keeps ticking between the daemon's polls.
 */
void usage_view_tick_1s(void);

void usage_view_set_status(enum usage_status status);

#endif /* USAGE_VIEW_H */
