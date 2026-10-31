/* When a fatal fault halts and when it reboots, pinned: firmware/src/fatal_policy.h.
 *
 *   cc -Wall -Werror -I firmware/src tests/fatal_policy/host_test.c \
 *      firmware/src/fatal_policy.c -o /tmp/fatal_policy
 *
 * The handler that acts on this (fatal.c) only runs on a board, after a real
 * fault, so this is the only automated coverage the rule has.
 */
#include <stdio.h>
#include "fatal_policy.h"

static int fails;
/* PASS on the passing arm too: tests/ci/check_host_tests.sh counts them. */
#define CHECK(c) do { \
	if (!(c)) { \
		fails++; \
		printf("FAIL %s:%d %s\n", __FILE__, __LINE__, #c); \
	} else { \
		printf("PASS %s:%d %s\n", __FILE__, __LINE__, #c); \
	} \
} while (0)

#define SEC(s)	((int64_t)(s) * 1000)

int main(void)
{
	struct fatal_memo m;

	/* --- a fault during boot halts, whatever the memo says --- */
	m = (struct fatal_memo){ FATAL_MEMO_MAGIC, 0 };
	CHECK(fatal_decide(0, &m) == FATAL_HALT);
	CHECK(fatal_decide(SEC(10), &m) == FATAL_HALT);
	CHECK(fatal_decide(FATAL_BOOT_WINDOW_MS - 1, &m) == FATAL_HALT);
	CHECK(m.streak == 0);	/* a halt counts nothing */

	/* --- the first fault after the window reboots --- */
	m = (struct fatal_memo){ FATAL_MEMO_MAGIC, 0 };
	CHECK(fatal_decide(FATAL_BOOT_WINDOW_MS, &m) == FATAL_REBOOT);
	CHECK(m.streak == 1);

	/* --- power-on garbage is no streak, in either field --- */
	m = (struct fatal_memo){ 0xdeadbeefu, 2 };
	CHECK(fatal_decide(SEC(60), &m) == FATAL_REBOOT);
	CHECK(m.magic == FATAL_MEMO_MAGIC);
	CHECK(m.streak == 1);
	m = (struct fatal_memo){ FATAL_MEMO_MAGIC, 0xffffffffu };
	CHECK(fatal_decide(SEC(60), &m) == FATAL_REBOOT);
	CHECK(m.streak == 1);

	/* --- a fault that comes back each boot, just past the window, stops
	 * rebooting after FATAL_STREAK_MAX goes and stays halted --- */
	m = (struct fatal_memo){ 0, 0 };
	for (int i = 0; i < FATAL_STREAK_MAX; i++) {
		CHECK(fatal_decide(SEC(31), &m) == FATAL_REBOOT);
	}
	CHECK(fatal_decide(SEC(31), &m) == FATAL_HALT);
	CHECK(fatal_decide(SEC(31), &m) == FATAL_HALT);
	CHECK(m.streak == FATAL_STREAK_MAX);

	/* Just short of the forget line is still the same streak. */
	CHECK(fatal_decide(FATAL_STREAK_FORGET_MS - 1, &m) == FATAL_HALT);

	/* --- a boot that ran long enough before faulting starts over --- */
	CHECK(fatal_decide(FATAL_STREAK_FORGET_MS, &m) == FATAL_REBOOT);
	CHECK(m.streak == 1);
	m = (struct fatal_memo){ FATAL_MEMO_MAGIC, FATAL_STREAK_MAX };
	CHECK(fatal_decide(SEC(3 * 3600), &m) == FATAL_REBOOT);

	/* A crash every few hours reboots every time, never halts. */
	m = (struct fatal_memo){ 0, 0 };
	for (int i = 0; i < 20; i++) {
		CHECK(fatal_decide(SEC(4 * 3600), &m) == FATAL_REBOOT);
	}

	/* --- a boot that stays up ten minutes wipes the streak, from the main
	 * loop, without needing a fault to come late --- */
	m = (struct fatal_memo){ FATAL_MEMO_MAGIC, 2 };
	fatal_memo_tick(&m, SEC(60));
	CHECK(m.streak == 2);
	fatal_memo_tick(&m, FATAL_STREAK_FORGET_MS - 1);
	CHECK(m.streak == 2);
	fatal_memo_tick(&m, FATAL_STREAK_FORGET_MS);
	CHECK(m.magic == FATAL_MEMO_MAGIC);
	CHECK(m.streak == 0);
	fatal_memo_tick(&m, SEC(48 * 3600));
	CHECK(m.streak == 0);
	m = (struct fatal_memo){ 0x12345678u, 0x9u };
	fatal_memo_tick(&m, FATAL_STREAK_FORGET_MS);
	CHECK(m.magic == FATAL_MEMO_MAGIC && m.streak == 0);
	m = (struct fatal_memo){ 0x12345678u, 0 };
	fatal_memo_tick(&m, FATAL_STREAK_FORGET_MS);
	CHECK(m.magic == FATAL_MEMO_MAGIC && m.streak == 0);

	/* The case the tick exists for: rare faults weeks apart, each followed
	 * by a reboot that keeps the memo (OTA, Settings, a warm reset), never
	 * add up to a halt as long as each boot in between ran ten minutes. */
	m = (struct fatal_memo){ 0, 0 };
	fatal_memo_at_boot(&m, true);
	for (int i = 0; i < 10; i++) {
		/* A fault 45 s into a boot (an OTA's, say) reboots... */
		CHECK(fatal_decide(SEC(45), &m) == FATAL_REBOOT);
		CHECK(m.streak == 1);
		/* ...and the boot after it runs for weeks... */
		fatal_memo_at_boot(&m, false);
		fatal_memo_tick(&m, SEC(60));
		fatal_memo_tick(&m, FATAL_STREAK_FORGET_MS + SEC(1));
		/* ...until a Settings reboot or an OTA, which is not cold. */
		fatal_memo_at_boot(&m, false);
	}
	CHECK(m.streak == 0);

	/* Without the long boot in between, the same faults do halt. */
	m = (struct fatal_memo){ 0, 0 };
	fatal_memo_at_boot(&m, true);
	for (int i = 0; i < FATAL_STREAK_MAX; i++) {
		fatal_memo_tick(&m, SEC(45));
		CHECK(fatal_decide(SEC(45), &m) == FATAL_REBOOT);
		fatal_memo_at_boot(&m, false);
	}
	CHECK(fatal_decide(SEC(45), &m) == FATAL_HALT);

	/* --- at boot: a power-on or the reset pin wipes the streak, any other
	 * start keeps it, and garbage is cleared either way --- */
	m = (struct fatal_memo){ FATAL_MEMO_MAGIC, FATAL_STREAK_MAX };
	fatal_memo_at_boot(&m, false);
	CHECK(m.streak == FATAL_STREAK_MAX);
	fatal_memo_at_boot(&m, true);
	CHECK(m.magic == FATAL_MEMO_MAGIC && m.streak == 0);
	CHECK(fatal_decide(SEC(45), &m) == FATAL_REBOOT);
	m = (struct fatal_memo){ 0xdeadbeefu, 1 };
	fatal_memo_at_boot(&m, false);
	CHECK(m.magic == FATAL_MEMO_MAGIC && m.streak == 0);
	m = (struct fatal_memo){ FATAL_MEMO_MAGIC, FATAL_STREAK_MAX + 1 };
	fatal_memo_at_boot(&m, false);
	CHECK(m.streak == 0);

	/* --- the numbers the rest of this file leans on --- */
	CHECK(FATAL_BOOT_WINDOW_MS == 30000);
	CHECK(FATAL_STREAK_FORGET_MS > FATAL_BOOT_WINDOW_MS);
	CHECK(FATAL_STREAK_MAX >= 1);

	return fails ? 1 : 0;
}
