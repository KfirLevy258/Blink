#ifndef USAGE_CLIENT_H
#define USAGE_CLIENT_H

#include "usage_parse.h"

/* Result of one fetch attempt. */
enum usage_result {
	USAGE_OK = 0,        /* http_status == 200, data valid */
	USAGE_RATE_LIMITED,  /* http 429 — back off */
	USAGE_UNAUTHORIZED,  /* http 401 — token expired */
	USAGE_HTTP_ERROR,    /* other non-200, or unparseable body */
	USAGE_NET_ERROR,     /* DNS/connect/TLS/socket failure */
};

/* Register the CA cert once before the first fetch. */
void usage_client_init_ca(void);

/* Fetch usage once. On USAGE_OK, fills *out; *http_status is always set. */
enum usage_result usage_client_fetch(struct usage_data *out, int *http_status);

#endif /* USAGE_CLIENT_H */
