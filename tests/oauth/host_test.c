/* Host test for the pure OAuth pieces (base64url + authorize URL).
 *   cc -DOAUTH_HOST_TEST -I firmware/src tests/oauth/host_test.c firmware/src/oauth.c -o /tmp/oa && /tmp/oa
 * The SHA-256 challenge needs mbedTLS, so it's excluded here and verified
 * on-device against the RFC 7636 vector instead. A stub is provided so the
 * URL builder links.
 */
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

	printf("\n%s (%d failures)\n", failures ? "FAILURES" : "ALL PASSED", failures);
	return failures != 0;
}
