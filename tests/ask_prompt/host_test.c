/* When the board asks "Keep figures fresh?", and when it stops asking, pinned.
 *
 *   cc -Wall -Werror -I firmware/src tests/ask_prompt/host_test.c \
 *      firmware/src/ask_prompt.c -o /tmp/ask_prompt
 *
 * The daemon sends {"t":"ask","id":"refresh"} at most once a day to a board
 * that lists "ask", and counts it as asked when it goes out. Everything the
 * board then does with it -- never on top of the update offer or any other
 * box, never to an empty room, "later" when nobody answers -- is in
 * ask_prompt_step(), so that it can be driven here rather than with a finger
 * on the panel. See ask_prompt.h.
 */
#include <stdio.h>
#include <string.h>
#include "ask_prompt.h"

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

#define T0 100000	/* uptime when the box went up, ms */

int main(void)
{
	struct ask_prompt_turn t;
	char buf[96];
	int n;

	/* --- the ordinary ask --------------------------------------------- */
	t = ask_prompt_step(true, false, false, false, false, T0, 0);
	CHECK(t.act == ASK_PROMPT_SHOW);
	CHECK(t.pending == false);	/* shown once, not on every tick */

	/* Nothing asked, nothing shown. */
	t = ask_prompt_step(false, false, false, false, false, T0, 0);
	CHECK(t.act == ASK_PROMPT_NOTHING);
	CHECK(t.pending == false);

	/* Up, and inside its minute: left alone. */
	t = ask_prompt_step(false, true, false, false, false, T0 + 59999, T0);
	CHECK(t.act == ASK_PROMPT_NOTHING);

	/* --- never on top of anything ------------------------------------- */
	/* `busy` is the update offer, the install confirm, a notice, What's
	 * new, the download screen or the settings panel. The ask waits for
	 * the glass to clear, and is still owed when it does. */
	t = ask_prompt_step(true, false, true, false, false, T0, 0);
	CHECK(t.act == ASK_PROMPT_NOTHING);
	CHECK(t.pending == true);
	t = ask_prompt_step(t.pending, false, false, false, false, T0 + 5000, 0);
	CHECK(t.act == ASK_PROMPT_SHOW);

	/* --- a second ask while the box is up is the same question --------- */
	/* Without this the box would go up again the moment it was answered. */
	t = ask_prompt_step(true, true, false, false, false, T0 + 1000, T0);
	CHECK(t.act == ASK_PROMPT_NOTHING);
	CHECK(t.pending == false);

	/* --- withdrawn when the computer goes ------------------------------ */
	/* Nobody is there to hear an answer, and the daemon has already
	 * counted the ask: it comes down with no answer at all. */
	t = ask_prompt_step(false, true, false, true, false, T0 + 1000, T0);
	CHECK(t.act == ASK_PROMPT_WITHDRAW);
	CHECK(t.pending == false);
	/* Host gone wins over the timeout: no "later" into an empty wire. */
	t = ask_prompt_step(false, true, false, true, false, T0 + 60000, T0);
	CHECK(t.act == ASK_PROMPT_WITHDRAW);
	/* An ask still waiting for the glass is dropped, not kept for the
	 * next computer. */
	t = ask_prompt_step(true, false, true, true, false, T0, 0);
	CHECK(t.act == ASK_PROMPT_NOTHING);
	CHECK(t.pending == false);
	t = ask_prompt_step(true, false, false, true, false, T0, 0);
	CHECK(t.act == ASK_PROMPT_NOTHING);
	CHECK(t.pending == false);

	/* --- nobody answered: "later", after 60 s --------------------------- */
	t = ask_prompt_step(false, true, false, false, false, T0 + ASK_PROMPT_OPEN_MS,
			    T0);
	CHECK(ASK_PROMPT_OPEN_MS == 60000);
	CHECK(t.act == ASK_PROMPT_EXPIRE);
	CHECK(t.pending == false);
	/* Busy does not hold an open box past its minute: busy only gates
	 * putting one up. */
	t = ask_prompt_step(false, true, true, false, false, T0 + 61000, T0);
	CHECK(t.act == ASK_PROMPT_EXPIRE);
	/* An uptime that went backwards (it cannot, but the subtraction is
	 * signed) does not close the box early. */
	t = ask_prompt_step(false, true, false, false, false, T0 - 1, T0);
	CHECK(t.act == ASK_PROMPT_NOTHING);

	/* --- never while the board dozes ------------------------------------ */
	/* The doze is half an hour without Claude activity, which is exactly
	 * when the daemon is likeliest to ask. A box drawn over the sleep clip
	 * swallows the tap meant to wake the board, and that tap can land on
	 * "No thanks" unseen -- which is for good. Dozing is busy: the ask
	 * waits, still owed, and goes up once the board is awake. */
	t = ask_prompt_step(true, false, false, false, true, T0, 0);
	CHECK(t.act == ASK_PROMPT_NOTHING);
	CHECK(t.pending == true);
	t = ask_prompt_step(t.pending, false, false, false, false, T0 + 5000, 0);
	CHECK(t.act == ASK_PROMPT_SHOW);
	/* A box already up when a doze starts comes down with no answer, as
	 * on host loss: the daemon counted the ask when it sent it. */
	t = ask_prompt_step(false, true, false, false, true, T0 + 1000, T0);
	CHECK(t.act == ASK_PROMPT_WITHDRAW);
	CHECK(t.pending == false);
	/* Even past its minute: never a "later" for a box nobody could see. */
	t = ask_prompt_step(false, true, false, false, true, T0 + 70000, T0);
	CHECK(t.act == ASK_PROMPT_WITHDRAW);
	/* Host loss while dozing still drops a pending ask. */
	t = ask_prompt_step(true, false, false, true, true, T0, 0);
	CHECK(t.act == ASK_PROMPT_NOTHING);
	CHECK(t.pending == false);

	/* --- the answers on the wire ---------------------------------------- */
	CHECK(strcmp(ask_prompt_word(ASK_ANSWER_ON), "on") == 0);
	CHECK(strcmp(ask_prompt_word(ASK_ANSWER_NO), "no") == 0);
	CHECK(strcmp(ask_prompt_word(ASK_ANSWER_LATER), "later") == 0);
	/* The one timeout answer is "later", never a yes or a no. */
	CHECK(ask_prompt_timeout_answer() == ASK_ANSWER_LATER);

	n = ask_prompt_line(buf, sizeof(buf), 2, ASK_ANSWER_LATER);
	CHECK(n > 0 && (size_t)n < sizeof(buf));
	CHECK(strcmp(buf,
		     "{\"t\":\"answer\",\"v\":2,\"id\":\"refresh\",\"a\":\"later\"}")
	      == 0);
	n = ask_prompt_line(buf, sizeof(buf), 2, ASK_ANSWER_ON);
	CHECK(strcmp(buf,
		     "{\"t\":\"answer\",\"v\":2,\"id\":\"refresh\",\"a\":\"on\"}")
	      == 0);
	/* A buffer too small is refused rather than sent cut short: a
	 * truncated line is not JSON. */
	CHECK(ask_prompt_line(buf, 10, 2, ASK_ANSWER_NO) < 0);

	/* --- only the question this firmware knows -------------------------- */
	CHECK(ask_prompt_known("refresh"));
	CHECK(!ask_prompt_known(""));
	CHECK(!ask_prompt_known("refresh2"));
	CHECK(!ask_prompt_known("Refresh"));

	printf("%s\n", fails ? "FAILURES" : "all checks passed");
	return fails ? 1 : 0;
}
