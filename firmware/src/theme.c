/*
 * The theme table and everything that can be decided about a theme without a
 * screen. See theme.h for why this file is pure C.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "theme.h"
#include "msg_parse.h"

/*
 * The built-in themes.
 *
 * The seven colours each theme is "about" (bg, text, the gauge pair and the
 * three severities) and its style come from the Theme Builder presets the user
 * picked from (artifact WG9LpsFRVuL7R6JsbwDUhB, 2026-10-25). The other roles
 * were derived from those seven -- dim text mixed toward the ground until it
 * clears 5:1, the track at 15% text, the settings card a step off the ground
 * -- and then corrected wherever tests/theme failed them. The corrections are
 * the part worth knowing about, so each one is named where it happens.
 *
 * Every number here is checked by tests/theme/host_test.c AFTER quantising to
 * RGB565, which is what the panel shows. A colour that passes at 24 bits and
 * fails at 16 is a colour that passes on a laptop and fails on the desk.
 */
static const struct palette builtin[THEME_BUILTIN_N] = {
	/*
	 * Dark is 1.3.4 to the byte. Every value below is what usage_view.c
	 * and ui_settings.c had as #defines before themes existed, so a board
	 * updated from 1.3.4 looks exactly as it did -- the reason for the
	 * twelve extra roles in struct palette. The settings panel's own
	 * green/amber/red (#2ECC71 and friends) were never the gauge's, and
	 * are kept apart here rather than "tidied" into one set.
	 */
	[THEME_DARK] = {
		.bg = 0x0E1116, .panel = 0x05070A, .track = 0x272C34,
		.text = 0xE6E8EB, .dim = 0x8A9199, .grey = 0x6B7280,
		.low = 0x0DA243, .mid = 0xBA8107,
		.high = 0xFF1900,
		.g1 = 0x4387DF, .g2 = 0x0DA243,
		.card = 0x161A20, .line = 0x20252D,
		.ok = 0x2ECC71, .warn = 0xF1C40F, .bad = 0xE74C3C,
		.danger_bg = 0x1E1412, .danger_bd = 0x7A2B23,
		.low_text = 0x0DA243, .ok_ink = 0xE6E8EB,
		.arc_w = 12, .flat = false, .dot = true,
	},
	/*
	 * Neon, Bold and Mono sit on pure black, and the derivation put the
	 * provider pill at the SAME black: a fill that means "you can press
	 * this" drawn in the colour of the ground. Lifted one step instead of
	 * darkened, since there is nothing darker than #000.
	 */
	[THEME_NEON] = {
		.bg = 0x05060A, .panel = 0x15161B, .track = 0x292A2F,
		.text = 0xF5F7FF, .dim = 0xB9BBC2, .grey = 0x65666C,
		.low = 0x00F0FF, .mid = 0xFFE600,
		.high = 0xFF2E88,
		.g1 = 0x00F0FF, .g2 = 0xB26BFF,
		.card = 0x131419, .line = 0x1D1E22,
		.ok = 0x00F0FF, .warn = 0xFFE600, .bad = 0xFF2E88,
		.danger_bg = 0x1E0A17, .danger_bd = 0x761843,
		.low_text = 0x00F0FF, .ok_ink = 0x05060A,
		.arc_w = THEME_W_THIN, .flat = false, .dot = false,
	},
	[THEME_BOLD] = {
		.bg = 0x000000, .panel = 0x161616, .track = 0x262626,
		.text = 0xFFFFFF, .dim = 0xBFBFBF, .grey = 0x666666,
		.low = 0x22C55E, .mid = 0xFACC15,
		.high = 0xEF4444,
		.g1 = 0x60A5FA, .g2 = 0x22C55E,
		.card = 0x0F0F0F, .line = 0x1A1A1A,
		.ok = 0x22C55E, .warn = 0xFACC15, .bad = 0xEF4444,
		.danger_bg = 0x180707, .danger_bd = 0x6C1F1F,
		.low_text = 0x22C55E, .ok_ink = 0x000000,
		.arc_w = THEME_W_THICK, .flat = true, .dot = false,
	},
	/*
	 * Black and white, with red reserved for the last 10%.
	 *
	 * Low and mid were both white, on the idea that the only threshold
	 * worth drawing is the last one. The builder's own check flagged it
	 * ("two levels look almost the same", audit of 2026-10-28), and it
	 * was right: crossing 60% changed nothing on the screen, so the arc
	 * said less than every other theme's. The low arc is now a grey that
	 * brightens to white at 60% -- the ink gets heavier as the window
	 * fills, which keeps the theme to black, white and the one red. #9C9C9C
	 * is 7.8:1 on the ground (6.9 on the pill, for the toast) and 171 from
	 * white by the builder's measure, where it asks for 60. Light ink is
	 * the same idea on paper.
	 */
	[THEME_MONO] = {
		.bg = 0x000000, .panel = 0x161616, .track = 0x262626,
		.text = 0xFFFFFF, .dim = 0xBFBFBF, .grey = 0x666666,
		.low = 0x9C9C9C, .mid = 0xFFFFFF,
		.high = 0xFF3B30,
		.g1 = 0xFFFFFF, .g2 = 0xBDBDBD,
		.card = 0x0F0F0F, .line = 0x1A1A1A,
		.ok = 0xFFFFFF, .warn = 0xFFFFFF, .bad = 0xFF3B30,
		.danger_bg = 0x1A0605, .danger_bd = 0x731B16,
		.low_text = 0x9C9C9C, .ok_ink = 0x000000,
		.arc_w = THEME_W_THIN, .flat = true, .dot = false,
	},
	[THEME_OCEAN] = {
		.bg = 0x0A1A2F, .panel = 0x060E1A, .track = 0x2B3A4D,
		.text = 0xE8F1FA, .dim = 0xB0BBC7, .grey = 0x637080,
		.low = 0x2EC4B6, .mid = 0xFFB347,
		.high = 0xFF6B6B,
		.g1 = 0x7FB2FF, .g2 = 0x2EC4B6,
		.card = 0x17273B, .line = 0x203043,
		.ok = 0x2EC4B6, .warn = 0xFFB347, .bad = 0xFF6B6B,
		.danger_bg = 0x222235, .danger_bd = 0x783E4A,
		.low_text = 0x2EC4B6, .ok_ink = 0x0A1A2F,
		.arc_w = THEME_W_NORMAL, .flat = false, .dot = true,
	},
	[THEME_SYNTHWAVE] = {
		.bg = 0x1A0B2E, .panel = 0x0E0619, .track = 0x3C2C4D,
		.text = 0xFBEAFF, .dim = 0xC3B2CB, .grey = 0x746482,
		.low = 0xFF71CE, .mid = 0xFFB86B,
		.high = 0xFF3860,
		.g1 = 0x01CDFE, .g2 = 0xFF71CE,
		.card = 0x28183B, .line = 0x302143,
		.ok = 0xFF71CE, .warn = 0xFFB86B, .bad = 0xFF3860,
		.danger_bg = 0x311033, .danger_bd = 0x811F44,
		.low_text = 0xFF71CE, .ok_ink = 0x1A0B2E,
		.arc_w = 8, .flat = false, .dot = true,
	},
	[THEME_FOREST] = {
		.bg = 0x0F1A14, .panel = 0x080E0B, .track = 0x303B34,
		.text = 0xEEF3E9, .dim = 0xB6BDB4, .grey = 0x687169,
		.low = 0x8FBF7F, .mid = 0xD9A441,
		.high = 0xD9643A,
		.g1 = 0xA3C4BC, .g2 = 0x8FBF7F,
		.card = 0x1C2721, .line = 0x253029,
		.ok = 0x8FBF7F, .warn = 0xD9A441, .bad = 0xD9643A,
		.danger_bg = 0x232118, .danger_bd = 0x6A3B25,
		.low_text = 0x8FBF7F, .ok_ink = 0x0F1A14,
		.arc_w = THEME_W_NORMAL, .flat = false, .dot = true,
	},
	/*
	 * The handheld's greens, and two colours it never had, on purpose.
	 *
	 * The preset's mid was #306230, the same as low, at 3.36:1 -- fine for
	 * an arc, short of the 4.5 a hint line needs. Its high was #0F380F,
	 * which is the TEXT colour, 1.20:1 from the amber: a used-up window
	 * and "Major outage" drew exactly like the ordinary screen, which the
	 * per-gauge note in usage_view.c (arc_tint) exists to forbid. Four
	 * greens cannot carry three levels and the text at 4.5:1 apiece on a
	 * ground this bright, so amber and red are an olive #3D3000 and a deep
	 * red #6E0F0F: dark enough to read on the LCD green, different enough
	 * in hue to read as a level. The low ARC keeps #306230; text in that
	 * level (the pip tally, the toast) uses #1F3F1F, which reads.
	 */
	[THEME_RETRO_LCD] = {
		.bg = 0x9BBC0F, .panel = 0x8DAF0F, .track = 0x86A80F,
		.text = 0x0F380F, .dim = 0x1F470F, .grey = 0x385F0F,
		.low = 0x306230, .mid = 0x3D3000,
		.high = 0x6E0F0F,
		.g1 = 0x306230, .g2 = 0x0F380F,
		.card = 0x93B40F, .line = 0x8DAF0F,
		.ok = 0x1F3F1F, .warn = 0x3D3000, .bad = 0x6E0F0F,
		.danger_bg = 0x8DAF0F, .danger_bd = 0x5C810F,
		.low_text = 0x1F3F1F, .ok_ink = 0x9BBC0F,
		.arc_w = THEME_W_NORMAL, .flat = true, .dot = false,
	},
	/*
	 * Colour-blind safe: the Okabe-Ito blue, orange and yellow, with
	 * LUMINANCE rising with urgency so the levels separate for someone who
	 * sees no hue at all.
	 *
	 * The preset (blue #1E88E5, orange #E69F00, vermillion #D55E00) measured
	 * 1.59, 1.71 and 1.07 between its levels -- the vermillion and the blue
	 * were the SAME brightness, so for a protanope the calmest and the
	 * most urgent arcs were one colour. Hue was the only thing separating
	 * them, which is exactly what this theme exists to not rely on.
	 *
	 * 3:1 between EVERY pair is not reachable, and that is arithmetic, not
	 * a palette choice: on this ground (luminance 0.006) the dimmest arc
	 * that is itself visible at 3:1 sits at 0.117, the next one 3:1 above it
	 * at 0.45, and the third would need 1.43 -- brighter than white. So the
	 * two ends are held 3:1 apart (3.9 here), each step to the next is at
	 * least 1.5:1, and the most urgent level is the brightest, because on
	 * a dark panel brightness is what the eye ranks first. Yellow for
	 * "almost used up" is the hazard-stripe idiom rather than the red one;
	 * for the people this theme is for, the red one never worked.
	 *
	 * Orange is #D69400 rather than Okabe-Ito's #E69F00: that sat 1.71
	 * above blue and 2.39 below yellow, and #D69400 evens the two steps
	 * at about 2.0 each, the most either can have. The blue reads as an
	 * arc (3.7:1) and not as text, so text in that level uses the sky
	 * blue the settings panel already had, #56B4E9.
	 */
	[THEME_CVD_SAFE] = {
		.bg = 0x0E1116, .panel = 0x08090C, .track = 0x2E3136,
		.text = 0xE6E8EB, .dim = 0xB0B2B6, .grey = 0x65686C,
		.low = 0x0072B2, .mid = 0xD69400,
		.high = 0xF0E442,
		.g1 = 0x0072B2, .g2 = 0xD69400,
		.card = 0x1B1E23, .line = 0x24262B,
		.ok = 0x56B4E9, .warn = 0xD69400, .bad = 0xF0E442,
		.danger_bg = 0x22200F, .danger_bd = 0x6E6626,
		.low_text = 0x56B4E9, .ok_ink = 0x0E1116,
		.arc_w = THEME_W_NORMAL, .flat = false, .dot = true,
	},
	/*
	 * The three light themes. Light mode is just these.
	 *
	 * PANEL is the one to watch. The first light previews drew the
	 * provider pill white on white -- a control that existed only as a
	 * word floating in the ground -- and Soft was worse: its pill was
	 * WHITER than its pale-grey ground. #EEF1F4 and #EDEDED are the fixes
	 * from those previews; Soft's goes darker than its ground for the same
	 * reason. tests/theme pins every light panel at least 1.1:1 off the
	 * background so it cannot quietly come back.
	 *
	 * Amber and red are darkened in all three: they double as hint and
	 * status-page text, and the presets were 3.39:1 on white. The first
	 * correction stopped at 4.52-4.54, one RGB565 step from failing, and
	 * on the settings card (#EEF1F4) those fell to 4.05-4.20; they are
	 * now 5.2-5.6 on the ground and 4.6+ on every fill. The green arc
	 * keeps its brightness; text in that level uses a darker green.
	 */
	[THEME_LIGHT] = {
		.bg = 0xFFFFFF, .panel = 0xEEF1F4, .track = 0xDBDCDC,
		.text = 0x111418, .dim = 0x4C4F52, .grey = 0x898A8D,
		.low = 0x0E9F48, .mid = 0x976100,
		.high = 0xCC2000,
		.g1 = 0x2F6FCB, .g2 = 0x0E9F48,
		.card = 0xEEF1F4, .line = 0xE7E8E8,
		.ok = 0x0B7F39, .warn = 0x976100, .bad = 0xCC2000,
		.danger_bg = 0xFCE9E6, .danger_bd = 0xF19C8C,
		.low_text = 0x0B7F39, .ok_ink = 0xFFFFFF,
		.arc_w = THEME_W_NORMAL, .flat = false, .dot = true,
	},
	/*
	 * Soft's preset ground was #F1F3F6, which the panel cannot show: RGB565
	 * rounds it to #F7F3F7 -- red and blue land on the same 5-bit step, a
	 * notch above green -- and the "pale grey" previewed as pale pink.
	 * #EFF3F7 is the cool grey nearest it that survives quantising exactly,
	 * which is the only kind of grey worth asking this panel for.
	 */
	[THEME_LIGHT_SOFT] = {
		.bg = 0xEFF3F7, .panel = 0xDFE3E8, .track = 0xD1D3D7,
		.text = 0x1B2027, .dim = 0x50555B, .grey = 0x7F8388,
		.low = 0x2E9E68, .mid = 0x875B0C,
		.high = 0xB3301E,
		.g1 = 0x3D7BD9, .g2 = 0x2E9E68,
		.card = 0xDFE3E8, .line = 0xDCDEE1,
		.ok = 0x21734B, .warn = 0x875B0C, .bad = 0xB3301E,
		.danger_bg = 0xEDE1E1, .danger_bd = 0xDDA299,
		.low_text = 0x21734B, .ok_ink = 0xEFF3F7,
		.arc_w = 8, .flat = false, .dot = false,
	},
	/*
	 * Ink on paper: pencil, then pen, then the red one.
	 *
	 * Low and mid were both black, so the arc looked the same at 10% and
	 * at 89%, and the builder's check flagged the shipped preset (audit of
	 * 2026-10-28). Under 60% now draws in the theme's own grey ink, the
	 * #555555 its second gauge already used, and turns black at 60%: the
	 * stroke gets heavier as the window fills and nothing new joins the
	 * palette. #555555 is 7.5:1 on the paper and 6.6 on the pill (as
	 * RGB565 draws it), so it holds as text too, and 147 from black by
	 * the builder's measure, where it asks for 60. Mid stays the text
	 * colour, which the light themes' 5.2:1 margin allows.
	 */
	[THEME_LIGHT_INK] = {
		.bg = 0xFFFFFF, .panel = 0xEDEDED, .track = 0xD9D9D9,
		.text = 0x000000, .dim = 0x404040, .grey = 0x8B8B8B,
		.low = 0x555555, .mid = 0x000000,
		.high = 0xD00600,
		.g1 = 0x000000, .g2 = 0x555555,
		.card = 0xEDEDED, .line = 0xE6E6E6,
		.ok = 0x000000, .warn = 0x000000, .bad = 0xD00600,
		.danger_bg = 0xFCE6E6, .danger_bd = 0xF28F8C,
		.low_text = 0x555555, .ok_ink = 0xFFFFFF,
		.arc_w = THEME_W_THIN, .flat = true, .dot = false,
	},
};

