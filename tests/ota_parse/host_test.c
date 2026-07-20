/* Host test for the pure OTA parsing logic.
 *   cc -I firmware/src tests/ota_parse/host_test.c firmware/src/ota_parse.c -o /tmp/ota && /tmp/ota
 */
#include <stdio.h>
#include <string.h>
#include "ota_parse.h"

static int failures;
#define CK(cond, name) do{ if(cond){printf("PASS: %s\n",name);} \
	else {printf("FAIL: %s\n",name); failures++;} }while(0)

int main(void)
{
	struct ota_manifest m;
	const char *good =
	  "{\"version\":\"0.4.0\",\"size\":1268532,"
	  "\"sha256\":\"aabbccddeeff00112233445566778899aabbccddeeff00112233445566778899\"}";

	CK(ota_parse_manifest(good, strlen(good), &m) == 0, "manifest parses");
	CK(strcmp(m.version, "0.4.0") == 0, "version extracted");
	CK(m.size == 1268532, "size extracted");
	CK(strlen(m.sha256) == 64, "sha256 is 64 hex chars");
	CK(ota_parse_manifest("{\"version\":\"0.4.0\"}", 19, &m) < 0, "missing fields rejected");
	CK(ota_parse_manifest(good, 20, &m) < 0, "truncated rejected");
	const char *shortsha =
	  "{\"version\":\"0.4.0\",\"size\":5,\"sha256\":\"abc\"}";
	CK(ota_parse_manifest(shortsha, strlen(shortsha), &m) < 0, "short sha rejected");

	CK(ota_version_newer("0.4.0", "0.3.0"), "0.4.0 > 0.3.0");
	CK(ota_version_newer("1.0.0", "0.9.9"), "1.0.0 > 0.9.9");
	CK(ota_version_newer("0.3.10", "0.3.9"), "0.3.10 > 0.3.9 (numeric, not lexical)");
	CK(!ota_version_newer("0.3.0", "0.3.0"), "equal is not newer");
	CK(!ota_version_newer("0.2.9", "0.3.0"), "older is not newer");
	CK(!ota_version_newer("garbage", "0.3.0"), "unparseable is not newer");

	char host[64], path[768];

	CK(ota_split_url("https://objects.githubusercontent.com/foo/bar?tok=x",
			 host, sizeof(host), path, sizeof(path)) == 0, "url splits");
	CK(strcmp(host, "objects.githubusercontent.com") == 0, "host extracted");
	CK(strcmp(path, "/foo/bar?tok=x") == 0, "path keeps query");
	CK(ota_split_url("http://x/y", host, sizeof(host), path, sizeof(path)) < 0,
	   "plain http rejected");

	printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
	return failures ? 1 : 0;
}
