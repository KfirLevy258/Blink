#ifndef PORTAL_H
#define PORTAL_H

/*
 * Two-stage setup portal on the SoftAP.
 *
 *   GET  /       -> WiFi page, or (once WiFi is up) the sign-in page
 *   POST /wifi   -> {ssid,psk}: the board connects (AP stays up via APSTA), then
 *                   the sign-in page is served -- so each POST blocks until its
 *                   step finishes and returns the next page. No JS polling.
 *   POST /token  -> {code}: the board exchanges the OAuth code, then the done
 *                   page is served and portal_run returns.
 *
 * The callbacks let main.c own the actual WiFi-connect and OAuth-exchange while
 * the portal owns the HTTP.
 */

struct portal_cb {
	const char *authorize_url;			/* PKCE authorize URL for the link */
	int (*connect_wifi)(const char *ssid, const char *psk);	/* 0 = connected */
	int (*sign_in)(const char *code);			/* 0 = signed in */
};

/* Networks to offer, scanned BEFORE the AP comes up (scanning while the SoftAP
 * runs sees only a fraction). */
void portal_set_networks(char list[][33], int n);

/* Serve until sign-in completes (returns 0) or the timeout elapses
 * (-ETIMEDOUT). */
int portal_run(const struct portal_cb *cb, int timeout_s);

/* Stations currently joined -- surfaced on the setup screen. */
int portal_conn_count(void);

#endif /* PORTAL_H */