/* Tile labels. They are clipped to THEME_NAME_W, 124 px (ui_settings.c; see
 * theme.h), which holds roughly sixteen characters at Montserrat 14, so it is
 * "Colour-blind" and not "Colour-blind safe". The 96 px tile this was first
 * sized for went with the old grid. */
static const char *const names[THEME_N] = {
	[THEME_DARK] = "Dark",
	[THEME_NEON] = "Neon",
	[THEME_BOLD] = "Bold",
	[THEME_MONO] = "Mono",
	[THEME_OCEAN] = "Ocean",
	[THEME_SYNTHWAVE] = "Synthwave",
	[THEME_FOREST] = "Forest",
	[THEME_RETRO_LCD] = "Retro LCD",
	[THEME_CVD_SAFE] = "Colour-blind",
	[THEME_LIGHT] = "Light",
	[THEME_LIGHT_SOFT] = "Light soft",
	[THEME_LIGHT_INK] = "Light ink",
	[THEME_CUSTOM] = "Custom",
};

/*
 * The live palette. A pointer into the const table for a built-in, so a
 * built-in costs no RAM at all; a custom theme is expanded once into `custom`
 * and pointed at. That struct is the whole RAM price of the theme engine.
 */
static struct palette custom;
static const struct palette *cur = &builtin[THEME_DARK];
static int cur_id = THEME_DARK;

