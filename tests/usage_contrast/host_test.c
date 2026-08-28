/* WCAG contrast for the gauge screen's palette, computed on the host.
 *
 * Build & run:
 *   cc -lm -I ../../firmware/src host_test.c -o /tmp/ctest && /tmp/ctest
 *
 * A desk gauge is read from across a room, sometimes in daylight, by people
 * whose colour vision varies. "Looks fine on my monitor" is not a check, and
 * the panel has already shipped two colours that failed one: the swipe
 * chevrons sat at 2.76:1 against the background, under the 3:1 minimum for a
 * graphic element -- on the one affordance that exists purely to be noticed --
 * and the two provider colours measured 1.02:1 against EACH OTHER, meaning
 * they were separated by hue alone and carried no signal at all for anyone who
 * could not resolve that hue.
 *
 * Neither was visible in the source, and neither would have been caught by the
 * layout test or by looking at a render. They are arithmetic, so they are
 * checked as arithmetic.
 *
 * The colours are duplicated here from usage_view.c rather than included: they
 * are #defines wrapped in lv_color_hex(), which needs LVGL. The final check in
 * this file greps the real source to prove the two lists still agree.
 */
#include <math.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

struct swatch { const char *name; unsigned rgb; };

/* Kept in step with firmware/src/usage_view.c by the check at the end. */
static const struct swatch pal[] = {
	{ "COL_BG",      0x0E1116 },
	{ "COL_TEXT",    0xE6E8EB },
	{ "COL_DIM",     0x8A9199 },
	{ "COL_GREEN",  0x0DA243 },
	{ "COL_AMBER",  0xBA8107 },
	{ "COL_RED",  0xFF1900 },
	{ "COL_GREY",    0x6B7280 },
	{ "COL_OTHER",   0x4387DF },
};
#define N (int)(sizeof(pal) / sizeof(pal[0]))

static double chan(unsigned c)
{
	double v = c / 255.0;

	return v <= 0.03928 ? v / 12.92 : pow((v + 0.055) / 1.055, 2.4);
}

static double luminance(unsigned rgb)
{
	return 0.2126 * chan((rgb >> 16) & 0xFF) +
	       0.7152 * chan((rgb >> 8) & 0xFF) +
	       0.0722 * chan(rgb & 0xFF);
}

static double contrast(unsigned a, unsigned b)
{
	double la = luminance(a), lb = luminance(b);
	double hi = la > lb ? la : lb, lo = la > lb ? lb : la;

	return (hi + 0.05) / (lo + 0.05);
}

/*
 * HSV saturation, 0..1. Grey is 0.
 *
 * Added because contrast alone said the palette was fine while the panel said
 * the green was grey, and both were telling the truth: #4AB07D cleared 7:1
 * against the background at 0.58 saturation, and 0.58 on an ILI9341 is a
 * grey-green. Luminance is what a colour WEIGHS; saturation is whether it is
 * a colour at all, and this file only ever checked the first one.
 */
static double saturation(unsigned rgb)
{
	unsigned r = (rgb >> 16) & 0xFF, g = (rgb >> 8) & 0xFF, b = rgb & 0xFF;
	unsigned hi = r > g ? (r > b ? r : b) : (g > b ? g : b);
	unsigned lo = r < g ? (r < b ? r : b) : (g < b ? g : b);

	return hi == 0 ? 0.0 : (double)(hi - lo) / (double)hi;
}

/*
 * The colour as the PANEL will actually show it.
 *
 * The display is RGB565: five bits of red, six of green, five of blue. Every
 * number in this file used to be checked at 24-bit precision, against a
 * hardware that has never displayed a 24-bit colour in its life. The drift is
 * small, but "small" is not a thing to assume when the whole point of the file
 * is that eyeballing it was not good enough.
 */
static unsigned as_rgb565(unsigned rgb)
{
	unsigned r = (rgb >> 16) & 0xFF, g = (rgb >> 8) & 0xFF, b = rgb & 0xFF;

	/* Quantise, then expand back the way the panel does: the top bits are
	 * replicated into the bottom ones. */
	r = ((r >> 3) << 3) | (r >> 5);
	g = ((g >> 2) << 2) | (g >> 6);
	b = ((b >> 3) << 3) | (b >> 5);
	return (r << 16) | (g << 8) | b;
}

