#include "fatal_policy.h"

static void memo_clear(struct fatal_memo *memo)
{
	memo->magic = FATAL_MEMO_MAGIC;
	memo->streak = 0;
}

static bool memo_valid(const struct fatal_memo *memo)
{
	return memo->magic == FATAL_MEMO_MAGIC &&
	       memo->streak <= FATAL_STREAK_MAX;
}

/* Inlined by hand rather than calling the two helpers above: they are in
 * flash, and this is the one function here that runs inside the fault. */
FATAL_IRAM enum fatal_action fatal_decide(int64_t uptime_ms,
					  struct fatal_memo *memo)
{
	/* Inside the grace window the memo is left alone: a halt is followed by
	 * a power cycle, which clears it anyway. */
	if (uptime_ms < FATAL_REBOOT_GRACE_MS) {
		return FATAL_HALT;
	}

	if (memo->magic != FATAL_MEMO_MAGIC ||
	    memo->streak > FATAL_STREAK_MAX ||
	    uptime_ms >= FATAL_STREAK_FORGET_MS) {
		memo->magic = FATAL_MEMO_MAGIC;
		memo->streak = 0;
	}

	if (memo->streak >= FATAL_STREAK_MAX) {
		return FATAL_HALT;
	}
	memo->streak++;
	return FATAL_REBOOT;
}

void fatal_memo_at_boot(struct fatal_memo *memo, bool cold)
{
	if (cold || !memo_valid(memo)) {
		memo_clear(memo);
	}
}

void fatal_memo_tick(struct fatal_memo *memo, int64_t uptime_ms)
{
	if (uptime_ms < FATAL_STREAK_FORGET_MS) {
		return;
	}
	if (memo->magic != FATAL_MEMO_MAGIC || memo->streak != 0) {
		memo_clear(memo);
	}
}
