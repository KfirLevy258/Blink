#pragma once
/* One thread in the host test, so plain longs. atomic_clear returns the old
 * value, as Zephyr's does (tick() uses it as a test-and-clear). */
typedef long atomic_t;
static inline long atomic_get(const atomic_t *a) { return *a; }
static inline void atomic_set(atomic_t *a, long v) { *a = v; }
static inline long atomic_inc(atomic_t *a) { return (*a)++; }
static inline long atomic_clear(atomic_t *a) { long o = *a; *a = 0; return o; }
static inline int atomic_cas(atomic_t *a, long o, long n)
{
	if (*a != o) {
		return 0;
	}
	*a = n;
	return 1;
}
