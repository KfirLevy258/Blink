/* Host test for the pure brightness stepping and quiet-hours window logic.
 *   cc -I firmware/src tests/backlight/host_test.c -o /tmp/bl && /tmp/bl
 */
#include <stdio.h>
#include "backlight.h"
#include "panel.h"

static int failures;
#define CK(cond, name) do{ if(cond){printf("PASS: %s\n",name);} \
	else {printf("FAIL: %s\n",name); failures++;} }while(0)

/* --- panel.h's decisions, driven through recording fakes ---
 *
 * Review round 4 of issue #23: the tests above checked panel.h's two lookup
 * tables and nothing that used them, so a boot that wrote the wrong MADCTL,
 * an alternate boot that wrote one at all, a PWM sum that wrapped, or a live
 * change that rescaled the backlight after a failed MADCTL write would all
 * have passed. main.c, backlight.c and proto.c now only hand panel.h their
 * hardware (tests/test_firmware_log_config.py holds them to it), so what
 * follows runs the code the board runs, with the hardware swapped for a log.
 *
 * The log is one letter per call, in order: M a MADCTL write, B a backlight
 * reapply, R a redraw, S a store, A a live apply.
 */
static char calls[32];
static int ncalls;
static uint8_t madctl_seen[8];
static int nmadctl;
static uint8_t bl_panel_seen;
static uint8_t stored;
static int write_rc;	/* what the fake bus answers */
static int store_rc;	/* what the fake record answers */

static void log_call(char c)
{
	if (ncalls < (int)sizeof(calls) - 1) {
		calls[ncalls++] = c;
		calls[ncalls] = '\0';
	}
}

static void reset_fakes(void)
{
	ncalls = 0;
	calls[0] = '\0';
	nmadctl = 0;
	bl_panel_seen = 0xEE;
	stored = 0xEE;
	write_rc = 0;
	store_rc = 0;
}

static int fake_write(uint8_t m)
{
	log_call('M');
	if (nmadctl < (int)sizeof(madctl_seen)) {
		madctl_seen[nmadctl++] = m;
	}
	return write_rc;
}

static void fake_reapply(uint8_t p)
{
	log_call('B');
	bl_panel_seen = p;
}

static void fake_redraw(void)
{
	log_call('R');
}

static const struct panel_ops fake_hw = {
	.write_madctl = fake_write,
	.backlight_reapply = fake_reapply,
	.redraw = fake_redraw,
};

static int fake_store(uint8_t p)
{
	log_call('S');
	if (store_rc == 0) {
		stored = p;
	}
	return store_rc;
}

/* What main.c's panel_apply is: the live apply over the hardware. */
static int fake_apply(uint8_t p)
{
	log_call('A');
	return panel_live_apply(p, &fake_hw);
}

static const struct panel_msg_ops fake_msg = {
	.store = fake_store,
	.apply = fake_apply,
};

static void pulse_checks(void)
{
	/* 1 kHz in nanoseconds, the overlay's period. */
	const uint32_t ns = 1000000u;

	CK(panel_bl_pulse(ns, 100, 0) == 600000u,
	   "pulse: standard at 100% is 60% of the period");
	CK(panel_bl_pulse(ns, 100, 1) == ns,
	   "pulse: alternate at 100% is the whole period");
	CK(panel_bl_pulse(ns, 50, 0) == 300000u,
	   "pulse: standard at 50% is 30% of the period");
	CK(panel_bl_pulse(ns, 50, 1) == 500000u,
	   "pulse: alternate at 50% is half the period");
	CK(panel_bl_pulse(ns, 20, 7) == 120000u,
	   "pulse: a stray panel byte at 20% is scaled as standard");
	CK(panel_bl_pulse(ns, 0, 0) == 0 && panel_bl_pulse(ns, 0, 1) == 0,
	   "pulse: level 0 is off on either panel");
	/* 0xFFFFFFFF * 100 * 60 is about 2.6e13: a 32-bit product wraps, and
	 * so does a 32-bit period * 100 alone (4.3e11). */
	CK(panel_bl_pulse(0xFFFFFFFFu, 100, 1) == 0xFFFFFFFFu,
	   "pulse: the largest period at full duty does not overflow");
	CK(panel_bl_pulse(0xFFFFFFFFu, 100, 0) ==
	   (uint32_t)(0xFFFFFFFFull * 60u / 100u),
	   "pulse: the largest period at 60% does not overflow");
	CK(panel_bl_pulse(4000000000u, 80, 0) == 1920000000u,
	   "pulse: a 4e9 period at 80% on standard is exact");
	CK(panel_bl_pulse(ns, 250, 1) == ns,
	   "pulse: a level past 100 never exceeds the period");
}

