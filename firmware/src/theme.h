/*
 * Themes: one palette table, indexed by a stable id, that every screen reads.
 *
 * Pure C on purpose -- no LVGL, no Zephyr -- so tests/theme/host_test.c can
 * check the real table rather than a copy of it. The copy is how this project
 * got burned before: tests/usage_contrast duplicated six colours out of
 * usage_view.c and needed a grep at the end to prove the two lists still
 * agreed. Here there is one list, and the test includes it.
 *
 * The ids are a wire and storage contract (orch contract 2026-10-27): the PC
 * names a theme by number and cfg_store keeps that number across reboots and
 * updates. NEVER renumber; append only.
 */
#ifndef THEME_H
#define THEME_H

#include <stdbool.h>
#include <stdint.h>

enum theme_id {
	THEME_DARK = 0,		/* the 1.3.4 look, exactly; the default */
	THEME_NEON = 1,
	THEME_BOLD = 2,
	THEME_MONO = 3,
	THEME_OCEAN = 4,
	THEME_SYNTHWAVE = 5,
	THEME_FOREST = 6,
	THEME_RETRO_LCD = 7,
	THEME_CVD_SAFE = 8,
	THEME_LIGHT = 9,
	THEME_LIGHT_SOFT = 10,
	THEME_LIGHT_INK = 11,
	THEME_CUSTOM = 12,
};

#define THEME_BUILTIN_N 12		/* ids 0..11 */
#define THEME_N 13			/* ...plus custom */

/* Arc widths a custom theme may ask for: thin, normal, thick. Built-ins may
 * use others (Synthwave and Soft are 8), but the wire accepts only these. */
#define THEME_W_THIN 5
#define THEME_W_NORMAL 12
#define THEME_W_THICK 22
#define THEME_ARC_W_MAX THEME_W_THICK

/*
 * Every colour any screen uses, by ROLE, as 0xRRGGBB.
 *
 * More than the seven a custom theme supplies, because the 1.3.4 screens used
 * more than seven and Dark has to reproduce them exactly: an update must not
 * repaint the desk of everyone who never asked for a theme. The settings
 * panel's own card, rule and status colours were never the gauge's colours
 * (its green is #2ECC71, the gauge's #0DA243), so they are roles of their own
 * rather than an approximation of each other.
 */
struct palette {
	uint32_t bg;		/* every screen's ground */
	uint32_t panel;		/* provider pill, peek card, toast */
	uint32_t track;		/* empty arc; control borders */
	uint32_t text;		/* numbers, titles, the dot */
	uint32_t dim;		/* captions, countdowns, clock */
	uint32_t grey;		/* chevrons, idle pips and rail */
	uint32_t low;		/* under 60% -- also "live" */
	uint32_t mid;		/* 60% to 90% */
	uint32_t high;		/* 90% and above; error lines */
	uint32_t g1, g2;	/* per-gauge arc colours (by_gauge only) */
	/* Settings panel. */
	uint32_t card;		/* row fill */
	uint32_t line;		/* section rules */
	uint32_t ok, warn, bad;	/* status text and buttons */
	uint32_t danger_bg, danger_bd;
	/* Style. */
	uint8_t arc_w;		/* px */
	bool flat;		/* square arc ends instead of round */
	bool dot;		/* the ball at the end of the arc */
	bool by_gauge;		/* arcs wear g1/g2 below 90% */
	/*
	 * Added after the 2026-10-27 review, at the end so nothing that names
	 * the roles above has to change.
	 *
	 * low_text: `low` where it is TEXT -- the pip tally, the toast -- which
	 *   needs 4.5:1 where the arc only needs 3. Equal to `low` wherever
	 *   `low` already reads (Dark among them).
	 * ok_ink: the label on an `ok` fill, the update buttons. It was the
	 *   text colour, which in Mono and Ink is the fill colour (1.00:1).
	 */
	uint32_t low_text;
	uint32_t ok_ink;
};

/*
 * What a custom theme is made of: the seven colours the builder offers and
 * the three style switches. The same bytes cfg_store keeps and the wire
 * carries, so one validator covers both.
 */
struct theme_custom {
	uint32_t c[7];		/* bg, text, gauge1, gauge2, low, mid, high */
	uint8_t w;		/* THEME_W_* */
	bool flat;
	bool dot;
	bool by_gauge;
};

