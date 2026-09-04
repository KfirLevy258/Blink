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
	USAGE_ACTIVITY_NONE = 0,	/* green: live data, no session says anything */
	USAGE_ACTIVITY_IDLE,		/* amber: a turn finished -- your turn */
	USAGE_ACTIVITY_RUNNING,		/* green, pulsing: everything is working */
	USAGE_ACTIVITY_WAITING,		/* amber, pulsing: asking permission right now */
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

void usage_view_set_activity(enum usage_activity a);

/*
 * The board has closed its eyes, or opened them again.
 *
 * The status-change popup is the only thing that needs to know. Sleep exists
 * so the panel STOPS asking for attention, and a popup that fired over a
 * sleeping board -- or worse, sat there through the whole sleep because
 * nothing was ticking it down and greeted the owner on waking -- would undo
 * the feature it is drawn on top of. So while this is true the popup is
 * skipped silently: the change is still recorded, so waking does not produce
 * a burst of everything that happened in the night.
 *
 * ui_sleep.c calls it, because ui_sleep_run() blocks the mode loop for the
 * whole sleep and keeps servicing the protocol between frames -- so the
 * setters on this file go on being called the entire time, with nobody
 * looking.
 */
void usage_view_set_sleeping(bool sleeping);

/*
 * Which project the panel should name, and how many sessions hold the state.
 *
 * `label` may be empty, which is the ordinary case whenever several sessions
 * share the state -- the daemon refuses to pick one, so the line falls back
 * to the count. See pc/providers/claude_state.py.
 *
 * `n` is kept for wire compatibility only -- the count itself now lives on
 * the pip row, fed by usage_view_set_counts(), and this function discards it.
 */
void usage_view_set_session(const char *label, int n);

/*
 * How many sessions are in each state, for the pip row.
 *
 * `n_stuck` is the wire's name and it carries FAILED -- claude_state folds
 * the two together and no provider produces `stuck` any more. Finished is not
 * sent: it is n_sess minus the other three, derived here rather than spending
 * bytes on a line that has two to spare.
 */
void usage_view_set_counts(int n_sess, int n_run, int n_wait, int n_stuck);

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
 *
 * The countdowns are in remaining seconds, like usage_view_update's; -1 is
 * unknown. Both appear under their gauge, side by side, each in its
 * provider's colour.
 */
/*
 * Which provider the OUTER ring belongs to. Sets its colour, and its
 * countdown's.
 *
 * The name, not the ring position: on a machine running only Codex the outer
 * ring is Codex, and it should not be wearing Claude's colour.
 */
/*
 * Move one page up (-1) or down (+1) the provider stack.
 *
 * A no-op when there is only one provider: the single-provider desk has no
 * second page to reach and no rail to explain one.
 */
/*
 * Whether a page change in this direction would do anything.
 *
 * Asked BEFORE a transition is armed: the page change is a full wipe now, and
 * running one to arrive back where you started is 650 ms of frozen panel in
 * exchange for nothing. With one provider reporting there is no second page
 * and every vertical swipe is one of these.
 */
bool usage_view_can_page(int delta);

void usage_view_page_step(int delta);

/*
 * Draw a page change part-way, while the swipe that would cause it is still
 * being made.
 *
 * `delta` is the direction the stroke is heading (-1/+1, 0 for none) and `pct`
 * is how far it has committed towards the distance a swipe needs, 0..100. The
 * rail's marks trade places continuously: the one being left shrinks by
 * exactly what the one being reached grows, arriving at full width at the same
 * moment the swipe fires.
 *
 * Called on every tick of the swipe drain, repeats included, so it drops
 * updates that would change nothing.
 */
void usage_view_page_preview(int delta, int pct);

/*
 * How fast the session window is filling, in percent per hour; 0 or less
 * means "no answer", which is the usual case.
 *
 * Drawn where the session countdown goes, and ONLY when there is no countdown
 * to draw. That configuration is real and is not a bug: Claude Desktop keeps
 * two percentages and no reset timestamps of any kind (verified across every
 * file, LevelDB store and cache it writes, 2026-08-28), so a machine with the
 * desktop app and no Claude Code has usable gauges and nothing to count down.
 *
 * A rate, not a countdown in disguise. Deriving a reset time from the same
 * history was investigated and refused: it is computable for only 13% of
 * windows on real data, and its failures are indistinguishable from its
 * successes. This is arithmetic over readings that were actually observed.
 *
 * The daemon sends at most one of the two, so the board is never asked to
 * choose (pc/normalizer).
 */
void usage_view_set_burn(double pph);

/*
 * How old each provider's READING is, in seconds, as measured by the daemon
 * (pc/protocol.py, `age_s`/`p2_age_s`); -1 where it did not say.
 *
 * Not the same clock as the message. The board sees a usage message every
 * 60 s whether or not the figure in it changed, so counting from arrival --
 * which is what this panel did, and the only reason its age caption never
 * appeared -- measures how well the CABLE is working, not how current the
 * number is. A Claude Desktop percentage can be hours old and still arrive
 * every minute, looking exactly as live as one taken a moment ago.
 *
 * Pass -1 for a provider that is absent or unknown; the board then falls
 * back to the message clock, which is what an older daemon leaves it with.
 */
void usage_view_set_ages(int32_t p1_age_s, int32_t p2_age_s);

void usage_view_set_provider1(const char *tag);

/*
 * The FIRST provider's own age, which is what the `stale` flag on the wire has
 * always meant. Recorded per page so the hint can ask about the page being
 * shown -- see usage_view_set_provider2.
 */
void usage_view_set_provider1_stale(bool stale);

/*
 * `stale` is the SECOND provider's own age, not the panel's.
 *
 * Freshness belongs to a reading. The two providers are read from different
 * places at different times, and only one page is on screen, so one flag for
 * both meant a live page could be labelled "Reading is old" because the page
 * you were not looking at had gone quiet -- which is exactly what a machine
 * running Claude Code all day with Codex touched once that morning does
 * (user-reported 2026-08-28).
 */
void usage_view_set_provider2(const char *tag, double session_pct,
			      double weekly_pct, int32_t session_resets_in_s,
			      int32_t weekly_resets_in_s, bool stale);

#endif /* USAGE_VIEW_H */
