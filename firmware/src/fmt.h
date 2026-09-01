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
 *   n > 1           -> "Waiting for you - 3 sessions"
 *   otherwise       -> "Working"
 *
 * A label BEATS a count, because the daemon only sends one when exactly one
 * session holds the state -- see pc/providers/claude_state.py. `n == 1` adds
 * nothing a reader does not already assume, so it is not written.
 *
 * The label is user-controlled text from a directory name, so it goes through
 * fmt_ascii on the way in: the built-in fonts draw non-ASCII as empty boxes,
 * and a project living under a non-ASCII profile is an ordinary setup.
 */
void fmt_hint(const char *status, const char *label, int n_sessions,
	      char *buf, size_t buflen);

#endif /* FMT_H */
