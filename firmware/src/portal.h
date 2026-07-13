#ifndef PORTAL_H
#define PORTAL_H

struct portal_result {
	char ssid[33];
	char psk[65];
	char code[256];	/* the pasted OAuth code, "<code>#<state>" */
};

/* Networks to offer in the form. Scanned BEFORE the AP comes up: scanning while
 * the SoftAP is running only sees a fraction of what is out there.
 */
void portal_set_networks(char list[][33], int n);

/*
 * Serve the setup page on the SoftAP until the user submits it.
 * Returns 0 and fills `out` on submit, or -ETIMEDOUT.
 */
/* Number of TCP connections accepted so far. Lets the setup screen show whether
 * the phone's HTTP request actually reaches the board -- diagnosable without a
 * serial cable, which would reset this board.
 */
int portal_conn_count(void);

/* Bytes received / sent on the most recent connection. Screen-visible so the
 * request/response exchange can be diagnosed without a serial cable. */
int portal_last_rx(void);
int portal_last_tx(void);

int portal_run(const char *authorize_url, struct portal_result *out, int timeout_s);

#endif /* PORTAL_H */
