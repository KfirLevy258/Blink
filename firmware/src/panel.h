#ifndef PANEL_H
#define PANEL_H

/*
 * The two display modules this board is built with, and what differs between
 * them (issue #23).
 *
 * Which one a board carries used to be a build option (the "pilot" build and
 * its overlay file, both retired), and the choice of build was the choice of panel:
 * the wrong image on a board came out mirrored, colour-swapped and too bright
 * or too dim, and nothing on the board could say so. Now the image is one and
 * the record chooses (cfg_get_panel, set with `blink config panel
 * standard|alternate`); this header is everything the choice changes.
 *
 * Header-only and pure, with no Zephyr include, so tests/backlight/host_test.c
 * reaches it with no extra sources (tests/ci/check_host_tests.sh compiles that
 * test alone). Any value other than CFG_PANEL_ALTERNATE (1) is the standard
 * panel, the one every desk in the field has; cfg_store.h's enum names them,
 * and the numbers are repeated here so this file needs nothing else.
 *
 * Measured on hardware 2026-08-21, the standard (production) panels differ
 * from the alternate ones (the two pilot boards) three ways. The registers we
 * send are byte-identical; the difference is in the module.
 *
 *  1. Mirrored. The usual cure, the SS bit in DISCTRL (B6h), was tried: the
 *     built devicetree carried it (disctrl = [0a a2 27 04]) and the image did
 *     not budge. MADCTL (36h) is honoured, so the fix lives there. It cannot
 *     be a devicetree `rotation`: the ili9xxx driver's CMD_SET_1 table reaches
 *     only MV (90 deg) and MV|MX|MY (270 deg) in landscape, a pure 180 deg turn
 *     apart, and neither un-mirrors. The standard panel needs MV|MY.
 *
 *  2. RGB, not BGR. The driver hardcodes the BGR bit for every orientation.
 *     With a six-bar test pattern of known values the RED bar read blue and
 *     the BLUE bar read red, GREEN stayed green and white/black were exact:
 *     a clean channel exchange, no inversion or gamma error. Clearing bit 3
 *     restores it. (Green surviving untouched is why eyeballing a green UI
 *     element misdiagnosed this.)
 *
 *  3. Far brighter at the same PWM duty. Compared side by side at an
 *     identical 80% setting, the standard panel had to be stepped down before
 *     it matched -- different backlight LEDs or transmissivity, nothing to do
 *     with the pixels. So its duty is scaled to 60% of nominal, rather than
 *     lowering the default level, which would have shrunk the usable 20..100
 *     range instead of recentring it: a given percent means the same light on
 *     either panel.
 *
 * The alternate panel needs none of it: the driver's own rotation=90 MADCTL,
 * BGR|MV = 0x28, is right for it, mirror and colour order alike, at full duty.
 * That value is written back explicitly rather than "left alone" so a live
 * change from standard to alternate works without a reboot.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>

#define PANEL_ID_STANDARD  0u	/* cfg_store.h's CFG_PANEL_STANDARD */
#define PANEL_ID_ALTERNATE 1u	/* cfg_store.h's CFG_PANEL_ALTERNATE */

#define PANEL_MADCTL_MY  0x80u	/* row address order */
#define PANEL_MADCTL_MV  0x20u	/* row/column exchange: landscape */
#define PANEL_MADCTL_BGR 0x08u	/* colour order; clear = RGB */

static inline uint8_t panel_madctl(uint8_t panel)
{
	if (panel == PANEL_ID_ALTERNATE) {
		return PANEL_MADCTL_BGR | PANEL_MADCTL_MV;	/* 0x28 */
	}
	return PANEL_MADCTL_MV | PANEL_MADCTL_MY;		/* 0xA0, RGB */
}

/* The backlight duty, as a percent of the nominal level (backlight.c). */
static inline uint8_t panel_bl_scale_pct(uint8_t panel)
{
	return panel == PANEL_ID_ALTERNATE ? 100 : 60;
}

/*
 * The PWM pulse for `level` percent on `panel`, out of a `period` (both in
 * the PWM's units; backlight.c passes nanoseconds). backlight.c's apply()
 * drives exactly this, so tests/backlight/host_test.c checks the duty the
 * board really sets rather than a copy of the sum.
 *
 * The multiply is 64-bit on purpose: the period is 1e6 ns at the overlay's
 * 1 kHz, and 1e6 * 100 * 60 is 6e9, past 32 bits -- a 32-bit product would
 * wrap and the standard panel would come up at a near-random duty. A level
 * above 100 is not one backlight.c can hold (it snaps to 20..100), but it is
 * clamped here as well, so the pulse can never exceed the period.
 */
static inline uint32_t panel_bl_pulse(uint32_t period, uint8_t level,
				      uint8_t panel)
{
	if (level > 100) {
		level = 100;
	}
	return (uint32_t)((uint64_t)period * level * panel_bl_scale_pct(panel) /
			  10000U);
}

/*
 * The hardware a panel change touches, as functions, so the decisions below
 * are the same code on the board (main.c fills these with the DBI bus, the
 * PWM and LVGL) and in the host tests (which fill them with recorders).
 *
 *   write_madctl       send one MADCTL; 0, or a negative errno
 *   backlight_reapply  drive the PWM again at the current level for this
 *                      panel (backlight.c), persisting nothing
 *   redraw             ask for the whole screen to be drawn again
 */
struct panel_ops {
	int (*write_madctl)(uint8_t madctl);
	void (*backlight_reapply)(uint8_t panel);
	void (*redraw)(void);
};

