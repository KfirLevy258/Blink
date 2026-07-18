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

static void apply(void)
{
	if (!pwm_is_ready_dt(&bl)) {
		return;
	}
	printk("[bl] %d%%\n", level);
	pwm_set_pulse_dt(&bl, bl.period * level / 100);
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

uint8_t backlight_get(void)
{
	return level;
}