/*
 * The burn view's inks for `cur`, worked out once when the palette changes
 * rather than on every draw. theme_burn_inks() is a soft-float contrast walk
 * (no FPU doubles on the ESP32): the 2026-11-01 re-review measured it at
 * about 2.2 ms on Retro LCD and up to about 9.5 ms on a garish custom theme,
 * and burn_draw_cb() ran it on every LV_EVENT_DRAW_MAIN -- each strip of the
 * 80-strip wipe and every redraw after. The inks depend on the palette alone,
 * so theme_select() and theme_select_custom() set them, and the boot default
 * (Dark, before anything is selected) is filled on the first read. 16 bytes.
 */
static struct theme_burn_inks cur_inks;
static bool cur_inks_ok;

const struct palette *theme_builtin(int id)
{
	if (id < 0 || id >= THEME_BUILTIN_N) {
		return NULL;
	}
	return &builtin[id];
}

const char *theme_name(int id)
{
	if (id < 0 || id >= THEME_N) {
		return "";
	}
	return names[id];
}

/*
 * sRGB to linear light, one entry per channel value, scaled to 0..65535.
 *
 * A table because the alternative was libm: pow() for the 2.4 exponent and
 * lround() for the mix pulled about 4 KB into an image that only ever asks
 * this question of 256 inputs. 512 bytes of rodata answers all of them.
 * Generated by
 *   python3 -c "print([round((c/255/12.92 if c/255 <= 0.03928 else
 *     ((c/255 + 0.055)/1.055)**2.4) * 65535) for c in range(256)])"
 * and held to pow() entry by entry in tests/theme.
 */
