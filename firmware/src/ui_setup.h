#ifndef UI_SETUP_H
#define UI_SETUP_H

/*
 * The provisioning screen ("boarding pass"): a three-step checklist on the
 * left that ticks green as setup advances, and a QR panel on the right that a
 * phone scans to join. The panel QR shows the join-AP code first, then the
 * sign-in page URL once WiFi is up -- the boarding pass stays on screen for
 * the whole flow.
 */

enum ui_setup_state {
	UI_SETUP_WAIT = 0,	/* waiting for a phone to join the setup network */
	UI_SETUP_PHONE,		/* phone joined; choosing WiFi in the page       */
	UI_SETUP_REBOOT,	/* creds stored; restarting to run the join      */
	UI_SETUP_CONNECTING,	/* AP torn down; joining the home network        */
	UI_SETUP_WIFI_OK,	/* on home WiFi; detail = the sign-in page URL   */
	UI_SETUP_SIGNIN,	/* signing in to Claude                          */
	UI_SETUP_DONE,		/* both done; handing over to the gauges         */
	UI_SETUP_RESTART,	/* farewell toast right before the final reboot;
				 * detail = optional first line ("Setup complete") */
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
