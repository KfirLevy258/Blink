#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "msg_parse.h"

/* Find `"key"` then its ':' . Returns pointer just after the colon, or NULL. */
static const char *after_colon(const char *json, const char *key)
{
	char pat[48];
	snprintf(pat, sizeof(pat), "\"%s\"", key);
	const char *k = strstr(json, pat);
	if (k == NULL) {
		return NULL;
	}
	const char *colon = strchr(k + strlen(pat), ':');
	return colon ? colon + 1 : NULL;
}

bool msg_get_str(const char *json, const char *key, char *buf, size_t buflen)
{
	const char *p = after_colon(json, key);
	if (p == NULL) {
		return false;
	}
	const char *q1 = strchr(p, '"');
	if (q1 == NULL) {
		return false;
	}
	const char *q2 = strchr(q1 + 1, '"');
	if (q2 == NULL) {
		return false;
	}
	size_t n = (size_t)(q2 - (q1 + 1));
	if (n >= buflen) {
		n = buflen - 1;
	}
	memcpy(buf, q1 + 1, n);
	buf[n] = '\0';
	return true;
}

bool msg_get_double(const char *json, const char *key, double *out)
{
	const char *p = after_colon(json, key);
	if (p == NULL) {
		return false;
	}
	while (*p == ' ' || *p == '\t') {
		p++;
	}
	if (*p == '"') {           /* a string value, not a number */
		return false;
	}
	char *end = NULL;
	double v = strtod(p, &end);
	if (end == p) {
		return false;
	}
	*out = v;
	return true;
}


bool msg_get_bool(const char *json, const char *key, bool *out)
{
	const char *p = after_colon(json, key);

	if (p == NULL) {
		return false;
	}
	while (*p == ' ' || *p == '\t') {
		p++;
	}
	if (*p == '"') {           /* a string value, not a boolean */
		return false;
	}
	if (strncmp(p, "true", 4) == 0) {
		*out = true;
		return true;
	}
	if (strncmp(p, "false", 5) == 0) {
		*out = false;
		return true;
	}
	return false;
}
