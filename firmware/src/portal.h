#ifndef PORTAL_H
#define PORTAL_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * Two-phase setup portal. Each phase is its own tiny HTTP/1.1 server; the
 * two never run at once, because the radio never runs AP and STA at once.
 *
 * Phase 1 (SoftAP + captive DNS):
 *   GET  /      -> WiFi form (scanned networks + password)
 *   POST /wifi  -> credentials are RETURNED to the caller after acking the
 *                  browser ("watch the device screen"); the caller tears the
 *                  AP down and attempts the join itself.
 *   other       -> 302 to /   (captive-portal probes)
 *
 * Phase 2 (home LAN, no DNS games):
 *   GET  /      -> sign-in page: a real link to the authorize URL (the phone
 *                  has internet here) and a paste box for the code
 *   POST /token -> sign_in() blocks (SNTP + PKCE exchange); success serves
 *                  the done page and returns 0, failure re-serves the form.
 */

/* Networks to offer, scanned BEFORE the AP comes up (scanning while the
 * SoftAP runs sees only a fraction). */
void portal_set_networks(char list[][33], int n);

/* Tell the phase-1 ack page what happens after the join. A device that
 * already holds a sign-in token skips phase 2 entirely (that is the "sign in
 * once" promise), and telling its user to expect a sign-in QR -- as the
 * fresh-device copy does -- reads like a malfunction when none appears. */
void portal_set_resume(bool token_already_stored);

/* Open the wifi phase's listening socket ahead of time (binds INADDR_ANY, so
 * it works before the AP exists). Call before net_wifi_start_ap() so the
 * first captive-portal probe a joining phone fires finds a live portal;
 * portal_run_wifi() adopts the socket. Returns 0 or -errno. */
int portal_preopen(void);

/* Close a pre-opened socket that portal_run_wifi() will not be adopting
 * (e.g. the AP failed to start). Safe to call when nothing is open. */
void portal_preclose(void);

/* Serve the WiFi form until credentials are POSTed. Fills ssid/psk and
 * returns 0 after acking the browser; -ETIMEDOUT if nobody submits in time.
 * err_msg (may be NULL) is shown on the form -- the previous attempt's
 * failure reason. */
int portal_run_wifi(char *ssid, size_t ssid_len, char *psk, size_t psk_len,
		    const char *err_msg, int timeout_s);

/* The provisioning phone's UTC offset (minutes east), captured with the WiFi
 * form. The phone is the one party that reliably knows the local timezone --
 * the board's own HTTP lookup dies on captive-ish networks. False until a
 * form carrying one has been submitted. */
bool portal_last_tzmin(int32_t *out);

/* Serve the sign-in page. sign_in_start(code) must return quickly and kick
 * the exchange off in the background; sign_in_poll() reports it (<0 still
 * running, 0 success, >0 failure). The server answers /status polls the
 * whole time -- that is how every open copy of the page, not just the tab
 * that POSTed, learns the outcome. Returns 0 after a success has been
 * broadcast, -ETIMEDOUT if the timeout elapses. */
int portal_run_signin(const char *authorize_url,
		      void (*sign_in_start)(const char *code),
		      int (*sign_in_poll)(void), int timeout_s);

#endif /* PORTAL_H */
