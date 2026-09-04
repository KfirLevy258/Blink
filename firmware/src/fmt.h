#ifndef FMT_H
#define FMT_H

#include <stdint.h>
#include <stddef.h>

/* Longest output is "999d 0h" plus NUL; round up. */
#define FMT_COUNTDOWN_MAX 16

/*
 * Render remaining seconds as a short human countdown.
 *
 *   secs < 0   -> "--"     (unknown: the daemon sends -1 when it has no
 *                           timestamp; rendering "now" there would be a lie)
 *   secs == 0  -> "now"
 *   < 1 hour   -> "8m 05s"
 *   < 1 day    -> "2h 14m"
 *   otherwise  -> "4d 3h"
 */
void fmt_countdown(int32_t secs, char *buf, size_t buflen);

/*
 * Render a burn rate: how fast the session window is filling, percent/hour.
 *
 *   pph <= 0   -> ""       (nothing to say; the caller shows its usual "--")
 *   < 10       -> "+2.4%/h"
 *   otherwise  -> "+14%/h"
 *
 * A decimal only below ten, where it is the difference between "barely
 * moving" and "moving"; above it the tenth is noise on a 5-minute sample and
 * the width is better spent staying inside GAUGE_CD_MAX_W.
 *
 * This appears ONLY where a countdown would have gone and only when there is
 * no countdown to show -- Claude Desktop with no Claude Code, the one
 * configuration with percentages and no reset time. It is deliberately not
 * labelled: the '%' and the '/h' say what it is, and a countdown never
 * contains a percent sign, so the two can never be read as each other.
 */
void fmt_burn(double pph, char *buf, size_t buflen);

/*
 * How long ago the data arrived: "12s ago", "3m ago", "1h 20m ago".
 * This is the one number on screen that cannot lie -- the countdowns keep
 * ticking locally even when the host is dead, so age is what tells the user
 * whether they are looking at live data or a corpse.
 */
void fmt_age(int32_t secs, char *buf, size_t buflen);

/*
 * Transliterate UTF-8 to the ASCII the built-in LVGL fonts can draw.
 * Smart quotes/dashes/ellipsis map to their plain cousins; anything else
 * non-ASCII becomes '?'. Without this an SSID like "someone’s iPhone" (U+2019)
 * renders as an empty box on the panel. Always NUL-terminates.
 */
void fmt_ascii(const char *src, char *dst, size_t dstlen);

/* Longest is a 15-char status, " - ", and a 24-byte label, plus NUL. */
#define FMT_HINT_MAX 64

/*
 * The line under the status dot: what is happening, and to what.
 *
 *   status  ""      -> ""                      (nothing to say)
 *   label   set     -> "Working - Blink"
 *   otherwise       -> "Working"
 *
 * The session count no longer appears here -- it moved to the pip row, see
 * fmt_pips() below.
 *
 * The label is user-controlled text from a directory name, so it goes through
 * fmt_ascii on the way in: the built-in fonts draw non-ASCII as empty boxes,
 * and a project living under a non-ASCII profile is an ordinary setup.
 */
void fmt_hint(const char *status, const char *label, char *buf, size_t buflen);

/*
 * What the pip row should draw, decided here so usage_view.c only positions
 * and colours. Pure, so it is the one part of this feature a host test can
 * reach at all -- usage_view.c needs LVGL and cannot be compiled on a laptop.
 */
enum fmt_pip_kind {
	FMT_PIP_FAILED,		/* a turn died -- an event, never inferred */
	FMT_PIP_WAITING,	/* a prompt is open */
	FMT_PIP_RUNNING,	/* working */
	FMT_PIP_FINISHED,	/* done, unread -- the one amber, see fmt_pip_tone */
};

struct fmt_pip {
	enum fmt_pip_kind kind;
	int count;		/* 0 in pip mode; the state's tally in counts mode */
};

/*
 * The three inks the pip row is allowed to use.
 *
 * A TONE rather than a colour, because a colour is an lv_color_t and that
 * would put this decision back inside usage_view.c, which needs LVGL and has
 * no host coverage. usage_view.c turns a tone into COL_RED/COL_AMBER/
 * COL_GREEN and does nothing else with it, so the mapping below is the whole
 * of the decision and a laptop can check it.
 */
