/*
 * The five-hour burn view's decisions, on a laptop: firmware/src/burn5h.c,
 * unchanged.
 *
 * Build & run (from the repo root), as tests/ci/check_host_tests.sh would:
 *   cc -Wall -Werror -I firmware/src tests/burn5h/host_test.c \
 *      firmware/src/burn5h.c firmware/src/msg_parse.c -o /tmp/burn5h && /tmp/burn5h
 *
 * Generated fixtures only: every line here is written by this file.
 */
#include <stdio.h>
#include <string.h>

#include "burn5h.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

#define Q "1793450700"		/* 12:45 UTC, a quarter-hour */
#define B20 "[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20]"

static bool parses(const char *json)
{
	struct burn5h m;

	return burn5h_parse(json, &m);
}

static void test_parse_good(void)
{
	struct burn5h m;
	bool ok = burn5h_parse("{\"t\":\"burn\",\"v\":2,\"p\":\"claude\",\"q\":"
			       Q ",\"b\":" B20 "}", &m);

	CHECK(ok, "a contract line parses");
	CHECK(strcmp(m.p, "claude") == 0, "p is kept");
	CHECK(m.q == 1793450700LL, "q is kept");
	CHECK(m.b[0] == 1 && m.b[19] == 20, "b is kept, oldest first");
	CHECK(parses("{\"t\":\"burn\",\"v\":1,\"p\":\"claude\",\"q\":" Q
		     ",\"b\":" B20 "}"), "v is not read: 1 parses");
	CHECK(parses("{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":" B20
		     "}"), "v is not read: absent parses");
	CHECK(parses("{ \"t\" : \"burn\", \"p\" : \"codex\", \"q\" : " Q
		     " , \"b\" : [ 1 , 2 , 3 , 4 , 5 , 6 , 7 , 8 , 9 , 10 ,"
		     " 11 , 12 , 13 , 14 , 15 , 16 , 17 , 18 , 19 , 20 ] }"),
	      "spaces between tokens are fine");
	CHECK(parses("{\"t\":\"burn\",\"b\":" B20 ",\"q\":" Q
		     ",\"p\":\"claude\"}"), "key order does not matter");
	CHECK(parses("{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		     "[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,99999999]}"),
	      "a bucket at the ceiling parses");
	{
		struct burn5h e;
		bool ok = burn5h_parse(
			"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
			"[0,0,0,0,0,0,0,0,0,0,0,2001,2001,2001,2001,1999,1999,"
			"1999,1999,7]}", &e);

		CHECK(ok && e.b[11] == 2001 && e.b[18] == 1999 && e.b[19] == 7,
		      "exact tokens are kept exact, not rounded to thousands");
	}
	CHECK(parses("{\"t\":\"burn\",\"p\":\"abcdefghijk\",\"q\":" Q
		     ",\"b\":" B20 "}"), "an 11-character tag parses");
	CHECK(parses("{\"t\":\"burn\",\"p\":\"b\",\"q\":" Q ",\"b\":" B20 "}"),
	      "a tag spelled like a key is not taken for one");
}

static void test_parse_refused(void)
{
	static const char *const bad[] = {
		/* p */
		"{\"t\":\"burn\",\"q\":" Q ",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"\",\"q\":" Q ",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"abcdefghijkl\",\"q\":" Q ",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"Claude\",\"q\":" Q ",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":7,\"x\":\"claude\",\"q\":" Q ",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"cla\\\"ude\",\"q\":" Q ",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude",
		/* q */
		"{\"t\":\"burn\",\"p\":\"claude\",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":1793450701,\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":0,\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":-900,\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":4102444800,\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":1793450700.0,\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":1.7934507e9,\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":\"1793450700\",\"b\":" B20 "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":17934507000,\"b\":" B20 "}",
		/* b */
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q "}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,-20]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,2.5]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,1e3]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,100000000]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,\"20\"]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,]}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":"
		"[1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":20}",
		"{\"t\":\"burn\",\"p\":\"claude\",\"q\":" Q ",\"b\":null}",
		"",
		"not json at all",
	};
	struct burn5h m;
	struct burn5h before;
	int refused = 0;

	for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
		char name[96];

		memset(&m, 0xA5, sizeof(m));
		before = m;
		bool ok = burn5h_parse(bad[i], &m);

		snprintf(name, sizeof(name), "malformed line %zu is refused", i);
		CHECK(!ok, name);
		snprintf(name, sizeof(name),
			 "malformed line %zu leaves the output untouched", i);
		CHECK(memcmp(&m, &before, sizeof(m)) == 0, name);
		refused += !ok;
	}
	CHECK(refused == (int)(sizeof(bad) / sizeof(bad[0])),
	      "every malformed line was refused");
}

