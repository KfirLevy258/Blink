#ifndef USAGE_VIEW_H
#define USAGE_VIEW_H

#include <stdint.h>
#include <stdbool.h>

/* Connection state, shown as a coloured dot. Mirrors the web UI's convention. */
enum usage_status {
	USAGE_STATUS_DISCONNECTED = 0,	/* grey  */
	USAGE_STATUS_OK,		/* green */
	USAGE_STATUS_STALE,		/* amber: reading is old, showing last good */
	USAGE_STATUS_ERROR,		/* red   */
};

/*
 * What the tool feeding us is doing right now, from the daemon's `state`.
 *
 * NONE is not a fifth state, it is the absence of one: a daemon that said
 * nothing, or one older than this firmware. The indicator stays dark for it
 * rather than defaulting to IDLE, because "idle" is a claim about a live
 * session and an absent field is not evidence of one.
 */
enum usage_activity {
	USAGE_ACTIVITY_NONE = 0,	/* hidden */
	USAGE_ACTIVITY_IDLE,		/* green: turn complete, waiting for you */
	USAGE_ACTIVITY_RUNNING,		/* green, pulsing: working */
	USAGE_ACTIVITY_WAITING,		/* amber: wants a human */
	USAGE_ACTIVITY_STUCK,		/* red: announced work, then went silent */
	USAGE_ACTIVITY_FAILED,		/* red: the turn died on an API error */
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

/*
 * Is the full-screen CONNECTING takeover currently covering the gauges?
 *
 * Exposed so the gesture handlers can refuse to act on a screen the user
 * cannot actually see. It is not enough for them to test have_data: the
 * takeover is what is DRAWN, and that is what the user is swiping on.
 */
bool usage_view_takeover_active(void);

/*
 * Re-apply the takeover's visibility from the current status and have_data.
 *
 * For callers that blanket-restore screen children and would otherwise put a
 * stale decision back on screen; see the definition.
 */
void usage_view_sync_takeover(void);

/* Fable's weekly utilization for the long-press card (Claude's windows
 * today are all-models + fable); -1 = unknown. Fed by either data source
 * alongside the headline numbers. */
void usage_view_set_models(double fable_pct);

void usage_view_set_status(enum usage_status status);

/*
 * Context window fullness, 0-100. Negative hides the bar entirely.
 *
 * Hidden rather than drawn empty: a 0-length bar and an unknown one look
 * identical at this size, and only one of them is a fact.
 *
 * `of_n` is how many live contexts this figure is the worst of. With several
 * agents running there are several context windows and one number cannot be
 * all of them, so the bar shows the fullest -- the one about to end somebody's
 * turn -- and the panel says so rather than letting it read as the only one.
 * 1 or 0 means there is nothing to qualify.
 */
void usage_view_set_context(double ctx_pct, int of_n);

/*
 * The model in use, e.g. "Opus 5 (1M context)". NULL or "" blanks the label.
 *
 * The daemon already caps the length (protocol.MODEL_MAX_CHARS); this copies
 * into a fixed buffer and truncates again rather than trusting that, because
 * the two sides ship separately and a longer name arriving from a newer
 * daemon must not run off the screen or off the end of the buffer.
 */
void usage_view_set_model(const char *name);

void usage_view_set_activity(enum usage_activity a);

/*
 * How many Claude Code sessions are open, and how many subagents are running
 * across them. Both 0 hides the readout entirely.
 *
 * Counts, not a list. The board drops an over-long line whole, and a
 * per-session array blows the 512-byte budget at around four sessions -- on
 * exactly the busy machine most likely to have four.
 */
void usage_view_set_sessions(int n_sessions, int n_agents);

/*
 * A second provider on the same two gauges, drawn as an inner ring.
 *
 * `tag` is a short name for it -- "codex" -- and NULL or "" hides the inner
 * rings and their readouts entirely, which is the state every single-provider
 * board stays in forever.
 *
 * Percentages outside 0-100 hide that ring on its own, so a provider that can
 * report a weekly figure but not a session one shows exactly what it knows.
 */
void usage_view_set_provider2(const char *tag, double session_pct,
			      double weekly_pct);

#endif /* USAGE_VIEW_H */
