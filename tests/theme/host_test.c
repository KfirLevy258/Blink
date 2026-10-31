/* The theme table, checked as arithmetic.
 *
 * Build & run (tests/ci/check_host_tests.sh does this):
 *   cc -I firmware/src tests/theme/host_test.c firmware/src/theme.c \
 *      firmware/src/msg_parse.c -lm -o /tmp/theme && /tmp/theme
 *
 * Compiled against the REAL table in theme.c, not a copy. tests/usage_contrast
 * had to keep a duplicate of six #defines and grep the source to prove they
 * still agreed; twelve themes of nineteen roles each would make that list the
 * largest thing in the repository and the likeliest to drift.
 *
 * What is checked, and why each threshold:
 *   - 4.5:1 for anything read as text (WCAG 1.4.3): text, dim, and mid/high,
 *     because the hint line wears those when a reading goes old or fails.
 *   - 3:1 for a graphic (WCAG 1.4.11): the low arc and the chevrons.
 *   - Everything AFTER quantising to RGB565, which is all the panel can show.
 *   - Light panels at least 1.1:1 off their ground: the white-on-white pill.
 *   - The colour-blind theme separates its levels by brightness, not hue.
 */
#include <math.h>
#include <stdio.h>
#include <string.h>

#include "theme.h"
#include "usage_layout.h"

static int failures;
#define CHECK(c, m) do { if (!(c)) { printf("FAIL: %s\n", m); failures++; } \
	else { printf("PASS: %s\n", m); } } while (0)

static uint32_t q565(uint32_t c)
{
	unsigned r = (c >> 16) & 0xFF, g = (c >> 8) & 0xFF, b = c & 0xFF;

	r = ((r >> 3) << 3) | (r >> 5);
	g = ((g >> 2) << 2) | (g >> 6);
	b = ((b >> 3) << 3) | (b >> 5);
	return (r << 16) | (g << 8) | b;
}

/* Contrast as the panel shows it. */
static double cr(uint32_t a, uint32_t b)
{
	return theme_contrast(q565(a), q565(b));
}

static double lumq(uint32_t c)
{
	/* Contrast against black is (L + 0.05) / 0.05, so L falls out. */
	return theme_contrast(q565(c), 0) * 0.05 - 0.05;
}

/* `user`: a custom theme, whose severity colours its owner picked. Pairs
 * between two of those are the owner's business -- the board can refuse an
 * unreadable ground, not a red they like -- so only the roles the board
 * derives itself are held to the fill rules. */
