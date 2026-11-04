/* Host-render stub. From Zephyr, usage_view.c needs a millisecond uptime and
 * the conveniences below. Nothing else here is real. */
#ifndef HOST_ZEPHYR_KERNEL_H
#define HOST_ZEPHYR_KERNEL_H
#include <stdint.h>
#include <sys/time.h>
static inline int64_t k_uptime_get(void)
{
	struct timeval tv;
	gettimeofday(&tv, 0);
	return (int64_t)tv.tv_sec * 1000 + tv.tv_usec / 1000;
}
/* The Zephyr conveniences usage_view.c uses. Same semantics. IS_ENABLED used
 * to be stubbed here as 0, for the CONFIG_BLINK_WIFI_MODE branches; they went
 * with that mode on 2026-10-27. */
#ifndef ARG_UNUSED
#define ARG_UNUSED(x) ((void)(x))
#endif
#ifndef MIN
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#endif
#ifndef MAX
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#endif
#endif
