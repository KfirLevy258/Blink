/* "Edit on computer" held while its builder is open: every way in and out.
 *
 *   cc -Wall -Werror -I firmware/src tests/edit_hold/host_test.c \
 *      firmware/src/edit_hold.c firmware/src/theme.c \
 *      firmware/src/msg_parse.c -lm -o /tmp/edit_hold
 *
 * The owner asked for the button back only once the builder page is closed
 * (2026-10-30). A hold that misses one way out is a button that never comes
 * back, which is how the 10 s lock-out before 2026-10-29 read to them, so
 * each way out is checked here on its own, and each way the hold must NOT
 * start. The welcome lines are the shapes pc/protocol.welcome() writes; see
 * edit_hold.h.
 */
#include <stdio.h>
#include "edit_hold.h"

static int fails;

/* Prints on both arms: check_host_tests.sh counts PASS lines. */
#define CHECK(c) do { \
	if (!(c)) { \
		fails++; \
		printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); \
	} else { \
		printf("PASS %s:%d %s\n", __FILE__, __LINE__, #c); \
	} \
} while (0)

/* greet()'s welcome from a daemon that tells, with and without a builder. */
static const char *const W_OPEN =
	"{\"t\":\"welcome\",\"v\":2,\"app\":\"blink-bridge\",\"app_ver\":"
	"\"1.4.0\",\"caps\":[\"theme\",\"edit_done\"],\"editing\":1}";
static const char *const W_SHUT =
	"{\"t\":\"welcome\",\"v\":2,\"app\":\"blink-bridge\",\"app_ver\":"
	"\"1.4.0\",\"caps\":[\"theme\",\"edit_done\"],\"editing\":0}";
/* The probe's: the cap, and nothing about a builder. */
static const char *const W_PROBE =
	"{\"t\":\"welcome\",\"v\":2,\"app\":\"blink-bridge\",\"app_ver\":"
	"\"1.4.0\",\"caps\":[\"theme\",\"edit_done\"]}";
/* A daemon from before the hold: it opens builders but never says one
 * ended in a way this board may rely on. */
static const char *const W_THEME_ONLY =
	"{\"t\":\"welcome\",\"v\":2,\"app\":\"blink-bridge\",\"app_ver\":"
	"\"1.4.0\",\"caps\":[\"theme\"]}";
/* 1.3.4: no caps at all. */
static const char *const W_134 =
	"{\"t\":\"welcome\",\"v\":2,\"app\":\"blink-bridge\",\"app_ver\":"
	"\"1.3.4\"}";

static struct edit_hold telling(void)
{
	struct edit_hold h = {0};

	edit_hold_welcome(&h, W_SHUT);
	return h;
}

