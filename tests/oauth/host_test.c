/* Host test for the pure OAuth pieces (base64url + authorize URL).
 *   cc -DOAUTH_HOST_TEST -I firmware/src tests/oauth/host_test.c firmware/src/oauth.c -o /tmp/oa && /tmp/oa
 * The SHA-256 challenge needs mbedTLS, so it's excluded here and verified
 * on-device against the RFC 7636 vector instead. A stub is provided so the
 * URL builder links.
 */
#include <errno.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "oauth.h"

static int failures;
#define CK(cond, name) do{ if(cond){printf("PASS: %s\n",name);} \
	else {printf("FAIL: %s\n",name); failures++;} }while(0)

/* Stub: on host we don't compute SHA-256; return a fixed marker so the URL
 * builder has something to place. base64url itself is what we're testing. */
int oauth_pkce_challenge(const char *verifier, char *out, size_t outlen)
{
	(void)verifier;
	snprintf(out, outlen, "CHALLENGE");
	return 9;
}

int main(void)
{
	char out[128];

	/* RFC 4648 base64url vectors (no padding). */
	oauth_base64url((const unsigned char *)"", 0, out, sizeof(out));
	CK(strcmp(out, "") == 0, "empty");
	oauth_base64url((const unsigned char *)"f", 1, out, sizeof(out));
	CK(strcmp(out, "Zg") == 0, "f -> Zg");
	oauth_base64url((const unsigned char *)"fo", 2, out, sizeof(out));
	CK(strcmp(out, "Zm8") == 0, "fo -> Zm8");
	oauth_base64url((const unsigned char *)"foo", 3, out, sizeof(out));
	CK(strcmp(out, "Zm9v") == 0, "foo -> Zm9v");
	oauth_base64url((const unsigned char *)"foobar", 6, out, sizeof(out));
	CK(strcmp(out, "Zm9vYmFy") == 0, "foobar -> Zm9vYmFy");

	/* url-safe alphabet: bytes 0xFB 0xFF -> "-_" region. 0x03FBFF... check '-'/'_' appear */
	unsigned char b[] = { 0xfb, 0xff, 0xbf };
	oauth_base64url(b, 3, out, sizeof(out));
	CK(strchr(out, '-') || strchr(out, '_'), "url-safe chars present");
	CK(!strchr(out, '+') && !strchr(out, '/') && !strchr(out, '='), "no +/= chars");

	/* authorize URL contains the required params. */
	char url[OAUTH_URL_LEN];
	oauth_authorize_url("VERIF123", url, sizeof(url));
	CK(strstr(url, "response_type=code") != NULL, "url has response_type");
	CK(strstr(url, "code_challenge_method=S256") != NULL, "url has S256");
	/* Security invariant: the verifier must NEVER appear in the authorize
	 * URL (it is served over plain HTTP on the LAN). state carries the
	 * public challenge instead. */
	CK(strstr(url, "VERIF123") == NULL, "verifier absent from authorize URL");
	CK(strstr(url, "state=CHALLENGE") != NULL, "state == challenge (the hash)");
	CK(strstr(url, "code_challenge=CHALLENGE") != NULL, "challenge present");
	CK(strstr(url, "9d1c250a") != NULL, "url has client_id");
	CK(strstr(url, "scope=org%3Acreate_api_key") != NULL, "url scope encoded");

	/*
	 * Which failures invalidate the stored refresh token.
	 *
	 * Regression guard for a device that logged itself out on any network
	 * blip: main.c treated every non-zero oauth_refresh() return as "the
	 * credential was rejected", wiped it, and rebooted into provisioning.
	 * A TLS reset mid-handshake (-ECONNRESET, seen on hardware 2026-07-26)
	 * says nothing at all about the credential -- only the token endpoint
	 * answering 400/401 does, and oauth.c already reports that as -EACCES.
	 */
	CK(oauth_creds_rejected(-EACCES), "400/401 rejects the credential");
	CK(!oauth_creds_rejected(-ECONNRESET), "TLS reset keeps the token");
	CK(!oauth_creds_rejected(-ENETUNREACH), "no route keeps the token");
	CK(!oauth_creds_rejected(-EIO), "5xx/garbled response keeps the token");
	CK(!oauth_creds_rejected(-EINVAL), "unparseable body keeps the token");
	CK(!oauth_creds_rejected(0), "success is not a rejection");

	/*
	 * The stored refresh token must survive a refresh that returns no new
	 * one -- including under the aliasing every caller uses.
	 *
	 * main.c calls oauth_refresh(tok.refresh, &tok): the argument IS the
	 * output field, and token_post() blanks that field before the fallback
	 * runs. Snapshotting before the call is the whole fix; these two
	 * functions are the seam that lets a host test reach it.
	 */
	{
		struct oauth_tokens tok;
		char keep[OAUTH_TOKEN_LEN];

		memset(&tok, 0, sizeof(tok));
		strcpy(tok.refresh, "OLD-REFRESH");

		oauth_refresh_snapshot(tok.refresh, keep, sizeof(keep));
		tok.refresh[0] = '\0';	/* what token_post does on a reply with no refresh_token */
		oauth_refresh_retain(keep, &tok);
		CK(strcmp(tok.refresh, "OLD-REFRESH") == 0,
		   "omitted refresh_token keeps the stored one (aliased caller)");

		strcpy(tok.refresh, "NEW-REFRESH");
		oauth_refresh_retain(keep, &tok);
		CK(strcmp(tok.refresh, "NEW-REFRESH") == 0,
		   "rotated refresh_token is kept, not overwritten");

		oauth_refresh_snapshot("", keep, sizeof(keep));
		CK(keep[0] == '\0', "empty snapshot stays empty");
	}

	/*
	 * expires_in bounds. The NaN and infinity cases are the reason this is
	 * a host test at all: msg_get_double is a bare strtod, so a body can
	 * hand back either, every comparison against a NaN is false (so the
	 * obvious `x < MIN` form lets one reach an undefined cast), and a
	 * result at or below zero puts main.c's token_deadline in the past --
	 * a token POST every 250 ms, forever.
	 */
	{
		CK(oauth_expires_clamp(3600) == 3600, "typical 3600 passes through");
		CK(oauth_expires_clamp(28800) == 28800, "typical 28800 passes through");
		CK(oauth_expires_clamp(600) == 600, "at the floor");
		CK(oauth_expires_clamp(599) == 600, "just under the floor");
		CK(oauth_expires_clamp(0) == 600, "zero floored");
		CK(oauth_expires_clamp(-1) == 600, "negative floored");
		CK(oauth_expires_clamp(NAN) >= 600, "NaN floored, not cast");
		CK(oauth_expires_clamp(INFINITY) > 0 &&
		   oauth_expires_clamp(INFINITY) <= 7 * 24 * 3600,
		   "+inf capped");
		CK(oauth_expires_clamp(-INFINITY) == 600, "-inf floored");
		CK(oauth_expires_clamp(1e300) <= 7 * 24 * 3600, "1e300 capped");
		CK(oauth_expires_clamp(3e9) <= 7 * 24 * 3600,
		   "past INT_MAX capped, never negative");
	}

	printf("\n%s (%d failures)\n", failures ? "FAILURES" : "ALL PASSED", failures);
	return failures != 0;
}
