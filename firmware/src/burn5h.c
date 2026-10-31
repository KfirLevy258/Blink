/*
 * The five-hour burn view's decisions. See burn5h.h for why this file is
 * pure C, and docs/plans/burn-5h.md for the contract it reads.
 */
#include <stdio.h>
#include <string.h>

#include "burn5h.h"
#include "fmt.h"
#include "msg_parse.h"

static const char *skip_ws(const char *p)
{
	while (*p == ' ' || *p == '\t') {
		p++;
	}
	return p;
}

/*
 * A JSON integer that is non-negative and no longer than `max_digits`, at
 * *pp, and nothing else: no sign, no fraction, no exponent. Advances *pp
 * past it. msg_get_double() would take "12.5", "1e3", "inf" and "0x10" here,
 * and a bucket that is any of those is a daemon that is not speaking the
 * contract -- the right answer is to refuse the line, not to guess.
 */
static bool get_uint(const char **pp, int max_digits, uint64_t *out)
{
	const char *p = skip_ws(*pp);
	uint64_t v = 0;
	int n = 0;

	while (*p >= '0' && *p <= '9') {
		if (++n > max_digits) {
			return false;
		}
		v = v * 10 + (uint64_t)(*p - '0');
		p++;
	}
	if (n == 0) {
		return false;
	}
	*out = v;
	*pp = p;
	return true;
}

/*
 * The provider, as a plain tag: lowercase letters, digits, '_' and '-'.
 *
 * Read here rather than with msg_get_str(), which takes the next quote it
 * finds -- so `"p":7` would borrow whatever string came later on the line --
 * and which truncates to its buffer, so an over-long tag would come back as
 * a shorter one. The tag is only ever compared with the page's, but a page
 * matched by accident is a view on the wrong page.
 */