static void boot_checks(void)
{
	uint8_t m;

	reset_fakes();
	m = 0;
	CK(panel_boot(0, &fake_hw, &m) == PANEL_BOOT_WROTE && m == 0xA0 &&
	   nmadctl == 1 && madctl_seen[0] == 0xA0 && strcmp(calls, "M") == 0,
	   "boot, standard: exactly one MADCTL write, of 0xA0");

	reset_fakes();
	m = 0x55;
	CK(panel_boot(1, &fake_hw, &m) == PANEL_BOOT_KEPT && nmadctl == 0 &&
	   ncalls == 0 && m == 0x55,
	   "boot, alternate: nothing written, the driver's MADCTL kept");

	reset_fakes();
	CK(panel_boot(7, &fake_hw, &m) == PANEL_BOOT_WROTE && nmadctl == 1 &&
	   madctl_seen[0] == 0xA0,
	   "boot, a stray byte 7: written as standard, 0xA0");

	reset_fakes();
	write_rc = -19;
	CK(panel_boot(0, &fake_hw, &m) == PANEL_BOOT_FAILED && nmadctl == 1,
	   "boot, standard, the write fails: reported as failed");
}

static void live_checks(void)
{
	reset_fakes();
	CK(panel_live_apply(0, &fake_hw) == 0 && strcmp(calls, "MBR") == 0 &&
	   madctl_seen[0] == 0xA0 && bl_panel_seen == 0,
	   "live, standard: 0xA0, then backlight for standard, then redraw");

	reset_fakes();
	CK(panel_live_apply(1, &fake_hw) == 0 && strcmp(calls, "MBR") == 0 &&
	   madctl_seen[0] == 0x28 && bl_panel_seen == 1,
	   "live, alternate: 0x28 written back, then backlight, then redraw");

	reset_fakes();
	write_rc = -5;
	CK(panel_live_apply(1, &fake_hw) == -5 && strcmp(calls, "M") == 0 &&
	   bl_panel_seen == 0xEE,
	   "live, the write fails: no backlight, no redraw, the errno back");
}

static void msg_checks(void)
{
	int err;

	reset_fakes();
	CK(panel_handle_msg("alternate", &fake_msg, &err) ==
	   PANEL_MSG_SET_ALTERNATE && err == 0 && stored == 1 &&
	   strcmp(calls, "SAMBR") == 0 && madctl_seen[0] == 0x28 &&
	   bl_panel_seen == 1,
	   "msg alternate: stored, then 0x28, backlight, redraw");

	reset_fakes();
	CK(panel_handle_msg("standard", &fake_msg, &err) ==
	   PANEL_MSG_SET_STANDARD && err == 0 && stored == 0 &&
	   strcmp(calls, "SAMBR") == 0 && madctl_seen[0] == 0xA0 &&
	   bl_panel_seen == 0,
	   "msg standard: stored, then 0xA0, backlight, redraw");

	{
		const char *bad[] = { "Standard", "alternate ", "", "rgb",
				      "standardx", "alt" };

		for (size_t i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
			char name[128];

			reset_fakes();
			err = 99;
			snprintf(name, sizeof(name),
				 "msg \"%s\": unknown, nothing stored or written",
				 bad[i]);
			CK(panel_handle_msg(bad[i], &fake_msg, &err) ==
			   PANEL_MSG_UNKNOWN && err == 0 && ncalls == 0, name);
		}
	}

	reset_fakes();
	err = 99;
	CK(panel_handle_msg(NULL, &fake_msg, &err) == PANEL_MSG_UNKNOWN &&
	   err == 0 && ncalls == 0,
	   "msg with no panel field: unknown, nothing stored or written");

	reset_fakes();
	store_rc = -28;
	CK(panel_handle_msg("alternate", &fake_msg, &err) ==
	   PANEL_MSG_STORE_FAILED && err == -28 && strcmp(calls, "S") == 0 &&
	   nmadctl == 0 && bl_panel_seen == 0xEE,
	   "msg, the store fails: no MADCTL, no backlight, the errno back");

	reset_fakes();
	write_rc = -116;
	CK(panel_handle_msg("alternate", &fake_msg, &err) ==
	   PANEL_MSG_NOT_SHOWN && err == -116 && stored == 1 &&
	   strcmp(calls, "SAM") == 0 && bl_panel_seen == 0xEE,
	   "msg, the MADCTL write fails: stored, no backlight, no redraw,"
	   " said as not shown");
}

