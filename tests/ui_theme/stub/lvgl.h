/* Host stub: the handful of LVGL names ui_theme.c and the headers it pulls
 * in use. The timer functions are defined by tests/ui_theme/host_test.c, so
 * the test can see whether a preview timer is still alive. */
#ifndef UI_THEME_STUB_LVGL_H
#define UI_THEME_STUB_LVGL_H
#include <stdint.h>

typedef struct lv_obj lv_obj_t;
typedef struct lv_timer lv_timer_t;
typedef void (*lv_timer_cb_t)(lv_timer_t *t);

lv_timer_t *lv_timer_create(lv_timer_cb_t cb, uint32_t period, void *user);
void lv_timer_delete(lv_timer_t *t);
void lv_timer_reset(lv_timer_t *t);
void lv_timer_set_repeat_count(lv_timer_t *t, int32_t n);
#endif