int main(void)
{
	struct edit_hold h;

	/* --- the owner's case: ok 1 holds, done frees --- */
	h = telling();
	CHECK(h.host_tells && !h.held);
	CHECK(edit_hold_reply(&h, true));
	CHECK(h.held);
	CHECK(!edit_hold_reply(&h, true));	/* already held: no repaint */
	CHECK(edit_hold_done(&h));
	CHECK(!h.held);
	CHECK(!edit_hold_done(&h));		/* a second done: nothing */

	/* --- the hold never starts without a daemon that tells --- */
	/* Before any welcome at all (a board whose host has not spoken). */
	h = (struct edit_hold){0};
	CHECK(!edit_hold_reply(&h, true) && !h.held);
	h = (struct edit_hold){0};
	edit_hold_welcome(&h, W_THEME_ONLY);
	CHECK(!h.host_tells);
	CHECK(!edit_hold_reply(&h, true) && !h.held);
	h = (struct edit_hold){0};
	edit_hold_welcome(&h, W_134);
	CHECK(!edit_hold_reply(&h, true) && !h.held);
	/* "edit_done" only as a whole member, never as a substring. */
	h = (struct edit_hold){0};
	edit_hold_welcome(&h, "{\"t\":\"welcome\",\"caps\":[\"edit_done_x\"],"
			      "\"editing\":1}");
	CHECK(!h.host_tells && !h.held);
	/* "editing" in a VALUE is not the field. */
	h = (struct edit_hold){0};
	edit_hold_welcome(&h, "{\"t\":\"welcome\",\"app\":\"editing\","
			      "\"caps\":[\"edit_done\"]}");
	CHECK(h.host_tells && !h.held);

	/* --- ok 0 is the fallback, never a hold, and frees nothing --- */
	h = telling();
	CHECK(!edit_hold_reply(&h, false) && !h.held);
	edit_hold_reply(&h, true);
	/* A reshow whose browser would not open, on a builder still running. */
	CHECK(!edit_hold_reply(&h, false) && h.held);

	/* --- edit_state: the restatement --- */
	/* The owner's lunch: held, the Mac sleeps, HOST LOST frees, and the
	 * same connection wakes with the builder still open. No welcome comes;
	 * the restatement holds it again. */
	h = telling();
	edit_hold_reply(&h, true);
	edit_hold_host_gone(&h);
	CHECK(!h.held);
	CHECK(edit_hold_state(&h, true) && h.held);
	CHECK(!edit_hold_state(&h, true));	/* restated: no repaint */
	/* A hold over a builder that is gone -- a done that overtook its own
	 * ok -- goes with the next restatement. */
	CHECK(edit_hold_state(&h, false) && !h.held);
	CHECK(!edit_hold_state(&h, false));
	/* From a daemon that never promised it, it is not believed. */
	h = (struct edit_hold){0};
	edit_hold_welcome(&h, W_THEME_ONLY);
	CHECK(!edit_hold_state(&h, true) && !h.held);
	h = (struct edit_hold){0};		/* nobody has said anything yet */
	CHECK(!edit_hold_state(&h, true) && !h.held);

	/* --- a late ok, after the board's own 5 s fallback --- */
	h = telling();
	CHECK(edit_hold_reply(&h, true) && h.held);

	/* --- host gone, or bye: free; the next welcome says again --- */
	h = telling();
	edit_hold_reply(&h, true);
	CHECK(edit_hold_host_gone(&h) && !h.held);
	CHECK(!edit_hold_host_gone(&h));
	/* Same daemon back, its builder still open (a nudged cable). */
	CHECK(edit_hold_welcome(&h, W_OPEN) && h.held);
	/* A new daemon: a builder lives in the daemon's process, so none. */
	h = telling();
	edit_hold_reply(&h, true);
	edit_hold_host_gone(&h);
	CHECK(!edit_hold_welcome(&h, W_SHUT) && !h.held);

	/* --- a welcome while held: what it says wins --- */
	h = telling();
	edit_hold_reply(&h, true);
	/* A daemon restarted fast enough that the board never saw HOST LOST:
	 * the builder went with the old process. */
	CHECK(edit_hold_welcome(&h, W_SHUT) && !h.held);
	h = telling();
	edit_hold_reply(&h, true);
	CHECK(!edit_hold_welcome(&h, W_OPEN) && h.held);
	/* The probe's welcome says nothing either way; greet()'s follows. */
	CHECK(!edit_hold_welcome(&h, W_PROBE) && h.held);
	CHECK(edit_hold_welcome(&h, W_SHUT) && !h.held);
	/* Downgraded to a daemon that never says done: free, and its ok 1
	 * no longer holds. */
	h = telling();
	edit_hold_reply(&h, true);
	CHECK(edit_hold_welcome(&h, W_THEME_ONLY) && !h.held);
	CHECK(!edit_hold_reply(&h, true) && !h.held);
	h = telling();
	edit_hold_reply(&h, true);
	CHECK(edit_hold_welcome(&h, W_134) && !h.held);

	/* --- a rebooted board: no state, and the welcome brings it back --- */
	h = (struct edit_hold){0};
	CHECK(edit_hold_welcome(&h, W_OPEN) && h.held);
	h = (struct edit_hold){0};
	CHECK(!edit_hold_welcome(&h, W_PROBE) && !h.held);

	/* --- the upper bound, for a done lost on the wire --- */
	h = telling();
	edit_hold_reply(&h, true);
	CHECK(edit_hold_expired(&h) && !h.held);
	CHECK(!edit_hold_expired(&h));
	/* After it the button works, and a fresh ok holds again. */
	CHECK(edit_hold_reply(&h, true) && h.held);

	if (fails) {
		printf("%d check(s) failed\n", fails);
		return 1;
	}
	return 0;
}
