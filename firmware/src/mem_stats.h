#ifndef MEM_STATS_H
#define MEM_STATS_H

#include <stdbool.h>

/*
 * The LVGL pool, printed to the console at the screen changes that move it.
 *
 * One line per call, and the daemon's console echo copies it into bridge.log
 * (pc/logbook.py, ConsoleEcho: anything not starting with '{' is kept):
 *
 *   [mem] lvgl <where>: used=N free=N largest=N peak=N boot_peak=N pool=N
 *
 *   used, free  bytes in allocated / free chunks, headers included, so their
 *               sum is the pool less the heap's own metadata -- about 30632
 *               of the 30720, and the same on every line.
 *   largest     the biggest free chunk, as the request it could hold: a
 *               ceiling, not a promise (see largest_free). A 4 KB
 *               strip (ui_boot.c, ui_anim.c) needs 4096 contiguous bytes, and
 *               free= says nothing about whether those exist.
 *   peak        the most that was allocated since the PREVIOUS [mem] line,
 *               so each line covers the stretch of screen time just before
 *               it, drawing included. Each call starts a new stretch.
 *   boot_peak   the most that was ever allocated since boot.
 *
 * Main thread only: every caller runs where LVGL does.
 *
 * Off (CONFIG_SYS_HEAP_RUNTIME_STATS unset) these compile to nothing.
 */
#ifdef CONFIG_SYS_HEAP_RUNTIME_STATS
void mem_stats_report(const char *where);

/* Called every pass of the USB mode loop. Prints "gauges" once per boot, the
 * first time usage has been on the gauge screen for two seconds -- long enough
 * for it to have been drawn, which a print on arrival would not be. */
void mem_stats_gauges_tick(bool have_data);
#else
static inline void mem_stats_report(const char *where)
{
	(void)where;
}

static inline void mem_stats_gauges_tick(bool have_data)
{
	(void)have_data;
}
#endif

#endif /* MEM_STATS_H */
