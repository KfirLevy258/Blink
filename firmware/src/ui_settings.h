#ifndef UI_SETTINGS_H
#define UI_SETTINGS_H

#include <lvgl.h>

/* Make `scr` (the gauge screen) open the settings panel on a left swipe.
 * The panel is an overlay on the same screen, NOT a separate LVGL screen:
 * the 16 KiB LVGL pool cannot hold two full screens (see usage_view_deinit).
 * All three actions (Reset WiFi / Re-sign-in / Factory reset) confirm first,
 * then persist the change and reboot. */
void ui_settings_attach(lv_obj_t *scr);

#endif /* UI_SETTINGS_H */
