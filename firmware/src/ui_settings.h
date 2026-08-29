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
/* Take a notice down without the tap; the sleep peek does this on timeout. */
void ui_settings_notice_dismiss(void);

/* Run any pending open/close transition. MUST be called from a mode loop in
 * thread context, not from an LVGL callback: the transition drives
 * lv_refr_now() itself (see ui_slide.h). `pump` (may be NULL) runs each step
 * so background duties and the boot watchdog stay alive. */
/*
 * Discard any open/close the handlers latched but nobody serviced.
 *
 * ui_settings_attach() arms the gestures long before either mode loop exists:
 * main.c runs boot_ssid_scan() and the WiFi settle in between, which pump
 * lv_timer_handler() with touch fully live and can take a minute on a failed
 * join. A tap latched in that window would otherwise be acted on by the mode
 * loop's first iteration -- the panel sliding in by itself long after the user
 * gave up on it. Call this once before entering a mode loop; a deliberate tap
 * repeats, and from there on the latch is serviced within one iteration.
 */
void ui_settings_drop_pending(void);

void ui_settings_service(void (*pump)(void));

#endif /* UI_SETTINGS_H */
