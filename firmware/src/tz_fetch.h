#ifndef TZ_FETCH_H
#define TZ_FETCH_H

#include <stdint.h>

/* UTC offset (minutes east of UTC) for our public IP, from ip-api.com over
 * plain HTTP (free tier, no key; the JSON `offset` field is seconds).
 * Blocking, bounded by 8 s socket timeouts. Best-effort by design: callers
 * fall back to the offset stored in NVS. Returns 0 on success. */
int tz_fetch_offset(int32_t *offset_min);

#endif /* TZ_FETCH_H */
