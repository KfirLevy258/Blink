#ifndef FATAL_POLICY_H
#define FATAL_POLICY_H

#include <stdbool.h>
#include <stdint.h>

/*
 * When a fatal fault should restart the board, and when it should stop it.
 *
 * Zephyr stops, always. For a panel that sits on a desk all day that is the
 * worse half of the choice once the board is past booting: the screen holds
 * its last reading, looks perfectly healthy, and stays that way until the
 * owner happens to notice the figures never move and pulls the cable. A
 * restart puts it back to work in a few seconds. Rebooting past a boot
 * window is DevOceanIT's idea, from their fork (c86226e); the streak
 * guard below is ours.
 *
 * Not during boot, though. For the first FATAL_BOOT_WINDOW_MS the board
 * still halts, and that is a deliberate part of the rule, not a leftover. A
 * fault that fires every boot and restarts every time plays the splash over
 * and over, which reads as a slow boot and never gets reported; a board
 * frozen on its boot screen is obviously broken.
 *
 * Restarting must not turn into a loop either. A fault that returns a little
 * after the boot window each time would cycle the board once a minute
 * forever. So every restart taken for a fault is counted, in a memo that
 * survives the restart, and once FATAL_STREAK_MAX have happened back to back
 * the next fault halts. "Back to back" ends, and the count goes to zero, on
 * any of:
 *
 *   - a boot that has been up FATAL_STREAK_FORGET_MS (fatal_memo_tick, from
 *     the main loop). Without it the count was cleared only by a fault that
 *     itself arrived late, so three faults weeks apart, each followed by a
 *     reboot that keeps the memo (an OTA, a Settings restart, EN), summed to
 *     a halt. Caught reviewing this change, 2026-10-27.
 *   - a power-on or reset-pin start (fatal_memo_at_boot): a person has
 *     restarted the board by hand.
 *   - a memo that is not ours: RTC memory is noise after power loss, and
 *     the magic and the range check turn any foreign pattern into zero.
 *
 * The rules are plain functions, kept apart from the handler, so that
 * tests/fatal_policy can run them on the host. fatal.c itself can only be
 * exercised on a board, by a real fault.
 *
 * What a restart could cost, and why it costs nothing a halt would not:
 * cfg_store keeps two sealed copies and writes one at a time, so a reset in
 * the middle loses only that write; the firmware only ever reads the logo
 * partition (logo.c maps it, esptool writes it); and rebooting touches
 * nothing in MCUboot's trailer. An image still on its test boot gets
 * reverted by the restart, the same thing its 90 s confirm deadline in
 * main.c would do. A test boot that halts is reset by the watchdog it arms,
 * about 60 s after the last feed -- its first 30 s stage only raises an
 * interrupt, which feeds it, and the second stage resets.
 *
 * IRAM: the handler and fatal_decide are FATAL_IRAM, and so are the kernel
 * functions the halt path calls (k_fatal_halt, arch_system_halt and the
 * uptime read; nm, 2026-10-27). Nothing more can be done from this side. A
 * fault while the flash cache is disabled, which happens only inside a
 * cfg_store or OTA flash operation with interrupts already locked, almost
 * certainly never arrives here: Zephyr's crash dump runs first and formats
 * strings held in flash, as do our printk lines and the restart path
 * (sys_reboot, esp_restart). Such a fault simply stops the CPU -- the halt
 * this policy would pick in the boot window anyway -- and can never loop.
 */
/* Uptime below which a fault halts rather than restarts. */
#define FATAL_BOOT_WINDOW_MS	30000
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
