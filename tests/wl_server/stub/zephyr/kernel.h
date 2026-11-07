#pragma once
#include <stdint.h>
extern int64_t fake_now_ms;
static inline int64_t k_uptime_get(void) { return fake_now_ms; }
static inline void k_msleep(int ms) { fake_now_ms += ms; }
#define ARG_UNUSED(x) (void)(x)
#define K_NO_WAIT 0
#define K_THREAD_STACK_DEFINE(n, sz) char n[sz]
#define K_THREAD_STACK_SIZEOF(n) sizeof(n)
struct k_thread { int unused; };
typedef char k_thread_stack_t;
/* wl_server_start is not under test: the thread is never run. */
static inline int k_thread_create(struct k_thread *t, char *s, size_t z,
				  void (*f)(void *, void *, void *), void *a,
				  void *b, void *c, int p, int o, int d)
{
	(void)t; (void)s; (void)z; (void)f; (void)a; (void)b; (void)c;
	(void)p; (void)o; (void)d;
	return 0;
}
static inline int k_thread_name_set(struct k_thread *t, const char *n)
{
	(void)t; (void)n;
	return 0;
}
