/*
 * How full the LVGL pool is, measured on the board instead of argued about.
 *
 * prj.conf has called the 30720-byte pool "ample headroom" since it was cut
 * from 32768, and nothing ever measured it. gapietro's fork measured theirs on
 * 2026-10-14, after a settings panel one row longer than ours halted the board
 * the moment it opened: LVGL wrote through a glyph buffer whose allocation had
 * just failed (lv_draw_label.c:610), with the pool at 23504 used and 7128 free
 * before the panel was even drawn. Most LVGL code paths use what they asked
 * for without checking it came back, so an empty pool shows up as a fault in
 * some unrelated draw call rather than as a screen missing a widget. Focus mode
 * (one big gauge) is next in line for that pool, and gets sized from these
 * numbers.
 *
 * Their first attempt printed stale stack: lvgl_heap_stats() writes nothing
 * unless CONFIG_SYS_HEAP_RUNTIME_STATS is on, and the struct it was handed
 * still held the panel's version string, read back as three integers. Here
 * the counters are read straight off the heap and this file is not compiled
 * at all without that option.
 *
 * The largest free block is not something Zephyr reports. sys_heap_print_info
 * computes it but only prints a table, so it is computed here the same way,
 * from the heap's free lists (lib/heap/heap.h, Zephyr 4.3.x). The LVGL heap
 * itself is static inside the Zephyr module; it is found through the heap
 * array (CONFIG_SYS_HEAP_ARRAY_SIZE) as the one whose memory holds the active
 * screen, which LVGL allocated from it.
 */
#include <zephyr/kernel.h>
#include <zephyr/sys/sys_heap.h>
#include <zephyr/sys/util.h>
#include <lvgl.h>

/* Zephyr's own lib/heap internals, spelled from ZEPHYR_BASE (see
 * CMakeLists.txt) so that no app header -- a future src/heap.h, say -- can
 * ever be picked up in its place. */
#include "lib/heap/heap.h"
#include "mem_stats.h"

static struct sys_heap *pool;
static size_t boot_peak;

static struct sys_heap *find_pool(void)
{
	struct sys_heap **heaps;
	int n = sys_heap_array_get(&heaps);
	uintptr_t probe = (uintptr_t)lv_screen_active();

	for (int i = 0; i < n; i++) {
		struct z_heap *h = heaps[i]->heap;
		uintptr_t lo = (uintptr_t)chunk_buf(h);
		uintptr_t hi = lo + (uintptr_t)h->end_chunk * CHUNK_UNIT;

		if (probe >= lo && probe < hi) {
			return heaps[i];
		}
	}
	return NULL;
}

/*
 * The biggest free chunk, as the largest request that chunk could hold.
 *
 * That is a ceiling, not a promise: sys_heap_alloc() looks at only
 * CONFIG_SYS_HEAP_ALLOC_LOOPS (3) chunks of the request's own bucket before
 * moving up a bucket, so a request about this size can fail while the chunk
 * sits further down the same list. Anything at or below the bucket under it
 * will be served.
 *
 * Free lists are bucketed by power-of-two size, so the biggest chunk is in the
 * highest non-empty bucket, and the walk visits every chunk in that bucket.
 * It is bounded by the chunk count, and a link that points outside the heap
 * stops it before it is followed, so a damaged list ends the walk rather than
 * the board.
 */
static size_t largest_free(struct z_heap *h)
{
	if (h->avail_buckets == 0) {
		return 0;
	}
	int b = 31 - __builtin_clz(h->avail_buckets);
	chunkid_t first = h->buckets[b].next;
	chunkid_t c = first;
	chunksz_t best = 0;

	for (chunkid_t n = 0; n < h->end_chunk; n++) {
		if (c == 0 || c >= h->end_chunk) {
			break;
		}
		best = MAX(best, chunk_size(h, c));
		c = next_free_chunk(h, c);
		if (c == first) {
			break;
		}
	}
	/* A chunk carries its header; the caller gets the rest. */
	return best ? chunksz_to_bytes(h, best) - chunk_header_bytes(h) : 0;
}

void mem_stats_report(const char *where)
{
	if (pool == NULL) {
		pool = find_pool();
		if (pool == NULL) {
			printk("[mem] lvgl %s: pool not found among the heaps\n",
			       where);
			return;
		}
	}

	/*
	 * One snapshot: the heap's own spinlock is static inside the Zephyr
	 * module, and on this single core it amounts to masking interrupts, so
	 * this excludes every other writer in the same way. The walk is the same
	 * work sys_heap_alloc() does on every allocation. On SMP irq_lock() would
	 * not stop the other core, so such a build must fail here instead.
	 */
	BUILD_ASSERT(!IS_ENABLED(CONFIG_SMP),
		     "mem_stats.c relies on irq_lock() excluding every heap user");
	struct z_heap *h = pool->heap;
	unsigned int key = irq_lock();
	size_t used = h->allocated_bytes;
	size_t avail = h->free_bytes;
	size_t peak = h->max_allocated_bytes;
	size_t largest = largest_free(h);

	boot_peak = MAX(boot_peak, peak);
	sys_heap_runtime_stats_reset_max(pool);
	irq_unlock(key);

	printk("[mem] lvgl %s: used=%u free=%u largest=%u peak=%u"
	       " boot_peak=%u pool=%u\n", where, (unsigned)used,
	       (unsigned)avail, (unsigned)largest, (unsigned)peak,
	       (unsigned)boot_peak, (unsigned)CONFIG_LV_Z_MEM_POOL_SIZE);
}

void mem_stats_gauges_tick(bool have_data)
{
	static int64_t since;
	static bool done;

	if (done) {
		return;
	}
	if (!have_data) {
		since = 0;
		return;
	}
	int64_t now = k_uptime_get();

	if (since == 0) {
		since = now;
	} else if (now - since >= 2000) {
		done = true;
		mem_stats_report("gauges");
	}
}
