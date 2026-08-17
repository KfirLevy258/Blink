#ifndef UI_SETTINGS_H
#define UI_SETTINGS_H

#include <lvgl.h>

/* Make `scr` (the gauge screen) open the settings panel on a left swipe.
 * The panel is an overlay on the same screen, NOT a separate LVGL screen:
 * the 16 KiB LVGL pool cannot hold two full screens (see usage_view_deinit).
 * All three actions (Reset WiFi / Re-sign-in / Factory reset) confirm first,
 * then persist the change and reboot. */
void ui_settings_attach(lv_obj_t *scr);

/* One-line notice popup on the gauge screen (update outcome etc.). Safe to
 * call once the screen exists; replaces any previous notice. */
void ui_settings_notice(const char *txt);

/* Run any pending open/close transition. MUST be called from a mode loop in
 * thread context, not from an LVGL callback: the transition drives
 * lv_refr_now() itself (see ui_slide.h). `pump` (may be NULL) runs each step
 * so background duties and the boot watchdog stay alive. */
void ui_settings_service(void (*pump)(void));

#endif /* UI_SETTINGS_H */