enum panel_boot_result {
	PANEL_BOOT_WROTE,	/* standard: MADCTL written, *madctl_out holds it */
	PANEL_BOOT_KEPT,	/* alternate: the driver's own MADCTL left alone */
	PANEL_BOOT_FAILED,	/* standard, and the write failed */
};

/*
 * At boot (main.c's panel_fix_madctl). The alternate panel writes nothing:
 * the driver's own rotation=90 MADCTL is already right for it, and leaving
 * the driver's init untouched on the boards that never needed a fix keeps
 * their boot exactly what it was under the old alternate build. Any other
 * byte is the standard panel and writes 0xA0 once.
 */
static inline enum panel_boot_result panel_boot(uint8_t panel,
						 const struct panel_ops *ops,
						 uint8_t *madctl_out)
{
	uint8_t madctl;

	if (panel == PANEL_ID_ALTERNATE) {
		return PANEL_BOOT_KEPT;
	}
	madctl = panel_madctl(panel);
	*madctl_out = madctl;
	return ops->write_madctl(madctl) == 0 ? PANEL_BOOT_WROTE
					      : PANEL_BOOT_FAILED;
}

/*
 * Put `panel` on the glass now, with no reboot (main.c's panel_apply): write
 * panel_madctl(panel) -- in both directions, so it never relies on what the
 * driver wrote at init; going back to alternate has to undo the standard
 * panel's value, and the driver will not write its own again until the next
 * boot -- then reapply the backlight and redraw, since everything already on
 * the panel was drawn for the other module.
 *
 * All or nothing. Until review round 4 of issue #23 the MADCTL write's result
 * was thrown away and the backlight was rescaled regardless, so a failed
 * write left the board brighter or dimmer for the new module while still
 * mirrored and colour-swapped for the old one, and the board said "panel set".
 * Now a failed write changes nothing else on the glass and its errno is
 * returned, for the caller to say so.
 *
 * Call it only from the main thread, which owns LVGL and the display bus.
 * There is no flush thread (prj.conf leaves LV_Z_FLUSH_THREAD off), so every
 * display_write -- a window set and its pixel write -- happens inside
 * lv_timer_handler() or the boot and sleep clip players on that thread, and a
 * MADCTL command sent from between them can never land in the middle of one.
 * proto.c's dispatch() runs there (proto_service, from main.c's loop), the
 * same reason ui_theme_wire() may repaint straight from it.
 */
static inline int panel_live_apply(uint8_t panel, const struct panel_ops *ops)
{
	int r = ops->write_madctl(panel_madctl(panel));

	if (r != 0) {
		return r;
	}
	ops->backlight_reapply(panel);
	ops->redraw();
	return 0;
}

/*
 * The `panel` message's two effects (proto.c fills these):
 *
 *   store  persist the panel (cfg_set_panel); 0, or a negative errno
 *   apply  put it on the glass now (main.c's panel_apply, which is
 *          panel_live_apply over the real hardware); 0, or a negative errno
 */
struct panel_msg_ops {
	int (*store)(uint8_t panel);
	int (*apply)(uint8_t panel);
};

enum panel_msg_result {
	PANEL_MSG_SET_STANDARD,	/* stored and on the glass */
	PANEL_MSG_SET_ALTERNATE,	/* stored and on the glass */
	PANEL_MSG_UNKNOWN,	/* absent, or not a name we know: nothing done */
	PANEL_MSG_STORE_FAILED,	/* not stored, so not applied; *err holds why */
	PANEL_MSG_NOT_SHOWN,	/* stored, but the MADCTL write failed; *err */
};

/*
 * The whole `panel` message after proto.c's cable-only check: `value` is the
 * message's "panel" string, or NULL when it had none. Exact names only, as
 * the CLI sends them -- "Standard" or "alternate " is a name the board does
 * not know, and is ignored rather than guessed at.
 *
 * Stored first, then applied, so the glass never shows a panel the record
 * does not hold: a board that could not store it keeps what it had, on flash
 * and on the glass alike, and a reboot shows the same thing. When the store
 * worked and the live write did not, the choice is already on flash and the
 * next boot puts it on the glass (panel_boot), which is what the board says.
 */
static inline enum panel_msg_result panel_handle_msg(
	const char *value, const struct panel_msg_ops *ops, int *err)
{
	uint8_t v;
	int r;

	*err = 0;
	if (value == NULL) {
		return PANEL_MSG_UNKNOWN;
	}
	if (strcmp(value, "standard") == 0) {
		v = PANEL_ID_STANDARD;
	} else if (strcmp(value, "alternate") == 0) {
		v = PANEL_ID_ALTERNATE;
	} else {
		return PANEL_MSG_UNKNOWN;
	}
	r = ops->store(v);
	if (r != 0) {
		*err = r;
		return PANEL_MSG_STORE_FAILED;
	}
	r = ops->apply(v);
	if (r != 0) {
		*err = r;
		return PANEL_MSG_NOT_SHOWN;
	}
	return v == PANEL_ID_ALTERNATE ? PANEL_MSG_SET_ALTERNATE
				       : PANEL_MSG_SET_STANDARD;
}

/*
 * panel_live_apply over the board's own hardware. Defined in main.c, which
 * owns the DBI bus handle; declared here for proto.c. Host tests that include
 * this header never call it.
 */
int panel_apply(uint8_t panel);

#endif /* PANEL_H */
