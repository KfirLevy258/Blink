#ifndef UI_SETUP_H
#define UI_SETUP_H

/*
 * Full-screen setup instructions plus a QR.
 *
 * The QR is a WIFI: payload, not a link: scanning it JOINS the board's access
 * point. Combined with the captive-portal DNS, that means one scan and the
 * setup page opens by itself -- no SSID picking, no typing an IP address.
 * The Anthropic login link then lives inside that page, where the phone can
 * follow it over cellular.
 */
void ui_setup_show(void);

/* Progress/error text at the bottom of the setup screen. */
void ui_setup_status(const char *msg);

#endif /* UI_SETUP_H */
