/* Host-render stub. usage_view.c reaches for exactly two things from Zephyr:
 * a millisecond uptime and IS_ENABLED. Nothing else here is real. */
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
/* The board build defines CONFIG_CLAUGE_WIFI_MODE or does not; this render is
 * the USB configuration, so it never does. */
#define IS_ENABLED(x) 0

/* The two other Zephyr conveniences usage_view.c uses. Same semantics. */
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