static bool get_tag(const char *json, char out[BURN5H_TAG_MAX])
{
	const char *p = msg_find_value(json, "p");
	size_t n = 0;

	if (p == NULL) {
		return false;
	}
	p = skip_ws(p);
	if (*p++ != '"') {
		return false;
	}
	for (; *p != '"'; p++) {
		bool ok = (*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') ||
			  *p == '_' || *p == '-';

		if (!ok || n + 1 >= BURN5H_TAG_MAX) {
			return false;	/* "\0" lands here too: no close quote */
		}
		out[n++] = *p;
	}
	out[n] = '\0';
	return n > 0;
}

bool burn5h_parse(const char *json, struct burn5h *out)
{
	struct burn5h m;
	const char *p;
	uint64_t v;

	memset(&m, 0, sizeof(m));
	if (!get_tag(json, m.p)) {
		return false;
	}

	/* q: a whole quarter-hour of epoch time, ten digits at most. */
	p = msg_find_value(json, "q");
	if (p == NULL || !get_uint(&p, 10, &v)) {
		return false;
	}
	p = skip_ws(p);
	if (*p != ',' && *p != '}') {
		return false;
	}
	if (v == 0 || v >= (uint64_t)BURN5H_Q_CEIL || v % BURN5H_BUCKET_S != 0) {
		return false;
	}
	m.q = (int64_t)v;

	/*
	 * b: exactly twenty, in one array. Nineteen is refused and so is
	 * twenty-one: a count that is off is a contract that moved, and drawing
	 * what arrived would put every bar under the wrong quarter-hour.
	 */
	p = msg_find_value(json, "b");
	if (p == NULL) {
		return false;
	}
	p = skip_ws(p);
	if (*p++ != '[') {
		return false;
	}
	for (int i = 0; i < BURN5H_N; i++) {
		if (!get_uint(&p, 10, &v) || v > BURN5H_BUCKET_MAX) {
			return false;
		}
		m.b[i] = (uint32_t)v;
		p = skip_ws(p);
		if (*p != (i == BURN5H_N - 1 ? ']' : ',')) {
			return false;
		}
		p++;
	}

	*out = m;
	return true;
}

static uint32_t sum(const struct burn5h *m, int from, int n)
{
	uint32_t s = 0;

	for (int i = from; i < from + n; i++) {
		s += m->b[i];
	}
	return s;
}

uint32_t burn5h_total(const struct burn5h *m)
{
	return sum(m, 0, BURN5H_N);
}

uint32_t burn5h_last_hour(const struct burn5h *m)
{
	return sum(m, BURN5H_LAST_FROM, BURN5H_HOUR_N);
}

enum burn5h_dir burn5h_direction(const struct burn5h *m)
{
	/* 64-bit for the comparison only: an hour fits in 32 bits, eleven of
	 * them do not. */
	uint64_t now = burn5h_last_hour(m);
	uint64_t before = sum(m, BURN5H_PREV_FROM, BURN5H_HOUR_N);

	if (now == 0 && before == 0) {
		return BURN5H_IDLE;
	}
	if (before == 0) {
		return BURN5H_RISING;
	}
	if (now * 10 > before * 11) {
		return BURN5H_RISING;
	}
	if (now * 10 < before * 9) {
		return BURN5H_EASING;
	}
	return BURN5H_STEADY;
}

const char *burn5h_word(enum burn5h_dir d)
{
	switch (d) {
	case BURN5H_RISING:	return "rising";
	case BURN5H_STEADY:	return "steady";
	case BURN5H_EASING:	return "easing";
	default:		return "idle";
	}
}

/*
 * Rounded to the nearest, in integers: under a thousand as it is, then a
 * tenth of the unit while that is under ten units, whole units from there.
 * The check is on the ROUNDED figure, so 9,960 becomes "10k" and not
 * "10.0k", and 999,600 becomes "1.0M" rather than "1000k" -- both of which
 * would be a fifth character. The same at every unit, now that the wire
 * carries exact tokens: it carried thousands until the review of
 * 2026-10-31, and "k" was then the smallest thing it could say.
 *
 * 64-bit inside, so whatever 32-bit count it is handed rounds without
 * wrapping. The largest total there can be, 1,999,999,980, would fit
 * either way; a count near the top of 32 bits plus half a billion for the
 * rounding would not.
 */
void burn5h_fmt_tokens(uint32_t tokens, char *buf, size_t buflen)
{
	static const struct { uint32_t per; char unit; } scale[] = {
		{ 1000u, 'k' },
		{ 1000000u, 'M' },
		{ 1000000000u, 'B' },
	};
	const uint64_t t = tokens;
	unsigned whole;

	if (t < 1000u) {
		snprintf(buf, buflen, "%u", (unsigned)t);
		return;
	}
	for (size_t i = 0; i < sizeof(scale) / sizeof(scale[0]); i++) {
		uint64_t per = scale[i].per;
		unsigned tenths = (unsigned)((t + per / 20) / (per / 10));

		if (tenths < 100) {
			snprintf(buf, buflen, "%u.%u%c", tenths / 10,
				 tenths % 10, scale[i].unit);
			return;
		}
		whole = (unsigned)((t + per / 2) / per);
		if (whole < 1000 || i + 1 == sizeof(scale) / sizeof(scale[0])) {
			snprintf(buf, buflen, "%u%c", whole, scale[i].unit);
			return;
		}
	}
}

void burn5h_fmt_rate(const struct burn5h *m, char *buf, size_t buflen)
{
	char n[BURN5H_TOKENS_MAX];

	burn5h_fmt_tokens(burn5h_last_hour(m), n, sizeof(n));
	snprintf(buf, buflen, "%s/h " FMT_BULLET " %s", n,
		 burn5h_word(burn5h_direction(m)));
}

int burn5h_bar_h(uint32_t v, uint32_t max, int tallest, int least)
{
	uint64_t h;

	if (v == 0 || max == 0 || tallest <= 0) {
		return 0;
	}
	if (v > max) {
		v = max;
	}
	h = ((uint64_t)v * (uint64_t)tallest + max / 2) / max;
	if (h < (uint64_t)least) {
		h = (uint64_t)least;
	}
	if (h > (uint64_t)tallest) {
		h = (uint64_t)tallest;
	}
	return (int)h;
}

int burn5h_hour(int64_t q, int i, int32_t off_min)
{
	int64_t t = q - (int64_t)(BURN5H_N - 1 - i) * BURN5H_BUCKET_S +
		    (int64_t)off_min * 60;
	int64_t s = t % 86400;

	if (s < 0) {
		s += 86400;	/* a western offset near the epoch */
	}
	if (s % 3600 != 0) {
		return -1;
	}
	return (int)(s / 3600);
}
