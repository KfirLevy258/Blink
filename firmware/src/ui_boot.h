#ifndef UI_BOOT_H
#define UI_BOOT_H

#include "cfg_store.h"

/* Placeholder boot animation (~2 s, blocking). Pumps LVGL and proto_service()
 * so a PC daemon's reply is already in by selection time. The real animation
 * later replaces only this function. */
void ui_boot_splash(void);

/* Mode selection: two touch buttons plus a countdown to `fallback`.
 * - fallback == CFG_MODE_UNSET: no countdown, waits forever (first boot).
 * - Any daemon traffic auto-selects CFG_MODE_USB (zero-touch on a PC).
 * Blocking; returns the chosen mode. The boot screen stays loaded --
 * call ui_boot_teardown() only after the next screen is loaded. */
enum cfg_mode ui_boot_select(enum cfg_mode fallback, int timeout_s);

void ui_boot_teardown(void);

#endif /* UI_BOOT_H */
