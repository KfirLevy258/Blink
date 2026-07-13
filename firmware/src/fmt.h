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
 * How long ago the data arrived: "12s ago", "3m ago", "1h 20m ago".
 * This is the one number on screen that cannot lie -- the countdowns keep
 * ticking locally even when the host is dead, so age is what tells the user
 * whether they are looking at live data or a corpse.
 */
void fmt_age(int32_t secs, char *buf, size_t buflen);

#endif /* FMT_H */
