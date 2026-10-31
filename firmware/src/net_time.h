#ifndef NET_TIME_H
#define NET_TIME_H

#include <stdint.h>
#include <stdbool.h>

bool net_time_valid(void);

/* Take the wall clock from the daemon's `time` message, the only clock
 * source this board has. The given epoch is tied to the uptime at the call,
 * and later readings count forward from there. */
void net_time_set_manual(int64_t unix_s);

/* Local timezone, minutes east of UTC (e.g. UTC+3 = 180, UTC-5 = -300). */
void net_time_set_offset(int32_t offset_min);

/* Local wall-clock HH:MM. Returns false (outputs untouched) until both a
 * time source and an offset are known -- callers hide the clock rather than
 * show a wrong one. */
bool net_time_local(int *hh, int *mm);

/* Seconds from now until the ISO-8601 instant `iso` (e.g. "2026-07-13T05:20:00Z").
 * Clamped at 0; returns -1 if the clock isn't set or the string is malformed --
 * the display renders -1 as "--" rather than a confident-but-wrong countdown. */
int32_t net_time_secs_until(const char *iso);

#endif /* NET_TIME_H */
