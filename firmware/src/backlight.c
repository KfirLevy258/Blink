/*
 * Screen backlight: the owner's brightness level, and quiet hours.
 *
 * Owns the LEDC PWM. Two percents live here, and keeping them apart is the
 * point of this file:
 *
 *   `level` is what the owner picked in settings. It is what backlight_get()
 *   answers, and the only value ever written to cfg.
 *
 *   `shown` is what the panel is driven at. Normally it equals `level`.
 *   Inside the quiet-hours window (cfg_get_quiet: on, From, Until, in local
 *   minutes since midnight) it is held at no more than BACKLIGHT_QUIET_PCT,
 *   and it goes back to `level` when the window closes.
 *
 * The dim is never stored. If it were, a board that lost power at 02:00
 * would boot at 20% in the middle of the afternoon, and the owner's real
 * choice would be gone the first time the window opened -- the settings
 * screen would show 20% and there would be nothing to go back to at Until.
 * So a level tapped during quiet hours is persisted as the owner's, while the
 * panel stays at 20% until the window ends.
 *
 * The board has no clock of its own: the local time of day comes from the
 * daemon's time message (net_time_local). Until that message has arrived,
 * nothing here happens -- the panel shows `level`, exactly as it did before
 * quiet hours existed. That includes every boot, so backlight_init never
 * dims. Once it has arrived, net_time counts forward from uptime, so a board
 * whose daemon then goes away -- a laptop shut for the night, the case this
 * exists for -- still knows when 22:00 and 07:00 come round.
 *
 * The window is re-checked by backlight_tick(), once a second, from the main
 * loop and from the doze (ui_sleep.c), and at once by backlight_quiet_changed()
 * when the schedule is edited.
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/sys/printk.h>

#include "backlight.h"
#include "cfg_store.h"
#include "net_time.h"

static const struct pwm_dt_spec bl = PWM_DT_SPEC_GET(DT_NODELABEL(backlight));
static uint8_t level = 100;	/* the owner's percent; loaded in backlight_init */
static uint8_t shown = 100;	/* the percent on the panel; see the top */
static int64_t last_check_ms;	/* backlight_tick's rate limit */

/*
 * Production-panel brightness calibration.
 *
 * The production batch is far brighter than the pilot units at the same PWM
 * duty -- compared side by side at an identical 80% setting on 2026-08-21, the
 * new panel had to be stepped down before it matched. Different backlight LEDs
 * or panel transmissivity; nothing to do with the pixel data.
 *
 * Scaling the duty rather than lowering the default keeps a given percent
 * meaning the same amount of light on either batch, and leaves the user the
 * full 20..100 range instead of shrinking it. The factor is BLINK_BL_SCALE_PCT
 * (100 on pilot builds, so they are unaffected) -- tune it by eye.
 *
 * The multiply is 64-bit on purpose: period is in nanoseconds (1 ms at the
 * 1 kHz in the overlay = 1e6), and 1e6 * 100 * 60 overflows 32 bits.
 */

static void apply(void)
{
	if (!pwm_is_ready_dt(&bl)) {
		return;
	}
	/* The shown value, in the line the console allowlist already passes:
	 * a window opening reads as "[bl] 20%" and closing as the owner's
	 * level, with no new tag to add to logbook.console_line. */
	printk("[bl] %d%%\n", shown);
	pwm_set_pulse_dt(&bl, (uint32_t)((uint64_t)bl.period * shown *
					  CONFIG_BLINK_BL_SCALE_PCT / 10000U));
}

/* Work out what the panel should show now, and drive it only on a change --
 * this runs every second, and an unchanged PWM write would print a console
 * line every second as well. */
static void recompute(void)
{
	uint8_t on = 0;
	uint16_t from = 0, until = 0;
	int hh = -1, mm = 0;
	bool have_time = net_time_local(&hh, &mm);
	bool quiet;
	uint8_t want;

	cfg_get_quiet(&on, &from, &until);
	quiet = backlight_quiet_now(on != 0, from, until, have_time,
				    hh * 60 + mm);
	want = level;
	if (quiet && want > BACKLIGHT_QUIET_PCT) {
		want = BACKLIGHT_QUIET_PCT;
	}
	if (want != shown) {
		shown = want;
		apply();
	}
}

void backlight_init(void)
{
	level = cfg_get_bright_pct();
	/* Not quiet: the daemon's time message cannot have arrived yet. */
	shown = level;
	apply();
}

void backlight_tick(void)
{
	int64_t now = k_uptime_get();

	/* Once a second is plenty for a window set to the minute, and keeps the
	 * cfg read and the time conversion off every 5 ms doze frame gap. */
	if (now - last_check_ms < 1000) {
		return;
	}
	last_check_ms = now;
	recompute();
}

void backlight_quiet_changed(void)
{
	recompute();
}

void backlight_step(int dir)
{
	uint8_t next = backlight_next_level(level, dir);

	if (next == level) {
		return;		/* already at an end */
	}
	level = next;
	cfg_set_bright_pct(level);
	recompute();
}

void backlight_set(uint8_t pct)
{
	/* Snap rather than trust the caller: the levels are a fixed set, and a
	 * value off the scale would persist and come back at the next boot as
	 * a brightness the UI has no way to represent. */
	int v = ((int)pct + 10) / 20 * 20;

	if (v < 20) {
		v = 20;
	}
	if (v > 100) {
		v = 100;
	}
	if ((uint8_t)v == level) {
		return;
	}
	level = (uint8_t)v;
	/* The owner's choice is stored even inside quiet hours; recompute()
	 * then decides whether the panel may show it yet. */
	cfg_set_bright_pct(level);
	recompute();
}

uint8_t backlight_get(void)
{
	return level;
}
