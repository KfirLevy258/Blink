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
#include "panel.h"

static const struct pwm_dt_spec bl = PWM_DT_SPEC_GET(DT_NODELABEL(backlight));
static uint8_t level = 100;	/* the owner's percent; loaded in backlight_init */
static uint8_t shown = 100;	/* the percent on the panel; see the top */
static uint8_t panel = PANEL_ID_STANDARD;	/* the panel on the glass; see below */
static int64_t last_check_ms;	/* backlight_tick's rate limit */

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
 * The panel (the `panel` variable) is kept here rather than read from the
 * record on every apply.
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
	/* The shown value, in the line the console allowlist already passes:
	 * a window opening reads as "[bl] 20%" and closing as the owner's
	 * level, with no new tag to add to logbook.console_line. */
	printk("[bl] %d%%\n", shown);
	pwm_set_pulse_dt(&bl, panel_bl_pulse(bl.period, shown, panel));
}

void backlight_reapply(uint8_t now)
{
	panel = now;
	apply();
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
	panel = cfg_get_panel();
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
