#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "ota_parse.h"

/* Find "key":<value-start> inside buf[0..len). Returns pointer past the
 * colon (and past an opening quote when the value is a string), or NULL. */
static const char *find_val(const char *buf, size_t len, const char *key,
			    bool string_val)
{
	char pat[24];
	int n = snprintf(pat, sizeof(pat), "\"%s\":", key);
	const char *end = buf + len;

	for (const char *p = buf; p + n < end; p++) {
		if (memcmp(p, pat, n) == 0) {
			p += n;
			if (string_val) {
				if (p >= end || *p != '"') {
					return NULL;
				}
				p++;
			}
			return p < end ? p : NULL;
		}
	}
	return NULL;
}

int ota_parse_manifest(const char *buf, size_t len, struct ota_manifest *out)
{
	memset(out, 0, sizeof(*out));

	const char *v = find_val(buf, len, "version", true);
	const char *s = find_val(buf, len, "size", false);
	const char *h = find_val(buf, len, "sha256", true);

	if (!v || !s || !h) {
		return -1;
	}

	size_t i = 0;

	while (v < buf + len && *v != '"' && i < sizeof(out->version) - 1) {
		out->version[i++] = *v++;
	}
	if (i == 0 || (v < buf + len && *v != '"')) {
		return -1;
	}

	out->size = (uint32_t)strtoul(s, NULL, 10);
	if (out->size == 0) {
		return -1;
	}

	i = 0;
	while (h < buf + len && *h != '"' && i < 64) {
		char c = *h++;

		if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') ||
		      (c >= 'A' && c <= 'F'))) {
			return -1;
		}
		out->sha256[i++] = (c >= 'A' && c <= 'F') ? c + 32 : c;
	}
	if (i != 64 || h >= buf + len || *h != '"') {
		return -1;
	}
	return 0;
}

/* Parse "M.m.p"; returns false on malformed input. */
static bool ver3(const char *s, long v[3])
{
	char *end;

	for (int i = 0; i < 3; i++) {
		v[i] = strtol(s, &end, 10);
		if (end == s || v[i] < 0) {
			return false;
		}
		if (i < 2) {
			if (*end != '.') {
				return false;
			}
			s = end + 1;
		}
	}
	return true;
}

bool ota_version_newer(const char *cand, const char *cur)
{
	long a[3], b[3];

	if (!ver3(cand, a) || !ver3(cur, b)) {
		return false;	/* unparseable never installs */
	}
	for (int i = 0; i < 3; i++) {
		if (a[i] != b[i]) {
			return a[i] > b[i];
		}
	}
	return false;
}

int ota_split_url(const char *url, char *host, size_t hlen,
		  char *path, size_t plen)
{
	static const char pfx[] = "https://";

	if (strncmp(url, pfx, sizeof(pfx) - 1) != 0) {
		return -1;
	}
	url += sizeof(pfx) - 1;

	const char *slash = strchr(url, '/');

	if (!slash || (size_t)(slash - url) >= hlen ||
	    strlen(slash) >= plen) {
		return -1;
	}
	memcpy(host, url, slash - url);
	host[slash - url] = '\0';
	strcpy(path, slash);
	return 0;
}
