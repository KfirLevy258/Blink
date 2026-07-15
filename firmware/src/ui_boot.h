#ifndef UI_BOOT_H
#define UI_BOOT_H

/* Placeholder boot animation (~2.5 s, blocking). Pumps LVGL and
 * proto_service(), so it doubles as the daemon-detection window: by the time
 * it returns, proto_host_seen() answers "is a PC daemon driving us?". The
 * real animation later replaces only this function. */
void ui_boot_splash(void);

/* Delete the splash screen. Call only AFTER the next screen is loaded --
 * LVGL cannot delete the active screen. Safe to call twice. */
void ui_boot_teardown(void);

#endif /* UI_BOOT_H */
