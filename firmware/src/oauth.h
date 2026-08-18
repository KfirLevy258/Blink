#ifndef OAUTH_H
#define OAUTH_H

#include <stddef.h>
#include <stdbool.h>

/*
 * OAuth (Authorization Code + PKCE) for the Claude Code OAuth client, ported
 * faithfully from the Claude Code CLI login. The board runs its OWN login and holds
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
 * token, the caller keeps the old one (as the CLI does). Returns 0 on success,
 * or a negative errno; a rejected refresh token surfaces as -EACCES. */
int oauth_refresh(const char *refresh_token, struct oauth_tokens *out);

/*
 * Snapshot / restore pair for the refresh token across a token call. Pure.
 * Host-tested only as helpers: host_test.c performs the snapshot-before-call
 * ordering itself rather than exercising code that decides it, so it passes
 * unchanged with the aliasing bug reintroduced. Treat the ordering below as
 * unguarded by tests until oauth_refresh grows a stubbable token_post seam.
 *
 * Callers pass out->refresh AS the refresh_token argument -- main.c's refresh
 * funnel does, for the two post-sign-in sites -- and the endpoint's reply is
 * parsed straight into *out. So by
 * the time we know whether a new refresh token came back, the old one is
 * already gone. Take the snapshot BEFORE the call; hand it back after.
 *
 * oauth_refresh() uses these itself; they are exposed so a host test can reach
 * the logic, which the TLS path around it cannot be.
 */
void oauth_refresh_snapshot(const char *refresh_token, char *keep, size_t keeplen);
void oauth_refresh_retain(const char *keep, struct oauth_tokens *out);

/*
 * Does this oauth_exchange_code/oauth_refresh return mean the STORED CREDENTIAL
 * is dead, as opposed to the network being briefly unavailable?
 *
 * Only the token endpoint answering 400/401 (-EACCES) says the credential is
 * bad. Everything else -- TLS resets, DNS, no route, 5xx, unparseable bodies --
 * is transport, and the refresh token is still perfectly good. Callers must
 * consult this before erasing it: treating any failure as a rejection logs the
 * device out for good on a momentary blip.
 */
bool oauth_creds_rejected(int rc);

/*
 * Bound a server-supplied expires_in (seconds) into something main.c can build
 * a deadline from. Rejects NaN and infinities as well as out-of-range values;
 * see the definition for why each end matters.
 */
int oauth_expires_clamp(double expires);

#endif /* OAUTH_H */
