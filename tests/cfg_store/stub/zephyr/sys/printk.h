/* Host stub: the store's log lines go to stdout, prefixed so the runner's
 * PASS/FAIL count never mistakes one for a result.
 *
 * tests/cfg_store also keeps them, to check what a boot line would have told
 * bridge.log: the stored SSID used to be printed verbatim. That test defines
 * stub_printk() and is built with -DCFG_STUB_CAPTURE; every other test that
 * borrows this stub (tests/ui_theme) gets the plain printf. */
#ifndef CFG_STUB_PRINTK_H
#define CFG_STUB_PRINTK_H
#include <stdio.h>
#ifdef CFG_STUB_CAPTURE
int stub_printk(const char *fmt, ...);
#define printk(...) stub_printk(__VA_ARGS__)
#else
#define printk(...) (printf("  log: "), printf(__VA_ARGS__))
#endif
#endif
