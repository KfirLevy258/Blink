#ifndef EDIT_HOLD_H
#define EDIT_HOLD_H

#include <stdbool.h>

/*
 * Settings > Theme > "Edit on computer", held while the builder it opened
 * is open on the computer (owner, 2026-10-30).
 *
 * The owner's words after a board run: "the edit on computer button should
 * re-appear ... only once the page is not active / closed". Until then the
 * slot read "Opened on your computer" for 3 s and then its own name again,
 * pressable while the builder was still open; a press opened another tab
 * on it, the older tab went to "This tab is no longer active.", and the
 * owner had two tabs for one edit. Now an ok 1 holds the slot, reading
 * "Editing on your computer" and refusing taps, until the daemon says the
 * builder has ended (theme_edit done:1, contract Addendum 6).
 *
 * This is the hold's whole policy, pure and in its own file for the reason
 * upd_tap is: every way out of the hold is a separate line from the daemon
 * or a separate event on the board, and a hold that misses one of them is a
 * button that never comes back -- which is what the 10 s lock-out before
 * 2026-10-29 looked like to the owner, and why it was taken out. A host
 * test (tests/edit_hold) walks every one. ui_settings.c does the drawing
 * and the timer; it decides nothing about the hold itself.
 *
 * The ways in:
 *   - an explicit theme_edit ok 1, at any time -- late ones included: a
 *     daemon whose browser took 28 s to open (measured, Launcher.close in
 *     pc/theme_server.py) answers long after the board's 5 s fallback, and
 *     the builder it opened is just as open;
 *   - a welcome that says "editing":1: the builder this daemon opened for
 *     the board is still open, told to a board that rebooted or whose cable
 *     was out while it was opened.
 * Both only from a daemon whose welcome listed the cap "edit_done", which
 * is a promise that it sends done:1 and states "editing" on every greeting.
 * Any other daemon -- one from before the hold, or before Addendum 6 --
 * never frees a hold, so with one the slot keeps the 3 s "Opened" note of
 * before.
 *
 * The ways out:
 *   - done:1, unless an ask of the board's own is in flight: the daemon
 *     writes every answer before any done on the same pass (Bridge.pump), so
 *     a done that lands mid-ask is for an older builder, and the answer on
 *     its way is for a new one;
 *   - a welcome with "editing":0, or from a daemon without the cap (one
 *     downgraded, or replaced: a builder lives inside the daemon process,
 *     so a new daemon has none open);
 *   - the host going away (HOST LOST) or saying bye: nothing can open, end
 *     or report a builder for this board while it is away, and the welcome
 *     that comes with the host's return says again whether one is open;
 *   - the upper bound, edit_hold_expired(), for a done lost on the wire.
 * A welcome with no "editing" field changes nothing: that is the probe's
 * welcome (claude_usage_bridge.probe_is_our_board), a question asked
 * before any Bridge exists, and greet()'s welcome follows it and says.
 */
struct edit_hold {
	bool held;		/* the slot refuses taps and says it is editing */
	bool host_tells;	/* the last welcome listed "edit_done" */
};

/* Every function returns true when `held` changed, so the caller repaints
 * only then. */

/* A welcome line, whole. Reads its caps for "edit_done" and its "editing"
 * field: 1 holds, 0 frees, absent leaves the hold as it is. */
bool edit_hold_welcome(struct edit_hold *h, const char *json);

/* An explicit theme_edit answer. ok 1 from a daemon that tells holds the
 * slot, whether or not an ask is in flight; ok 0 changes nothing, not even
 * a hold: the daemon answers ok 0 for a browser that would not open on a
 * builder that is still running. */
bool edit_hold_reply(struct edit_hold *h, bool ok);

/* theme_edit done:1. `asking`: an ask of the board's own is in flight. */
bool edit_hold_done(struct edit_hold *h, bool asking);

/* The host went away (HOST LOST), or said bye. */
bool edit_hold_host_gone(struct edit_hold *h);

/* The upper bound ran out with no done. */
bool edit_hold_expired(struct edit_hold *h);

#endif /* EDIT_HOLD_H */
