#ifndef UI_SETUP_H
#define UI_SETUP_H

/*
 * The provisioning screen ("boarding pass"): a two-step checklist on the left
 * that ticks green as setup advances, and a QR on the right that a phone scans
 * to join. Splits WiFi and account sign-in into two visible stages.
 */

enum ui_setup_state {
	UI_SETUP_WAIT = 0,	/* waiting for a phone to join the setup network */
	UI_SETUP_PHONE,		/* phone joined; choosing WiFi in the page       */
	UI_SETUP_WIFI_OK,	/* joined home WiFi; ready to sign in            */
	UI_SETUP_SIGNIN,	/* signing in to Claude                          */
	UI_SETUP_DONE,		/* both done; handing over to the gauges         */
	UI_SETUP_ERROR,		/* something failed (detail carries the reason)  */
};

void ui_setup_show(void);

/* Advance the screen. `detail` is an optional short line (e.g. the network
 * name, or an error) or NULL. Safe to call from any context -- the change is
 * applied on the LVGL thread by ui_setup_service().
 */
void ui_setup_set_state(enum ui_setup_state state, const char *detail);

/* Pump pending state changes onto the screen. Call from the main LVGL loop. */
void ui_setup_service(void);

#endif /* UI_SETUP_H */
