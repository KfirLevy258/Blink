/* Host stub for the touch-target harness (tools/panel_render/touch.sh).
 * The uptime is the harness's own simulated clock, so a stroke's timing is
 * the board's timing and not however fast this machine runs. */
#ifndef HOST_TOUCH_ZEPHYR_KERNEL_H
#define HOST_TOUCH_ZEPHYR_KERNEL_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

int64_t k_uptime_get(void);

#ifndef ARG_UNUSED
#define ARG_UNUSED(x) ((void)(x))
#endif
#ifndef MIN
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#endif
#ifndef MAX
#define MAX(a, b) ((a) > (b) ? (a) : (b))
#endif
#define BUILD_ASSERT(c, msg) _Static_assert(c, msg)
#endif