enum fmt_pip_tone {
	FMT_TONE_GREEN,
	FMT_TONE_AMBER,
	FMT_TONE_RED,
};

/*
 * RED = WANTS YOU NOW, AMBER = FINISHED, GREEN = WORKING.
 *
 * Red covers WAITING as well as FAILED, and merging those two is the point of
 * the scheme rather than a shortcut in it: both demand that a person do
 * something, the hint line under the row already NAMES which condition fired,
 * so the colour is free to say "act" while the words say "why".
 *
 * This supersedes the 2026-08-29 decision that gave WAITING and FINISHED the
 * same amber. That decision reasoned from a failure: the panel had already
 * tried to separate two states with green-steady against green-PULSING, that
 * failed across a desk, and an 8 px pip looked like a finer channel still. But
 * the channel that failed was motion, not hue -- a pulse asks the reader to
 * watch a mark over time, a colour is legible in the glance the panel exists
 * for. And the merge was measured costing real information: shown six pips (3
 * running, 2 waiting, 1 finished) the owner read "3 running and 3 waiting",
 * because the finished one was not distinct from the waiting ones at all.
 *
 * So the amber is spent on the state that does NOT want anything -- a turn
 * that ended, sitting there unread -- and the two that do share the red.
 */
enum fmt_pip_tone fmt_pip_tone(enum fmt_pip_kind k);

/*
 * Fill `out` with what to draw, most urgent first, and return how many.
 *
 *   0 sessions        -> 0 entries. An empty corner is true.
 *   1-6 sessions      -> one entry per SESSION, count 0.
 *   7+                -> one entry per NON-EMPTY state, carrying its tally.
 *
 * Six is not the geometric limit -- seven pips fit in the 75 px between the
 * clock and the brand. It is where a row stops being read and starts being
 * counted, which is the opposite of what a desk display is for.
 *
 * Counts mode holds THREE groups in that space, not four, so an overflow
 * drops FINISHED first and then RUNNING -- the worst case still shows the two
 * states that actually need a person.
 */
int fmt_pips(int n_run, int n_wait, int n_fail, int n_fin,
	     struct fmt_pip *out, int max);

/* The longest sentence below is a 27-byte label and " is waiting for you". */
#define FMT_TOAST_MAX 64

/*
 * The one sentence a status-change popup says.
 *
 *   count <= 0             -> ""                            (nothing happened)
 *   count == 1, label set  -> "Blink is waiting for you"
 *   count == 1, no label   -> "A session is waiting for you"
 *   count >  1             -> "3 sessions are waiting"
 *
 * ONE RULE FOR ALL FOUR STATES, so a reader can predict the next sentence
 * from the last one: `2 sessions finished`, `2 sessions failed`, `3 sessions
 * are running`. Only the clause changes.
 *
 * THE COUNT RATHER THAN A NAME when several sessions share the state, on the
 * owner's call: "if there is multi sessions waiting, you can just say x
 * session are waiting". The number is already on the wire (n_wait, n_stuck,
 * n_sess) and it is the more useful sentence -- naming one of three says
 * something true about one and implies it about the other two, which is the
 * mistake pc/providers/claude_state.py refuses to make when it picks `label`.
 *
 * The singular and the plural are separate strings rather than one string
 * with an "s" bolted on, because "1 sessions are waiting" is exactly the kind
 * of thing that ships. Count 1 never reaches the plural branch at all.
 *
 * `label` is user-controlled text from a directory name, so it goes through
 * fmt_ascii like fmt_hint's does, and its first letter is capitalised: it
 * LEADS the sentence here rather than trailing a status, and every sentence on
 * this panel starts with a capital (standing owner request). A count-led
 * sentence starts with a digit, which is the one case a capital cannot apply
 * to.
 *
 * RUNNING has a wording even though nothing announces it today -- see
 * usage_toast_change(), which is where that policy lives. Giving the formatter
 * one complete rule and the decider the policy keeps the two from arguing.
 */
void fmt_toast(enum fmt_pip_kind kind, const char *label, int count,
	       char *buf, size_t buflen);

#endif /* FMT_H */
