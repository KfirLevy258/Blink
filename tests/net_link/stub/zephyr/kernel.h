#pragma once
#include <errno.h>
#include <stdint.h>
#include <stddef.h>
#include <zephyr/sys/sys_heap.h>
extern int64_t fake_now_ms;
static inline int64_t k_uptime_get(void) { return fake_now_ms; }
#define ARG_UNUSED(x) (void)(x)
#define BIT(n) (1UL << (n))
#define MIN(a, b) ((a) < (b) ? (a) : (b))
#define IS_ENABLED(x) (x)
#define BUILD_ASSERT(c, m) _Static_assert(c, m)
#define CONFIG_ESP32_WIFI_STA_AUTO_DHCPV4 1
#define CONFIG_APPLICATION_INIT_PRIORITY 90
#define APPLICATION 0
/* The test calls net_link_init() itself. */
#define SYS_INIT(f, lvl, prio) \
	static int (*const __attribute__((unused)) sysinit_##f)(void) = f
#define SYS_FOREVER_MS (-1)
#define K_SECONDS(s) ((s) * 1000)
#define K_MSEC(m) (m)

/* A semaphore with a clock: a take that finds nothing waits out its timeout
 * by advancing the fake time, then fails. */
struct k_sem { int count, limit; };
/* net_link.c writes `static K_SEM_DEFINE(...)`, as Zephyr's own is not static. */
#define K_SEM_DEFINE(n, i, l) struct k_sem n = { i, l }
static inline void k_sem_give(struct k_sem *s)
{
	if (s->count < s->limit) {
		s->count++;
	}
}
static inline void k_sem_reset(struct k_sem *s) { s->count = 0; }
static inline int k_sem_take(struct k_sem *s, int timeout_ms)
{
	if (s->count > 0) {
		s->count--;
		return 0;
	}
	fake_now_ms += timeout_ms;
	return -EAGAIN;
}

struct k_spinlock { int unused; };
typedef int k_spinlock_key_t;
static inline k_spinlock_key_t k_spin_lock(struct k_spinlock *l) { (void)l; return 0; }
static inline void k_spin_unlock(struct k_spinlock *l, k_spinlock_key_t k)
{
	(void)l; (void)k;
}
struct k_heap { struct sys_heap heap; };
