#ifndef USAGE_VIEW_H
#define USAGE_VIEW_H

#include <stdint.h>
#include <stdbool.h>

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

/*
 * Boot progress: one CONNECTING takeover with a segmented bar that fills
 * green, current step named below. Both modes wear the same screen -- only
 * the step list differs (USB: link daemon / first push; WiFi: join / sign
 * in / first fetch). Call _begin once with the mode's steps (max 3), then
 * _stage as the worker advances. Segments before `stage` render as done,
 * `stage` pulses, the rest stay dim; stage == n means "Ready".
 */
void usage_view_boot_begin(const char *const *steps, int nsteps);
void usage_view_boot_stage(int stage);

/* True once any usage numbers have arrived this boot -- lets the mode loops
 * tell "still waiting for the first data" from "data went stale". */
bool usage_view_have_data(void);

/* Fable's weekly utilization for the long-press card (Claude's windows
 * today are all-models + fable); -1 = unknown. Fed by either data source
 * alongside the headline numbers. */
void usage_view_set_models(double fable_pct);

void usage_view_set_status(enum usage_status status);

#endif /* USAGE_VIEW_H */
