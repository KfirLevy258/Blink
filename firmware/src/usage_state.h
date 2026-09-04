#ifndef USAGE_STATE_H
#define USAGE_STATE_H

#include <stdbool.h>

#include "fmt.h"
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

/*
 * The four session counts as they arrive on the wire, in one struct so a
 * before and an after can be compared without four pairs of arguments.
 *
 * `n_stuck` is the wire's name and it carries FAILED -- claude_state folds the
 * two together. FINISHED is not sent: it is n_sess minus the other three, and
 * is derived here for the same reason usage_view.c derives it, which is that
 * the usage line measures 511 of MAX_LINE_BYTES = 512 and proto.c drops an
 * over-long line WHOLE. There is no room for a fifth count and there will not
 * be one.
 */
struct usage_counts {
	int n_sess;
	int n_run;
	int n_wait;
	int n_stuck;
};

/* What a popup should say, if one should appear at all. */
struct usage_toast {
	enum fmt_pip_kind kind;	/* which state gained a session */
	int count;		/* how many sessions are now in it */
	bool nameable;		/* may the `session` label stand for it? */
};

/*
 * Did the execution state change in a way worth interrupting someone about?
 *
 * WHAT THE WIRE ALLOWS, which is the whole shape of this. It carries counts
 * and ONE label; it cannot name sessions individually and cannot grow a field
 * to do so (see struct usage_counts above). So a change is a RISE in one
 * state's count -- somebody's turn ended, somebody's prompt opened -- and the
 * sentence is either that state's tally or, in the single case the label was
 * built for, that session's name.
 *
 * `prev == NULL` means "no known previous state" and NOTHING fires. Boot and
 * reconnect both land here, and at both of them every count is a change from
 * nothing: without this the owner would get a popup every time the daemon
 * restarted, which is the fastest way to teach someone to ignore a popup.
 *
 * SEVERITY ORDER, one popup. Two states can rise on the same poll -- a
 * session finishes in the same minute another opens a prompt -- and this
 * returns only the worse of them, in fmt_pips()' order: failed, then waiting,
 * then finished. A queue of popups on a 320x240 panel is worse than one true
 * sentence, and the pip row is still showing all of it anyway.
 *
 * RUNNING IS NOT ANNOUNCED. A session starting to work is something the
 * person at the desk just did on purpose; a popup for it is the panel
 * narrating their own keystrokes back at them. The two the owner asked for --
 * "session x is finished, session y is waiting for you" -- both describe work
 * that has stopped needing them, and FAILED joins them because it is the same
 * kind of news.
 *
 * `nameable` is true only when the state that rose is ALSO the aggregate
 * state the daemon picked its label for, and exactly one session holds it.
 * pc/providers/claude_state.py sets `label` from the WORST state and only
 * when a single session holds it, so on a desk with one waiting session and
 * one that just finished, the label names the waiting one -- and captioning
 * the finished popup with it would be the panel making up a fact.
 */
bool usage_toast_change(const struct usage_counts *prev,
			const struct usage_counts *now,
			enum usage_activity aggregate,
			struct usage_toast *out);

#endif /* USAGE_STATE_H */
