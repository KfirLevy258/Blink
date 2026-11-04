/* The vendor status-page level the board holds per provider, and the line it
 * puts under the brand.
 *
 *   cc -Wall -Werror -I firmware/src tests/svc_state/host_test.c \
 *      firmware/src/svc_state.c firmware/src/fmt.c -o /tmp/svc_state
 *
 * The cases worth a test are the ones a desk would only show once, if ever:
 * an incident outliving the daemon that reported it, Claude's incident
 * landing on the Codex page, and a level from a newer daemon that this
 * firmware has no word for.
 */
#include <stdio.h>
#include <string.h>
#include "svc_state.h"

static int fails;
/* Prints on both arms: tests/ci/check_host_tests.sh counts PASS lines, and a
 * file that printed nothing on success would look like one that ran nothing. */
#define CHECK(c) do { \
	if (!(c)) { \
		fails++; \
		printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); \
	} else { \
		printf("PASS %s:%d %s\n", __FILE__, __LINE__, #c); \
	} \
} while (0)

static char buf[FMT_HINT_MAX];

static enum svc_level hint(const char *p, int64_t now)
{
	return svc_hint(p, now, buf, sizeof(buf));
}

int main(void)
{
	svc_forget();

	/* The wire's five words, and everything else is ok. */
	CHECK(svc_level_from_wire("ok") == SVC_OK);
	CHECK(svc_level_from_wire("maint") == SVC_MAINT);
	CHECK(svc_level_from_wire("degraded") == SVC_DEGRADED);
	CHECK(svc_level_from_wire("partial") == SVC_PARTIAL);
	CHECK(svc_level_from_wire("major") == SVC_MAJOR);
	CHECK(svc_level_from_wire("critical") == SVC_OK);
	CHECK(svc_level_from_wire("MAJOR") == SVC_OK);
	CHECK(svc_level_from_wire("") == SVC_OK);
	CHECK(svc_level_from_wire(NULL) == SVC_OK);

	/* Nothing heard is ok, and says nothing. */
	CHECK(hint("claude", 1000) == SVC_OK);
	CHECK(buf[0] == '\0');

	/* A major outage, named. */
	svc_note("claude", "major", "Claude Code", 1000);
	CHECK(hint("claude", 1000) == SVC_MAJOR);
	CHECK(strcmp(buf, "Major outage - Claude Code") == 0);
	CHECK(svc_tone(SVC_MAJOR) == FMT_TONE_RED);

	/* Keyed by provider: Claude's incident is not the Codex page's. */
	CHECK(hint("codex", 1000) == SVC_OK);
	CHECK(buf[0] == '\0');
	/* And a provider with no status page never matches anything. */
	CHECK(hint("opencode", 1000) == SVC_OK);
	CHECK(hint("", 1000) == SVC_OK);
	CHECK(hint(NULL, 1000) == SVC_OK);

	/* An unknown provider is ignored rather than stored somewhere. */
	svc_note("gemini", "major", "API", 1000);
	CHECK(hint("gemini", 1000) == SVC_OK);
	CHECK(hint("codex", 1000) == SVC_OK);
	CHECK(hint("claude", 1000) == SVC_MAJOR);

	/* Each of the amber three, with its own words. */
	svc_note("codex", "maint", "Login", 2000);
	CHECK(hint("codex", 2000) == SVC_MAINT);
	CHECK(strcmp(buf, "Maintenance - Login") == 0);
	CHECK(svc_tone(SVC_MAINT) == FMT_TONE_AMBER);
	svc_note("codex", "degraded", "API", 2000);
	CHECK(hint("codex", 2000) == SVC_DEGRADED);
	CHECK(strcmp(buf, "Degraded - API") == 0);
	CHECK(svc_tone(SVC_DEGRADED) == FMT_TONE_AMBER);
	svc_note("codex", "partial", "Codex", 2000);
	CHECK(hint("codex", 2000) == SVC_PARTIAL);
	CHECK(strcmp(buf, "Partial outage - Codex") == 0);
	CHECK(svc_tone(SVC_PARTIAL) == FMT_TONE_AMBER);

	/* No component named: the level alone still starts with a capital. */
	svc_note("codex", "major", NULL, 2000);
	CHECK(hint("codex", 2000) == SVC_MAJOR);
	CHECK(strcmp(buf, "Major outage") == 0);
	svc_note("codex", "partial", "", 2000);
	CHECK(strcmp((hint("codex", 2000), buf), "Partial outage") == 0);

	/* An all-clear clears, and forgets the component with it. */
	svc_note("codex", "major", "Codex", 3000);
	svc_note("codex", "ok", NULL, 3000);
	CHECK(hint("codex", 3000) == SVC_OK);
	CHECK(buf[0] == '\0');

	/* A level this firmware has no word for reads as ok -- including over
	 * an incident already held, because it is the newer statement. */
	svc_note("codex", "major", "Codex", 4000);
	svc_note("codex", "catastrophic", "Codex", 4000);
	CHECK(hint("codex", 4000) == SVC_OK);

	/* LIFETIME. Shown only while it keeps being re-stated: 150 s after the
	 * last word, it is gone by itself. */
	svc_forget();
	svc_note("claude", "partial", "API", 10000);
	CHECK(hint("claude", 10000 + SVC_HOLD_MS) == SVC_PARTIAL);
	CHECK(hint("claude", 10000 + SVC_HOLD_MS + 1) == SVC_OK);
	CHECK(buf[0] == '\0');
	/* ...and each re-statement renews it, as the 60 s heartbeat does. */
	svc_note("claude", "partial", "API", 100000);
	svc_note("claude", "partial", "API", 160000);
	svc_note("claude", "partial", "API", 220000);
	CHECK(hint("claude", 220000 + SVC_HOLD_MS) == SVC_PARTIAL);
	CHECK(hint("claude", 220000 + SVC_HOLD_MS + 1) == SVC_OK);
	/* The hold is per provider: codex re-stated does not keep claude up. */
	svc_forget();
	svc_note("claude", "major", "API", 0);
	svc_note("codex", "major", "Codex", 0);
	svc_note("codex", "major", "Codex", 140000);
	CHECK(hint("claude", 200000) == SVC_OK);
	CHECK(hint("codex", 200000) == SVC_MAJOR);
	/* A clock that reads earlier than the note (it cannot, on uptime, but
	 * this costs one comparison) keeps the level rather than inventing an
	 * expiry. */
	CHECK(hint("codex", 100000) == SVC_MAJOR);

	/* The component is network text through the daemon: kept to the wire's
	 * bound, and drawn in ASCII. */
	svc_note("claude", "major",
		 "abcdefghijklmnopqrstuvwxyz0123456789", 300000);
	CHECK(hint("claude", 300000) == SVC_MAJOR);
	CHECK(strcmp(buf, "Major outage - abcdefghijklmnopqrstuvwx") == 0);
	svc_note("claude", "degraded", "caf\xc3\xa9", 300000);
	CHECK(hint("claude", 300000) == SVC_DEGRADED);
	CHECK(strcmp(buf, "Degraded - caf?") == 0);

	/* A NULL or zero-length buffer is not written. */
	CHECK(svc_hint("claude", 300000, NULL, 0) == SVC_DEGRADED);

	if (fails) {
		printf("%d failed\n", fails);
		return 1;
	}
	return 0;
}
