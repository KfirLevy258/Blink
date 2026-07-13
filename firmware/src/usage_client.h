#ifndef USAGE_CLIENT_H
#define USAGE_CLIENT_H

#include "usage_parse.h"

enum usage_result {
	USAGE_OK = 0,        /* 200, data valid */
	USAGE_RATE_LIMITED,  /* 429 -- back off */
	USAGE_UNAUTHORIZED,  /* 401 -- token expired, refresh */
	USAGE_HTTP_ERROR,    /* other non-200 / unparseable */
	USAGE_NET_ERROR,     /* DNS / connect / TLS failure */
};

/* Fetch usage once, authenticating with `access_token`. On USAGE_OK fills *out;
 * *http_status is always set. */
enum usage_result usage_client_fetch(const char *access_token,
				     struct usage_data *out, int *http_status);

#endif /* USAGE_CLIENT_H */