static void check_palette(const char *name, const struct palette *p,
			  bool light, bool user)
{
	char m[160];
	struct { const char *role; uint32_t c; double want; } need[] = {
		{ "text",  p->text, 4.5 },
		{ "dim",   p->dim,  4.5 },
		{ "mid",   p->mid,  4.5 },	/* hint: reading is old; svc amber */
		{ "high",  p->high, 4.5 },	/* hint: error, host lost; svc red */
		{ "low",   p->low,  3.0 },
		{ "grey",  p->grey, 3.0 },
		{ "ok",    p->ok,   4.5 },	/* settings status text */
		{ "warn",  p->warn, 4.5 },
		{ "bad",   p->bad,  4.5 },
	};

	for (unsigned i = 0; i < sizeof(need) / sizeof(need[0]); i++) {
		double r = cr(need[i].c, p->bg);

		snprintf(m, sizeof(m), "%s: %s on the ground %.2f:1 >= %.1f",
			 name, need[i].role, r, need[i].want);
		CHECK(r >= need[i].want, m);
	}

	/* The settings rows draw their title and subtitle on the card, and
	 * the provider pill draws its name on the panel. */
	snprintf(m, sizeof(m), "%s: text on a settings row %.2f:1 >= 4.5",
		 name, cr(p->text, p->card));
	CHECK(cr(p->text, p->card) >= 4.5, m);
	snprintf(m, sizeof(m), "%s: dim on a settings row %.2f:1 >= 4.5",
		 name, cr(p->dim, p->card));
	CHECK(cr(p->dim, p->card) >= 4.5, m);
	/* The plain buttons -- Later, Cancel, OK, Back, What's new's arrows --
	 * are mk_btn() on a track fill with the default text on it. Retro LCD
	 * is the closest, at 4.90. */
	snprintf(m, sizeof(m), "%s: text on a track-filled button %.2f:1 >= 4.5",
		 name, cr(p->text, p->track));
	CHECK(cr(p->text, p->track) >= 4.5, m);
	snprintf(m, sizeof(m), "%s: text on the pill %.2f:1 >= 4.5",
		 name, cr(p->text, p->panel));
	CHECK(cr(p->text, p->panel) >= 4.5, m);

	/*
	 * Text drawn on something other than the ground. The first version of
	 * this file checked every colour against the ground only, and the
	 * 2026-10-27 review found three places where text sits on a fill:
	 *   - the update row's status ("Up to date", "Install 1.4.1") on the
	 *     settings card: Light measured 4.08-4.20, Soft 3.96, Retro 4.17;
	 *   - the session toast, in its level's colour, on its pill: Light's
	 *     green 3.14, Soft 2.64, Retro 2.83, colour-blind 3.84;
	 *   - the pip tally digits, in the same colours, on the ground.
	 * Those last two draw `low` as TEXT, which is why low_text exists:
	 * the arc can stay the brighter graphic colour.
	 */
	struct { const char *role; uint32_t c; } on_fill_all[] = {
		{ "ok", p->ok }, { "warn", p->warn }, { "bad", p->bad },
		{ "dim", p->dim },
	};

	/* For a custom theme only dim is the board's own; it comes last. */
	for (unsigned i = user ? 3 : 0; i < 4; i++) {
		double r = cr(on_fill_all[i].c, p->card);

		snprintf(m, sizeof(m), "%s: %s status on a settings row %.2f:1"
			 " >= 4.5", name, on_fill_all[i].role, r);
		CHECK(r >= 4.5, m);
	}

	struct { const char *role; uint32_t c; } toast[] = {
		{ "low_text", p->low_text }, { "mid", p->mid }, { "high", p->high },
	};

	for (unsigned i = 0; !user && i < sizeof(toast) / sizeof(toast[0]);
	     i++) {
		double r = cr(toast[i].c, p->panel);

		snprintf(m, sizeof(m), "%s: %s toast text on its pill %.2f:1"
			 " >= 4.5", name, toast[i].role, r);
		CHECK(r >= 4.5, m);
	}
	snprintf(m, sizeof(m), "%s: low_text tally digits on the ground %.2f:1"
		 " >= 4.5", name, cr(p->low_text, p->bg));
	CHECK(user || cr(p->low_text, p->bg) >= 4.5, m);

	/* The update buttons write on an `ok` fill. Dark keeps 1.3.4's
	 * #E6E8EB there (1.71:1 on #2ECC71, as it shipped); every other theme
	 * was 1.00 in Mono and Ink before ok_ink existed. */
	if (p != theme_builtin(THEME_DARK)) {
		snprintf(m, sizeof(m), "%s: button label on an ok fill %.2f:1"
			 " >= 4.5", name, cr(p->ok_ink, p->ok));
		CHECK(cr(p->ok_ink, p->ok) >= 4.5, m);
	}

	/*
	 * The two urgent levels must not look alike. Retro LCD's 90% arc was
	 * #0F380F, the TEXT colour, and 1.20:1 from its amber: a used-up
	 * window drew like a normal one. 60 is the builder's own threshold
	 * for "two levels look almost the same" (Euclidean RGB distance).
	 */
	/*
	 * And no two levels may look alike, low and mid included. Mono and
	 * Light ink shipped to the 1.4.0 branch with low and mid the same
	 * colour, and the builder's own check warned about both presets
	 * (audit, 2026-10-28): crossing 60% changed nothing on the arc. This
	 * is the same rule the builder applies, so no built-in can earn its
	 * warning again; tests/pc/test_theme_builtins.py holds the page to it
	 * from the other side.
	 */
	if (!user) {
		struct { const char *what; uint32_t a, b; } pair[] = {
			{ "under 60% and 60-90%", p->low, p->mid },
			{ "60-90% and 90%+", p->mid, p->high },
			{ "under 60% and 90%+", p->low, p->high },
		};

		for (unsigned i = 0; i < 3; i++) {
			uint32_t a = pair[i].a, b = pair[i].b;
			int dr = (int)((a >> 16) & 0xFF) - (int)((b >> 16) & 0xFF);
			int dg = (int)((a >> 8) & 0xFF) - (int)((b >> 8) & 0xFF);
			int db = (int)(a & 0xFF) - (int)(b & 0xFF);
			double d = sqrt((double)(dr * dr + dg * dg + db * db));

			snprintf(m, sizeof(m), "%s: %s are told apart"
				 " (distance %.0f >= 60)", name, pair[i].what, d);
			CHECK(d >= 60, m);
		}
		snprintf(m, sizeof(m), "%s: almost-gone is not the text colour",
			 name);
		CHECK(p->high != p->text, m);
	}

	/* A pill the colour of the ground is a control nobody can see. */
	snprintf(m, sizeof(m), "%s: the pill is not the ground", name);
	CHECK(q565(p->panel) != q565(p->bg), m);
	if (light) {
		snprintf(m, sizeof(m), "%s: light pill stands off the ground"
			 " %.2f:1 >= 1.1", name, cr(p->panel, p->bg));
		CHECK(cr(p->panel, p->bg) >= 1.1, m);
		/* And on the dark side of it. Soft's preview pill measured
		 * 1.10:1 -- by ratio it passes -- but it was WHITER than its
		 * pale ground, which reads as a hole in the page rather than
		 * as a control sitting on it. */
		snprintf(m, sizeof(m), "%s: light pill is darker than the"
			 " ground", name);
		CHECK(lumq(p->panel) < lumq(p->bg), m);
		snprintf(m, sizeof(m), "%s: light settings row stands off the"
			 " ground %.2f:1 >= 1.1", name, cr(p->card, p->bg));
		CHECK(cr(p->card, p->bg) >= 1.1, m);
		/* Margin, not just a pass. Amber and red at 4.52-4.54 were
		 * one RGB565 step from failing, on the colours the status line
		 * uses for an outage. */
		snprintf(m, sizeof(m), "%s: amber and red clear 5.2:1 (%.2f, %.2f)",
			 name, cr(p->mid, p->bg), cr(p->high, p->bg));
		CHECK((cr(p->mid, p->bg) >= 5.2 || p->mid == p->text) &&
		      cr(p->high, p->bg) >= 5.2, m);
	}

	snprintf(m, sizeof(m), "%s: arc width %d fits the ring", name,
		 p->arc_w);
	CHECK(p->arc_w >= 4 && p->arc_w <= THEME_ARC_W_MAX, m);
}

