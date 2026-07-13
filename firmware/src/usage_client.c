/*
 * TLS GET https://api.anthropic.com/api/oauth/usage with a Bearer token.
 * Adapted from the pre-strip WiFi-era client; the token is now passed in at
 * runtime (from the board's own OAuth) rather than baked in at build time.
 *
 * The User-Agent MUST look like claude-code/<version>; without it the endpoint
 * drops into an aggressively rate-limited bucket (see usage.js).
 */
#include <zephyr/kernel.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/tls_credentials.h>
#include <zephyr/net/http/client.h>
#include <errno.h>
#include <string.h>
#include <stdio.h>

#include "usage_client.h"
#include "usage_parse.h"
#include "certs.h"

#define CA_TAG_GTS 1
#define HOST "api.anthropic.com"
#define PORT "443"
#define PATH "/api/oauth/usage"

static uint8_t recv_buf[1536];
static char body_buf[2048];  /* usage JSON is < 2 KB */
static size_t body_len;
static int captured_status;

static void response_cb(struct http_response *rsp, enum http_final_call final, void *u)
{
	ARG_UNUSED(final); ARG_UNUSED(u);
	captured_status = rsp->http_status_code;
	if (rsp->body_frag_start && rsp->body_frag_len) {
		size_t n = rsp->body_frag_len;

		if (body_len + n > sizeof(body_buf) - 1) {
			n = sizeof(body_buf) - 1 - body_len;
		}
		if (n > 0) {
			memcpy(body_buf + body_len, rsp->body_frag_start, n);
			body_len += n;
		}
	}
}

enum usage_result usage_client_fetch(const char *access_token,
				     struct usage_data *out, int *http_status)
{
	body_len = 0;
	captured_status = 0;
	*http_status = 0;

	tls_credential_add(CA_TAG_GTS, TLS_CREDENTIAL_CA_CERTIFICATE,
			   ca_cert_gts_r4, sizeof(ca_cert_gts_r4));

	struct zsock_addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM };
	struct zsock_addrinfo *res = NULL;

	if (zsock_getaddrinfo(HOST, PORT, &hints, &res) != 0 || !res) {
		return USAGE_NET_ERROR;
	}

	int sock = zsock_socket(res->ai_family, res->ai_socktype, IPPROTO_TLS_1_2);

	if (sock < 0) {
		zsock_freeaddrinfo(res);
		return USAGE_NET_ERROR;
	}

	sec_tag_t tags[] = { CA_TAG_GTS };

	zsock_setsockopt(sock, SOL_TLS, TLS_SEC_TAG_LIST, tags, sizeof(tags));
	zsock_setsockopt(sock, SOL_TLS, TLS_HOSTNAME, HOST, sizeof(HOST));

	int rc = zsock_connect(sock, res->ai_addr, res->ai_addrlen);

	zsock_freeaddrinfo(res);
	if (rc < 0) {
		zsock_close(sock);
		return USAGE_NET_ERROR;
	}

	static char auth[384];

	snprintf(auth, sizeof(auth), "Authorization: Bearer %s\r\n", access_token);

	const char *headers[] = {
		auth,
		"anthropic-beta: oauth-2025-04-20\r\n",
		"anthropic-version: 2023-06-01\r\n",
		"User-Agent: claude-code/2.1.168\r\n",
		NULL,
	};

	struct http_request req = {
		.method = HTTP_GET,
		.url = PATH,
		.host = HOST,
		.protocol = "HTTP/1.1",
		.header_fields = headers,
		.response = response_cb,
		.recv_buf = recv_buf,
		.recv_buf_len = sizeof(recv_buf),
	};

	rc = http_client_req(sock, &req, 12000, NULL);
	zsock_close(sock);
	if (rc < 0) {
		return USAGE_NET_ERROR;
	}

	*http_status = captured_status;
	if (captured_status == 429) {
		return USAGE_RATE_LIMITED;
	}
	if (captured_status == 401) {
		return USAGE_UNAUTHORIZED;
	}
	if (captured_status != 200) {
		return USAGE_HTTP_ERROR;
	}
	if (usage_parse(body_buf, body_len, out) < 0) {
		return USAGE_HTTP_ERROR;
	}
	return USAGE_OK;
}
