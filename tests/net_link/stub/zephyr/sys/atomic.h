#pragma once
/* One thread in the host test, so plain longs. atomic_clear returns the old
 * value, as Zephyr's does. */
typedef long atomic_t;
typedef long atomic_val_t;
#define ATOMIC_INIT(v) (v)
static inline long atomic_get(const atomic_t *a) { return *a; }
static inline void atomic_set(atomic_t *a, long v) { *a = v; }
static inline long atomic_clear(atomic_t *a) { long o = *a; *a = 0; return o; }
static inline long atomic_or(atomic_t *a, long v) { long o = *a; *a |= v; return o; }
static inline long atomic_and(atomic_t *a, long v) { long o = *a; *a &= v; return o; }