static struct burn5h with(const unsigned *b)
{
	struct burn5h m;

	memset(&m, 0, sizeof(m));
	strcpy(m.p, "claude");
	m.q = 1793450700LL;
	for (int i = 0; i < BURN5H_N; i++) {
		m.b[i] = b[i];
	}
	return m;
}

/*
 * The two hours that are compared, one bucket set in each: buckets 11..14
 * are "before" and 15..18 "now". Bucket 19, the quarter-hour still filling,
 * is left empty here; test_partial_quarter() fills it.
 */
static struct burn5h hours(unsigned before, unsigned now)
{
	unsigned b[BURN5H_N] = { 0 };

	b[11] = before;
	b[15] = now;
	return with(b);
}

/* Each quarter-hour of each compared hour at one figure, the newest bucket
 * at another. */
static struct burn5h quarters(unsigned before, unsigned now, unsigned partial)
{
	unsigned b[BURN5H_N] = { 0 };

	for (int i = 0; i < BURN5H_HOUR_N; i++) {
		b[BURN5H_PREV_FROM + i] = before;
		b[BURN5H_LAST_FROM + i] = now;
	}
	b[BURN5H_N - 1] = partial;
	return with(b);
}

static void test_direction(void)
{
	CHECK(BURN5H_LAST_FROM == 15 && BURN5H_PREV_FROM == 11,
	      "the last hour is buckets 15-18, the one before it 11-14");
	CHECK(burn5h_direction(&(struct burn5h){ .q = 900 }) == BURN5H_IDLE,
	      "all zero is idle");
	{
		unsigned b[BURN5H_N] = { 500, 500 };	/* old activity only */
		struct burn5h m = with(b);

		CHECK(burn5h_direction(&m) == BURN5H_IDLE,
		      "nothing in the compared hours is idle, whatever came before");
	}
	struct burn5h m;

	m = hours(0, 1);
	CHECK(burn5h_direction(&m) == BURN5H_RISING, "from zero is rising");
	m = hours(100, 110);
	CHECK(burn5h_direction(&m) == BURN5H_STEADY, "+10% exactly is steady");
	m = hours(100, 111);
	CHECK(burn5h_direction(&m) == BURN5H_RISING, "+11% is rising");
	m = hours(100, 90);
	CHECK(burn5h_direction(&m) == BURN5H_STEADY, "-10% exactly is steady");
	m = hours(100, 89);
	CHECK(burn5h_direction(&m) == BURN5H_EASING, "-11% is easing");
	m = hours(100, 0);
	CHECK(burn5h_direction(&m) == BURN5H_EASING, "to zero is easing");
	m = quarters(99999999, 99999999, 99999999);
	CHECK(burn5h_direction(&m) == BURN5H_STEADY,
	      "the ceiling does not overflow the comparison");
	m = quarters(99999999, 90909090, 0);
	CHECK(burn5h_direction(&m) == BURN5H_STEADY,
	      "at the ceiling, -9.1% is steady (before x 11 is past 32 bits)");
	m = quarters(90000000, 99999999, 0);
	CHECK(burn5h_direction(&m) == BURN5H_RISING,
	      "near the ceiling, +11% is rising");
	CHECK(strcmp(burn5h_word(BURN5H_IDLE), "idle") == 0 &&
	      strcmp(burn5h_word(BURN5H_RISING), "rising") == 0 &&
	      strcmp(burn5h_word(BURN5H_STEADY), "steady") == 0 &&
	      strcmp(burn5h_word(BURN5H_EASING), "easing") == 0,
	      "the four words");
}

/*
 * The review of 2026-10-31's cases, each of which the old rules got wrong.
 *
 * The newest bucket is the quarter-hour the daemon's clock is in. Compared
 * as if it were complete, a desk writing 1,000 tokens a minute -- 15,000 a
 * quarter-hour -- read "easing" for the first nine minutes of every
 * quarter: 45,000 plus 1,000 a minute against 60,000. And with buckets in
 * thousands rounded up, 2,001 then 1,999 tokens a quarter went out as 3
 * then 2 and read "easing", and 1,000 then 4 went out as 1 and 1 and read
 * "steady".
 */
