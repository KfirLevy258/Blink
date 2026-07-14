/*
 * OAuth PKCE for the Claude Code client -- ported from auth.js.
 *
 * The base64url encoder is pure and host-tested. The SHA-256 (PKCE challenge)
 * and the TLS token calls use mbedTLS / Zephyr sockets and run on-device; the
 * full challenge is checked against the RFC 7636 vector at boot in debug builds.
 */
#include <string.h>
#include <stdio.h>
#include "oauth.h"

#ifndef OAUTH_HOST_TEST
#include <zephyr/kernel.h>
#include <zephyr/random/random.h>
#include <zephyr/net/socket.h>
#include <zephyr/net/tls_credentials.h>
#include <zephyr/net/http/client.h>
#include <mbedtls/sha256.h>
#include "certs.h"
#include "msg_parse.h"
#endif

#define CLIENT_ID    "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
#define AUTHORIZE    "https://claude.ai/oauth/authorize"
#define REDIRECT     "https://console.anthropic.com/oauth/code/callback"
#define SCOPE        "org:create_api_key user:profile user:inference"
#define TOKEN_HOST   "console.anthropic.com"
#define TOKEN_PORT   "443"
#define TOKEN_PATH   "/v1/oauth/token"
#define CA_TAG_ISRG  2

/* --- base64url (no padding). Pure, host-tested. --- */
int oauth_base64url(const unsigned char *in, size_t inlen, char *out, size_t outlen)
{
	static const char t[] =
		"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
	size_t o = 0;

	for (size_t i = 0; i < inlen; i += 3) {
		unsigned int n = in[i] << 16;

		if (i + 1 < inlen) {
			n |= in[i + 1] << 8;
		}
		if (i + 2 < inlen) {
			n |= in[i + 2];
		}
		int chars = (i + 2 < inlen) ? 4 : (i + 1 < inlen) ? 3 : 2;

		for (int c = 0; c < chars; c++) {
			if (o + 1 >= outlen) {
				return -1;
			}
			out[o++] = t[(n >> (18 - 6 * c)) & 0x3F];
		}
	}
	out[o] = '\0';
	return (int)o;
}

