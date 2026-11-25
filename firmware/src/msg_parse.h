#ifndef MSG_PARSE_H
#define MSG_PARSE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include "fmt.h"	/* enum fmt_srow_kind, FMT_SNAME_MAX: no function of it */

/* Extract the string value for `key` from a flat JSON line into buf.
 * Returns true if found. */
bool msg_get_str(const char *json, const char *key, char *buf, size_t buflen);

/* Extract a numeric value for `key`. Returns true if found (sets *out). */
bool msg_get_double(const char *json, const char *key, double *out);

/* Extract a JSON boolean (`true`/`false`) for `key`. Returns true if found
 * (sets *out). A quoted "true" is NOT a boolean and returns false, the same
 * way msg_get_double refuses a quoted number: the daemon emits real JSON
 * booleans, and accepting the string form would let a typo read as valid. */
bool msg_get_bool(const char *json, const char *key, bool *out);

/* The raw text just after `"key":`, for values the getters above do not
 * cover -- arrays, mostly. NULL if `key` is not present as a key. The caller
 * parses from there and must stop at the end of its own value. */
const char *msg_find_value(const char *json, const char *key);

/*
 * One row of the sessions card, off one `srow` line
 * (docs/sessions-card-design.md, "The wire"):
 *
 *   {"t":"srow","v":1,"i":0,"n":4,"tot":6,"pg":0,"pv":"c","k":"wait",
 *    "age":123,"nm":"Blink","cx":42}
 *
 * The kind's enum is fmt.h's (included above for it alone), so a file that
 * includes this header stays free of LVGL and links with fmt.c or without.
 */
struct msg_srow {
	int i;			/* the row on this page, 0..n-1; 0 when n is 0 */
	int n;			/* rows on this page, 0..4; 0 is an empty list */
	int tot;		/* every live session, 0..999 */
	int pg;			/* the page, 0..249 */
	char pv;		/* 'c' Claude, 'x' Codex; 0 when n is 0 */
	enum fmt_srow_kind k;
	int32_t age;		/* seconds, 0..FMT_SAGE_MAX */
	char nm[FMT_SNAME_MAX];	/* "" when the daemon sent none */
	int cx;			/* context used, 0..100, or -1 for unknown */
};

/*
 * Parse one srow line into `out`, whole or not at all: false leaves the
 * table as it was, the theme and burn lines' rule. The caller has already
 * matched "t". `v` is not read (the daemon sends 2). Refused: a count, index or page that is
 * not a whole number in range (`i` must be under `n`, `n` no more than
 * `tot`), and with a row present an unknown `pv` or `k` or a missing or
 * negative `age` -- a row whose state this firmware cannot name must not
 * be drawn as one it can. Held rather than refused, because the row is
 * still right without them: an `age` past FMT_SAGE_MAX (held at it), a
 * `cx` over 100 (held at 100), and a `nm` over 24 bytes (cut at 24). A
 * `cx` that is not a number from 0 is unknown, -1, and draws no bar -- an
 * unknown is never shown as 0%. Nothing of the line is logged, here or by
 * the caller.
 */
bool msg_parse_srow(const char *json, struct msg_srow *out);

#endif /* MSG_PARSE_H */
