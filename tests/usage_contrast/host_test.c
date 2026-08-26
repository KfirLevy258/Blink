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
	{ "COL_GREEN",   0x2ECC71 },
	{ "COL_AMBER",   0xF1C40F },
	{ "COL_RED",     0xE74C3C },
	{ "COL_GREY",    0x6B7280 },
	{ "COL_CLAUDE",  0xD97757 },
	{ "COL_CODEX",   0x2DD4BF },
	{ "COL_OTHER",   0x6E8BC4 },
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
					 "COL_GREY", "COL_CLAUDE", "COL_CODEX",
					 "COL_OTHER" };

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
	 * The provider colours must differ from each other in BRIGHTNESS, not
	 * only in hue. This is the check that would have caught #10A37F, which
	 * cleared the background comfortably and was still invisible as a
	 * distinction.
	 */
	double pp = contrast(by_name("COL_CLAUDE"), by_name("COL_CODEX"));

	snprintf(msg, sizeof(msg),
		 "the two provider colours differ in brightness (%.2f:1 >= 1.5)",
		 pp);
	CHECK(pp >= 1.5, msg);

	/*
	 * The severity ramp is NOT held to the same rule, deliberately. Its
	 * redundant encoding is the arc itself: a 91% arc is nearly a full
	 * circle whatever colour it is drawn in, so the ramp reinforces a shape
	 * the eye has already read. Colour is the only cue for the provider
	 * ball, which is why that one is checked and this one is not.
	 */
	CHECK(contrast(by_name("COL_RED"), bg) >= 3.0 &&
	      contrast(by_name("COL_AMBER"), bg) >= 3.0 &&
	      contrast(by_name("COL_GREEN"), bg) >= 3.0,
	      "every step of the severity ramp is visible against the panel");

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
