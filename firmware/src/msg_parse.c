#include <string.h>
#include <stdlib.h>
#include <stdio.h>
#include "msg_parse.h"

/*
 * Find `"key"` used AS A KEY, and return the pointer just after its colon.
 *
 * The "as a key" part is the whole of it. This used to take the first
 * occurrence of `"key"` anywhere in the document and then scan forward for
 * the next colon -- which reads a VALUE as a key whenever some earlier value
 * happens to spell the same word, and then attaches a completely different
 * field's colon to it.
 *
 * That is not hypothetical. `{"t":"edition","v":2,"edition":"codex"}` --
 * a message whose type has the same name as one of its fields, which is the
 * natural way to name such a message -- resolved to the literal string
 * "edition", because the match landed on the value of "t" and the next colon
 * belonged to "v". The board reported `unknown edition 'edition'` and the
 * provisioning step silently did nothing.
 *
 * So: a match only counts when the very next non-space character is the
 * colon. Anything else and the search continues past it. Every existing
 * message happens to be safe today (no type shares a name with a key of the
 * same message), which is exactly why this was never noticed.
 */
static const char *after_colon(const char *json, const char *key)
{
	char pat[48];
	snprintf(pat, sizeof(pat), "\"%s\"", key);
	size_t plen = strlen(pat);

	for (const char *k = strstr(json, pat); k != NULL;
	     k = strstr(k + 1, pat)) {
		const char *p = k + plen;

		while (*p == ' ' || *p == '\t') {
			p++;
		}
		if (*p == ':') {
			return p + 1;
		}
	}
	return NULL;
}

const char *msg_find_value(const char *json, const char *key)
{
	return after_colon(json, key);
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


/* A whole number in [lo, hi] for `key`, or false. NaN fails both bounds,
 * and the cast happens only once the value is known to fit. */
static bool get_whole(const char *json, const char *key, double lo, double hi,
		      int *out)
{
	double v;

	if (!msg_get_double(json, key, &v) || !(v >= lo && v <= hi) ||
	    v != (double)(int)v) {
		return false;
	}
	*out = (int)v;
	return true;
}

static bool srow_kind(const char *s, enum fmt_srow_kind *out)
{
	static const struct {
		const char *word;
		enum fmt_srow_kind k;
	} kinds[] = {
		{ "wait", FMT_SROW_WAIT }, { "fail", FMT_SROW_FAIL },
		{ "edit", FMT_SROW_EDIT }, { "tool", FMT_SROW_TOOL },
		{ "work", FMT_SROW_WORK }, { "idle", FMT_SROW_IDLE },
	};

	for (size_t i = 0; i < sizeof(kinds) / sizeof(kinds[0]); i++) {
		if (strcmp(s, kinds[i].word) == 0) {
			*out = kinds[i].k;
			return true;
		}
	}
	return false;
}

bool msg_parse_srow(const char *json, struct msg_srow *out)
{
	struct msg_srow r;
	double d;
	/* Wider than any word this firmware knows, so a newer daemon's
	 * "waiting" is refused rather than cut to a "wait" it never meant --
	 * the svc line's rule (proto.c). */
	char word[12];

	if (json == NULL || out == NULL) {
		return false;
	}
	memset(&r, 0, sizeof(r));
	r.cx = -1;
	/* `v` is not read. The daemon stamps every line with PROTO_VERSION
	 * (2, pc/protocol.srow), the design's example showed 1, and a parser
	 * that insisted on either would refuse every row the other sends: the
	 * card would sit empty with the daemon's log saying it was sent. The
	 * protocol's rule (pc/version.py) is additive keys, and a version is
	 * a floor, not a format selector; the board reads `v` only on welcome. */
	if (!get_whole(json, "n", 0, FMT_SROWS_PER_PAGE, &r.n) ||
	    !get_whole(json, "tot", 0, FMT_STOT_MAX, &r.tot) ||
	    !get_whole(json, "pg", 0, FMT_SPAGE_MAX, &r.pg) ||
	    r.n > r.tot) {
		return false;
	}
	if (r.n == 0) {
		/* The empty list: no row, and nothing else on the line is
		 * read. */
		*out = r;
		return true;
	}
	if (!get_whole(json, "i", 0, r.n - 1, &r.i)) {
		return false;
	}
	if (!msg_get_str(json, "pv", word, sizeof(word)) ||
	    (strcmp(word, "c") != 0 && strcmp(word, "x") != 0)) {
		return false;
	}
	r.pv = word[0];
	if (!msg_get_str(json, "k", word, sizeof(word)) ||
	    !srow_kind(word, &r.k)) {
		return false;
	}
	/* Whole seconds from 0. Past the cap is a session that has sat for
	 * more than four days: held at 99h 59m, not refused. */
	if (!msg_get_double(json, "age", &d) || !(d >= 0)) {
		return false;
	}
	r.age = d > FMT_SAGE_MAX ? FMT_SAGE_MAX : (int32_t)d;
	if (!msg_get_str(json, "nm", r.nm, sizeof(r.nm))) {
		r.nm[0] = '\0';
	}
	/* Unknown is absent, never zero: a cx this cannot read stays -1, and
	 * the card draws no bar for it rather than an empty one. */
	if (msg_get_double(json, "cx", &d) && d >= 0) {
		r.cx = d > 100 ? 100 : (int)(d + 0.5);
	}
	*out = r;
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
