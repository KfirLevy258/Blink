/* Host stub: console text goes to stderr, so the harness's own PASS/FAIL
 * lines on stdout are the whole of what it reports. */
#ifndef HOST_CLIP_PRINTK_H
#define HOST_CLIP_PRINTK_H
#include <stdio.h>
#define printk(...) fprintf(stderr, __VA_ARGS__)
#endif
