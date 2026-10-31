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

/* Runs inside the fault, so it touches nothing in flash: memo_valid and
 * memo_clear above are not IRAM, and their logic is spelled out here
 * instead of called. */
FATAL_IRAM enum fatal_action fatal_decide(int64_t uptime_ms,
					  struct fatal_memo *memo)
{
	/* Still booting: halt, and leave the memo as it is. Getting past a
	 * halt takes a power cycle, and that clears the memo regardless. */
	if (uptime_ms < FATAL_BOOT_WINDOW_MS) {
		return FATAL_HALT;
	}

	/* A memo that is not ours, or a boot that ran long enough to have
	 * proved itself, both mean this fault starts a new streak. */
	const bool ours = memo->magic == FATAL_MEMO_MAGIC &&
			  memo->streak <= FATAL_STREAK_MAX;
	const uint32_t before =
		(ours && uptime_ms < FATAL_STREAK_FORGET_MS) ? memo->streak : 0;

	memo->magic = FATAL_MEMO_MAGIC;
	memo->streak = before;
	if (before >= FATAL_STREAK_MAX) {
		return FATAL_HALT;
	}
	memo->streak = before + 1;
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
