#ifndef OAUTH_H
#define OAUTH_H

#include <stddef.h>
#include <stdbool.h>

/*
 * OAuth (Authorization Code + PKCE) for the Claude Code OAuth client, ported
 * faithfully from the web app's auth.js. The board runs its OWN login and holds
 * its OWN refresh token, so it never touches Claude Code's credentials.
 */

#define OAUTH_VERIFIER_LEN 64	/* base64url of 32 bytes = 43 chars + NUL slack */
#define OAUTH_CHALLENGE_LEN 64
#define OAUTH_URL_LEN 512
#define OAUTH_TOKEN_LEN 320

struct oauth_tokens {
	char access[OAUTH_TOKEN_LEN];
	char refresh[OAUTH_TOKEN_LEN];
	int expires_in;			/* seconds */
};

/* base64url without padding. Returns bytes written (excl. NUL), or -1. Pure;
 * host-tested against known vectors. */
int oauth_base64url(const unsigned char *in, size_t inlen, char *out, size_t outlen);

/* PKCE challenge = base64url(SHA256(verifier)). Pure given the verifier;
 * verified on-device against the RFC 7636 vector. */
int oauth_pkce_challenge(const char *verifier, char *out, size_t outlen);

/* Fresh random verifier (base64url of 32 entropy bytes). */
int oauth_gen_verifier(char *out, size_t outlen);

/* The authorize URL the user opens. `state` equals the verifier, per the flow. */
int oauth_authorize_url(const char *verifier, char *out, size_t outlen);

/* Exchange the pasted code ("<code>#<state>") for tokens. TLS POST to the token
 * endpoint. Returns 0 on success. */
int oauth_exchange_code(const char *pasted_code, const char *verifier,
			struct oauth_tokens *out);

/* Refresh using a stored refresh token. If the endpoint returns no new refresh
 * token, the caller keeps the old one (as auth.js does). Returns 0 on success,
 * or a negative errno; a rejected refresh token surfaces as -EACCES. */
int oauth_refresh(const char *refresh_token, struct oauth_tokens *out);

#endif /* OAUTH_H */
