/*
 * The board's answer to a fatal fault, in place of Zephyr's weak default
 * (kernel/fatal.c), which halts every time. Which faults reboot and which
 * halt, and why a reboot is safe for settings, logo and the confirmed image,
 * is in fatal_policy.h; this file only carries the decision out.
 *
 * From DevOceanIT's fork (c86226e). Before it, a fault at any hour of the day
 * left the panel frozen on its last reading until the cable was pulled.
 *
 * Runs with interrupts locked, possibly on a corrupted stack (STACK_SENTINEL
 * is the usual way here), so it does as little as it can: flush the log, say
 * one line, act. The line names the reason code and the uptime, never any
 * buffer -- the fault dump Zephyr printed before calling us already says
 * which thread and where.
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

	LOG_PANIC();

	int64_t up = k_uptime_get();
	enum fatal_action act = fatal_decide(up, &fatal_memo);
	/* Seconds in a uint32: no 64-bit printk conversion to depend on here. */
	unsigned int up_s = (unsigned int)(up / 1000);

	if (act == FATAL_REBOOT) {
		printk("[fatal] reason %u at %u s -- rebooting (%u in a row)\n",
		       reason, up_s, (unsigned int)fatal_memo.streak);
		/* Not ui_boot_mark_intentional_reboot(): the full splash is the
		 * visible sign that the board went down and came back. */
		sys_reboot(SYS_REBOOT_COLD);
	}

	if (up < FATAL_REBOOT_GRACE_MS) {
		printk("[fatal] reason %u at %u s, inside the %d s boot window"
		       " -- halting\n", reason, up_s,
		       FATAL_REBOOT_GRACE_MS / 1000);
	} else {
		printk("[fatal] reason %u at %u s after %u fault reboots in a"
		       " row -- halting\n", reason, up_s,
		       (unsigned int)fatal_memo.streak);
	}
	k_fatal_halt(reason);
}

#if IS_ENABLED(CONFIG_BLINK_FATAL_SELFTEST)
/* A real fault on purpose, from a thread, the way a bad pointer would make
 * one. See BLINK_FATAL_SELFTEST in firmware/Kconfig. */
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

	/* Unreadable is treated as warm: keeping a streak a power-on should
	 * have cleared costs at worst one early halt, and only after a fault. */
	(void)hwinfo_get_reset_cause(&cause);
	fatal_memo_at_boot(&fatal_memo, (cause & (RESET_POR | RESET_PIN)) != 0);
	if (fatal_memo.streak != 0) {
		printk("[fatal] %u fault reboot(s) in a row before this boot\n",
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