static void test_partial_quarter(void)
{
	char name[96], rate[BURN5H_RATE_MAX];

	for (unsigned min = 0; min <= 15; min++) {
		unsigned b[BURN5H_N];
		struct burn5h m;

		for (int i = 0; i < BURN5H_N - 1; i++) {
			b[i] = 15000;
		}
		b[BURN5H_N - 1] = min * 1000;
		m = with(b);
		snprintf(name, sizeof(name),
			 "1,000 tokens a minute, %u min into the quarter: steady",
			 min);
		CHECK(burn5h_direction(&m) == BURN5H_STEADY, name);
		burn5h_fmt_rate(&m, rate, sizeof(rate));
		snprintf(name, sizeof(name),
			 "...and the rate is the last complete hour (%s)", rate);
		CHECK(strcmp(rate, "60k/h \xE2\x80\xA2 steady") == 0, name);
	}

	struct burn5h m = quarters(2001, 1999, 0);

	CHECK(burn5h_direction(&m) == BURN5H_STEADY,
	      "2,001 then 1,999 tokens a quarter-hour is steady");
	m = quarters(1000, 4, 0);
	CHECK(burn5h_direction(&m) == BURN5H_EASING,
	      "1,000 then 4 tokens a quarter-hour is easing");
	m = quarters(0, 0, 50000);
	CHECK(burn5h_direction(&m) == BURN5H_IDLE,
	      "only the quarter-hour in progress is idle: it is drawn, not compared");
	m = quarters(1000, 1000, 0);
	CHECK(burn5h_direction(&m) == BURN5H_STEADY,
	      "an empty quarter-hour in progress does not read as easing");
	m = quarters(1000, 1000, 99999999);
	CHECK(burn5h_direction(&m) == BURN5H_STEADY,
	      "nor does a huge one read as rising");
	burn5h_fmt_rate(&m, rate, sizeof(rate));
	CHECK(strcmp(rate, "4.0k/h \xE2\x80\xA2 steady") == 0,
	      "the rate leaves the quarter-hour in progress out as well");
}

static void test_tokens(void)
{
	static const struct { unsigned t; const char *want; } t[] = {
		{ 0, "0" }, { 1, "1" }, { 7, "7" }, { 412, "412" },
		{ 999, "999" }, { 1000, "1.0k" }, { 1049, "1.0k" },
		{ 1050, "1.1k" }, { 2149, "2.1k" }, { 9949, "9.9k" },
		{ 9950, "10k" }, { 10499, "10k" }, { 10500, "11k" },
		{ 412000, "412k" }, { 999499, "999k" }, { 999500, "1.0M" },
		{ 1049999, "1.0M" }, { 1050000, "1.1M" }, { 2149000, "2.1M" },
		{ 9949999, "9.9M" }, { 9950000, "10M" }, { 23400000, "23M" },
		{ 99999999, "100M" }, { 999499999, "999M" },
		{ 999500000, "1.0B" }, { 1999999980u, "2.0B" },
		{ 4294967295u, "4.3B" },
	};

	for (size_t i = 0; i < sizeof(t) / sizeof(t[0]); i++) {
		char got[BURN5H_TOKENS_MAX], name[64];

		burn5h_fmt_tokens(t[i].t, got, sizeof(got));
		snprintf(name, sizeof(name), "%u tokens read %s (got %s)",
			 t[i].t, t[i].want, got);
		CHECK(strcmp(got, t[i].want) == 0, name);
		CHECK(strlen(got) <= 4, "four characters at most");
	}
}