static const uint16_t lin[256] = {
	    0,    20,    40,    60,    80,    99,   119,   139,
	  159,   179,   199,   219,   241,   264,   288,   313,
	  340,   367,   396,   427,   458,   491,   526,   562,
	  599,   637,   677,   718,   761,   805,   851,   898,
	  947,   997,  1048,  1101,  1156,  1212,  1270,  1330,
	 1391,  1453,  1517,  1583,  1651,  1720,  1790,  1863,
	 1937,  2013,  2090,  2170,  2250,  2333,  2418,  2504,
	 2592,  2681,  2773,  2866,  2961,  3058,  3157,  3258,
	 3360,  3464,  3570,  3678,  3788,  3900,  4014,  4129,
	 4247,  4366,  4488,  4611,  4736,  4864,  4993,  5124,
	 5257,  5392,  5530,  5669,  5810,  5953,  6099,  6246,
	 6395,  6547,  6700,  6856,  7014,  7174,  7335,  7500,
	 7666,  7834,  8004,  8177,  8352,  8528,  8708,  8889,
	 9072,  9258,  9445,  9635,  9828, 10022, 10219, 10417,
	10619, 10822, 11028, 11235, 11446, 11658, 11873, 12090,
	12309, 12530, 12754, 12980, 13209, 13440, 13673, 13909,
	14146, 14387, 14629, 14874, 15122, 15371, 15623, 15878,
	16135, 16394, 16656, 16920, 17187, 17456, 17727, 18001,
	18277, 18556, 18837, 19121, 19407, 19696, 19987, 20281,
	20577, 20876, 21177, 21481, 21787, 22096, 22407, 22721,
	23038, 23357, 23678, 24002, 24329, 24658, 24990, 25325,
	25662, 26001, 26344, 26688, 27036, 27386, 27739, 28094,
	28452, 28813, 29176, 29542, 29911, 30282, 30656, 31033,
	31412, 31794, 32179, 32567, 32957, 33350, 33745, 34143,
	34544, 34948, 35355, 35764, 36176, 36591, 37008, 37429,
	37852, 38278, 38706, 39138, 39572, 40009, 40449, 40891,
	41337, 41785, 42236, 42690, 43147, 43606, 44069, 44534,
	45002, 45473, 45947, 46423, 46903, 47385, 47871, 48359,
	48850, 49344, 49841, 50341, 50844, 51349, 51858, 52369,
	52884, 53401, 53921, 54445, 54971, 55500, 56032, 56567,
	57105, 57646, 58190, 58737, 59287, 59840, 60396, 60955,
	61517, 62082, 62650, 63221, 63795, 64372, 64952, 65535
};

uint16_t theme_lin(uint8_t v)
{
	return lin[v];
}

static double lum(uint32_t c)
{
	return (0.2126 * lin[(c >> 16) & 0xFF] + 0.7152 * lin[(c >> 8) & 0xFF] +
		0.0722 * lin[c & 0xFF]) / 65535.0;
}

double theme_contrast(uint32_t a, uint32_t b)
{
	double la = lum(a), lb = lum(b);
	double hi = la > lb ? la : lb, lo = la > lb ? lb : la;

	return (hi + 0.05) / (lo + 0.05);
}

uint32_t theme_mix(uint32_t a, uint32_t b, double t)
{
	uint32_t out = 0;

	for (int sh = 16; sh >= 0; sh -= 8) {
		double x = (a >> sh) & 0xFF, y = (b >> sh) & 0xFF;
		/* x and y are 0..255 and t is 0..1, so v is never negative and
		 * +0.5 then truncation is round-half-up, without libm. */
		long v = (long)(x + (y - x) * t + 0.5);

		out |= (uint32_t)(v < 0 ? 0 : v > 255 ? 255 : v) << sh;
	}
	return out;
}

