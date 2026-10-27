#ifndef FATAL_POLICY_H
#define FATAL_POLICY_H

#include <stdbool.h>
#include <stdint.h>

/*
 * What a fatal fault does to the board: halt where it stands, or reboot.
 *
 * Zephyr's own answer is always "halt". On a desk display that answer is the
 * wrong one for a fault an hour into a working day: the panel freezes on its
 * last reading, nothing on it says it has stopped, and it stays that way
 * until someone notices the numbers have not moved and pulls the cable.
 * DevOceanIT's fork (c86226e) reboots instead once the board has been up for
 * 30 s, and halts inside that window. The window is the point of the change,
 * not a detail of it: a fault during boot rebooted at once would replay the
 * splash forever, and a crash loop that looks like a slow boot is the one
 * nobody reports. A frozen boot screen is a fault somebody can see.
 *
 * Added here, so that a reboot cannot become a loop either: a board whose
 * fault comes back every boot, a little after the 30 s line, would reboot
 * every minute for ever. So each reboot taken for a fault is counted in a
 * memo that outlives the reboot, and after FATAL_STREAK_MAX of them in a row
 * the next fault halts instead. A count is only "in a row" while nothing
 * else happens in between, so it is wiped by:
 *
 *   - a boot that stays up FATAL_STREAK_FORGET_MS (fatal_memo_tick, from the
 *     main loop). Before that rule, the count only reset when a fault itself
 *     came late, so three rare faults weeks apart -- each followed by an OTA,
 *     a Settings reboot or an EN press that kept the memo -- added up to a
 *     halt. (Review of this change, 2026-10-27.)
 *   - a power-on or a reset pin (fatal_memo_at_boot): someone is at the desk
 *     and has started the board over on purpose.
 *   - garbage: RTC memory holds noise after a power loss, and the magic and
 *     range check read any bit pattern that is not ours as "no streak".
 *
 * The rules are pure and live here, apart from the handler, so that
 * tests/fatal_policy can pin them on a host. The handler itself (fatal.c)
 * cannot be tested anywhere but on a board.
 *
 * Why a reboot here cannot cost the owner anything a halt would not:
 * cfg_store writes an A/B pair of sealed records, so a reset mid-write loses
 * at most the write in flight; the logo partition is only ever read by the
 * firmware (logo.c maps it, esptool writes it); and a reboot writes nothing
 * to MCUboot's trailer. An image still on its test boot is reverted by the
 * reboot, which is the same outcome as its 90 s confirm deadline in main.c.
 * A test boot that halts is reset by the watchdog that boot arms, about 60 s
 * after the last feed: its first 30 s stage only raises an interrupt, which
 * feeds it, and the reset comes from the second.
 *
 * IRAM: fatal_decide and the handler are placed in IRAM (FATAL_IRAM), as are
 * the kernel calls the halt branch makes (k_fatal_halt, arch_system_halt,
 * the uptime read -- checked with nm, 2026-10-27). That is as far as it can
 * be taken from here. A fault while the flash cache is off -- only inside a
 * cfg_store or OTA flash operation, with interrupts already locked -- most
 * likely never reaches this code at all: Zephyr's own crash dump, which runs
 * first, formats strings that live in flash, and so do our printk lines and
 * the reboot path (sys_reboot, esp_restart). What such a fault does is
 * stop, which is the halt this code would have chosen inside the window
 * anyway; it cannot become a reboot loop.
 */
#define FATAL_REBOOT_GRACE_MS	30000
#define FATAL_STREAK_MAX	3
#define FATAL_STREAK_FORGET_MS	(10 * 60 * 1000)

#define FATAL_MEMO_MAGIC	0xfa7a1b00u

#ifdef __ZEPHYR__
#include <esp_attr.h>
#define FATAL_IRAM IRAM_ATTR
#else
#define FATAL_IRAM
#endif

enum fatal_action {
	FATAL_HALT,
	FATAL_REBOOT,
};

/* Lives in RTC slow memory on the board, so it outlasts the reboot it
 * counts (fatal.c says why there and not in .noinit). Any bit pattern is a
 * legal input: garbage fails the magic or the range check and reads as "no
 * streak". */
struct fatal_memo {
	uint32_t magic;
	uint32_t streak;
};

/* At a fault: decide, and update the memo to match the decision. */
enum fatal_action fatal_decide(int64_t uptime_ms, struct fatal_memo *memo);

/* Once per boot. `cold` is a power-on or a reset-pin start, which wipes any
 * streak; every other start (software, watchdog, the fault reboot itself)
 * keeps it. */
void fatal_memo_at_boot(struct fatal_memo *memo, bool cold);

/* From the main loop: past FATAL_STREAK_FORGET_MS of uptime this boot has
 * proved itself and the streak is wiped. Writes only when there is something
 * to wipe. */
void fatal_memo_tick(struct fatal_memo *memo, int64_t uptime_ms);

#endif /* FATAL_POLICY_H */
