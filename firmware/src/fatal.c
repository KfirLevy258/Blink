/*
 * k_sys_fatal_error_handler, overriding the weak one in Zephyr's
 * kernel/fatal.c. That default stops the CPU on every fault, which on a desk
 * display means a panel that keeps showing its last numbers with nothing to
 * say they have stopped, until somebody pulls the cable. The policy -- when a
 * fault reboots, when it halts, and why rebooting cannot hurt settings, the
 * logo or the confirmed image -- is fatal_policy.h. This file only acts on it.
 * The idea of rebooting instead came from DevOceanIT's fork (c86226e).
 *
 * By the time we are called, interrupts are locked and the stack may be the
 * thing that broke (a STACK_SENTINEL hit is the common case on this board),
 * so the handler keeps to three steps: drain the log, print one line, act.
 * That line carries the reason code, the uptime and the streak, and no
 * buffer contents: Zephyr's own fault dump, printed just before this,
 * already names the thread and the PC.
 */
#include <zephyr/kernel.h>
#include <zephyr/fatal.h>
#include <zephyr/drivers/hwinfo.h>
#include <zephyr/logging/log_ctrl.h>
#include <zephyr/sys/printk.h>
#include <zephyr/sys/reboot.h>
#include <esp_attr.h>

#include "fatal.h"
#include "fatal_policy.h"

/*
 * RTC slow memory, not .noinit, and the difference is the whole reason the
 * streak can be trusted.
 *
 * .noinit only survives a reboot where MCUboot, which runs in between, does
 * not reach. MCUboot's data and stack sit in the same DRAM and overwrite the
 * first ~22 KB of the app's .dram0.noinit on every boot (its _end is
 * 0x3ffed708; the app's .dram0.noinit starts at 0x3ffe8000, measured
 * 2026-10-27). The first build of this file put the memo at 0x3fff3004, safe
 * only because the main stack, the workqueue stack and the LVGL pool happen
 * to be linked ahead of it. One more stack or a reordering by the linker and
 * the count would have been wiped on every boot, silently turning the loop
 * guard off.
 *
 * MCUboot puts nothing in RTC slow memory (0x50000000, nothing of its own
 * linked there), the software reset behind sys_reboot resets the CPUs and
 * leaves RTC memory alone, and RTC_NOINIT_ATTR is the section ESP-IDF itself
 * uses for state that must outlive esp_restart. A power loss leaves noise,
 * which fatal_memo_at_boot clears.
 */
static RTC_NOINIT_ATTR struct fatal_memo fatal_memo;

FATAL_IRAM void k_sys_fatal_error_handler(unsigned int reason,
					  const struct arch_esf *esf)
{
	ARG_UNUSED(esf);

	/* Deferred logging would otherwise lose whatever is still queued:
	 * nothing will run the log thread again after this. */
	LOG_PANIC();

	const int64_t now_ms = k_uptime_get();
	const bool still_booting = now_ms < FATAL_BOOT_WINDOW_MS;
	const enum fatal_action action = fatal_decide(now_ms, &fatal_memo);
	/* printk's 64-bit conversions are a Kconfig option; seconds fit in 32. */
	const unsigned int now_s = (unsigned int)(now_ms / 1000);
	const unsigned int streak = (unsigned int)fatal_memo.streak;

	switch (action) {
	case FATAL_REBOOT:
		printk("[fatal] reason %u, up %u s: rebooting, fault reboot"
		       " %u of %d\n", reason, now_s, streak, FATAL_STREAK_MAX);
		/* The full splash plays on the way back up, on purpose: it is
		 * the only sign on the panel that the board went down. So no
		 * ui_boot_mark_intentional_reboot() here. */
		sys_reboot(SYS_REBOOT_COLD);
		break;	/* sys_reboot does not return */
	case FATAL_HALT:
	default:
		if (still_booting) {
			printk("[fatal] reason %u, up %u s: halting, still in"
			       " the first %d s of boot\n", reason, now_s,
			       FATAL_BOOT_WINDOW_MS / 1000);
		} else {
			printk("[fatal] reason %u, up %u s: halting, %u fault"
			       " reboots back to back already\n", reason,
			       now_s, streak);
		}
		break;
	}
	k_fatal_halt(reason);
}

#if IS_ENABLED(CONFIG_BLINK_FATAL_SELFTEST)
/* A genuine fault, raised from the system workqueue the way a stray pointer
 * in any thread would raise one. Board-test builds only: see
 * BLINK_FATAL_SELFTEST in firmware/Kconfig. */
static void selftest_fault(struct k_work *w)
{
	ARG_UNUSED(w);
	printk("[fatal] selftest: writing through NULL at %u s\n",
	       (unsigned int)(k_uptime_get() / 1000));
	*(volatile int *)0 = 0;
}
static K_WORK_DELAYABLE_DEFINE(selftest_work, selftest_fault);
#endif

void fatal_boot(void)
{
	uint32_t cause = 0;

	/* An unreadable cause counts as a warm start. The cost of keeping a
	 * streak a power-on should have wiped is one early halt at most, and
	 * only after another fault. */
	(void)hwinfo_get_reset_cause(&cause);
	const bool cold = (cause & (RESET_POR | RESET_PIN)) != 0;

	fatal_memo_at_boot(&fatal_memo, cold);
	if (fatal_memo.streak != 0) {
		printk("[fatal] this boot follows %u fault reboot(s) in a row\n",
		       (unsigned int)fatal_memo.streak);
	}

#if IS_ENABLED(CONFIG_BLINK_FATAL_SELFTEST)
	printk("[fatal] SELFTEST BUILD: will fault at %d s -- never ship this\n",
	       CONFIG_BLINK_FATAL_SELFTEST_AT_S);
	k_work_schedule(&selftest_work,
			K_TIMEOUT_ABS_MS((int64_t)CONFIG_BLINK_FATAL_SELFTEST_AT_S *
					 1000));
#endif
}

void fatal_pump(void)
{
	fatal_memo_tick(&fatal_memo, k_uptime_get());
}
