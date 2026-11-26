#ifndef BACKLIGHT_H
#define BACKLIGHT_H

#include <stdbool.h>
#include <stdint.h>

/* The most light quiet hours allow. Equal to the bottom of the 20..100 scale,
 * so a dimmed panel looks exactly like one the owner stepped all the way down,
 * and an owner already at 20% sees no change when the window opens. */
#define BACKLIGHT_QUIET_PCT 20

/* Load the persisted level and drive the PWM. Call once, after cfg_init().
 * The time of day is not known yet at boot, so this is never dimmed. */
void backlight_init(void);

/* Re-check the quiet-hours window, at most once a second. Call it from every
 * loop that runs for long: the main loop's 1 s tick and the doze. */
void backlight_tick(void);

/* Re-check the quiet-hours window now, without waiting for the next tick.
 * Call after the schedule is changed in settings, so the panel answers the
 * tap that changed it. */
void backlight_quiet_changed(void);

/* Step one level in `dir` (+1 brighter, -1 dimmer), clamp to the 20..100
 * scale, persist, and apply. A no-op at the ends. */
void backlight_step(int dir);

/* Set an absolute level, snapped to the 20..100 scale, persisted and applied.
 * A no-op if it is already there. The settings screen picks a level directly
 * now rather than stepping toward one. During quiet hours the new level is
 * stored but the panel stays at BACKLIGHT_QUIET_PCT until Until. */
void backlight_set(uint8_t pct);

/* Drive the PWM again at the current level, with `panel`'s scale (panel.h),
 * and keep that panel for every later step. For a live panel change, once
 * its MADCTL is on the glass (panel_live_apply); persists nothing. */
void backlight_reapply(uint8_t panel);

/* The owner's chosen percent -- one of 20/40/60/80/100. This is the stored
 * level, not what the panel shows: during quiet hours the panel is held at
 * BACKLIGHT_QUIET_PCT while this still answers what was picked, so the
 * settings screen keeps showing the owner's choice. */
uint8_t backlight_get(void);

/* Pure: whether quiet hours hold at `now_min` minutes since local midnight.
 *
 * Never quiet when the schedule is off, when the time of day is not known
 * (no time message from the daemon yet), or when From equals Until -- an empty
 * window rather than a whole day, so a schedule left at two equal times can
 * never dim the panel round the clock. From is inclusive and Until exclusive,
 * so 22:00-07:00 is dim at 22:00 and bright again at 07:00. When From is later
 * than Until the window runs past midnight. In the header so host tests reach
 * it with no Zephyr underneath. */
static inline bool backlight_quiet_now(bool on, int from_min, int until_min,
				       bool have_time, int now_min)
{
	if (!on || !have_time || from_min == until_min) {
		return false;
	}
	if (from_min < until_min) {
		return now_min >= from_min && now_min < until_min;
	}
	return now_min >= from_min || now_min < until_min;
}

/* Pure: the level reached by stepping `cur` by `dir` (+1/-1) within the
 * 20..100 scale in steps of 20. In the header so host tests can reach it. */
static inline uint8_t backlight_next_level(uint8_t cur, int dir)
{
	int v = (int)cur + dir * 20;

	if (v < 20) {
		v = 20;
	}
	if (v > 100) {
		v = 100;
	}
	return (uint8_t)v;
}

#endif /* BACKLIGHT_H */