int main(void)
{
	CK(backlight_next_level(60, +1) == 80, "up from 60 -> 80");
	CK(backlight_next_level(60, -1) == 40, "down from 60 -> 40");
	CK(backlight_next_level(80, +1) == 100, "up from 80 -> 100");
	CK(backlight_next_level(100, +1) == 100, "clamp at 100");
	CK(backlight_next_level(20, -1) == 20, "clamp at 20");
	CK(backlight_next_level(100, -1) == 80, "down from 100 -> 80");

	/* The two panels (firmware/src/panel.h, issue #23). The values are the
	 * ones the retired build option produced: 0xA0 and a 60% duty for the
	 * standard panel, the driver's own 0x28 and full duty for the
	 * alternate one. A byte other than 1 is the standard panel. */
	CK(panel_madctl(0) == 0xA0, "standard panel: MADCTL MV|MY, RGB = 0xA0");
	CK(panel_madctl(1) == 0x28, "alternate panel: MADCTL BGR|MV = 0x28");
	CK(panel_madctl(7) == 0xA0, "a stray panel byte is the standard panel");
	CK(panel_bl_scale_pct(0) == 60, "standard panel: backlight at 60%");
	CK(panel_bl_scale_pct(1) == 100, "alternate panel: backlight at 100%");
	CK(panel_bl_scale_pct(7) == 60, "a stray panel byte: backlight at 60%");

	pulse_checks();
	boot_checks();
	live_checks();
	msg_checks();

	/* Quiet hours. Times are minutes since local midnight. */
#define HM(h, m) ((h) * 60 + (m))
	CK(!backlight_quiet_now(false, HM(22, 0), HM(7, 0), true, HM(23, 0)),
	   "quiet: schedule off is never quiet");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), false, HM(23, 0)),
	   "quiet: time unknown is never quiet");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), false, 0),
	   "quiet: time unknown at 00:00 is never quiet");

	/* Same-day window, 13:00-15:00. */
	CK(backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(14, 0)),
	   "quiet: 14:00 inside 13:00-15:00");
	CK(backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(13, 0)),
	   "quiet: From inclusive (13:00 in 13:00-15:00)");
	CK(!backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(15, 0)),
	   "quiet: Until exclusive (15:00 not in 13:00-15:00)");
	CK(backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(14, 59)),
	   "quiet: 14:59 inside 13:00-15:00");
	CK(!backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(12, 59)),
	   "quiet: 12:59 before 13:00-15:00");
	CK(!backlight_quiet_now(true, HM(13, 0), HM(15, 0), true, HM(3, 0)),
	   "quiet: 03:00 outside 13:00-15:00 (no wrap)");

	/* Across midnight, 22:00-07:00. */
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(23, 0)),
	   "quiet: 23:00 inside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(3, 0)),
	   "quiet: 03:00 inside 22:00-07:00");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(7, 0)),
	   "quiet: 07:00 is Until, not quiet");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(6, 59)),
	   "quiet: 06:59 still inside 22:00-07:00");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(12, 0)),
	   "quiet: 12:00 outside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(22, 0)),
	   "quiet: 22:00 is From, quiet");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(21, 59)),
	   "quiet: 21:59 not yet quiet");

	/* Midnight edges. */
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(0, 0)),
	   "quiet: 00:00 inside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(7, 0), true, HM(23, 59)),
	   "quiet: 23:59 inside 22:00-07:00");
	CK(backlight_quiet_now(true, HM(0, 0), HM(7, 0), true, HM(0, 0)),
	   "quiet: From 00:00 inclusive at 00:00");
	CK(!backlight_quiet_now(true, HM(0, 0), HM(7, 0), true, HM(23, 59)),
	   "quiet: 23:59 outside 00:00-07:00");
	CK(backlight_quiet_now(true, HM(22, 0), HM(0, 0), true, HM(23, 59)),
	   "quiet: 23:59 inside 22:00-00:00");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(0, 0), true, HM(0, 0)),
	   "quiet: Until 00:00 exclusive at 00:00");

	/* Equal times: an empty window, never a whole day. */
	CK(!backlight_quiet_now(true, HM(22, 0), HM(22, 0), true, HM(22, 0)),
	   "quiet: equal times never quiet (at that time)");
	CK(!backlight_quiet_now(true, HM(22, 0), HM(22, 0), true, HM(3, 0)),
	   "quiet: equal times never quiet (elsewhere)");
	CK(!backlight_quiet_now(true, 0, 0, true, 0),
	   "quiet: equal 00:00 times never quiet");
#undef HM
	printf(failures ? "\n%d FAILED\n" : "\nALL PASSED\n", failures);
	return failures ? 1 : 0;
}
