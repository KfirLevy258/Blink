#ifndef BACKLIGHT_H
#define BACKLIGHT_H

#include <stdint.h>

/* Load the persisted level and drive the PWM. Call once, after cfg_init(). */
void backlight_init(void);

/* Step one level in `dir` (+1 brighter, -1 dimmer), clamp to the 20..100
 * scale, persist, and apply. A no-op at the ends. */
void backlight_step(int dir);

/* Current backlight percent -- one of 20/40/60/80/100. */
uint8_t backlight_get(void);

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
