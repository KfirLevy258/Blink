/*
 * Manual scanner for the fixed /api/oauth/usage response. Chosen over
 * json_obj_parse because the payload is small and fixed, and this avoids
 * version-specific quirks in Zephyr's JSON float decoding. Field-order- and
 * whitespace-tolerant; only the three windows we care about are extracted.
 */
#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include <errno.h>
#include "usage_parse.h"

/* Locate the value object for "key" and extract utilization + resets_at.
 * Returns 0 if utilization was found (window present), -1 otherwise.
 * Matching uses the quoted key ("five_hour") so "seven_day" does not match
 * inside "seven_day_sonnet"/"seven_day_opus". A null value (e.g. opus) yields -1. */
static int parse_named_window(const char *json, const char *key,
			      struct usage_window *w)
{
	w->present = false;
	w->utilization = 0.0;
	w->resets_at[0] = '\0';

	char pat[48];
	snprintf(pat, sizeof(pat), "\"%s\"", key);

	const char *k = strstr(json, pat);
	if (k == NULL) {
		return -1;
	}

	const char *colon = strchr(k, ':');
	if (colon == NULL) {
		return -1;
	}

	const char *p = colon + 1;
	while (*p == ' ' || *p == '\t' || *p == '\n' || *p == '\r') {
		p++;
	}
	if (*p != '{') {
		return -1; /* null or non-object value */
	}

	const char *end = strchr(p, '}');
	if (end == NULL) {
		return -1;
	}

	const char *u = strstr(p, "\"utilization\"");
	if (u != NULL && u < end) {
		const char *uc = strchr(u, ':');
		if (uc != NULL && uc < end) {
			w->utilization = strtod(uc + 1, NULL);
			w->present = true;
		}
	}

	const char *r = strstr(p, "\"resets_at\"");
	if (r != NULL && r < end) {
		const char *rc = strchr(r, ':');
		if (rc != NULL && rc < end) {
			const char *vq = strchr(rc, '"');
			if (vq != NULL && vq < end) {
				const char *vend = strchr(vq + 1, '"');
				if (vend != NULL && vend <= end) {
					size_t n = (size_t)(vend - (vq + 1));
					if (n >= sizeof(w->resets_at)) {
						n = sizeof(w->resets_at) - 1;
					}
					memcpy(w->resets_at, vq + 1, n);
					w->resets_at[n] = '\0';
				}
			}
		}
	}

	return w->present ? 0 : -1;
}

int usage_parse(const char *json, size_t len, struct usage_data *out)
{
	/* strstr/strchr need a NUL terminator; copy into a bounded scratch. */
	static char scratch[4096];
	if (len >= sizeof(scratch)) {
		return -E2BIG;
	}
	memcpy(scratch, json, len);
	scratch[len] = '\0';

	memset(out, 0, sizeof(*out));
	parse_named_window(scratch, "five_hour", &out->five_hour);
	parse_named_window(scratch, "seven_day", &out->seven_day);
	parse_named_window(scratch, "seven_day_fable", &out->seven_day_fable);
	parse_named_window(scratch, "seven_day_sonnet", &out->seven_day_sonnet);
	parse_named_window(scratch, "seven_day_opus", &out->seven_day_opus);

	if (!out->five_hour.present || !out->seven_day.present) {
		return -ENODATA;
	}
	return 0;
}
