/* Host stub: the harness's stdout is its wire, so console text goes to
 * stderr, where the board_touch.py side never reads it. */
#ifndef HOST_TOUCH_PRINTK_H
#define HOST_TOUCH_PRINTK_H
#include <stdio.h>
#define printk(...) fprintf(stderr, __VA_ARGS__)
#endif