/* Percent-encode for a query-string value. */
static int urlenc(const char *in, char *out, size_t outlen)
{
	static const char hex[] = "0123456789ABCDEF";
	size_t o = 0;

	for (const char *p = in; *p; p++) {
		unsigned char c = (unsigned char)*p;
		bool safe = (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
			    (c >= '0' && c <= '9') || c == '-' || c == '_' ||
			    c == '.' || c == '~';
		if (safe) {
			if (o + 1 >= outlen) {
				return -1;
			}
			out[o++] = c;
		} else {
			if (o + 3 >= outlen) {
				return -1;
			}
			out[o++] = '%';
			out[o++] = hex[c >> 4];
			out[o++] = hex[c & 0xF];
		}
	}
	out[o] = '\0';
	return (int)o;
}

int oauth_authorize_url(const char *verifier, char *out, size_t outlen)
{
	char challenge[OAUTH_CHALLENGE_LEN];
	char scope_enc[128];
	char redir_enc[128];

	if (oauth_pkce_challenge(verifier, challenge, sizeof(challenge)) < 0) {
		return -1;
	}
	urlenc(SCOPE, scope_enc, sizeof(scope_enc));
	urlenc(REDIRECT, redir_enc, sizeof(redir_enc));

	int n = snprintf(out, outlen,
		"%s?code=true&response_type=code&client_id=%s"
		"&redirect_uri=%s&scope=%s"
		"&code_challenge=%s&code_challenge_method=S256&state=%s",
		AUTHORIZE, CLIENT_ID, redir_enc, scope_enc, challenge, verifier);

	return (n > 0 && (size_t)n < outlen) ? n : -1;
}

#ifndef OAUTH_HOST_TEST

int oauth_pkce_challenge(const char *verifier, char *out, size_t outlen)
{
	unsigned char digest[32];

	if (mbedtls_sha256((const unsigned char *)verifier, strlen(verifier),
			   digest, 0) != 0) {
		return -1;
	}
	return oauth_base64url(digest, sizeof(digest), out, outlen);
}

int oauth_gen_verifier(char *out, size_t outlen)
{
	unsigned char rnd[32];

	sys_rand_get(rnd, sizeof(rnd));
	return oauth_base64url(rnd, sizeof(rnd), out, outlen);
}

/* --- TLS POST to the token endpoint, JSON body, JSON reply --- */
static uint8_t http_recv_buf[1024];
static char tok_body[1280];  /* token JSON is small */
static size_t tok_body_len;
static int tok_status;

static void tok_cb(struct http_response *rsp, enum http_final_call final, void *u)
{
	ARG_UNUSED(final); ARG_UNUSED(u);
	tok_status = rsp->http_status_code;
	if (rsp->body_frag_start && rsp->body_frag_len) {
		size_t n = rsp->body_frag_len;

		if (tok_body_len + n > sizeof(tok_body) - 1) {
			n = sizeof(tok_body) - 1 - tok_body_len;
		}
		if (n > 0) {
			memcpy(tok_body + tok_body_len, rsp->body_frag_start, n);
			tok_body_len += n;
		}
	}
}

static int token_post(const char *json, struct oauth_tokens *out)
{
	tls_credential_add(CA_TAG_ISRG, TLS_CREDENTIAL_CA_CERTIFICATE,
			   ca_cert_isrg_x1, sizeof(ca_cert_isrg_x1));

	struct zsock_addrinfo hints = { .ai_family = AF_INET, .ai_socktype = SOCK_STREAM };
	struct zsock_addrinfo *res = NULL;

	if (zsock_getaddrinfo(TOKEN_HOST, TOKEN_PORT, &hints, &res) != 0 || !res) {
		printk("[oauth] DNS lookup of %s failed\n", TOKEN_HOST);
		return -ENETUNREACH;
	}

	int sock = zsock_socket(res->ai_family, res->ai_socktype, IPPROTO_TLS_1_2);

	if (sock < 0) {
		printk("[oauth] tls socket failed: %d\n", -errno);
		zsock_freeaddrinfo(res);
		return -errno;
	}

	sec_tag_t tags[] = { CA_TAG_ISRG };

	zsock_setsockopt(sock, SOL_TLS, TLS_SEC_TAG_LIST, tags, sizeof(tags));
	zsock_setsockopt(sock, SOL_TLS, TLS_HOSTNAME, TOKEN_HOST, sizeof(TOKEN_HOST));

	int rc = zsock_connect(sock, res->ai_addr, res->ai_addrlen);

	zsock_freeaddrinfo(res);
	if (rc < 0) {
		printk("[oauth] TLS connect/handshake failed: %d\n", -errno);
		zsock_close(sock);
		return -errno;
	}

	tok_body_len = 0;
	tok_status = 0;

	const char *headers[] = { "Content-Type: application/json\r\n", NULL };
	struct http_request req = {
		.method = HTTP_POST,
		.url = TOKEN_PATH,
		.host = TOKEN_HOST,
		.protocol = "HTTP/1.1",
		.payload = json,
		.payload_len = strlen(json),
		.header_fields = headers,
		.response = tok_cb,
		.recv_buf = http_recv_buf,
		.recv_buf_len = sizeof(http_recv_buf),
	};

	rc = http_client_req(sock, &req, 15000, NULL);
	zsock_close(sock);
	if (rc < 0) {
		printk("[oauth] http request failed: %d\n", rc);
		return -EIO;
	}
	printk("[oauth] token endpoint: status %d, body %d bytes\n",
	       tok_status, (int)tok_body_len);
	if (tok_status == 400 || tok_status == 401) {
		return -EACCES;	/* code/refresh-token rejected */
	}
	if (tok_status != 200) {
		return -EIO;
	}

	tok_body[tok_body_len] = '\0';

	if (tok_body_len >= sizeof(tok_body) - 1) {
		printk("[oauth] WARNING: response truncated at %d bytes\n",
		       (int)tok_body_len);
	}

	double expires = 3600;

	if (!msg_get_str(tok_body, "access_token", out->access, sizeof(out->access))) {
		printk("[oauth] no access_token in response body\n");
		return -EINVAL;
	}
	/* Anthropic may omit a new refresh token -- caller keeps the old one. */
	if (!msg_get_str(tok_body, "refresh_token", out->refresh, sizeof(out->refresh))) {
		out->refresh[0] = '\0';
	}
	msg_get_double(tok_body, "expires_in", &expires);
	out->expires_in = (int)expires;
	return 0;
}

int oauth_exchange_code(const char *pasted, const char *verifier,
			struct oauth_tokens *out)
{
	/* The copy/paste code is "<code>#<state>"; split it, as auth.js does. */
	char code[256];
	char state[OAUTH_VERIFIER_LEN];

	strncpy(code, pasted, sizeof(code) - 1);
	code[sizeof(code) - 1] = '\0';

	char *hash = strchr(code, '#');

	if (hash) {
		*hash = '\0';
		strncpy(state, hash + 1, sizeof(state) - 1);
		state[sizeof(state) - 1] = '\0';
	} else {
		strncpy(state, verifier, sizeof(state) - 1);
		state[sizeof(state) - 1] = '\0';
	}

	static char json[640];

	snprintf(json, sizeof(json),
		"{\"grant_type\":\"authorization_code\",\"code\":\"%s\","
		"\"code_verifier\":\"%s\",\"client_id\":\"%s\","
		"\"redirect_uri\":\"%s\",\"state\":\"%s\"}",
		code, verifier, CLIENT_ID, REDIRECT, state);

	return token_post(json, out);
}

int oauth_refresh(const char *refresh_token, struct oauth_tokens *out)
{
	static char json[512];

	snprintf(json, sizeof(json),
		"{\"grant_type\":\"refresh_token\",\"refresh_token\":\"%s\","
		"\"client_id\":\"%s\"}",
		refresh_token, CLIENT_ID);

	int rc = token_post(json, out);

	/* Keep the old refresh token if the endpoint didn't rotate it. */
	if (rc == 0 && out->refresh[0] == '\0') {
		strncpy(out->refresh, refresh_token, sizeof(out->refresh) - 1);
		out->refresh[sizeof(out->refresh) - 1] = '\0';
	}
	return rc;
}

#endif /* !OAUTH_HOST_TEST */