static unsigned by_name(const char *name)
{
	for (int i = 0; i < N; i++) {
		if (strcmp(pal[i].name, name) == 0) {
			return pal[i].rgb;
		}
	}
	printf("FAIL: no swatch named %s\n", name);
	failures++;
	return 0;
}

/* Does the real source still define this colour as this value? */
static int source_agrees(const char *name, unsigned rgb)
{
	char needle[64], line[512];
	FILE *f = fopen("../../firmware/src/usage_view.c", "r");

	if (!f) {
		f = fopen("firmware/src/usage_view.c", "r");
	}
	if (!f) {
		return -1;	/* cannot check from here */
	}
	/*
	 * Match the NAME and the VALUE independently rather than one exact
	 * string. The source aligns these defines with tabs, so short names get
	 * two and long ones get one -- and a check that hardcoded the spacing
	 * reported three false drifts the first time it ran.
	 */
	snprintf(needle, sizeof(needle), "#define %s", name);
	while (fgets(line, sizeof(line), f)) {
		char *at = strstr(line, needle);
		char hex[16];

		if (!at) {
			continue;
		}
		/* Guard against COL_GREEN matching COL_GREEN_INK. */
		at += strlen(needle);
		if (*at != ' ' && *at != '\t') {
			continue;
		}
		snprintf(hex, sizeof(hex), "0x%06X", rgb);
		fclose(f);
		return strstr(line, hex) != NULL;
	}
	fclose(f);
	return 0;
}

