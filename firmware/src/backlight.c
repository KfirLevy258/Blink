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
#include "panel.h"

static const struct pwm_dt_spec bl = PWM_DT_SPEC_GET(DT_NODELABEL(backlight));
static uint8_t level = 100;	/* percent; real value loaded in backlight_init */
static uint8_t shown = PANEL_ID_STANDARD;	/* the panel on the glass */

/*
 * Per-panel brightness calibration.
 *
 * The standard panel is far brighter than the alternate one at the same PWM
 * duty, so its duty is scaled down (panel.h has the 2026-08-21 measurement and
 * why it is a scale rather than a lower default). The factor used to be a
 * build option; it is the stored panel's now, read at boot, and a live
 * `blink config panel` change takes effect through backlight_reapply() with
 * no reboot. The sum, 64-bit multiply and all, is panel_bl_pulse in panel.h,
 * where the host tests reach it.
 *
 * The panel is kept here rather than read from the record on every apply.
 * The record can hold a panel the glass is not showing: a live change stores
 * first and then writes MADCTL, and when that write fails the board keeps the
 * old module's MADCTL until the next boot. Reading the record, the next
 * brightness step would have scaled for the new module on a screen still
 * set up for the old one -- the half-applied change panel_live_apply exists
 * to prevent. So only backlight_init (the record, at boot) and
 * backlight_reapply (a live change whose MADCTL write worked) move it.
 */

static void apply(void)
{
	if (!pwm_is_ready_dt(&bl)) {
		return;
	}
	printk("[bl] %d%%\n", level);
	pwm_set_pulse_dt(&bl, panel_bl_pulse(bl.period, level, shown));
}

void backlight_reapply(uint8_t now)
{
	shown = now;
	apply();
}

void backlight_init(void)
{
	level = cfg_get_bright_pct();
	shown = cfg_get_panel();
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
