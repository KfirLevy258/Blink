/*
 * One dial, sized for the distance the thing is actually read from.
 *
 * The constraint that drove this, measured rather than guessed: on a 2.8"
 * 320x240 panel at 60 cm, one pixel subtends about one arcminute. Comfortable
 * sustained reading wants a cap height near 20 arcminutes; montserrat_14's cap
 * is 10 px, so it lands at 10. Every label I had been packing in was at the
 * threshold of what an eye can resolve at that distance -- present, but
 * unreadable without leaning in, which is the definition of visual noise.
 *
 * So: at most two things in type you can actually read, and the rest carried
 * by GEOMETRY. A needle's angle survives well past the distance at which text
 * stops resolving, which is the whole reason instruments have needles.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <lvgl.h>

#define W 320
#define H 240

#define COL_BG      lv_color_hex(0x0E1116)
#define COL_FACE    lv_color_hex(0x141922)
#define COL_BEZEL   lv_color_hex(0x2E353F)
#define COL_TICK    lv_color_hex(0x4A525C)
#define COL_TEXT    lv_color_hex(0xE6E8EB)
#define COL_FAR     lv_color_hex(0x6E7782)
#define COL_GREEN   lv_color_hex(0x4AB07D)
#define COL_AMBER   lv_color_hex(0xCA9E45)
#define COL_RED     lv_color_hex(0xFF5447)
#define COL_CODEX   lv_color_hex(0x21B6A7)

#define ROT   150
#define SPAN  240
#define DIA   214
#define DY    12

static uint16_t fb[W * H];

static void flush_cb(lv_display_t *d, const lv_area_t *a, uint8_t *px)
{
	uint16_t *src = (uint16_t *)px;

	for (int32_t y = a->y1; y <= a->y2; y++) {
		for (int32_t x = a->x1; x <= a->x2; x++) {
			fb[y * W + x] = *src++;
		}
	}
	lv_display_flush_ready(d);
}

static void write_ppm(const char *path)
{
	FILE *f = fopen(path, "wb");

	fprintf(f, "P6\n%d %d\n255\n", W, H);
	for (int i = 0; i < W * H; i++) {
		uint16_t c = fb[i];
		uint8_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;
		uint8_t o[3] = { (uint8_t)((r << 3) | (r >> 2)),
				 (uint8_t)((g << 2) | (g >> 4)),
				 (uint8_t)((b << 3) | (b >> 2)) };
		fwrite(o, 1, 3, f);
	}
	fclose(f);
}

static lv_color_t sev(double p)
{
	return p >= 90 ? COL_RED : p >= 60 ? COL_AMBER : COL_GREEN;
}

static lv_obj_t *lbl(lv_obj_t *p, const char *t, lv_color_t c,
		     const lv_font_t *f, int x, int y)
{
	lv_obj_t *o = lv_label_create(p);

	lv_label_set_text(o, t);
	lv_obj_set_style_text_color(o, c, 0);
	if (f) {
		lv_obj_set_style_text_font(o, f, 0);
	}
	lv_obj_align(o, LV_ALIGN_TOP_MID, x, y);
	return o;
}

static void build(lv_obj_t *s, double a, double b, double weekly,
		  const char *cd, bool paired)
{
	/* The face. Its edge is what makes this an object rather than a chart
	 * floating on black. */
	lv_obj_t *face = lv_obj_create(s);

	lv_obj_set_size(face, DIA, DIA);
	lv_obj_align(face, LV_ALIGN_TOP_MID, 0, DY);
	lv_obj_set_style_radius(face, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_bg_color(face, COL_FACE, 0);
	lv_obj_set_style_bg_opa(face, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(face, COL_BEZEL, 0);
	lv_obj_set_style_border_width(face, 2, 0);
	lv_obj_clear_flag(face, LV_OBJ_FLAG_SCROLLABLE);

	/* The weekly window, as a band around the rim. Geometry, not text --
	 * so it still says something at a distance where a "91%" would not. */
	lv_obj_t *wk = lv_arc_create(s);

	lv_obj_set_size(wk, DIA - 8, DIA - 8);
	lv_obj_align(wk, LV_ALIGN_TOP_MID, 0, DY + 4);
	lv_arc_set_rotation(wk, ROT);
	lv_arc_set_bg_angles(wk, 0, SPAN);
	lv_arc_set_range(wk, 0, 100);
	lv_arc_set_value(wk, (int)weekly);
	lv_obj_remove_style(wk, NULL, LV_PART_KNOB);
	lv_obj_set_style_arc_width(wk, 4, LV_PART_MAIN);
	lv_obj_set_style_arc_width(wk, 4, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(wk, COL_BEZEL, LV_PART_MAIN);
	lv_obj_set_style_arc_color(wk, sev(weekly), LV_PART_INDICATOR);
	/* Held back deliberately. It is the outermost and longest mark on the
	 * panel, so at full strength it reads as the headline -- and it is the
	 * secondary window. Weight follows importance, not size. */
	lv_obj_set_style_arc_opa(wk, LV_OPA_50, LV_PART_INDICATOR);

	/* Ticks. Also geometry: they give the face its character and cost no
	 * legibility, because nobody reads a tick. */
	lv_obj_t *sc = lv_scale_create(s);

	lv_obj_set_size(sc, DIA - 30, DIA - 30);
	lv_obj_align(sc, LV_ALIGN_TOP_MID, 0, DY + 15);
	lv_scale_set_mode(sc, LV_SCALE_MODE_ROUND_INNER);
	lv_scale_set_range(sc, 0, 100);
	lv_scale_set_total_tick_count(sc, 26);
	lv_scale_set_major_tick_every(sc, 5);
	lv_scale_set_angle_range(sc, SPAN);
	lv_scale_set_rotation(sc, ROT);
	lv_scale_set_label_show(sc, false);	/* the numerals were noise at 60 cm */
	lv_obj_set_style_bg_opa(sc, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_width(sc, 0, 0);
	lv_obj_set_style_arc_opa(sc, LV_OPA_TRANSP, LV_PART_MAIN);
	lv_obj_set_style_line_color(sc, COL_TICK, LV_PART_ITEMS);
	lv_obj_set_style_line_width(sc, 2, LV_PART_ITEMS);
	lv_obj_set_style_length(sc, 6, LV_PART_ITEMS);
	lv_obj_set_style_line_color(sc, COL_FAR, LV_PART_INDICATOR);
	lv_obj_set_style_line_width(sc, 3, LV_PART_INDICATOR);
	lv_obj_set_style_length(sc, 12, LV_PART_INDICATOR);

	/* The danger band, on the rim where the ticks end. */
	lv_obj_t *band = lv_arc_create(s);

	lv_obj_set_size(band, DIA - 52, DIA - 52);
	lv_obj_align(band, LV_ALIGN_TOP_MID, 0, DY + 26);
	lv_arc_set_rotation(band, ROT + SPAN * 9 / 10);
	lv_arc_set_bg_angles(band, 0, SPAN / 10);
	lv_obj_remove_style(band, NULL, LV_PART_KNOB);
	lv_obj_remove_style(band, NULL, LV_PART_INDICATOR);
	lv_obj_set_style_arc_width(band, 5, LV_PART_MAIN);
	lv_obj_set_style_arc_color(band, COL_RED, LV_PART_MAIN);
	lv_obj_set_style_arc_opa(band, LV_OPA_90, LV_PART_MAIN);

	if (paired) {
		lv_obj_t *n2 = lv_line_create(sc);

		lv_obj_set_style_line_width(n2, 4, 0);
		lv_obj_set_style_line_color(n2, COL_CODEX, 0);
		lv_obj_set_style_line_rounded(n2, true, 0);
		lv_scale_set_line_needle_value(sc, n2, 68, (int)b);
	}

	lv_obj_t *n1 = lv_line_create(sc);

	lv_obj_set_style_line_width(n1, 5, 0);
	lv_obj_set_style_line_color(n1, COL_TEXT, 0);
	lv_obj_set_style_line_rounded(n1, true, 0);
	lv_scale_set_line_needle_value(sc, n1, 80, (int)a);

	/* THE TWO THINGS YOU CAN ACTUALLY READ AT 60 CM.
	 *
	 * 48 px puts a cap height at ~35 arcminutes and 28 px at ~20, which is
	 * the comfortable range. Everything else on this panel is geometry.
	 */
	char buf[16];

	/*
	 * BELOW the hub, in the gap the needles never sweep.
	 *
	 * The first pass centred these and the needles drew straight through
	 * the digits. A real gauge puts its readout in the dead sector for
	 * exactly this reason -- the sweep is 240 degrees, so the bottom 120
	 * is guaranteed clear whatever the value.
	 */
	snprintf(buf, sizeof(buf), "%d%%", (int)a);
	lbl(s, buf, sev(a), &lv_font_montserrat_48, 0, DY + 118);
	lbl(s, cd, COL_FAR, &lv_font_montserrat_28, 0, DY + 168);

	/* The hub, over the needles so they read as pivoting. */
	lv_obj_t *hub = lv_obj_create(s);

	lv_obj_set_size(hub, 18, 18);
	lv_obj_align(hub, LV_ALIGN_TOP_MID, 0, DY + DIA / 2 - 9);
	lv_obj_set_style_radius(hub, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_bg_color(hub, COL_BEZEL, 0);
	lv_obj_set_style_bg_opa(hub, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(hub, COL_FACE, 0);
	lv_obj_set_style_border_width(hub, 3, 0);
}

int main(int argc, char **argv)
{
	static uint8_t buf[W * 40 * 2];
	const char *dir = argc > 1 ? argv[1] : ".";
	struct { const char *n; double a, b, w; const char *cd; bool p; } c[] = {
		{ "calm", 27, 0,  42, "3h 40m", false },
		{ "busy", 78, 34, 91, "30 min", true  },
		{ "gone", 97, 88, 99, "4 min",  true  },
	};

	lv_init();
	for (unsigned i = 0; i < sizeof(c) / sizeof(c[0]); i++) {
		lv_display_t *d = lv_display_create(W, H);
		char path[256];

		lv_display_set_flush_cb(d, flush_cb);
		lv_display_set_buffers(d, buf, NULL, sizeof(buf),
				       LV_DISPLAY_RENDER_MODE_PARTIAL);
		lv_obj_t *s = lv_obj_create(NULL);

		lv_obj_clear_flag(s, LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_set_style_pad_all(s, 0, 0);
		lv_obj_set_style_border_width(s, 0, 0);
		lv_obj_set_style_bg_color(s, COL_BG, 0);
		lv_obj_set_style_bg_opa(s, LV_OPA_COVER, 0);
		lv_screen_load(s);
		build(s, c[i].a, c[i].b, c[i].w, c[i].cd, c[i].p);
		for (int k = 0; k < 30; k++) {
			lv_tick_inc(16);
			lv_timer_handler();
		}
		snprintf(path, sizeof(path), "%s/big-%s.ppm", dir, c[i].n);
		write_ppm(path);
		printf("wrote %s\n", path);
	}
	return 0;
}