static void test_totals_and_rate(void)
{
	/* The contract's example: the last complete hour 1.1M against 580k
	 * before it, and 330k in the quarter-hour still filling. */
	unsigned b[BURN5H_N] = { 90000, 120000, 80000, 0, 0, 40000, 110000,
				 150000, 130000, 160000, 140000, 120000,
				 150000, 140000, 170000, 230000, 260000,
				 290000, 320000, 330000 };
	struct burn5h m = with(b);
	char rate[BURN5H_RATE_MAX];

	CHECK(burn5h_total(&m) == 3030000, "the total is all twenty");
	CHECK(burn5h_last_hour(&m) == 1100000,
	      "the last hour is buckets 15-18, not the newest four");
	burn5h_fmt_rate(&m, rate, sizeof(rate));
	CHECK(strcmp(rate, "1.1M/h \xE2\x80\xA2 rising") == 0,
	      "the contract's own example line");
	m = hours(0, 0);
	burn5h_fmt_rate(&m, rate, sizeof(rate));
	CHECK(strcmp(rate, "0/h \xE2\x80\xA2 idle") == 0, "the idle line");
	m = hours(0, 412);
	burn5h_fmt_rate(&m, rate, sizeof(rate));
	CHECK(strcmp(rate, "412/h \xE2\x80\xA2 rising") == 0,
	      "a light hour reads in tokens, not as a thousand");
	{
		unsigned big[BURN5H_N];

		for (int i = 0; i < BURN5H_N; i++) {
			big[i] = 99999999;
		}
		m = with(big);
		CHECK(burn5h_total(&m) == 1999999980u,
		      "twenty buckets at the ceiling still sum in 32 bits");
		CHECK(burn5h_last_hour(&m) == 399999996u,
		      "an hour at the ceiling sums in 32 bits");
		burn5h_fmt_rate(&m, rate, sizeof(rate));
		CHECK(strlen(rate) < BURN5H_RATE_MAX, "the widest rate fits");
		CHECK(strcmp(rate, "400M/h \xE2\x80\xA2 steady") == 0,
		      "the widest rate reads 400M/h");
	}
}

static void test_bars(void)
{
	CHECK(burn5h_bar_h(0, 100, 56, 2) == 0, "an empty bucket has no bar");
	CHECK(burn5h_bar_h(5, 0, 56, 2) == 0, "nothing to scale by draws nothing");
	CHECK(burn5h_bar_h(100, 100, 56, 2) == 56, "the largest is the tallest");
	CHECK(burn5h_bar_h(1, 99999999, 56, 2) == 2,
	      "one token beside a huge bucket is still visible");
	CHECK(burn5h_bar_h(50, 100, 56, 2) == 28, "half is half");
	CHECK(burn5h_bar_h(99999999, 99999999, 56, 2) == 56,
	      "the ceiling does not overflow");
	CHECK(burn5h_bar_h(80000000, 99999999, 56, 2) == 45,
	      "80% of the ceiling is 45 px (a bucket times 56 is past 32 bits)");
	CHECK(burn5h_bar_h(200, 100, 56, 2) == 56, "over the max is clamped");
}

static int count_hours(int64_t q, int32_t off)
{
	int n = 0;

	for (int i = 0; i < BURN5H_N; i++) {
		n += burn5h_hour(q, i, off) >= 0;
	}
	return n;
}

static void test_hours(void)
{
	const int64_t q = 1793450700LL;	/* 12:45 UTC */
	static const int32_t offs[] = { 0, 60, 120, 330, 345, -300, -210,
					-840, 840, 765 };

	CHECK(burn5h_hour(q, 19, 0) == -1, "12:45 is not an hour");
	CHECK(burn5h_hour(q, 16, 0) == 12, "bucket 16 starts 12:00 UTC");
	CHECK(burn5h_hour(q, 16, 120) == 14, "...which is 14 at UTC+2");
	CHECK(burn5h_hour(q, 0, 120) == 10, "bucket 0 starts 10:00 at UTC+2");
	CHECK(burn5h_hour(q, 18, 330) == 18, "+5:30: bucket 18 starts 18:00");
	CHECK(burn5h_hour(q, 17, 345) == 18, "+5:45: bucket 17 starts 18:00");
	CHECK(burn5h_hour(q, 16, -300) == 7, "UTC-5: bucket 16 starts 07:00");
	CHECK(burn5h_hour(q, 16, -840) == 22,
	      "UTC-14 wraps to the day before");
	CHECK(burn5h_hour(q, 16, 840) == 2, "UTC+14 wraps to the day after");
	for (size_t i = 0; i < sizeof(offs) / sizeof(offs[0]); i++) {
		char name[64];

		snprintf(name, sizeof(name),
			 "exactly five hour ticks at offset %d", offs[i]);
		CHECK(count_hours(q, offs[i]) == 5, name);
	}
	CHECK(count_hours(q, 7) == 0, "an offset off the quarter-hour labels nothing");
	CHECK(burn5h_hour(900, 0, -840) == -1 || burn5h_hour(900, 0, -840) >= 0,
	      "a q at the epoch with a western offset does not fault");
}

int main(void)
{
	test_parse_good();
	test_parse_refused();
	test_direction();
	test_partial_quarter();
	test_tokens();
	test_totals_and_rate();
	test_bars();
	test_hours();
	if (failures) {
		printf("%d FAILED\n", failures);
		return 1;
	}
	printf("all passed\n");
	return 0;
}
