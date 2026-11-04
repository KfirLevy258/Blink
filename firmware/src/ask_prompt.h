#ifndef ASK_PROMPT_H
#define ASK_PROMPT_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

/*
 * "Keep figures fresh?" -- the board's half of asking someone who already
 * has BLINK whether the daemon may run their own `claude -p /usage` now and
 * then (docs/plans/usage-refresh.md, 1b).
 *
 * Someone updated automatically never sees `blink install`, where the
 * question is asked in the terminal, and the refresh never runs without a
 * yes. So the daemon sends {"t":"ask","v":2,"id":"refresh"} to a board that
 * lists the "ask" cap, at most once a day and three times in all, and only
 * while someone is at the computer. It counts the ask when it sends it, so
 * the board owes it an answer only when there is one: "on" for Turn on,
 * "no" for No thanks, and "later" when the box closes itself after
 * ASK_PROMPT_OPEN_MS with nobody having touched it.
 *
 * Pure, and in its own file, for the reason upd_prompt is: every rule below
 * would otherwise live in a 250 ms LVGL timer that only a finger can reach.
 *
 * THE RULES.
 *   - Never on top of anything. `busy` is the update offer, the install
 *     confirm, a notice, What's new, the download screen or the settings
 *     panel. An ask that arrives while one of those is up waits, still
 *     owed, until the glass is clear.
 *   - Withdrawn when the computer goes (HOST LOST). Nobody is there to hear
 *     an answer, and the daemon already counted the ask, so it comes down
 *     with none; an ask still waiting for the glass is dropped as well,
 *     rather than kept for whatever computer the board meets next.
 *   - Closed after a minute as "later". A question left on the gauges all
 *     afternoon is not a question; it is a sticker.
 *   - An ask that arrives while the box is up is the same question, and is
 *     forgotten: otherwise answering it would put it straight back.
 */

/* How long the box stays up unanswered before it closes itself as later. */
#define ASK_PROMPT_OPEN_MS 60000

enum ask_prompt_act {
	ASK_PROMPT_NOTHING,	/* leave the glass alone */
	ASK_PROMPT_SHOW,	/* put the question up */
	ASK_PROMPT_WITHDRAW,	/* take it down and answer nothing: host gone */
	ASK_PROMPT_EXPIRE,	/* take it down and answer "later" */
};

struct ask_prompt_turn {
	enum ask_prompt_act act;
	bool pending;		/* what the "an ask is owed" flag must be after */
};

/*
 * `pending` is an ask received and not yet shown, `open` whether the box is
 * on the glass, `busy` whether anything else owns it, `host_gone` whether
 * the computer has stopped answering. `now_ms` and `shown_ms` are uptimes;
 * `shown_ms` is read only while `open`.
 *
 * The caller applies the answer verbatim -- act, then `pending`.
 */
struct ask_prompt_turn ask_prompt_step(bool pending, bool open, bool busy,
				       bool host_gone, int64_t now_ms,
				       int64_t shown_ms);

enum ask_answer {
	ASK_ANSWER_ON,		/* Turn on */
	ASK_ANSWER_NO,		/* No thanks: never asked again */
	ASK_ANSWER_LATER,	/* closed with no answer */
};

/* The answer's word on the wire: "on", "no" or "later". */
const char *ask_prompt_word(enum ask_answer a);

/* What a box that timed out answers. Always later: silence is never a yes,
 * and never a no either -- a no stops the asking for good. */
enum ask_answer ask_prompt_timeout_answer(void);

/*
 * {"t":"answer","v":<proto>,"id":"refresh","a":"<word>"} into `buf`.
 * Returns its length, or -1 when it would not fit: a line cut short is not
 * JSON, and is better not sent at all.
 */
int ask_prompt_line(char *buf, size_t n, int proto, enum ask_answer a);

/* Whether `id` names a question this firmware knows how to ask. Only
 * "refresh" so far; anything else is ignored, never shown as a blank box. */
bool ask_prompt_known(const char *id);

#endif /* ASK_PROMPT_H */