static bool width_ok(uint8_t w)
{
	return w == THEME_W_THIN || w == THEME_W_NORMAL || w == THEME_W_THICK;
}

bool theme_custom_valid(const struct theme_custom *t)
{
	for (int i = 0; i < 7; i++) {
		if (t->c[i] > 0xFFFFFF) {
			return false;
		}
	}
	/* 3:1 is the daemon's floor too. The board checks it again because
	 * the board is what has to stay readable: a theme that arrives from
	 * anything but our own daemon -- or from a daemon with a bug -- must
	 * not be able to put text the colour of the ground on the one device
	 * whose whole job is being read. */
	/*
	 * 2.995, not 3.0: the daemon measures with pow() and this with a
	 * 16-bit table, and a pair it passed at exactly 3.0 can come out a
	 * hair under here (#595959 on black: 3.00 there, 2.998 here). A
	 * theme the PC accepted must not be refused for rounding. The PC
	 * itself refuses below 3.05, so nothing it sends sits this close.
	 */
	return width_ok(t->w) && theme_contrast(t->c[1], t->c[0]) >= 2.995;
}

/*
 * Dim text for a custom theme: the text moved toward the ground, but never so
 * far that it reads worse than 4.5:1 -- the ratio every built-in palette's dim
 * meets on the ground and on a settings card (tests/theme) -- or, when the
 * owner's own text is below that, worse than the text itself.
 *
 * The contract's plain 25% mix (2026-10-27) is kept wherever it is readable,
 * which is every custom theme with text of about 7:1 or better, so most
 * themes draw exactly as before. It stopped being enough at the floor: the
 * board accepts text at 3:1 (theme_custom_valid), and 25% of the ground
 * mixed into such text came to 2.0-2.3:1, for the clock, the window names
 * and every settings subtitle (2026-10-28 review, NICE-8). Here the mix
 * walks back a percent at a time; at 0 it is the text, which always passes,
 * so the loop ends. 26 contrast pairs at most, once per theme selection.
 * pc/theme_builder.html draws its preview with the same walk.
 */
#define DIM_MIN_CONTRAST 4.5

/*
 * A colour as the panel shows it: truncated to RGB565 as LVGL's
 * lv_color_to_u16 does, and widened back by repeating the top bits -- the
 * same model tests/theme measures every palette with. Measured in 24 bits
 * instead, the walk let through a dim 4.7% under its target once drawn (the
 * truncation darkens a colour, which on a dark ground is toward the ground;
 * worst of 20,000 random customs in tests/theme), so it measures what is
 * drawn.
 */
static uint32_t q565(uint32_t c)
{
	unsigned r = (c >> 16) & 0xFF, g = (c >> 8) & 0xFF, b = c & 0xFF;

	r = ((r >> 3) << 3) | (r >> 5);
	g = ((g >> 2) << 2) | (g >> 6);
	b = ((b >> 3) << 3) | (b >> 5);
	return (r << 16) | (g << 8) | b;
}

static double shown_contrast(uint32_t a, uint32_t b)
{
	return theme_contrast(q565(a), q565(b));
}

/*
 * The text mixed `pct` percent into the ground, stepped back a percent at a
 * time until it reads at least `floor` -- or the text's own ratio, if that is
 * lower -- on both `bg` and `on`. At 0 it is the text, which always passes,
 * so the loop ends.
 */
static uint32_t readable_mix(uint32_t tx, uint32_t bg, uint32_t on, int pct,
			     double floor)
{
	double want_bg = shown_contrast(tx, bg);
	double want_on = shown_contrast(tx, on);

	want_bg = want_bg < floor ? want_bg : floor;
	want_on = want_on < floor ? want_on : floor;
	for (; pct > 0; pct--) {
		uint32_t d = theme_mix(tx, bg, pct / 100.0);

		if (shown_contrast(d, bg) >= want_bg &&
		    shown_contrast(d, on) >= want_on) {
			return d;
		}
	}
	return tx;
}

static uint32_t custom_dim(uint32_t tx, uint32_t bg, uint32_t card)
{
	return readable_mix(tx, bg, card, 25, DIM_MIN_CONTRAST);
}

/*
 * Grey, 45% of the way into the ground, has the same floor problem as dim,
 * and worse: at 3:1 text it came to about 1.5:1. It is drawn as text too --
 * the pip numbers and the page chevrons on the gauges (usage_view.c) -- so
 * it walks back the same way, to 3:1, the figure every built-in palette's
 * grey meets on the ground (tests/theme). It is checked on the ground only,
 * because that is the only place it is drawn (fp/fix-fwlog review, NICE-4).
 * The builder page draws grey too, since the burn view: its hour numbers
 * are in it and its baseline and ticks are mixed from it, so
 * pc/theme_builder.html's customGrey() walks the same way and
 * tests/pc/test_theme_page_burn.py holds the two to each other.
 */
#define GREY_MIN_CONTRAST 3.0

static uint32_t custom_grey(uint32_t tx, uint32_t bg)
{
	return readable_mix(tx, bg, bg, 45, GREY_MIN_CONTRAST);
}

