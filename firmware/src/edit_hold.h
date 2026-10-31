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
 * on it, the older tab stopped and told its owner a newer one had taken
 * over, and the
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
 *     was out while it was opened;
 *   - an edit_state line that says "editing":1, the same fact restated
 *     every 60 s and at once when the daemon hears the board again after a
 *     silence (pc/bridge.py, _restate_edit). It is what holds the slot
 *     again after HOST LOST on a connection that never closed: the Mac
 *     slept over lunch with the builder open, and the welcome, written only
 *     when a connection starts, never came (review of fp/edit-held,
 *     SHOULD-1).
 * All only from a daemon whose welcome listed the cap "edit_done", which
 * is a promise that it sends done:1, says "editing" on every greeting, and
 * restates it. The board relies on no other daemon to free a hold -- a
 * 1.4.0 daemon from before the hold does send done:1, but it never says
 * "editing", so a board that missed its done would be held until the
 * bound -- and with one the slot keeps the 3 s "Opened" note of before.
 *
 * The ways out:
 *   - done:1. Not while an ask of the board's own is in flight, which the
 *     caller checks (ui_settings_theme_edit_done): the daemon writes every
 *     answer queued before any done on the same pass (Bridge.pump), so such
 *     a done is for an older builder and the answer on its way for a new
 *     one. The one exception is a builder that ends in the instant between
 *     its open and its ok being queued, whose done can then come first and
 *     leave a hold over nothing (review NICE-1); the next edit_state frees
 *     it, within 60 s;
 *   - a welcome or edit_state with "editing":0, or a welcome from a daemon
 *     without the cap (one downgraded, or replaced: a builder lives inside
 *     the daemon process, so a new daemon has none open);
 *   - the host going away (HOST LOST) or saying bye: nothing can open, end
 *     or report a builder for this board while it is away, and the first
 *     edit_state or welcome after its return says again whether one is;
 *   - the upper bound, edit_hold_expired(): EDIT_HOLD_MS (150 s) with no
 *     restatement, ok 1 or preview, for a daemon that stopped saying.
 * A welcome with no "editing" field changes nothing: that is the probe's
 * welcome (claude_usage_bridge.probe_is_our_board), a question asked
 * before any Bridge exists, and greet()'s welcome follows it and says.
 *
 * The hold follows the builder's life, which is not quite the page's: a
 * builder whose tab went without its close beacon (a browser killed, a tab
 * shut before it loaded) ends only at its idle limit, 10 min after the last
 * edit, and the slot is held until then (review NICE-2).
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

/* An edit_state line: `editing` is its field. Holds or frees only for a
 * daemon that tells; from any other it changes nothing. */
bool edit_hold_state(struct edit_hold *h, bool editing);

/* theme_edit done:1, with no ask of the board's own in flight. */
bool edit_hold_done(struct edit_hold *h);

/* The host went away (HOST LOST), or said bye. */
bool edit_hold_host_gone(struct edit_hold *h);

/* The upper bound ran out with nothing to say the builder is open. */
bool edit_hold_expired(struct edit_hold *h);

#endif /* EDIT_HOLD_H */
