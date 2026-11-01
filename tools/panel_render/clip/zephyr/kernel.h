/* Host stub for the clip harness (tools/panel_render/clip.sh). The uptime is
 * the harness's simulated clock, and a sleep moves it on by exactly what was
 * asked -- LVGL's tick with it -- so the real ui_anim.c, ui_slide.c and
 * ui_sleep.c run at the board's pace and not however fast this machine is. */
#ifndef HOST_CLIP_ZEPHYR_KERNEL_H
#define HOST_CLIP_ZEPHYR_KERNEL_H
#include <stdint.h>
#include <stdbool.h>
#include <stddef.h>

int64_t k_uptime_get(void);

typedef struct {
	int64_t ms;
} k_timeout_t;
#define K_MSEC(n) ((k_timeout_t){ .ms = (n) })

int32_t k_sleep(k_timeout_t t);

/* One thread here: nothing else could run. */
static inline void k_yield(void)
{
}

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