void theme_derive(const struct theme_custom *t, struct palette *p)
{
	uint32_t bg = t->c[0], tx = t->c[1];

	memset(p, 0, sizeof(*p));
	p->bg = bg;
	p->text = tx;
	p->g1 = t->c[2];
	p->g2 = t->c[3];
	p->low = t->c[4];
	p->mid = t->c[5];
	p->high = t->c[6];
	/* Contract (2026-10-27): dim and track are text mixed into the ground,
	 * 25% and 15% of the way respectively -- dim keeps 75% of the text,
	 * the track takes 15% of it. Dim takes less of the ground when 25%
	 * would leave it hard to read; see custom_dim(). */
	p->track = theme_mix(bg, tx, 0.15);
	p->grey = custom_grey(tx, bg);
	p->panel = theme_mix(bg, tx, 0.08);
	p->card = theme_mix(bg, tx, 0.06);
	p->dim = custom_dim(tx, bg, p->card);
	p->line = theme_mix(bg, tx, 0.10);
	p->low_text = p->low;	/* the user's colour; theirs to choose */
	p->ok = p->low;
	p->warn = p->mid;
	p->bad = p->high;
	p->danger_bg = theme_mix(bg, p->high, 0.10);
	p->danger_bd = theme_mix(bg, p->high, 0.45);
	/* The button label is the one role a custom theme cannot leave to
	 * its owner: it sits on a colour they chose for something else. The
	 * ground, black or white, whichever reads best on that colour. */
	p->ok_ink = bg;
	if (theme_contrast(0x000000, p->ok) > theme_contrast(p->ok_ink, p->ok)) {
		p->ok_ink = 0x000000;
	}
	if (theme_contrast(0xFFFFFF, p->ok) > theme_contrast(p->ok_ink, p->ok)) {
		p->ok_ink = 0xFFFFFF;
	}
	p->arc_w = t->w;
	p->flat = t->flat;
	p->dot = t->dot;
	p->by_gauge = t->by_gauge;
}

const struct palette *theme_pal(void)
{
	return cur;
}

int theme_active_id(void)
{
	return cur_id;
}

bool theme_select(int id)
{
	const struct palette *p = theme_builtin(id);

	if (p == NULL) {
		return false;
	}
	cur = p;
	cur_id = id;
	theme_burn_inks(cur, cur_id == THEME_DARK, &cur_inks);
	cur_inks_ok = true;
	return true;
}

bool theme_select_custom(const struct theme_custom *t)
{
	if (!theme_custom_valid(t)) {
		return false;
	}
	theme_derive(t, &custom);
	cur = &custom;
	cur_id = THEME_CUSTOM;
	theme_burn_inks(cur, false, &cur_inks);
	cur_inks_ok = true;
	return true;
}

const struct theme_burn_inks *theme_cur_burn_inks(void)
{
	if (!cur_inks_ok) {
		theme_burn_inks(cur, cur_id == THEME_DARK, &cur_inks);
		cur_inks_ok = true;
	}
	return &cur_inks;
}

static bool neutral(uint32_t c)
{
	return ((c >> 16) & 0xFF) == ((c >> 8) & 0xFF) &&
	       ((c >> 8) & 0xFF) == (c & 0xFF);
}

void theme_dot_colours(const struct palette *p, uint32_t *live,
		       uint32_t *stale)
{
	*live = p->low;
	*stale = p->mid;
	if (neutral(p->low) && neutral(p->mid) &&
	    theme_contrast(p->mid, p->bg) > theme_contrast(p->low, p->bg)) {
		*live = p->mid;
		*stale = p->low;
	}
}

uint32_t theme_arc_colour(const struct palette *p, int which, double pct)
{
	if (pct >= 90.0) {
		return p->high;		/* per-gauge mode still warns */
	}
	if (p->by_gauge) {
		return which == 0 ? p->g1 : p->g2;
	}
	return pct >= 60.0 ? p->mid : p->low;
}

/*
 * The burn view's inks; see theme.h.
 *
 * Dark's start from the contract's bytes. Two of the contract's colours are
 * already Dark ROLES -- the arrow's #4387DF is g1 and the hour numbers'
 * #6B7280 is grey -- so usage_view.c reads those two from the theme on every
 * theme. These four are what no role describes, so every other theme gets
 * the contract's RELATIONSHIPS from its own roles, which is what Dark's
 * bytes are to within a few steps of RGB: the newest hour is g1 35% of the
 * way to the text colour, the older bars g1 35% into the ground, the
 * baseline and the ticks the track lifted 30% and 77% towards grey.
 * Expressed that way the newest hour is still the stronger mark on a white
 * ground, where "brighter" would have made it the fainter one.
 *
 * The floor. A bar is a graphic, and 3:1 is the figure tests/theme holds
 * every graphic on this screen to (WCAG 1.4.11; the low arc, the grey). The
 * plain mix did not meet it: the /code-review max of 2026-11-01 measured the
 * older bars, as drawn, at 2.14:1 on Retro LCD, 2.23 on Light soft, 2.26 on
 * Colour-blind, 2.60 on Light and 2.88 on Dark -- the contract's own byte --
 * and a custom theme had no floor at all. So each ink walks, a percent at a
 * time, from where the mix put it until it reads the floor:
 *
 *   - the older bars back towards g1, which keeps their hue, and if g1
 *     itself is under the floor (a custom theme's g1 is its owner's choice,
 *     and only the text is checked against the ground) on from g1 towards
 *     the text;
 *   - the newest hour on towards the text, until it reads the floor AND
 *     sits THEME_BURN_INK_STEP from the older bars on the far side of them
 *     from the ground. Raising the older bars closes the gap the plain mix
 *     left; on Retro LCD it would have closed to 1.44:1. 1.5 is the step
 *     tests/theme already calls "a visible step" between two of the
 *     colour-blind theme's levels.
 *
 * Where the text cannot give the newest hour its step -- a custom theme
 * whose g1 reads stronger than its text, so the older bars, mixed from g1,
 * already outshine where the newest hour can go -- the newest hour takes
 * whichever of the text and g1 is the stronger, and the older bars walk
 * back towards the ground, never under the floor, until the step is there.
 * 20,000 random customs found 14 such themes among those with text of 5:1
 * or better before this second stage existed.
 *
 * The text always passes the floor, because a custom theme's floor is the
 * text's own ratio where that is under 3:1 (as custom_grey() does), so
 * every walk ends. The step cannot always be had: with nothing on the
 * screen reading much over 4.5:1 there is no room for two marks 3:1 and
 * 1.5 apart, and there the floor wins and the older bars stop at it, as
 * far from the newest hour as the floor lets them be. Every built-in gets
 * both from the first stage alone; tests/theme checks every one, and
 * 20,000 random customs. Measured on the colours as the panel shows them,
 * as custom_dim() is.
 */
