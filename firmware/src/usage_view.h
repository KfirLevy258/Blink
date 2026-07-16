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

/* Delete the gauge screen (before showing the setup screen). */
void usage_view_deinit(void);

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

/* Wall clock, top-center. hh < 0 hides it (unknown time is blanked, never
 * shown wrong). */
void usage_view_set_clock(int hh, int mm);

/* Reword the full-screen "no data yet" takeover. The default text is USB
 * mode's ("waiting for host"); standalone WiFi waits for something else
 * entirely, and telling the user to start a PC daemon there is a lie. */
void usage_view_set_waiting(const char *title, const char *sub);

/*
 * Standalone boot progress: one CONNECTING screen with a segmented bar that
 * fills green, current step named below. Segments before `stage` render as
 * done, `stage` pulses, the rest stay dim.
 * Stages: 0 join WiFi, 1 sign in (clock sync rides inside), 2 first fetch.
 * The first call switches the takeover from title+sub to bar form.
 */
void usage_view_boot_stage(int stage);

void usage_view_set_status(enum usage_status status);

#endif /* USAGE_VIEW_H */
