/* Standalone host test for msg_parse (macOS-friendly; native_sim is Linux-only).
 * Build & run:
 *   cc -I ../../firmware/src host_test.c ../../firmware/src/msg_parse.c \
 *      ../../firmware/src/burn5h.c -o /tmp/mptest && /tmp/mptest
 */
#include <assert.h>
#include <math.h>
#include <stdio.h>
#include <string.h>
#include "msg_parse.h"
#include "burn5h.h"

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

	/*
	 * The plan message is the same shape twice over: its type spells one
	 * of its keys ("plan"), and its page key "p" is a prefix of the "p2"
	 * the usage line carries. pc/protocol.plan writes it in this order.
	 */
	{
		printf("\n-- the plan message --\n");
		const char *plan =
			"{\"t\":\"plan\",\"v\":2,\"p\":\"claude\",\"plan\":\"spend\","
			"\"period\":\"monthly\",\"usd\":314.12,\"lim\":500.0}";
		const char *bare = "{\"t\":\"plan\",\"v\":2,\"p\":\"claude\","
				   "\"plan\":\"windows\"}";
		char v[16];
		double d = -1;

		CHECK(msg_get_str(plan, "p", v, sizeof(v)) &&
		      strcmp(v, "claude") == 0, "the page is read from \"p\"");
		CHECK(msg_get_str(plan, "plan", v, sizeof(v)) &&
		      strcmp(v, "spend") == 0,
		      "the plan key wins over the type that spells it");
		CHECK(msg_get_double(plan, "usd", &d) && d > 314.11 && d < 314.13,
		      "the dollars parse");
		CHECK(msg_get_double(plan, "lim", &d) && d == 500.0,
		      "the cap parses");
		CHECK(!msg_get_double(bare, "usd", &d),
		      "a windows plan carries no amount");
		CHECK(!msg_get_str("{\"t\":\"usage\",\"p2\":\"codex\"}", "p", v,
				   sizeof(v)),
		      "\"p2\" is not \"p\"");
	}

	/*
	 * The plan's two flags (pc/protocol.plan): "est" when the dollars are
	 * an estimate, "hint" when there is no /usage figure at all. Each is
	 * omitted when false, so the common line does not grow; proto.c reads
	 * each as a number with -1 left standing for "absent".
	 */
	{
		printf("\n-- the plan's estimate and hint --\n");
		const char *est =
			"{\"t\":\"plan\",\"v\":2,\"p\":\"claude\",\"plan\":\"spend\","
			"\"period\":\"monthly\",\"usd\":314.12,\"lim\":500.0,"
			"\"est\":1}";
		const char *hint =
			"{\"t\":\"plan\",\"v\":2,\"p\":\"claude\",\"plan\":\"spend\","
			"\"period\":\"monthly\",\"usd\":12.0,\"est\":1,\"hint\":1}";
		const char *exact =
			"{\"t\":\"plan\",\"v\":2,\"p\":\"claude\",\"plan\":\"spend\","
			"\"period\":\"monthly\",\"usd\":314.12,\"lim\":500.0}";
		double d;

		d = -1;
		CHECK(msg_get_double(est, "est", &d) && d == 1.0,
		      "est:1 is read");
		CHECK(!msg_get_double(est, "hint", &d),
		      "an estimate without a hint has no hint key");
		d = -1;
		CHECK(msg_get_double(hint, "hint", &d) && d == 1.0,
		      "hint:1 is read");
		CHECK(!msg_get_double(hint, "lim", &d),
		      "a hinted page has no cap on the line");
		CHECK(!msg_get_double(exact, "est", &d) &&
		      !msg_get_double(exact, "hint", &d),
		      "an exact figure carries neither flag");
	}

	/*
	 * The dollar burn line (pc/protocol.spend_burn) is the burn line's
	 * twin under its own type, so a board with only the token parser never
	 * takes cents for tokens. proto.c dispatches on the WHOLE type, which
	 * this pins: "sburn" is not "burn" read from an offset, and neither
	 * type is the other's prefix match.
	 */
	{
		printf("\n-- burn and sburn --\n");
		const char *sburn =
			"{\"t\":\"sburn\",\"v\":2,\"p\":\"claude\",\"q\":1793450700,"
			"\"b\":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,412,120]}";
		const char *burn =
			"{\"t\":\"burn\",\"v\":2,\"p\":\"claude\",\"q\":1793450700,"
			"\"b\":[0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,4120,1200]}";
		char t[16];

		CHECK(msg_get_str(sburn, "t", t, sizeof(t)) &&
		      strcmp(t, "sburn") == 0 && strcmp(t, "burn") != 0,
		      "a dollar line's type is sburn, never burn");
		CHECK(msg_get_str(burn, "t", t, sizeof(t)) &&
		      strcmp(t, "burn") == 0 && strcmp(t, "sburn") != 0,
		      "a token line's type is burn, never sburn");
		CHECK(burn5h_line_of("sburn") == BURN5H_LINE_DOLLARS,
		      "sburn goes to the dollar record");
		CHECK(burn5h_line_of("burn") == BURN5H_LINE_TOKENS,
		      "burn goes to the token record, never the dollar one");
		CHECK(burn5h_line_of("burns") == BURN5H_LINE_NONE &&
		      burn5h_line_of("sbur") == BURN5H_LINE_NONE &&
		      burn5h_line_of("") == BURN5H_LINE_NONE &&
		      burn5h_line_of(NULL) == BURN5H_LINE_NONE,
		      "nothing else is a burn line of either kind");
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

	printf("\n%s (%d failures)\n", failures ? "TESTS FAILED" : "ALL TESTS PASSED", failures);
	return failures ? 1 : 0;
}
