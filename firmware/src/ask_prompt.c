#include <stdio.h>
#include <string.h>
#include "ask_prompt.h"

struct ask_prompt_turn ask_prompt_step(bool pending, bool open, bool busy,
				       bool host_gone, bool dozing,
				       int64_t now_ms, int64_t shown_ms,
				       int64_t pending_ms)
{
	struct ask_prompt_turn t = { ASK_PROMPT_NOTHING, pending };

	if (open) {
		/* The same question, already on the glass. */
		t.pending = false;
		/* Before the timeout: a "later" into a wire nobody reads is a
		 * line for nothing. A doze is the same case: nobody saw the box,
		 * so it says nothing either, and the daemon counts only an
		 * answer. */
		if (host_gone || dozing) {
			t.act = ASK_PROMPT_WITHDRAW;
		} else if (now_ms - shown_ms >= ASK_PROMPT_OPEN_MS) {
			t.act = ASK_PROMPT_EXPIRE;
		}
		return t;
	}
	if (!pending) {
		return t;
	}
	if (host_gone) {
		t.pending = false;	/* not kept for the next computer */
		return t;
	}
	/* Too late to be the question the daemon meant: it asked because
	 * someone was at the computer then. Nothing is sent; the daemon asks
	 * again an hour later. */
	if (now_ms - pending_ms >= ASK_PROMPT_PENDING_MS) {
		t.act = ASK_PROMPT_DROP;
		t.pending = false;
		return t;
	}
	/* Never stack on top of something the person is already answering,
	 * nor over the sleep clip, where it would eat the tap that wakes the
	 * board; still owed, so the next clear, awake tick asks. */
	if (!busy && !dozing) {
		t.act = ASK_PROMPT_SHOW;
		t.pending = false;
	}
	return t;
}

const char *ask_prompt_word(enum ask_answer a)
{
	switch (a) {
	case ASK_ANSWER_ON:
		return "on";
	case ASK_ANSWER_NO:
		return "no";
	case ASK_ANSWER_LATER:
	default:
		return "later";
	}
}

enum ask_answer ask_prompt_timeout_answer(void)
{
	return ASK_ANSWER_LATER;
}

int ask_prompt_line(char *buf, size_t n, int proto, enum ask_answer a)
{
	int len = snprintf(buf, n,
			   "{\"t\":\"answer\",\"v\":%d,\"id\":\"refresh\","
			   "\"a\":\"%s\"}", proto, ask_prompt_word(a));

	if (len < 0 || (size_t)len >= n) {
		return -1;
	}
	return len;
}

bool ask_prompt_known(const char *id)
{
	return strcmp(id, "refresh") == 0;
}
