/* Host stub: the few kernel symbols cfg_store.c uses, single-threaded. */
#ifndef CFG_STUB_KERNEL_H
#define CFG_STUB_KERNEL_H
#include <stddef.h>
#include <stdint.h>

struct k_mutex { int unused; };
#define K_MUTEX_DEFINE(name) struct k_mutex name
#define K_FOREVER 0
static inline int k_mutex_lock(struct k_mutex *m, int t)
{
	(void)m;
	(void)t;
	return 0;
}
static inline int k_mutex_unlock(struct k_mutex *m)
{
	(void)m;
	return 0;
}

/* For tests/ui_theme, which links cfg_store.c against these same stubs. */
#ifndef ARG_UNUSED
#define ARG_UNUSED(x) (void)(x)
#endif

#ifndef __packed
#define __packed __attribute__((__packed__))
#endif
#ifndef ROUND_UP
#define ROUND_UP(x, a) ((((x) + (a) - 1) / (a)) * (a))
#endif
#endif
