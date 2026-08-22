/*
 * Screen backlight: a manual brightness level the user sets in settings.
 *
 * Owns the LEDC PWM and the current percent. There is no automatic schedule --
 * the level is loaded at boot and only ever changes when the user steps it.
 */
#include <zephyr/kernel.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/sys/printk.h>

#include "backlight.h"
#include "cfg_store.h"

static const struct pwm_dt_spec bl = PWM_DT_SPEC_GET(DT_NODELABEL(backlight));
static uint8_t level = 100;	/* percent; real value loaded in backlight_init */

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
 * full 20..100 range instead of shrinking it. The factor is CLAUGE_BL_SCALE_PCT
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
	printk("[bl] %d%%\n", level);
	pwm_set_pulse_dt(&bl, (uint32_t)((uint64_t)bl.period * level *
					  CONFIG_CLAUGE_BL_SCALE_PCT / 10000U));
}

void backlight_init(void)
{
	level = cfg_get_bright_pct();
	apply();
}

void backlight_step(int dir)
{
	uint8_t next = backlight_next_level(level, dir);

	if (next == level) {
		return;		/* already at an end */
	}
	level = next;
	cfg_set_bright_pct(level);
	apply();
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
	cfg_set_bright_pct(level);
	apply();
}

uint8_t backlight_get(void)
{
	return level;
}
