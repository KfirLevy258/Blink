/* Host stub: the store's log lines go to stdout, prefixed so the runner's
 * PASS/FAIL count never mistakes one for a result. */
#ifndef CFG_STUB_PRINTK_H
#define CFG_STUB_PRINTK_H
#include <stdio.h>
#define printk(...) (printf("  log: "), printf(__VA_ARGS__))
#endif