static bool ink_apart(uint32_t hot, uint32_t cold, uint32_t bg)
{
	return shown_contrast(hot, bg) > shown_contrast(cold, bg) &&
	       shown_contrast(hot, cold) >= THEME_BURN_INK_STEP;
}

/* `from` walked a percent at a time towards `to` until it reads `floor`
 * on the ground and, with `cold` given, stands its step above it. */
static uint32_t ink_walk(uint32_t from, uint32_t to, uint32_t bg,
			 double floor, const uint32_t *cold)
{
	for (int pct = 0; pct < 100; pct++) {
		uint32_t c = theme_mix(from, to, pct / 100.0);

		if (shown_contrast(c, bg) >= floor &&
		    (cold == NULL || ink_apart(c, *cold, bg))) {
			return c;
		}
	}
	return to;
}

void theme_burn_inks(const struct palette *p, bool contract,
		     struct theme_burn_inks *k)
{
	double floor = shown_contrast(p->text, p->bg);

	floor = floor < THEME_BURN_INK_FLOOR ? floor : THEME_BURN_INK_FLOOR;
	if (contract) {
		k->hot = 0x7FB2F0;
		k->cold = 0x2D5C99;
		k->base = 0x3A414B;
		k->tick = 0x5C636B;
	} else {
		k->hot = theme_mix(p->g1, p->text, 0.35);
		k->cold = theme_mix(p->g1, p->bg, 0.35);
		k->base = theme_mix(p->track, p->grey, 0.30);
		k->tick = theme_mix(p->track, p->grey, 0.77);
	}
	k->cold = ink_walk(k->cold, p->g1, p->bg, floor, NULL);
	if (shown_contrast(k->cold, p->bg) < floor) {
		k->cold = ink_walk(p->g1, p->text, p->bg, floor, NULL);
	}
	k->hot = ink_walk(k->hot, p->text, p->bg, floor, &k->cold);
	if (ink_apart(k->hot, k->cold, p->bg)) {
		return;
	}
	/* The second stage: the stronger of the two, and the older bars
	 * stepped back towards the ground, the floor before the step. */
	k->hot = shown_contrast(p->g1, p->bg) > shown_contrast(p->text, p->bg)
		 ? p->g1 : p->text;
	{
		uint32_t from = k->cold;

		for (int pct = 1; pct < 100; pct++) {
			uint32_t c = theme_mix(from, p->bg, pct / 100.0);

			if (shown_contrast(c, p->bg) < floor) {
				break;
			}
			k->cold = c;
			if (ink_apart(k->hot, c, p->bg)) {
				break;
			}
		}
	}
}

/*
 * SHA-1, here rather than from mbedTLS: the build enables only the digests
 * the OTA path needs (sha256, src/ota.c), and this runs on 26 bytes once per
 * theme_now. A hundred lines of C that the host test checks against the
 * FIPS 180 vector cost less than a Kconfig change to the crypto library, and
 * the host test can reach them. Integrity is not the point -- the PC uses the
 * hash only to tell two custom themes apart.
 */
static uint32_t rol(uint32_t v, int n)
{
	return (v << n) | (v >> (32 - n));
}

static void sha1_block(uint32_t h[5], const uint8_t *p)
{
	uint32_t w[80];

	for (int i = 0; i < 16; i++) {
		w[i] = ((uint32_t)p[4 * i] << 24) | ((uint32_t)p[4 * i + 1] << 16) |
		       ((uint32_t)p[4 * i + 2] << 8) | p[4 * i + 3];
	}
	for (int i = 16; i < 80; i++) {
		w[i] = rol(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1);
	}
	uint32_t a = h[0], b = h[1], c = h[2], d = h[3], e = h[4];

	for (int i = 0; i < 80; i++) {
		uint32_t f, k;

		if (i < 20) {
			f = (b & c) | (~b & d);
			k = 0x5A827999;
		} else if (i < 40) {
			f = b ^ c ^ d;
			k = 0x6ED9EBA1;
		} else if (i < 60) {
			f = (b & c) | (b & d) | (c & d);
			k = 0x8F1BBCDC;
		} else {
			f = b ^ c ^ d;
			k = 0xCA62C1D6;
		}
		uint32_t t = rol(a, 5) + f + e + k + w[i];

		e = d;
		d = c;
		c = rol(b, 30);
		b = a;
		a = t;
	}
	h[0] += a;
	h[1] += b;
	h[2] += c;
	h[3] += d;
	h[4] += e;
}

void theme_sha1(const uint8_t *data, unsigned len, uint8_t out[20])
{
	uint32_t h[5] = { 0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476,
			  0xC3D2E1F0 };
	uint8_t blk[64];
	unsigned i = 0;

	for (; len - i >= 64; i += 64) {
		sha1_block(h, data + i);
	}
	unsigned rest = len - i;

	memset(blk, 0, sizeof(blk));
	memcpy(blk, data + i, rest);
	blk[rest] = 0x80;
	if (rest >= 56) {
		sha1_block(h, blk);
		memset(blk, 0, sizeof(blk));
	}
	uint64_t bits = (uint64_t)len * 8;

	for (int k = 0; k < 8; k++) {
		blk[63 - k] = (uint8_t)(bits >> (8 * k));
	}
	sha1_block(h, blk);
	for (int k = 0; k < 20; k++) {
		out[k] = (uint8_t)(h[k / 4] >> (24 - 8 * (k % 4)));
	}
}

