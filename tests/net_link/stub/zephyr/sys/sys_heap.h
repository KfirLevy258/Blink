#pragma once
#include <stddef.h>
struct sys_heap { int unused; };
struct sys_memory_stats { size_t free_bytes, allocated_bytes, max_allocated_bytes; };
int sys_heap_runtime_stats_get(struct sys_heap *h, struct sys_memory_stats *st);
