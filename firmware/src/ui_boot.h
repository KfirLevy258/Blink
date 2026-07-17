#ifndef UI_BOOT_H
#define UI_BOOT_H

#include <stdbool.h>

/* Placeholder boot animation (~2.5 s, blocking). Pumps LVGL and
 * proto_service(), so it doubles as the daemon-detection window: by the time
 * it returns, proto_host_seen() answers "is a PC daemon driving us?". The
 * real animation later replaces only this function.
 * After a reboot marked with ui_boot_mark_intentional_reboot() the same
 * screen renders static (no fade, no spinner, ~0.3 s dwell). */
void ui_boot_splash(void);

/* Call right before an on-purpose sys_reboot: the next boot skips the splash
 * animation. Power-on and crash resets still get the full animation (the
 * flag lives in noinit RAM behind a magic). */
void ui_boot_mark_intentional_reboot(void);

/* Peek (without consuming) at the intentional-reboot mark. main() uses it to
 * route mid-provisioning reboots straight to the setup screen, bypassing the
 * splash and its detect window entirely. */
bool ui_boot_intentional_pending(void);

/* Delete the splash screen. Call only AFTER the next screen is loaded --
 * LVGL cannot delete the active screen. Safe to call twice. */
void ui_boot_teardown(void);

#endif /* UI_BOOT_H */