void theme_custom_hash(const struct theme_custom *t, char out[9])
{
	uint8_t b[26], d[20];

	b[0] = THEME_CUSTOM;
	for (int i = 0; i < 7; i++) {
		b[1 + 3 * i] = (uint8_t)(t->c[i] >> 16);
		b[2 + 3 * i] = (uint8_t)(t->c[i] >> 8);
		b[3 + 3 * i] = (uint8_t)t->c[i];
	}
	b[22] = t->w;
	b[23] = t->flat ? 1 : 0;
	b[24] = t->dot ? 1 : 0;
	b[25] = t->by_gauge ? 1 : 0;
	theme_sha1(b, sizeof(b), d);
	snprintf(out, 9, "%02x%02x%02x%02x", d[0], d[1], d[2], d[3]);
}

/* --- The wire ------------------------------------------------------------ */

static int hexval(char c)
{
	if (c >= '0' && c <= '9') {
		return c - '0';
	}
	if (c >= 'a' && c <= 'f') {
		return c - 'a' + 10;
	}
	if (c >= 'A' && c <= 'F') {
		return c - 'A' + 10;
	}
	return -1;
}

/*
 * `"c":["rrggbb", ...]` -- exactly seven six-digit hex strings, nothing else.
 *
 * Strict on purpose, because "ignore a bad line whole" is only as good as the
 * definition of bad: a five-digit colour, an eighth element or a missing
 * bracket each fail here, rather than reading as black or as whatever the
 * previous slot held.
 */
static bool parse_colours(const char *json, uint32_t out[7])
{
	const char *p = msg_find_value(json, "c");

	if (p == NULL) {
		return false;
	}
	while (*p == ' ') {
		p++;
	}
	if (*p++ != '[') {
		return false;
	}
	for (int i = 0; i < 7; i++) {
		uint32_t v = 0;

		while (*p == ' ') {
			p++;
		}
		if (*p++ != '"') {
			return false;
		}
		for (int k = 0; k < 6; k++) {
			int h = hexval(*p++);

			if (h < 0) {
				return false;
			}
			v = (v << 4) | (uint32_t)h;
		}
		if (*p++ != '"') {
			return false;
		}
		while (*p == ' ') {
			p++;
		}
		if (*p != (i == 6 ? ']' : ',')) {
			return false;
		}
		p++;
		out[i] = v;
	}
	return true;
}

/* An integer field, and only an integer: 12.5 is not a width. */
static bool get_int(const char *json, const char *key, int *out)
{
	double v;

	if (!msg_get_double(json, key, &v) || v != v || v < -1e6 || v > 1e6 ||
	    v != (double)(long)v) {
		return false;
	}
	*out = (int)v;
	return true;
}

/* A string field compared EXACTLY. msg_get_str truncates to its buffer, so
 * a buffer one longer than the longest legal value makes "roundish" fail
 * instead of reading as "round". */
static bool str_is(const char *json, const char *key, const char *want,
		   bool *found)
{
	char buf[12];

	*found = msg_get_str(json, key, buf, sizeof(buf));
	return *found && strcmp(buf, want) == 0;
}

bool theme_msg_parse(const char *json, struct theme_msg *out)
{
	bool f;

	memset(out, 0, sizeof(*out));
	if (str_is(json, "op", "preview", &f)) {
		out->op = THEME_OP_PREVIEW;
	} else if (str_is(json, "op", "save", &f)) {
		out->op = THEME_OP_SAVE;
	} else if (str_is(json, "op", "cancel", &f)) {
		out->op = THEME_OP_CANCEL;
		return true;		/* needs nothing else */
	} else {
		return false;
	}

	if (!get_int(json, "id", &out->id) || out->id < 0 ||
	    out->id >= THEME_N) {
		return false;
	}
	if (out->id != THEME_CUSTOM) {
		return true;		/* custom fields, if any, mean nothing */
	}

	struct theme_custom *t = &out->custom;
	int w, dot;

	if (!parse_colours(json, t->c) || !get_int(json, "w", &w) ||
	    !get_int(json, "dot", &dot) || (dot != 0 && dot != 1)) {
		return false;
	}
	if (w != THEME_W_THIN && w != THEME_W_NORMAL && w != THEME_W_THICK) {
		return false;
	}
	t->w = (uint8_t)w;
	t->dot = dot == 1;
	if (str_is(json, "ends", "flat", &f)) {
		t->flat = true;
	} else if (!str_is(json, "ends", "round", &f)) {
		return false;
	}
	if (str_is(json, "by", "gauge", &f)) {
		t->by_gauge = true;
	} else if (!str_is(json, "by", "level", &f)) {
		return false;
	}
	return theme_custom_valid(t);
}

bool theme_caps_has(const char *json, const char *cap)
{
	const char *p = msg_find_value(json, "caps");
	size_t n = strlen(cap);

	if (p == NULL) {
		return false;
	}
	while (*p == ' ') {
		p++;
	}
	if (*p++ != '[') {
		return false;
	}
	/* Walk the quoted members up to the closing bracket. A member only
	 * counts when it is the whole string, so "themes" is not "theme". */
	while (*p && *p != ']') {
		if (*p == '"') {
			const char *q = strchr(p + 1, '"');

			if (q == NULL) {
				return false;
			}
			if ((size_t)(q - p - 1) == n &&
			    strncmp(p + 1, cap, n) == 0) {
				return true;
			}
			p = q + 1;
		} else {
			p++;
		}
	}
	return false;
}
