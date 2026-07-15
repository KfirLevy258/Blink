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

/* Serve the sign-in page until sign_in() returns 0 (done page served,
 * returns 0) or the timeout elapses (-ETIMEDOUT). */
int portal_run_signin(const char *authorize_url,
		      int (*sign_in)(const char *code), int timeout_s);

#endif /* PORTAL_H */
