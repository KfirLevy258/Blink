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
int portal_run(const char *authorize_url, struct portal_result *out, int timeout_s);

#endif /* PORTAL_H */