int main(void)
{
	unsigned bg = by_name("COL_BG");
	char msg[128];

	/* Text has to clear 4.5:1; a graphic element 3:1 (WCAG 1.4.3, 1.4.11). */
	static const char *text[] = { "COL_TEXT", "COL_DIM" };
	static const char *graphic[] = { "COL_GREEN", "COL_AMBER", "COL_RED",
					 "COL_GREY", "COL_OTHER" };

	for (unsigned i = 0; i < sizeof(text) / sizeof(text[0]); i++) {
		double r = contrast(by_name(text[i]), bg);

		snprintf(msg, sizeof(msg), "%s is readable text (%.2f:1 >= 4.5)",
			 text[i], r);
		CHECK(r >= 4.5, msg);
	}
	for (unsigned i = 0; i < sizeof(graphic) / sizeof(graphic[0]); i++) {
		double r = contrast(by_name(graphic[i]), bg);

		snprintf(msg, sizeof(msg), "%s is a visible graphic (%.2f:1 >= 3)",
			 graphic[i], r);
		CHECK(r >= 3.0, msg);
	}

	/*
	 * There are no provider colours to compare any more.
	 *
	 * They used to have to differ from each other in BRIGHTNESS rather than
	 * hue alone -- the check that caught #10A37F, which cleared the
	 * background comfortably and was still invisible as a distinction. One
	 * provider per page retires the whole question: identity is carried by
	 * a name under the brand and a position on the rail, and severity is
	 * the only thing colour is spent on.
	 */

	/*
	 * THE SEVERITY BAND MUST STAY FLAT.
	 *
	 * On a dark panel brightness is attention, so a ramp whose middle step
	 * is the brightest inverts its own meaning. That is exactly what
	 * shipped: amber at 11.39:1 against red at 4.95:1, a 2.30x spread with
	 * the merely-getting-close colour shouting over the critical one.
	 *
	 * The fix was not a brighter red. A red luminous enough to outshine a
	 * yellow is a pale salmon and stops reading as red, which is physics
	 * rather than taste. So luminance is held flat and urgency is carried
	 * by saturation and by the arc's own area instead -- and THIS is the
	 * check that keeps someone from "improving" one step later.
	 */
	double g = contrast(by_name("COL_GREEN"), bg);
	double a = contrast(by_name("COL_AMBER"), bg);
	double rd = contrast(by_name("COL_RED"), bg);
	double hi = g > a ? (g > rd ? g : rd) : (a > rd ? a : rd);
	double lo = g < a ? (g < rd ? g : rd) : (a < rd ? a : rd);

	snprintf(msg, sizeof(msg),
		 "the severity band is flat, not inverted (%.2fx spread <= 1.35)",
		 hi / lo);
	CHECK(hi / lo <= 1.35, msg);


	/*
	 * A SEVERITY COLOUR MUST ACTUALLY BE A COLOUR.
	 *
	 * This is the check that was missing when the user looked at the board
	 * and said the green looked grey. The ramp had been built to carry
	 * urgency in its saturation -- 0.58, 0.66, 0.72 -- which put its
	 * safest, most-often-displayed step nearest to grey, on a panel whose
	 * own gamma takes another bite out of it.
	 *
	 * 0.85 rather than something gentler because the failure was not
	 * marginal: at 0.58 the green read as grey to the naked eye at 60 cm.
	 * There is no reason for a green/amber/red ramp to sit anywhere but
	 * near the top of the range -- flat LUMINANCE is what the band rule
	 * below is protecting, and saturation costs it nothing.
	 */
	static const char *severity[] = { "COL_GREEN", "COL_AMBER", "COL_RED" };

	for (unsigned i = 0; i < sizeof(severity) / sizeof(severity[0]); i++) {
		double sv = saturation(by_name(severity[i]));

		snprintf(msg, sizeof(msg), "%s is saturated enough to read as a"
			 " colour (%.2f >= 0.85)", severity[i], sv);
		CHECK(sv >= 0.85, msg);
	}

	/*
	 * And the whole palette still holds up once the panel has quantised it.
	 *
	 * Cheap to check and it closes the gap this file had: every figure
	 * above describes a colour in 24-bit sRGB, and the hardware displays
	 * RGB565. A palette that passes at 24 bits and fails at 16 is a palette
	 * that passes here and fails on the desk.
	 */
	for (int i = 0; i < N; i++) {
		unsigned shown = as_rgb565(pal[i].rgb);
		double r = contrast(shown, as_rgb565(bg));
		double want = (strcmp(pal[i].name, "COL_BG") == 0) ? 0.0
			    : (strcmp(pal[i].name, "COL_TEXT") == 0 ||
			       strcmp(pal[i].name, "COL_DIM") == 0) ? 4.5 : 3.0;

		if (want == 0.0) {
			continue;
		}
		snprintf(msg, sizeof(msg), "%s survives RGB565 (%.2f:1 >= %.1f)",
			 pal[i].name, r, want);
		CHECK(r >= want, msg);
	}
	{
		double qg = contrast(as_rgb565(by_name("COL_GREEN")),
				     as_rgb565(bg));
		double qa = contrast(as_rgb565(by_name("COL_AMBER")),
				     as_rgb565(bg));
		double qr = contrast(as_rgb565(by_name("COL_RED")),
				     as_rgb565(bg));
		double qhi = qg > qa ? (qg > qr ? qg : qr) : (qa > qr ? qa : qr);
		double qlo = qg < qa ? (qg < qr ? qg : qr) : (qa < qr ? qa : qr);

		snprintf(msg, sizeof(msg), "the band is still flat in RGB565"
			 " (%.2fx spread <= 1.35)", qhi / qlo);
		CHECK(qhi / qlo <= 1.35, msg);
	}

	/* And the list above still matches the source it claims to mirror. */
	int checked = 0, agreed = 0;

	for (int i = 0; i < N; i++) {
		int r = source_agrees(pal[i].name, pal[i].rgb);

		if (r < 0) {
			break;	/* run from a directory that cannot see it */
		}
		checked++;
		agreed += r;
		if (!r) {
			printf("       %s has drifted from usage_view.c\n",
			       pal[i].name);
		}
	}
	if (checked) {
		CHECK(agreed == checked,
		      "this palette still matches firmware/src/usage_view.c");
	} else {
		printf("SKIP: usage_view.c not reachable from here\n");
	}

	printf(failures ? "\n%d FAILED\n" : "\nall contrast checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
