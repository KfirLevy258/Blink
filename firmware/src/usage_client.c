#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/tls_credentials.h>
#include <zephyr/net/http/client.h>
#include <errno.h>
#include <string.h>

#include "usage_client.h"
#include "usage_parse.h"
#include "certs.h"
#include "secrets.h"

LOG_MODULE_REGISTER(usage_client, LOG_LEVEL_INF);

#define CA_TAG 1
#define HOST   "api.anthropic.com"
#define PORT   "443"
#define PATH   "/api/oauth/usage"

/* recv_buf is http_client's working buffer; body_buf accumulates the response
 * body across fragments (kept separate so the callback never aliases the
 * socket buffer http_client is actively filling). */
static uint8_t recv_buf[2048];
static char body_buf[4096];
static size_t body_len;
static int captured_status;

void usage_client_init_ca(void)
{
	int ret = tls_credential_add(CA_TAG, TLS_CREDENTIAL_CA_CERTIFICATE,
				     ca_certificate, sizeof(ca_certificate));
	if (ret != 0 && ret != -EEXIST) {
		LOG_ERR("tls_credential_add failed: %d", ret);
	}
}

static void response_cb(struct http_response *rsp,
			enum http_final_call final, void *user)
{
	ARG_UNUSED(final);
	ARG_UNUSED(user);

	captured_status = rsp->http_status_code;

	if (rsp->body_frag_start != NULL && rsp->body_frag_len > 0) {
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

enum usage_result usage_client_fetch(struct usage_data *out, int *http_status)
{
	body_len = 0;
	captured_status = 0;
	*http_status = 0;

	struct zsock_addrinfo hints = {
		.ai_family = AF_INET,
		.ai_socktype = SOCK_STREAM,
	};
	struct zsock_addrinfo *res = NULL;

	if (zsock_getaddrinfo(HOST, PORT, &hints, &res) != 0 || res == NULL) {
		LOG_ERR("DNS resolve failed");
		return USAGE_NET_ERROR;
	}

	int sock = zsock_socket(res->ai_family, res->ai_socktype, IPPROTO_TLS_1_2);

	if (sock < 0) {
		LOG_ERR("socket() failed: %d", errno);
		zsock_freeaddrinfo(res);
		return USAGE_NET_ERROR;
	}

	sec_tag_t sec_tags[] = { CA_TAG };

	zsock_setsockopt(sock, SOL_TLS, TLS_SEC_TAG_LIST, sec_tags, sizeof(sec_tags));
	zsock_setsockopt(sock, SOL_TLS, TLS_HOSTNAME, HOST, sizeof(HOST));

	if (zsock_connect(sock, res->ai_addr, res->ai_addrlen) < 0) {
		LOG_ERR("TLS connect failed (errno %d)", errno);
		zsock_close(sock);
		zsock_freeaddrinfo(res);
		return USAGE_NET_ERROR;
	}
	zsock_freeaddrinfo(res);

	static const char *headers[] = {
		"Authorization: Bearer " CLAUDE_TOKEN "\r\n",
		"anthropic-beta: oauth-2025-04-20\r\n",
		"anthropic-version: 2023-06-01\r\n",
		"User-Agent: claude-usage-display/0.1\r\n",
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

	int ret = http_client_req(sock, &req, 10000, NULL);

	zsock_close(sock);

	if (ret < 0) {
		LOG_ERR("http_client_req failed: %d", ret);
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
		LOG_ERR("failed to parse usage body (%zu bytes)", body_len);
		return USAGE_HTTP_ERROR;
	}
	return USAGE_OK;
}
