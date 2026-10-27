#ifndef TOUCH_WIDGETS_H
#define TOUCH_WIDGETS_H

/*
 * Every control on the panel, found by walking what LVGL has, for the bench
 * (CONFIG_BLINK_TOUCH_INJECT; the wire is in touch_inject.h).
 *
 * The hand-measured targets in tools/board_touch.py cover Settings and its
 * three sub-screens. They do not cover a popup -- the update offer, a notice,
 * What's new, the install confirm, the sleep peek -- because a popup's
 * buttons are placed by flex layout and by its text, and a mirror of that in
 * Python would be a second layout engine to keep in step. This asks LVGL
 * instead: every clickable object on the active screen and the layers above
 * and below it, where it is, what it says, and whether a finger at its
 * centre would really reach it.
 *
 * "Really reach it" is LVGL's own answer. Each aim point is put through
 * lv_indev_search_obj() over the four layers in the order the pointer
 * device searches them (lv_indev.c, pointer_search_obj), and an object is
 * HIT only when that search returns the object itself. A button under a
 * notice is listed, and listed as not hit, which is the report a bench
 * needs: something is on top of it.
 *
 * No allocation of its own anywhere in here: the LVGL pool has about 3.2 KB
 * of headroom on its worst screen ([mem] lines, the theme screen), and a walk
 * that took any of it would change the screen it is measuring. Two LVGL calls
 * do more than read. lv_obj_update_layout() runs the layout the next refresh
 * would run (touch_widgets.c says why) and sends SIZE_CHANGED, CHILD_CHANGED
 * and LAYOUT_CHANGED as it does; lv_indev_search_obj() sends HIT_TEST to any
 * object with ADV_HITTEST. No handler in this firmware listens for any of
 * them. One cost of the first: a bench image lays a popup out sooner than a
 * release board would, so a "coordinates read before layout" bug elsewhere
 * can hide on the bench. The walk recurses over the object tree on the
 * caller's stack -- a few dozen bytes a level, eight levels on the deepest
 * screen -- and the line goes into the caller's buffer. Main thread only,
 * where LVGL runs.
 *
 * Compiled into the bench image (CMakeLists.txt, with touch_inject.c) and
 * into the host touch harness (tools/panel_render/touch.sh), and the parsing
 * of the two requests is here too, so the listing and the click a script
 * checks on the laptop are produced by the same code as on the board. Only
 * the stroke player differs: the board's feeds the XPT2046 input device,
 * the harness's feeds its LVGL pointer.
 */
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "touch_inject_geom.h"

/* One page of the listing as a whole wire line, no newline, into buf.
 * Returns its length, or 0 if it did not fit (it always does; see
 * touch_widgets_fmt.h). */
int tw_page_line(char *buf, size_t len, int proto_v, int page);

/* {"t":"widgets","page":P} answered into buf: tw_page_line for that page
 * (0 when absent or not a number). */
int tw_widgets_reply(const char *json, char *buf, size_t len, int proto_v);

enum tw_aim {
	TW_AIM_OK = 0,
	TW_AIM_STALE,	/* the listing it came from is not the one up now */
	TW_AIM_RANGE,	/* that listing is up, and has no such index */
	TW_AIM_DISABLED,/* listed, in LV_STATE_DISABLED: LVGL drops its input */
	TW_AIM_COVERED,	/* listed, but nothing on it is reachable */
};

/* Where to press widget `index` of the listing whose generation was `gen`.
 * TW_AIM_OK with *x, *y set, or why not. `force` presses a disabled one. */
enum tw_aim tw_aim(uint32_t gen, int index, int *x, int *y, bool force);

/*
 * {"t":"touch","op":"click","i":N,"gen":G[,"ms":HOLD][,"force":1]} as the
 * tap to play: NULL with *s filled in, or the touch_refused word (args, busy,
 * stale, range, disabled, covered). `busy` is the caller's: a transition its
 * screens have latched. The aim is found from the panel as it is now.
 */
const char *tw_click_parse(const char *json, bool busy, struct ti_stroke *s);

#endif /* TOUCH_WIDGETS_H */
