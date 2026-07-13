#ifndef NET_TIME_H
#define NET_TIME_H

#include <stdint.h>
#include <stdbool.h>

/* Sync the wall clock over SNTP. TLS certificate validation needs a correct
 * time, so this must succeed before the first HTTPS request. Returns 0 on
 * success. */
int net_time_sync(int timeout_s);

bool net_time_valid(void);

/* Seconds from now until the ISO-8601 instant `iso` (e.g. "2026-07-13T05:20:00Z").
 * Clamped at 0; returns -1 if the clock isn't set or the string is malformed --
 * the display renders -1 as "--" rather than a confident-but-wrong countdown. */
int32_t net_time_secs_until(const char *iso);

#endif /* NET_TIME_H */
