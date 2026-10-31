#ifndef BURN5H_H
#define BURN5H_H

/*
 * The five-hour burn view: what an account with no session window sees in
 * the session gauge's place. Contract: docs/plans/burn-5h.md.
 *
 * Everything here is a decision that needs no screen -- reading the `burn`
 * line, the total, which way it is heading, how tall a bar is, which buckets
 * start an hour -- and it is pure C, no LVGL and no Zephyr, for the reason
 * fmt.c and usage_state.c are: usage_view.c needs both and cannot be compiled
 * by a host test, so a decision left in there is one nothing checks.
 * usage_view.c only positions and colours what these return.
 *
 * NOT the burn RATE that usage_view_set_burn() draws. That one is percent of
 * the session window per hour, for Claude Desktop with no reset time; this
 * one is output tokens, for an account that has no session window at all.
 * Two meanings of "burn", so this file says "burn5h" throughout.
 */

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "usage_view.h"

/* Twenty buckets of fifteen minutes: the trailing five hours. */
#define BURN5H_N		20
#define BURN5H_BUCKET_S		900
/* Four buckets make an hour: the newest four are the bars drawn brighter. */
#define BURN5H_HOUR_N		4

/*
 * The hours the direction and the rate are worked out from, by their first
 * bucket: complete quarter-hours only. The newest bucket (19) is the one the
 * daemon's clock is in, still filling, so it is drawn but never compared.
 * The last hour is buckets 15-18 and the hour before it 11-14.
 *
 * It used to be 16-19 against 12-15, and the review of 2026-10-31 measured
 * what that cost: a quarter-hour that has run for m minutes holds m/15 of a
 * full one, so a desk writing at an even pace read "easing" for the first
 * 9 minutes of every quarter-hour -- 45k plus 1k a minute against 60k is
 * under 54k until minute 9.
 */
#define BURN5H_LAST_FROM	(BURN5H_N - 1 - BURN5H_HOUR_N)
#define BURN5H_PREV_FROM	(BURN5H_LAST_FROM - BURN5H_HOUR_N)

/*
 * The provider tag, NUL included: the size of the page tags in usage_view.c,
 * which is what it is compared against, so it is their figure and not a
 * copy of it. It was 12 beside a `provider` read into 16, so an id of 12 to
 * 15 characters had every burn line refused while its page was named; see
 * USAGE_TAG_MAX for what an id longer than that gets.
 */
#define BURN5H_TAG_MAX		USAGE_TAG_MAX

/*
 * One bucket's ceiling, in output tokens: a hundred million in fifteen
 * minutes, more than a hundred times the heaviest quarter-hour the dev
 * desk has written (under a million). pc/protocol.py caps what it sends at
 * the same figure (BURN_MAX), so a value past it is not a reading but a
 * misparse on one side or the other, and the line it rides on is refused
 * whole.
 *
 * It also bounds the arithmetic. Twenty of these sum to 1,999,999,980, so
 * the total and an hour fit 32 bits unsigned; what does not -- an hour
 * times eleven, a bucket times a bar's height in pixels -- is worked in
 * 64 bits where it is worked.
 */
#define BURN5H_BUCKET_MAX	99999999u

/*
 * `q` is a quarter-hour of epoch time, and nothing outside the window the
 * `time` handler in proto.c accepts: after 1970, before 2100.
 */
#define BURN5H_Q_CEIL		4102444800LL

struct burn5h {
	char p[BURN5H_TAG_MAX];		/* whose page this belongs to */
	int64_t q;			/* epoch s: the NEWEST bucket's start */
	uint32_t b[BURN5H_N];		/* output tokens, exact, oldest first */
};

/*
 * A `{"t":"burn",...}` line, validated whole before anything is kept.
 *
 * Returns false -- and leaves *out untouched -- for any line that is not
 * exactly the contract: no "p", a "p" that is empty, too long or not a plain
 * tag; a "q" that is not a whole quarter-hour inside the window above; a "b"
 * that is not twenty non-negative integers each under BURN5H_BUCKET_MAX, in
 * one array. A bad line is ignored, never half-applied, the rule the theme
 * line already follows (theme_msg_parse). "v" is not read: it is the
 * protocol's version, the same on every line, and no other handler reads it
 * either.
 */
bool burn5h_parse(const char *json, struct burn5h *out);

/* All twenty buckets, and the last complete hour (BURN5H_LAST_FROM), in
 * tokens. */
uint32_t burn5h_total(const struct burn5h *m);
uint32_t burn5h_last_hour(const struct burn5h *m);

/*
 * Which way it is heading: the last complete hour against the one before
 * it (BURN5H_LAST_FROM against BURN5H_PREV_FROM; the quarter-hour still
 * filling is not compared).
 *
 *   both zero                    -> IDLE
 *   before zero, now above zero  -> RISING
 *   now more than 10% above      -> RISING
 *   now more than 10% below      -> EASING
 *   anything between             -> STEADY
 *
 * From `b` alone, so it costs the wire nothing.
 */
enum burn5h_dir {
	BURN5H_IDLE,
	BURN5H_RISING,
	BURN5H_STEADY,
	BURN5H_EASING,
};

enum burn5h_dir burn5h_direction(const struct burn5h *m);

/* "idle", "rising", "steady", "easing". */
const char *burn5h_word(enum burn5h_dir d);

/*
 * Tokens, the way the panel writes them:
 *
 *   0                 -> "0"
 *   412               -> "412"       under a thousand, as they are
 *   2149              -> "2.1k"      one decimal below ten of a unit
 *   412000            -> "412k"
 *   2149000           -> "2.1M"
 *   23400000          -> "23M"
 *   1999999980        -> "2.0B"      twenty buckets at the ceiling
 *
 * Four characters at most -- "999", "9.9k", "999k", "999M" -- so the total
 * never outgrows the room beside its glyph.
 */
#define BURN5H_TOKENS_MAX	8
void burn5h_fmt_tokens(uint32_t tokens, char *buf, size_t buflen);

/*
 * The line where the session countdown sits: the last complete hour as a
 * rate, and which way it is going -- "1.1M/h <bullet> rising",
 * "0/h <bullet> idle". "999k/h <bullet> steady" is the longest, 17 bytes
 * with the bullet's three; four buckets at the ceiling read "400M/h".
 */
#define BURN5H_RATE_MAX		24
void burn5h_fmt_rate(const struct burn5h *m, char *buf, size_t buflen);

/*
 * A bar's height in pixels: scaled so the largest bucket is `tallest`, and a
 * bucket with anything in it at least `least` -- one token must not draw as
 * nothing. 0 for an empty bucket.
 */
int burn5h_bar_h(uint32_t v, uint32_t max, int tallest, int least);

/*
 * The local hour bucket `i` starts on, 0..23, or -1 when it does not start
 * on a full hour. `off_min` is the board clock's offset from UTC, minutes
 * east, as the `time` message carries it.
 *
 * Buckets sit on epoch quarter-hours, and every time zone's offset is a
 * whole number of quarter-hours (+5:30 and +5:45 included), so exactly every
 * fourth bucket starts an hour, wherever the board is.
 */
int burn5h_hour(int64_t q, int i, int32_t off_min);

#endif /* BURN5H_H */