int main(void)
{
	char m[160];

	/* --- Every built-in --- */
	for (int id = 0; id < THEME_BUILTIN_N; id++) {
		const struct palette *p = theme_builtin(id);

		snprintf(m, sizeof(m), "theme %d exists", id);
		CHECK(p != NULL, m);
		if (!p) {
			continue;
		}
		check_palette(theme_name(id), p,
			      id == THEME_LIGHT || id == THEME_LIGHT_SOFT ||
			      id == THEME_LIGHT_INK, false);

		const char *n = theme_name(id);

		snprintf(m, sizeof(m), "theme %d has a tile name that fits"
			 " (\"%s\", %zu <= 12)", id, n, strlen(n));
		CHECK(n[0] && strlen(n) <= 12, m);
	}
	CHECK(theme_builtin(-1) == NULL && theme_builtin(THEME_CUSTOM) == NULL,
	      "out-of-range ids have no built-in palette");

	/*
	 * Dark is 1.3.4, byte for byte. An update must not repaint the desk of
	 * someone who never chose a theme, and these are the #defines that
	 * usage_view.c and ui_settings.c carried in 1.3.4 (63f2386).
	 */
	{
		const struct palette *d = theme_builtin(THEME_DARK);

		CHECK(d->bg == 0x0E1116 && d->panel == 0x05070A &&
		      d->track == 0x272C34 && d->text == 0xE6E8EB &&
		      d->dim == 0x8A9199 && d->grey == 0x6B7280,
		      "Dark keeps 1.3.4's ground, pill, track, text, dim, grey");
		CHECK(d->low == 0x0DA243 && d->low_text == 0x0DA243 &&
		      d->mid == 0xBA8107 && d->high == 0xFF1900,
		      "Dark keeps 1.3.4's severity ramp, as arcs and as text");
		CHECK(d->ok_ink == 0xE6E8EB,
		      "Dark keeps 1.3.4's label on the update buttons");
		CHECK(d->card == 0x161A20 && d->line == 0x20252D &&
		      d->ok == 0x2ECC71 && d->warn == 0xF1C40F &&
		      d->bad == 0xE74C3C && d->danger_bg == 0x1E1412 &&
		      d->danger_bd == 0x7A2B23,
		      "Dark keeps 1.3.4's settings colours");
		CHECK(d->arc_w == GAUGE_ARC_W && !d->flat && d->dot &&
		      !d->by_gauge, "Dark keeps 1.3.4's arc style");
	}

	/*
	 * The colour-blind theme: brightness, not hue, separates its levels.
	 * See the note on THEME_CVD_SAFE for why every-pair 3:1 is out of
	 * reach on a dark ground, and therefore what is checked instead.
	 */
	{
		const struct palette *c = theme_builtin(THEME_CVD_SAFE);
		double lm = cr(c->low, c->mid), mh = cr(c->mid, c->high);
		double lh = cr(c->low, c->high);

		snprintf(m, sizeof(m), "colour-blind: plenty and almost-gone are"
			 " %.2f:1 apart >= 3", lh);
		CHECK(lh >= 3.0, m);
		snprintf(m, sizeof(m), "colour-blind: each step is a visible"
			 " step (%.2f, %.2f >= 1.5)", lm, mh);
		CHECK(lm >= 1.5 && mh >= 1.5, m);
		CHECK(lumq(c->low) < lumq(c->mid) && lumq(c->mid) < lumq(c->high),
		      "colour-blind: urgency is the brightest level");
		/* #E69F00 left the steps at 1.71 and 2.39; #D69400 evens them
		 * out at about 2.0 each, which is the most either can have. */
		snprintf(m, sizeof(m), "colour-blind: the two steps are even"
			 " (%.2f vs %.2f, within 0.2)", lm, mh);
		CHECK(fabs(lm - mh) <= 0.2, m);
	}

	/*
	 * The sRGB-to-linear table that replaced libm's pow() on the board,
	 * held to pow() itself here. 1/65535 is the table's own step, so this
	 * is "every entry is the nearest one it could be".
	 */
	{
		double worst = 0;

		for (int v = 0; v < 256; v++) {
			double c = v / 255.0;
			double want = c <= 0.03928 ? c / 12.92
						   : pow((c + 0.055) / 1.055, 2.4);
			double got = theme_lin(v) / 65535.0;
			double e = fabs(got - want);

			worst = e > worst ? e : worst;
		}
		snprintf(m, sizeof(m), "sRGB table matches pow() (worst %.2e"
			 " <= 1/65535)", worst);
		CHECK(worst <= 1.0 / 65535.0, m);
	}

	/* Every width the wire accepts still leaves the percentage room. */
	snprintf(m, sizeof(m), "the thickest arc leaves %d px for the number"
		 " (>= 63)", GAUGE_ARC_SZ - 2 * THEME_ARC_W_MAX);
	CHECK(GAUGE_ARC_SZ - 2 * THEME_ARC_W_MAX >= 63, m);

	/* --- Custom: derivation --- */
	struct theme_custom t = {
		.c = { 0x101418, 0xF0F0F0, 0x4387DF, 0x0DA243, 0x0DA243,
		       0xBA8107, 0xFF1900 },
		.w = THEME_W_NORMAL, .flat = false, .dot = true,
		.by_gauge = false,
	};
	struct palette dp;

	CHECK(theme_custom_valid(&t), "a sensible custom theme is valid");
	theme_derive(&t, &dp);
	CHECK(dp.dim == theme_mix(t.c[1], t.c[0], 0.25),
	      "custom dim is the text 25% of the way into the ground");
	CHECK(dp.track == theme_mix(t.c[0], t.c[1], 0.15),
	      "custom track is the ground with 15% of the text");
	check_palette("custom sample", &dp, false, true);

	{
		/*
		 * The 3:1 floor, met with the PC's arithmetic, must not be
		 * refused by the board's. The board computes from a 16-bit
		 * table and the daemon from pow(), so a theme the daemon
		 * passed at exactly 3.0 can land a hair under it here:
		 * #595959 on black is 3.00 by pow() and 2.998 by the table.
		 * The board allows 0.005 for that; the PC leaves 0.05 more
		 * (it refuses below 3.05).
		 */
		struct theme_custom edge = t;

		edge.c[0] = 0x000000;
		edge.c[1] = 0x595959;
		CHECK(theme_custom_valid(&edge),
		      "custom at 3:1 by the PC's maths is not refused for"
		      " table rounding");
		edge.c[1] = 0x555555;
		CHECK(!theme_custom_valid(&edge),
		      "custom clearly under 3:1 is still refused");
	}
	{
		struct theme_custom bad = t;

		bad.c[1] = 0x202428;	/* text nearly the ground */
		CHECK(!theme_custom_valid(&bad),
		      "custom with unreadable text is refused");
		bad = t;
		bad.w = 7;
		CHECK(!theme_custom_valid(&bad), "custom with width 7 is refused");
	}

	/*
	 * --- Custom: dim stays readable ---
	 *
	 * Dim is the board's own colour in a custom theme: the clock, the
	 * window names, every settings subtitle. Mixed a plain 25% into the
	 * ground it came to 2.0-2.3:1 on themes at the 3:1 text floor
	 * (2026-10-28 review, NICE-8). It must now reach 4.5:1 -- the
	 * built-ins' figure -- on the ground and on a settings card, or match
	 * the text where the owner's text is itself below that; and wherever
	 * the 25% mix was already readable it must still be exactly that mix,
	 * so the builder's preview and themes already in use do not move.
	 *
	 * Over random customs: the ground and the text drawn at random and
	 * kept when the board would accept the pair, so the floor and the
	 * near-floor themes are well represented. A fixed xorshift seed keeps
	 * every run the same set.
	 */
	{
		uint32_t x = 0x2545F491u;
		int tried = 0, kept = 0, walked = 0;
		double worst = 99.0, worst_24 = 99.0, worst_grey = 99.0;
		int grey_walked = 0, grey_moved = 0;
		uint32_t worst_bg = 0, worst_tx = 0;

		while (kept < 20000 && tried < 2000000) {
			struct theme_custom r = t;
			struct palette rp;

			tried++;
			for (int k = 0; k < 2; k++) {
				x ^= x << 13;
				x ^= x >> 17;
				x ^= x << 5;
				r.c[k] = x & 0xFFFFFF;
			}
			if (!theme_custom_valid(&r)) {
				continue;
			}
			kept++;
			theme_derive(&r, &rp);

			/* Measured as the panel shows it (cr(), RGB565), the
			 * way custom_dim() measures and every palette check in
			 * this file does. */
			double tx_bg = cr(r.c[1], r.c[0]);
			double tx_card = cr(r.c[1], rp.card);
			double want_bg = tx_bg < 4.5 ? tx_bg : 4.5;
			double want_card = tx_card < 4.5 ? tx_card : 4.5;
			double got_bg = cr(rp.dim, r.c[0]);
			double got_card = cr(rp.dim, rp.card);
			uint32_t plain = theme_mix(r.c[1], r.c[0], 0.25);
			bool plain_ok = cr(plain, r.c[0]) >= want_bg &&
					cr(plain, rp.card) >= want_card;

			if (rp.dim != plain) {
				walked++;
			}
			/* Grey: 3:1 or the text's own, on the ground, and the
			 * plain 45% mix wherever that already reads. */
			{
				double gw = tx_bg < 3.0 ? tx_bg : 3.0;
				double g = cr(rp.grey, r.c[0]) / gw;
				uint32_t gp = theme_mix(r.c[1], r.c[0], 0.45);

				if (g < worst_grey) {
					worst_grey = g;
				}
				if (cr(gp, r.c[0]) >= gw && rp.grey != gp) {
					grey_moved++;
				}
				if (rp.grey != gp) {
					grey_walked++;
				}
			}
			if (got_bg / want_bg < worst) {
				worst = got_bg / want_bg;
				worst_bg = r.c[0];
				worst_tx = r.c[1];
			}
			if (got_card / want_card < worst) {
				worst = got_card / want_card;
				worst_bg = r.c[0];
				worst_tx = r.c[1];
			}
			/* And the 24-bit figure the builder page computes,
			 * which may sit a little either side of the panel's. */
			double q = theme_contrast(rp.dim, r.c[0]) /
				   (theme_contrast(r.c[1], r.c[0]) < 4.5
				    ? theme_contrast(r.c[1], r.c[0]) : 4.5);

			if (q < worst_24) {
				worst_24 = q;
			}
			if (plain_ok && rp.dim != plain) {
				snprintf(m, sizeof(m), "custom %06X on %06X: dim"
					 " left the 25%% mix though it was"
					 " readable", (unsigned)r.c[1],
					 (unsigned)r.c[0]);
				CHECK(0, m);
			}
		}
		snprintf(m, sizeof(m), "%d random customs accepted (of %d"
			 " drawn), %d needed a lighter mix", kept, tried,
			 walked);
		CHECK(kept == 20000 && walked > 0 && walked < kept, m);
		snprintf(m, sizeof(m), "every custom dim, as drawn, meets"
			 " min(4.5, text) on the ground and a card (worst"
			 " %.4f of the target, %06X on %06X)", worst,
			 (unsigned)worst_tx, (unsigned)worst_bg);
		CHECK(worst >= 1.0, m);
		snprintf(m, sizeof(m), "every custom grey, as drawn, meets"
			 " min(3.0, text) on the ground (worst %.4f of the"
			 " target; %d needed a lighter mix)", worst_grey,
			 grey_walked);
		CHECK(worst_grey >= 1.0 && grey_walked > 0, m);
		snprintf(m, sizeof(m), "and keeps the plain 45%% mix wherever"
			 " that already read (%d moved)", grey_moved);
		CHECK(grey_moved == 0, m);
		/* Not a check: how far the builder's 24-bit preview can read
		 * from the panel. Measured 0.94 of the target at worst, which
		 * is rounding to 565 alone; recorded so a change in it shows. */
		printf("  note: 24-bit reading of the same dims, worst %.4f of"
		       " the target\n", worst_24);
	}
	{
		/* The floor case the review measured, and a near-floor one:
		 * grey text on black at 3:1 and on navy at about 4:1. */
		struct theme_custom f = t;
		struct palette fp;

		f.c[0] = 0x000000;
		f.c[1] = 0x595959;
		theme_derive(&f, &fp);
		snprintf(m, sizeof(m), "custom at the 3:1 floor: dim %.2f:1,"
			 " was %.2f:1 with the plain mix",
			 cr(fp.dim, f.c[0]),
			 cr(theme_mix(f.c[1], f.c[0], 0.25),
					f.c[0]));
		CHECK(cr(fp.dim, f.c[0]) >=
		      cr(f.c[1], f.c[0]), m);
		snprintf(m, sizeof(m), "custom at the 3:1 floor: grey %.2f:1,"
			 " was %.2f:1 with the plain mix",
			 cr(fp.grey, f.c[0]),
			 cr(theme_mix(f.c[1], f.c[0], 0.45), f.c[0]));
		CHECK(cr(fp.grey, f.c[0]) >= cr(f.c[1], f.c[0]), m);
		f.c[0] = 0x101A3A;
		f.c[1] = 0x8A8F9A;
		theme_derive(&f, &fp);
		snprintf(m, sizeof(m), "custom near the floor (text %.2f:1):"
			 " dim %.2f:1", cr(f.c[1], f.c[0]),
			 cr(fp.dim, f.c[0]));
		CHECK(cr(fp.dim, f.c[0]) >=
		      (cr(f.c[1], f.c[0]) < 4.5
		       ? cr(f.c[1], f.c[0]) : 4.5), m);
	}

	/* --- Selection never half-applies --- */
	CHECK(theme_select(THEME_OCEAN) && theme_active_id() == THEME_OCEAN &&
	      theme_pal() == theme_builtin(THEME_OCEAN), "select a built-in");
	CHECK(!theme_select(13) && theme_active_id() == THEME_OCEAN &&
	      theme_pal() == theme_builtin(THEME_OCEAN),
	      "a bad id leaves the current theme alone");
	{
		struct theme_custom bad = t;

		bad.c[1] = bad.c[0];
		CHECK(!theme_select_custom(&bad) &&
		      theme_pal() == theme_builtin(THEME_OCEAN),
		      "a bad custom theme leaves the current theme alone");
	}
	CHECK(theme_select_custom(&t) && theme_active_id() == THEME_CUSTOM &&
	      theme_pal()->bg == 0x101418, "select a custom theme");
	theme_select(THEME_DARK);

	/* --- Arc colour --- */
	{
		const struct palette *d = theme_builtin(THEME_DARK);
		struct palette g = dp;

		g.by_gauge = true;
		CHECK(theme_arc_colour(d, 0, 10) == d->low &&
		      theme_arc_colour(d, 1, 60) == d->mid &&
		      theme_arc_colour(d, 0, 89.9) == d->mid &&
		      theme_arc_colour(d, 1, 90) == d->high,
		      "by level: thresholds at 60 and 90");
		CHECK(theme_arc_colour(&g, 0, 70) == g.g1 &&
		      theme_arc_colour(&g, 1, 70) == g.g2,
		      "per gauge: each arc wears its own colour");
		CHECK(theme_arc_colour(&g, 0, 90) == g.high &&
		      theme_arc_colour(&g, 1, 95) == g.high,
		      "per gauge: an arc still turns to high at 90%");
	}

	/* --- The wire --- */
	struct theme_msg msg;

	CHECK(theme_msg_parse("{\"t\":\"theme\",\"v\":2,\"op\":\"preview\","
			      "\"id\":4}", &msg) &&
	      msg.op == THEME_OP_PREVIEW && msg.id == 4,
	      "wire: preview a built-in");
	CHECK(theme_msg_parse("{\"t\":\"theme\",\"v\":2,\"op\":\"save\","
			      "\"id\":0}", &msg) &&
	      msg.op == THEME_OP_SAVE && msg.id == 0, "wire: save Dark");
	CHECK(theme_msg_parse("{\"t\":\"theme\",\"v\":2,\"op\":\"cancel\"}",
			      &msg) && msg.op == THEME_OP_CANCEL,
	      "wire: cancel needs no id");

	static const char good_custom[] =
		"{\"t\":\"theme\",\"v\":2,\"op\":\"save\",\"id\":12,"
		"\"c\":[\"0e1116\",\"E6E8EB\",\"4387df\",\"0da243\","
		"\"0da243\",\"ba8107\",\"ff1900\"],\"w\":22,\"ends\":\"flat\","
		"\"dot\":0,\"by\":\"gauge\"}";

	CHECK(theme_msg_parse(good_custom, &msg) && msg.id == THEME_CUSTOM &&
	      msg.custom.c[0] == 0x0E1116 && msg.custom.c[1] == 0xE6E8EB &&
	      msg.custom.c[6] == 0xFF1900 && msg.custom.w == 22 &&
	      msg.custom.flat && !msg.custom.dot && msg.custom.by_gauge,
	      "wire: a full custom theme, either hex case");
	CHECK(strlen(good_custom) < 512, "wire: a custom line fits 512 bytes");

	/* Each of these differs from good_custom in exactly one way. */
	static const struct { const char *why; const char *line; } bad[] = {
		{ "unknown op", "{\"t\":\"theme\",\"op\":\"apply\",\"id\":1}" },
		{ "no op", "{\"t\":\"theme\",\"id\":1}" },
		{ "op prefix", "{\"t\":\"theme\",\"op\":\"savex\",\"id\":1}" },
		{ "id 13", "{\"t\":\"theme\",\"op\":\"save\",\"id\":13}" },
		{ "id -1", "{\"t\":\"theme\",\"op\":\"save\",\"id\":-1}" },
		{ "id 1.5", "{\"t\":\"theme\",\"op\":\"save\",\"id\":1.5}" },
		{ "id quoted", "{\"t\":\"theme\",\"op\":\"save\",\"id\":\"1\"}" },
		{ "no id", "{\"t\":\"theme\",\"op\":\"preview\"}" },
		{ "custom, no colours",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"w\":12,"
		  "\"ends\":\"round\",\"dot\":1,\"by\":\"level\"}" },
		{ "six colours",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"000000\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\"],"
		  "\"w\":12,\"ends\":\"round\",\"dot\":1,\"by\":\"level\"}" },
		{ "eight colours",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"000000\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\",\"666666\"],\"w\":12,\"ends\":\"round\",\"dot\":1,"
		  "\"by\":\"level\"}" },
		{ "five-digit colour",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"00000\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\"],\"w\":12,\"ends\":\"round\",\"dot\":1,"
		  "\"by\":\"level\"}" },
		{ "not hex",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"00000g\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\"],\"w\":12,\"ends\":\"round\",\"dot\":1,"
		  "\"by\":\"level\"}" },
		{ "width 7",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"000000\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\"],\"w\":7,\"ends\":\"round\",\"dot\":1,"
		  "\"by\":\"level\"}" },
		{ "dot 2",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"000000\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\"],\"w\":12,\"ends\":\"round\",\"dot\":2,"
		  "\"by\":\"level\"}" },
		{ "ends roundish",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"000000\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\"],\"w\":12,\"ends\":\"roundish\",\"dot\":1,"
		  "\"by\":\"level\"}" },
		{ "by missing",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"000000\","
		  "\"ffffff\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\"],\"w\":12,\"ends\":\"round\",\"dot\":1}" },
		{ "unreadable text",
		  "{\"t\":\"theme\",\"op\":\"save\",\"id\":12,\"c\":[\"000000\","
		  "\"111111\",\"111111\",\"222222\",\"333333\",\"444444\","
		  "\"555555\"],\"w\":12,\"ends\":\"round\",\"dot\":1,"
		  "\"by\":\"level\"}" },
	};

	for (unsigned i = 0; i < sizeof(bad) / sizeof(bad[0]); i++) {
		snprintf(m, sizeof(m), "wire: a line with %s is refused whole",
			 bad[i].why);
		CHECK(!theme_msg_parse(bad[i].line, &msg), m);
	}

	/* --- theme_now's "h" --- */
	{
		uint8_t d[20];
		static const uint8_t abc_sha1[20] = {
			0xa9, 0x99, 0x3e, 0x36, 0x47, 0x06, 0x81, 0x6a, 0xba, 0x3e,
			0x25, 0x71, 0x78, 0x50, 0xc2, 0x6c, 0x9c, 0xd0, 0xd8, 0x9d,
		};
		uint8_t big[100];

		theme_sha1((const uint8_t *)"abc", 3, d);
		CHECK(memcmp(d, abc_sha1, 20) == 0, "sha1: the FIPS 180 \"abc\" vector");
		/* 100 bytes crosses a block, and 56..63 bytes of tail forces
		 * the length into a block of its own: both padding paths.
		 * Reference: python3 -c "import hashlib;
		 *   print(hashlib.sha1(bytes(range(100))).hexdigest()[:8])" */
		for (int i = 0; i < 100; i++) {
			big[i] = (uint8_t)i;
		}
		theme_sha1(big, sizeof(big), d);
		snprintf(m, sizeof(m), "sha1: 100 bytes (%02x%02x%02x%02x)", d[0],
			 d[1], d[2], d[3]);
		CHECK(d[0] == 0x1e && d[1] == 0x66 && d[2] == 0x34 && d[3] == 0xbf, m);
		theme_sha1(big, 60, d);
		snprintf(m, sizeof(m), "sha1: 60 bytes (%02x%02x%02x%02x)", d[0],
			 d[1], d[2], d[3]);
		CHECK(d[0] == 0xc9 && d[1] == 0x87 && d[2] == 0x14 && d[3] == 0xb1, m);

		/* The contract's own example, which the PC side checks too. */
		struct theme_custom ex = {
			.c = { 0x0E1116, 0xE6E8EB, 0x4387DF, 0x0DA243, 0x0DA243,
			       0xBA8107, 0xFF1900 },
			.w = 12, .flat = false, .dot = true, .by_gauge = false,
		};
		char h[9];

		theme_custom_hash(&ex, h);
		snprintf(m, sizeof(m), "h: the contract example hashes to %s"
			 " (want 7c534a4e)", h);
		CHECK(strcmp(h, "7c534a4e") == 0, m);
	}

	/* --- The welcome's capabilities --- */
	CHECK(theme_caps_has("{\"t\":\"welcome\",\"caps\":[\"theme\"]}",
			     "theme"), "caps: theme listed");
	CHECK(theme_caps_has("{\"t\":\"welcome\",\"caps\":[\"x\", \"theme\"]}",
			     "theme"), "caps: theme listed second");
	CHECK(!theme_caps_has("{\"t\":\"welcome\",\"caps\":[\"themes\"]}",
			      "theme"), "caps: a longer word is not theme");
	CHECK(!theme_caps_has("{\"t\":\"welcome\",\"v\":2}", "theme"),
	      "caps: an old daemon advertises nothing");
	CHECK(!theme_caps_has("{\"t\":\"welcome\",\"caps\":[],"
			      "\"note\":\"theme\"}", "theme"),
	      "caps: theme outside the array does not count");

	/*
	 * The header dot. Mono and Light ink drew a healthy reading in their
	 * lighter ink and a stale one in full ink once their low and mid
	 * parted (review of fp/audit-fixes, NICE 6): the warning was the bold
	 * mark, and healthy sat in the disconnected dot's grey family. Where
	 * the two are neutral, strength is all that separates them, so a sound
	 * reading must be the stronger mark; with a hue, the hue does it.
	 */
	for (int id = 0; id < THEME_BUILTIN_N; id++) {
		const struct palette *p = theme_builtin(id);
		uint32_t live, stale;
		bool grey_pair;

		theme_dot_colours(p, &live, &stale);
		grey_pair = ((live >> 16) & 0xFF) == (live & 0xFF) &&
			    ((live >> 8) & 0xFF) == (live & 0xFF) &&
			    ((stale >> 16) & 0xFF) == (stale & 0xFF) &&
			    ((stale >> 8) & 0xFF) == (stale & 0xFF);
		snprintf(m, sizeof(m), "%s: the dot's live and stale differ",
			 theme_name(id));
		CHECK(live != stale && live != p->grey && stale != p->grey, m);
		snprintf(m, sizeof(m), "%s: a stale dot is no stronger than a"
			 " live one where only strength tells them apart"
			 " (%.2f >= %.2f)", theme_name(id), cr(live, p->bg),
			 cr(stale, p->bg));
		CHECK(!grey_pair || cr(live, p->bg) >= cr(stale, p->bg), m);
		snprintf(m, sizeof(m), "%s: the dot is still low and mid where a"
			 " hue separates them", theme_name(id));
		CHECK(grey_pair || (live == p->low && stale == p->mid), m);
	}
	{
		uint32_t live, stale;

		theme_dot_colours(theme_builtin(THEME_MONO), &live, &stale);
		CHECK(live == 0xFFFFFF && stale == 0x9C9C9C,
		      "Mono: the dot is white when sound, grey when old");
		theme_dot_colours(theme_builtin(THEME_LIGHT_INK), &live, &stale);
		CHECK(live == 0x000000 && stale == 0x555555,
		      "Light ink: the dot is black when sound, grey when old");
		theme_dot_colours(theme_builtin(THEME_DARK), &live, &stale);
		CHECK(live == 0x0DA243 && stale == 0xBA8107,
		      "Dark: the dot is 1.3.4's green and amber");
	}
	/* "A custom theme made the same way" (theme.h): through
	 * theme_derive, the path a custom theme reaches the dot by. */
	{
		struct theme_custom grey = {
			.c = { 0x000000, 0xFFFFFF, 0xFFFFFF, 0xBDBDBD,
			       0x9C9C9C, 0xFFFFFF, 0xFF3B30 },
			.w = THEME_W_NORMAL, .dot = true,
		};
		struct theme_custom hue = grey;
		struct palette d;
		uint32_t live, stale;

		theme_derive(&grey, &d);
		theme_dot_colours(&d, &live, &stale);
		CHECK(live == 0xFFFFFF && stale == 0x9C9C9C,
		      "custom: grey low under white mid swaps, as Mono does");
		hue.c[4] = 0x22C55E;	/* a green low: the hue tells them apart */
		theme_derive(&hue, &d);
		theme_dot_colours(&d, &live, &stale);
		CHECK(live == 0x22C55E && stale == 0xFFFFFF,
		      "custom: a hued low keeps low and mid");
	}

	/*
	 * --- The burn view's bars: a floor on every theme ---
	 *
	 * The older bars were g1 mixed 35% into the ground with no floor, and
	 * the /code-review max of 2026-11-01 measured them, as drawn, at 2.14
	 * (Retro LCD) to 2.88 (Dark, the contract's own byte) on five
	 * built-ins. Both bar inks must read THEME_BURN_INK_FLOOR on the
	 * ground, and the newest hour must stay the stronger mark, a
	 * THEME_BURN_INK_STEP apart from the rest. Printed with the plain mix's
	 * figure beside it, so the log is the before and after.
	 */
	for (int id = 0; id < THEME_BUILTIN_N; id++) {
		const struct palette *p = theme_builtin(id);
		struct theme_burn_inks k;
		uint32_t hot0 = id == THEME_DARK ? 0x7FB2F0 :
				theme_mix(p->g1, p->text, 0.35);
		uint32_t cold0 = id == THEME_DARK ? 0x2D5C99 :
				 theme_mix(p->g1, p->bg, 0.35);

		theme_burn_inks(p, id == THEME_DARK, &k);
		snprintf(m, sizeof(m), "%s: burn bars on the ground, newest"
			 " %.2f:1 (was %.2f), older %.2f:1 (was %.2f) >= 3",
			 theme_name(id), cr(k.hot, p->bg), cr(hot0, p->bg),
			 cr(k.cold, p->bg), cr(cold0, p->bg));
		CHECK(cr(k.hot, p->bg) >= THEME_BURN_INK_FLOOR &&
		      cr(k.cold, p->bg) >= THEME_BURN_INK_FLOOR, m);
		snprintf(m, sizeof(m), "%s: the newest hour is the stronger"
			 " mark, %.2f:1 from the older bars (was %.2f) >= 1.5",
			 theme_name(id), cr(k.hot, k.cold), cr(hot0, cold0));
		CHECK(cr(k.hot, p->bg) > cr(k.cold, p->bg) &&
		      cr(k.hot, k.cold) >= THEME_BURN_INK_STEP, m);
		/* A theme the plain mix already served is drawn as before. */
		if (cr(cold0, p->bg) >= THEME_BURN_INK_FLOOR &&
		    cr(hot0, p->bg) >= THEME_BURN_INK_FLOOR &&
		    cr(hot0, cold0) >= THEME_BURN_INK_STEP) {
			snprintf(m, sizeof(m), "%s: already readable, so the"
				 " burn bars keep the plain mix",
				 theme_name(id));
			CHECK(k.hot == hot0 && k.cold == cold0, m);
		}
	}
	{
		/* Dark keeps every contract byte but the one under the floor,
		 * and that one only moves towards g1, the arrow's own blue. */
		struct theme_burn_inks k;
		const struct palette *d = theme_builtin(THEME_DARK);

		theme_burn_inks(d, true, &k);
		CHECK(k.hot == 0x7FB2F0 && k.base == 0x3A414B &&
		      k.tick == 0x5C636B,
		      "Dark: the burn view keeps the contract's newest-hour,"
		      " baseline and tick bytes");
		snprintf(m, sizeof(m), "Dark: the older bars are %06X, a step"
			 " from #2D5C99 towards g1", (unsigned)k.cold);
		CHECK(k.cold != 0x2D5C99 &&
		      ((k.cold >> 16) & 0xFF) >= 0x2D &&
		      ((k.cold >> 8) & 0xFF) >= 0x5C && (k.cold & 0xFF) >= 0x99 &&
		      ((k.cold >> 16) & 0xFF) <= 0x43 &&
		      ((k.cold >> 8) & 0xFF) <= 0x87 && (k.cold & 0xFF) <= 0xDF,
		      m);
	}
	{
		/*
		 * Custom themes: the floor is min(3:1, the text's own), as for
		 * grey, and the step holds wherever the text leaves room for
		 * it -- two marks 3:1 and 1.5 apart need about 4.5:1 on one
		 * side of the ground. g1 drawn at random too, since it is the
		 * owner's and nothing checks it against the ground. Measured
		 * 2026-11-01 on this seed: the step holds for every theme
		 * whose text reads 4.8:1 or more; 5 is the check, with room.
		 */
		uint32_t x = 0x9E3779B9u;
		int kept = 0, tried = 0, no_room = 0, step_short = 0;
		double worst = 99.0;

		while (kept < 20000 && tried < 2000000) {
			struct theme_custom r = t;
			struct palette rp;
			struct theme_burn_inks k;

			tried++;
			for (int i = 0; i < 3; i++) {
				x ^= x << 13;
				x ^= x >> 17;
				x ^= x << 5;
				r.c[i] = x & 0xFFFFFF;
			}
			if (!theme_custom_valid(&r)) {
				continue;
			}
			kept++;
			theme_derive(&r, &rp);
			theme_burn_inks(&rp, false, &k);

			double tx = cr(rp.text, rp.bg);
			double want = tx < 3.0 ? tx : 3.0;
			double got = cr(k.hot, rp.bg) < cr(k.cold, rp.bg) ?
				     cr(k.hot, rp.bg) : cr(k.cold, rp.bg);

			if (got / want < worst) {
				worst = got / want;
			}
			if (cr(k.hot, k.cold) < THEME_BURN_INK_STEP ||
			    cr(k.hot, rp.bg) <= cr(k.cold, rp.bg)) {
				if (tx >= 5.0) {
					step_short++;
					printf("  short: %06X on %06X, g1 %06X\n",
					       (unsigned)rp.text, (unsigned)rp.bg,
					       (unsigned)rp.g1);
				} else {
					no_room++;
				}
			}
		}
		snprintf(m, sizeof(m), "every custom theme's burn bars meet"
			 " min(3.0, text) on the ground (%d themes, worst %.4f"
			 " of the target)", kept, worst);
		CHECK(kept == 20000 && worst >= 1.0, m);
		snprintf(m, sizeof(m), "and the newest hour is 1.5:1 the"
			 " stronger wherever the text reads 5:1 or more (%d"
			 " short; %d with text under 5:1 went without)",
			 step_short, no_room);
		CHECK(step_short == 0, m);
	}

	printf(failures ? "\n%d FAILED\n" : "\nall theme checks passed\n",
	       failures);
	return failures ? 1 : 0;
}