/* A built-in palette, or NULL for anything that is not 0..11. */
const struct palette *theme_builtin(int id);

/* Name for the theme screen, sentence case. A tile is 144 px wide and its
 * label is clipped to THEME_NAME_W, 124 px (ui_settings.c); the 96 px tile
 * this used to name went with the old grid. */
const char *theme_name(int id);

/* Does this custom theme make a usable screen? Checks every field's range
 * and that the text reads against the background: 3:1, less 0.005 for the
 * board's table rounding (the daemon refuses below 3.05, so its themes
 * never sit that close). */
bool theme_custom_valid(const struct theme_custom *t);

/* Expand a custom theme into a full palette. Dim text and the arc track are
 * mixed from the text and background (contract: 25% / 15%). */
void theme_derive(const struct theme_custom *t, struct palette *out);

/*
 * The palette every screen draws with right now.
 *
 * theme_select() switches to a built-in; theme_select_custom() expands and
 * switches to a custom one. Both refuse what they cannot honour and leave the
 * current palette alone, so a caller never has to undo a half-applied theme.
 * Neither touches storage or the screen: see ui_theme.c for that.
 */
const struct palette *theme_pal(void);
int theme_active_id(void);
bool theme_select(int id);
bool theme_select_custom(const struct theme_custom *t);

/* WCAG contrast ratio of two 0xRRGGBB colours, and a linear mix (t = 0 is
 * `a`, 1 is `b`). Exposed for the derivation and its test. */
double theme_contrast(uint32_t a, uint32_t b);

/* An sRGB channel value as linear light, 0..65535. A table, not pow(): see
 * theme.c. Exposed so the test can hold it to pow(). */
uint16_t theme_lin(uint8_t v);
uint32_t theme_mix(uint32_t a, uint32_t b, double t);

/*
 * The header dot's two "there is data" colours: `live` for a sound reading,
 * `stale` for an old one. Ordinarily low and mid, the green and the amber.
 *
 * Where both are neutral -- Mono's grey and white, Light ink's grey and
 * black, or a custom theme made the same way -- hue cannot tell them apart,
 * only strength can, and low-then-mid would put the BOLDER mark on the
 * stale reading and a healthy dot in the same grey family as the
 * disconnected one (review of fp/audit-fixes, NICE 6). There the two swap:
 * full ink while the reading is sound, fading to the lighter ink when it
 * goes old, and the disconnected grey after that.
 */
void theme_dot_colours(const struct palette *p, uint32_t *live,
		       uint32_t *stale);

/* The arc colour for gauge `which` (0 session, 1 weekly) at `pct`. */
uint32_t theme_arc_colour(const struct palette *p, int which, double pct);

/*
 * A `{"t":"theme",...}` line, parsed and validated as a whole.
 *
 * Returns false -- and leaves *out unspecified -- for ANY bad value: an
 * unknown op, an id out of range, a custom theme with six colours or a width
 * of 7. The contract is that a bad line is ignored entire, and the only way
 * to guarantee "no partial apply" is to decide before anything is applied.
 */
enum theme_op { THEME_OP_PREVIEW, THEME_OP_SAVE, THEME_OP_CANCEL };

struct theme_msg {
	enum theme_op op;
	int id;				/* unused for cancel */
	struct theme_custom custom;	/* only when id == THEME_CUSTOM */
};

bool theme_msg_parse(const char *json, struct theme_msg *out);

/*
 * theme_now's "h": which custom theme this is, so the PC can tell one custom
 * theme from another without the board sending all seven colours back.
 * Contract (Addendum 2, 2026-10-27): SHA-1 over 26 bytes -- id 12, the seven
 * colours as RGB888 in "c" order, then w, ends, dot, by -- and the first
 * four bytes as lowercase hex. `out` gets 8 characters and a NUL.
 */
void theme_custom_hash(const struct theme_custom *t, char out[9]);

/* Plain SHA-1, exposed for its test vector. */
void theme_sha1(const uint8_t *data, unsigned len, uint8_t out[20]);

/* Does a welcome line's "caps" array list `cap`? */
bool theme_caps_has(const char *json, const char *cap);

#endif /* THEME_H */
