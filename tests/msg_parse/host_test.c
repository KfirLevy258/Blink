/* Standalone host test for msg_parse (macOS-friendly; native_sim is Linux-only).
 * Build & run:
 *   cc -I ../../firmware/src host_test.c ../../firmware/src/msg_parse.c -o /tmp/mptest && /tmp/mptest
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "msg_parse.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

int main(void)
{
	const char *u = "{\"t\":\"usage\",\"v\":1,\"session_pct\":61.0,"
		"\"session_resets_at\":\"2026-06-08T21:50:01Z\",\"weekly_pct\":26.0}";
	char buf[40];
	double d;

	CHECK(msg_get_str(u, "t", buf, sizeof(buf)) && strcmp(buf, "usage") == 0, "type=usage");
	CHECK(msg_get_str(u, "session_resets_at", buf, sizeof(buf)) &&
	      strcmp(buf, "2026-06-08T21:50:01Z") == 0, "session_resets_at");
	CHECK(msg_get_double(u, "session_pct", &d) && fabs(d - 61.0) < 0.01, "session_pct=61");
	CHECK(msg_get_double(u, "weekly_pct", &d) && fabs(d - 26.0) < 0.01, "weekly_pct=26");
	CHECK(!msg_get_double(u, "missing", &d), "missing key -> false");
	CHECK(!msg_get_str("not json", "t", buf, sizeof(buf)), "garbage -> false");

	/* The v2 time message: epoch is large, the offset can be negative. */
	const char *tm = "{\"t\":\"time\",\"v\":2,\"epoch\":1752444000,"
		"\"utc_offset_min\":-300}";
	CHECK(msg_get_double(tm, "epoch", &d) && fabs(d - 1752444000.0) < 0.5,
	      "time epoch");
	CHECK(msg_get_double(tm, "utc_offset_min", &d) && fabs(d + 300.0) < 0.01,
	      "negative utc_offset_min");

	/* msg_get_bool: the daemon marks a reading it cannot vouch for, and the
	 * panel must be able to tell that from a real rate limit. */
	{
		const char *fresh = "{\"t\":\"usage\",\"session_pct\":50.0,\"stale\":false}";
		const char *old = "{\"t\":\"usage\",\"session_pct\":50.0,\"stale\":true}";
		const char *absent = "{\"t\":\"usage\",\"session_pct\":50.0}";
		const char *quoted = "{\"t\":\"usage\",\"stale\":\"true\"}";
		bool b;

		b = true;
		CHECK(msg_get_bool(fresh, "stale", &b) && b == false, "stale=false");
		b = false;
		CHECK(msg_get_bool(old, "stale", &b) && b == true, "stale=true");
		b = false;
		CHECK(!msg_get_bool(absent, "stale", &b) && b == false,
		      "absent stale leaves the default (old daemon reads as OK)");
		b = false;
		CHECK(!msg_get_bool(quoted, "stale", &b),
		      "a quoted \"true\" is not a boolean");
		CHECK(!msg_get_bool(fresh, "session_pct", &b),
		      "a number is not a boolean");
	}

	{
		/*
		 * A key is only a key when a colon follows it.
		 *
		 * The bug this pins: `{"t":"edition",...,"edition":"codex"}`
		 * returned the literal "edition". after_colon() matched the
		 * VALUE of "t" -- same spelling -- and then took the next
		 * colon it could find, which belonged to "v". The board
		 * reported `unknown edition 'edition'` and provisioning did
		 * nothing, on hardware, silently.
		 */
		printf("\n-- a value that spells a key name --\n");
		const char *collide =
			"{\"t\":\"edition\",\"v\":2,\"edition\":\"codex\"}";
		const char *spaced =
			"{\"t\": \"edition\", \"v\": 2, \"edition\": \"codex\"}";
		char v[16];

		CHECK(msg_get_str(collide, "t", v, sizeof(v)) &&
		      strcmp(v, "edition") == 0, "t is still read correctly");
		CHECK(msg_get_str(collide, "edition", v, sizeof(v)) &&
		      strcmp(v, "codex") == 0,
		      "the KEY wins over an identically spelled value");
		CHECK(msg_get_str(spaced, "edition", v, sizeof(v)) &&
		      strcmp(v, "codex") == 0,
		      "...and with spaces after the colons too");

		double d = 0;

		CHECK(msg_get_double(collide, "v", &d) && d == 2,
		      "a number after a colliding value still parses");
		CHECK(!msg_get_str(collide, "nope", v, sizeof(v)),
		      "an absent key is still absent");
	}

	/* The session message: an optional label and a count. */
	{
		char lbl[28] = "";
		double n = 0;
		const char *j =
			"{\"t\":\"session\",\"v\":1,"
			"\"label\":\"LiveClaudeUi\",\"n\":1}";

		CHECK(msg_get_str(j, "label", lbl, sizeof(lbl)) &&
		      strcmp(lbl, "LiveClaudeUi") == 0, "session label");
		CHECK(msg_get_double(j, "n", &n) && (int)n == 1, "session n=1");
	}
	{
		/* Absent label is the normal several-sessions case. */
		char lbl[28] = "x";
		const char *j = "{\"t\":\"session\",\"v\":1,\"n\":3}";

		CHECK(!msg_get_str(j, "label", lbl, sizeof(lbl)),
		      "absent label is not a parse failure");
	}
	{
		/* A label longer than the buffer must truncate, not overrun. */
		char lbl[8] = "";
		const char *j =
			"{\"t\":\"session\",\"label\":\"abcdefghijklmno\"}";

		msg_get_str(j, "label", lbl, sizeof(lbl));
		CHECK(strlen(lbl) == 7, "an over-long label truncates to buflen-1");
	}

	/* The counts have always been on the wire; only n_sess was read. */
	{
		double v = -1;
		const char *j = "{\"t\":\"usage\",\"n_sess\":4,\"n_run\":2,"
			"\"n_wait\":1,\"n_stuck\":1}";

		CHECK(msg_get_double(j, "n_run", &v) && (int)v == 2, "n_run=2");
		CHECK(msg_get_double(j, "n_wait", &v) && (int)v == 1, "n_wait=1");
		CHECK(msg_get_double(j, "n_stuck", &v) && (int)v == 1, "n_stuck=1");
	}
	{
		/* Absent counts are the common case -- the daemon omits a zero. */
		double v = -1;
		const char *j = "{\"t\":\"usage\",\"n_sess\":1}";

		CHECK(!msg_get_double(j, "n_run", &v), "absent n_run -> false");
	}

	/*
	 * The third page's own line, as pc/protocol.page3() writes it. It
	 * reuses the usage line's key names on purpose -- it is its own
	 * message, so there is no p2_-style prefix to tell it apart -- and
	 * what has to hold is that the type fits proto.c's 16-byte `t`
	 * buffer and every field reads back exactly.
	 */
	{
		char t[16] = "", p3[16] = "x";
		double v = 0;
		bool st = false;
		const char *j = "{\"t\":\"page3\",\"v\":2,\"provider\":\"opencode\","
			"\"session_pct\":41.5,\"session_resets_in_s\":7200,"
			"\"weekly_pct\":12.0,\"weekly_resets_in_s\":400000,"
			"\"stale\":true,\"age_s\":30}";

		CHECK(msg_get_str(j, "t", t, sizeof(t)) &&
		      strcmp(t, "page3") == 0, "page3: type fits proto.c's buffer");
		CHECK(msg_get_str(j, "provider", p3, sizeof(p3)) &&
		      strcmp(p3, "opencode") == 0, "page3: provider");
		CHECK(msg_get_double(j, "session_pct", &v) && v == 41.5,
		      "page3: session_pct");
		CHECK(msg_get_double(j, "session_resets_in_s", &v) && v == 7200,
		      "page3: session countdown");
		CHECK(msg_get_double(j, "weekly_pct", &v) && v == 12.0,
		      "page3: weekly_pct");
		CHECK(msg_get_double(j, "weekly_resets_in_s", &v) && v == 400000,
		      "page3: weekly countdown");
		CHECK(msg_get_bool(j, "stale", &st) && st, "page3: stale");
		CHECK(msg_get_double(j, "age_s", &v) && v == 30, "page3: age_s");
	}
	{
		/* The clear: an empty provider is PRESENT and empty, which is
		 * what proto.c hands set_provider3 to drop the page. */
		char p3[16] = "x";
		const char *j = "{\"t\":\"page3\",\"v\":2,\"provider\":\"\"}";

		CHECK(msg_get_str(j, "provider", p3, sizeof(p3)) && p3[0] == '\0',
		      "page3 clear: empty provider reads as empty");
	}

	printf("\n%s (%d failures)\n", failures ? "TESTS FAILED" : "ALL TESTS PASSED", failures);
	return failures ? 1 : 0;
}
