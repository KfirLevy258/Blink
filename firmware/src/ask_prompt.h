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
 * lists the "ask" cap, a day after the last answer (an hour after an ask
 * that got none), and only while someone is at the computer. It counts an ask toward its three only when an answer comes
 * back -- "on" for Turn on, "no" for No thanks, and "later" when the box
 * closes itself after ASK_PROMPT_OPEN_MS with nobody having touched it --
 * because an answer is the one proof the box was seen. A box withdrawn, or
 * an ask dropped unshown, answers nothing, and so costs none of the three
 * (final review of feat/usage-refresh, Important 4: counted on send, a
 * dozing board could spend all three unseen).
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
 *     an answer, so it comes down with none; an ask still waiting for the
 *     glass is dropped as well, rather than kept for whatever computer the
 *     board meets next.
 *   - Dropped, with no answer, when it could not go up within
 *     ASK_PROMPT_PENDING_MS of arriving. The daemon sends it because
 *     someone is at the computer now; a box that surfaced hours later,
 *     once a doze ended, would be asking an empty room. The daemon asks
 *     again an hour later, and the dropped ask has cost none of the three.
 *   - Never while the board dozes (ui_sleep.c), and withdrawn, with no
 *     answer, when a doze starts. The doze is four hours without Claude
 *     activity (SLEEP_ABSENT_AFTER_S) -- a desk where the daemon may well
 *     be asking -- and
 *     lv_timer_handler keeps running under the sleep clip, so a box drawn
 *     then sat over the clip and took the tap meant to wake the board,
 *     which could land on "No thanks" unseen; that answer is for good
 *     (Task 7 review, round 1). The ask stays owed and goes up once the
 *     board is awake.
 *   - Closed after a minute as "later". A question left on the gauges all
 *     afternoon is not a question; it is a sticker.
 *   - An ask that arrives while the box is up is the same question, and is
 *     forgotten: otherwise answering it would put it straight back.
 */

/* How long the box stays up unanswered before it closes itself as later. */
#define ASK_PROMPT_OPEN_MS 60000

/* How long an ask may wait for the glass (a doze, another box) before it is
 * dropped unshown. */
#define ASK_PROMPT_PENDING_MS 600000

enum ask_prompt_act {
	ASK_PROMPT_NOTHING,	/* leave the glass alone */
	ASK_PROMPT_SHOW,	/* put the question up */
	ASK_PROMPT_WITHDRAW,	/* take it down and answer nothing: host gone */
	ASK_PROMPT_EXPIRE,	/* take it down and answer "later" */
	ASK_PROMPT_DROP,	/* forget an ask never shown in time; no answer */
};

struct ask_prompt_turn {
	enum ask_prompt_act act;
	bool pending;		/* what the "an ask is owed" flag must be after */
};

/*
 * `pending` is an ask received and not yet shown, `open` whether the box is
 * on the glass, `busy` whether anything else owns it, `host_gone` whether
 * the computer has stopped answering, `dozing` whether the sleep clip is
 * up (a tap's peek included: the board is still dozing under it). `now_ms`,
 * `shown_ms` and `pending_ms` are uptimes; `shown_ms` is read only while
 * `open`, `pending_ms` (when the ask arrived) only while `pending`.
 *
 * The caller applies the answer verbatim -- act, then `pending`.
 */
struct ask_prompt_turn ask_prompt_step(bool pending, bool open, bool busy,
				       bool host_gone, bool dozing,
				       int64_t now_ms, int64_t shown_ms,
				       int64_t pending_ms);

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
