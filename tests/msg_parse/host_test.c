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

	/* The sessions card's srow line (docs/sessions-card-design.md): whole
	 * or not at all, and an unknown context is -1, never 0. Every line
	 * here is made up. */
	{
		struct msg_srow r;
		const char *full = "{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":4,"
			"\"tot\":6,\"pg\":0,\"pv\":\"c\",\"k\":\"wait\",\"age\":123,"
			"\"nm\":\"Blink\",\"cx\":42}";

		CHECK(msg_parse_srow(full, &r) && r.i == 0 && r.n == 4 &&
		      r.tot == 6 && r.pg == 0 && r.pv == 'c' &&
		      r.k == FMT_SROW_WAIT && r.age == 123 &&
		      strcmp(r.nm, "Blink") == 0 && r.cx == 42,
		      "srow: the design's example line, every field");
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":1,\"i\":3,\"n\":4,"
				     "\"tot\":9,\"pg\":2,\"pv\":\"x\",\"k\":\"idle\","
				     "\"age\":5}", &r) &&
		      r.pv == 'x' && r.k == FMT_SROW_IDLE && r.nm[0] == '\0' &&
		      r.cx == -1,
		      "srow: nm and cx absent read as no name and unknown (-1)");
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":1,\"n\":0,\"tot\":0,"
				     "\"pg\":0}", &r) && r.n == 0 && r.tot == 0,
		      "srow: n 0 is the empty list, no row fields needed");

		static const char *const kinds[] = {
			"wait", "fail", "edit", "tool", "work", "idle"
		};
		static const enum fmt_srow_kind want[] = {
			FMT_SROW_WAIT, FMT_SROW_FAIL, FMT_SROW_EDIT,
			FMT_SROW_TOOL, FMT_SROW_WORK, FMT_SROW_IDLE
		};
		bool all = true;

		for (int i = 0; i < 6; i++) {
			char line[160];

			snprintf(line, sizeof(line),
				 "{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,\"tot\":1,"
				 "\"pg\":0,\"pv\":\"c\",\"k\":\"%s\",\"age\":1}",
				 kinds[i]);
			all = all && msg_parse_srow(line, &r) && r.k == want[i];
		}
		CHECK(all, "srow: each of the six kinds parses to its own");

		/* Refused whole. */
		static const char *const bad[] = {
			/* unknown kind, and one a cut buffer would misread */
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,\"tot\":1,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"busy\",\"age\":1}",
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,\"tot\":1,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"waiting\",\"age\":1}",
			/* unknown provider */
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,\"tot\":1,\"pg\":0,"
			"\"pv\":\"z\",\"k\":\"idle\",\"age\":1}",
			/* i not under n */
			"{\"t\":\"srow\",\"v\":1,\"i\":2,\"n\":2,\"tot\":2,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"idle\",\"age\":1}",
			/* n past four */
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":5,\"tot\":5,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"idle\",\"age\":1}",
			/* n more than tot */
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":3,\"tot\":2,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"idle\",\"age\":1}",
			/* tot and pg past their caps, a fractional index */
			"{\"t\":\"srow\",\"v\":1,\"n\":0,\"tot\":1000,\"pg\":0}",
			"{\"t\":\"srow\",\"v\":1,\"n\":0,\"tot\":0,\"pg\":250}",
			"{\"t\":\"srow\",\"v\":1,\"i\":0.5,\"n\":1,\"tot\":1,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"idle\",\"age\":1}",
			/* age missing, negative, or a string */
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,\"tot\":1,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"idle\"}",
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,\"tot\":1,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"idle\",\"age\":-1}",
			"{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,\"tot\":1,\"pg\":0,"
			"\"pv\":\"c\",\"k\":\"idle\",\"age\":\"1\"}",
			/* a count missing */
			"{\"t\":\"srow\",\"v\":2,\"tot\":0,\"pg\":0}",
		};
		bool none = true;

		for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
			r.n = 77;
			if (msg_parse_srow(bad[i], &r) || r.n != 77) {
				none = false;
				printf("  refused line %zu was taken\n", i);
			}
		}
		CHECK(none, "srow: every malformed line refused, out untouched");

		/* The daemon's own line: pc/protocol.srow stamps v with
		 * PROTO_VERSION, 2, and the parser must take it (and a line with
		 * no v, and any extra key) or the card never fills. */
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":2,\"i\":1,\"n\":2,"
				     "\"tot\":2,\"pg\":0,\"pv\":\"c\",\"k\":\"edit\","
				     "\"age\":7,\"nm\":\"Blink\",\"cx\":12,\"z\":9}",
				     &r) && r.i == 1 && r.k == FMT_SROW_EDIT &&
		      r.cx == 12,
		      "srow: v 2, the daemon's stamp, and an extra key are taken");
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"n\":0,\"tot\":0,\"pg\":0}",
				     &r) && r.n == 0,
		      "srow: no v at all is taken");

		/* Held, not refused. */
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,"
				     "\"tot\":1,\"pg\":0,\"pv\":\"c\",\"k\":\"work\","
				     "\"age\":9999999,\"cx\":140}", &r) &&
		      r.age == FMT_SAGE_MAX && r.cx == 100,
		      "srow: age past its cap and cx past 100 are held");
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,"
				     "\"tot\":1,\"pg\":0,\"pv\":\"c\",\"k\":\"work\","
				     "\"age\":1,\"cx\":-3}", &r) && r.cx == -1,
		      "srow: a negative cx is unknown, not 0");
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,"
				     "\"tot\":1,\"pg\":0,\"pv\":\"c\",\"k\":\"work\","
				     "\"age\":1,\"cx\":0}", &r) && r.cx == 0,
		      "srow: a real 0% stays 0");
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,"
				     "\"tot\":1,\"pg\":0,\"pv\":\"c\",\"k\":\"work\","
				     "\"age\":1,\"nm\":\"abcdefghijklmnopqrstuvwxyz\"}",
				     &r) && strlen(r.nm) == 24,
		      "srow: a name past 24 bytes is cut at 24");
		CHECK(msg_parse_srow("{\"t\":\"srow\",\"v\":1,\"i\":0,\"n\":1,"
				     "\"tot\":1,\"pg\":0,\"pv\":\"c\",\"k\":\"idle\","
				     "\"age\":1,\"nm\":\"n\"}", &r) &&
		      r.n == 1 && strcmp(r.nm, "n") == 0,
		      "srow: a name that spells a key is not read as one");
	}

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
	 * each as a number starting at 0, so an absent flag is false and only an
 * exact 1 sets it.
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
