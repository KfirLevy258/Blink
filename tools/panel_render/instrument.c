/*
 * The instrument direction, developed. NOT shipping code yet.
 *
 * The sketch that won the comparison was a bare scale with a floating line on
 * it. What makes a dial read as a MECHANISM rather than as a chart is a
 * handful of specific things it was missing: a bezel that gives the face an
 * edge, a hub the needle visibly pivots on, a danger band painted onto the
 * face rather than implied by tick colour, and numerals sitting inside the
 * ticks the way an instrument prints them.
 *
 * Two providers become TWO NEEDLES on one dial, which is an existing gauge
 * convention (dual-needle temperature and pressure gauges) and avoids the
 * nesting that ran out at two rings.
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
#define COL_TRACK   lv_color_hex(0x272C34)
#define COL_TEXT    lv_color_hex(0xE6E8EB)
#define COL_DIM     lv_color_hex(0x8A9199)
#define COL_FAR     lv_color_hex(0x59616B)
#define COL_GREEN   lv_color_hex(0x0DA243)
#define COL_AMBER   lv_color_hex(0xBA8107)
#define COL_RED     lv_color_hex(0xFF1900)
#define COL_CLAUDE  lv_color_hex(0xE6E8EB)   /* the primary needle: plain white */
#define COL_CODEX   lv_color_hex(0x21B6A7)

/* The main dial's sweep. 0% at the lower left, 100% at the lower right. */
#define DIAL_ROT    165
#define DIAL_SPAN   210
#define DIAL_CX     (-56)
#define DIAL_Y      26
#define DIAL_SZ     186
#define REDZONE_PCT 90

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
		     const lv_font_t *f, lv_align_t al, int x, int y)
{
	lv_obj_t *o = lv_label_create(p);

	lv_label_set_text(o, t);
	lv_obj_set_style_text_color(o, c, 0);
	if (f) {
		lv_obj_set_style_text_font(o, f, 0);
	}
	lv_obj_align(o, al, x, y);
	return o;
}

/* A plain filled disc, used for the face and for the needle hub. */
static lv_obj_t *disc(lv_obj_t *p, int sz, lv_color_t c, int cx, int y)
{
	lv_obj_t *o = lv_obj_create(p);

	lv_obj_set_size(o, sz, sz);
	lv_obj_align(o, LV_ALIGN_TOP_MID, cx, y);
	lv_obj_set_style_radius(o, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_bg_color(o, c, 0);
	lv_obj_set_style_bg_opa(o, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(o, 0, 0);
	lv_obj_set_style_pad_all(o, 0, 0);
	lv_obj_clear_flag(o, LV_OBJ_FLAG_SCROLLABLE);
	return o;
}

static void build(lv_obj_t *s, double s_claude, double s_codex, double weekly,
		  const char *cd, const char *wcd, bool paired)
{
	/* --- the face and its bezel: what makes this an object ---------- */
	lv_obj_t *face = disc(s, DIAL_SZ, COL_FACE, DIAL_CX, DIAL_Y);

	lv_obj_set_style_border_color(face, COL_BEZEL, 0);
	lv_obj_set_style_border_width(face, 2, 0);

	/* --- the danger band, painted ON the face ----------------------- */
	lv_obj_t *band = lv_arc_create(s);
	int red_deg = DIAL_SPAN * (100 - REDZONE_PCT) / 100;

	lv_obj_set_size(band, DIAL_SZ - 6, DIAL_SZ - 6);
	lv_obj_align(band, LV_ALIGN_TOP_MID, DIAL_CX, DIAL_Y + 3);
	lv_arc_set_rotation(band, DIAL_ROT + DIAL_SPAN - red_deg);
	lv_arc_set_bg_angles(band, 0, red_deg);
	lv_arc_set_value(band, 0);
	lv_obj_remove_style(band, NULL, LV_PART_KNOB);
	lv_obj_remove_style(band, NULL, LV_PART_INDICATOR);
	lv_obj_set_style_arc_width(band, 5, LV_PART_MAIN);
	lv_obj_set_style_arc_color(band, COL_RED, LV_PART_MAIN);
	lv_obj_set_style_arc_opa(band, LV_OPA_80, LV_PART_MAIN);

	/* --- the scale: ticks and numerals ------------------------------ */
	lv_obj_t *sc = lv_scale_create(s);

	lv_obj_set_size(sc, DIAL_SZ - 14, DIAL_SZ - 14);
	lv_obj_align(sc, LV_ALIGN_TOP_MID, DIAL_CX, DIAL_Y + 7);
	lv_scale_set_mode(sc, LV_SCALE_MODE_ROUND_INNER);
	lv_scale_set_range(sc, 0, 100);
	lv_scale_set_total_tick_count(sc, 21);
	lv_scale_set_major_tick_every(sc, 5);
	lv_scale_set_angle_range(sc, DIAL_SPAN);
	lv_scale_set_rotation(sc, DIAL_ROT);
	lv_obj_set_style_bg_opa(sc, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_width(sc, 0, 0);
	lv_obj_set_style_arc_opa(sc, LV_OPA_TRANSP, LV_PART_MAIN);
	lv_obj_set_style_line_color(sc, COL_FAR, LV_PART_ITEMS);
	lv_obj_set_style_line_width(sc, 2, LV_PART_ITEMS);
	lv_obj_set_style_length(sc, 5, LV_PART_ITEMS);
	lv_obj_set_style_line_color(sc, COL_DIM, LV_PART_INDICATOR);
	lv_obj_set_style_line_width(sc, 2, LV_PART_INDICATOR);
	lv_obj_set_style_length(sc, 10, LV_PART_INDICATOR);
	lv_obj_set_style_text_color(sc, COL_FAR, LV_PART_INDICATOR);
	lv_obj_set_style_text_opa(sc, LV_OPA_70, LV_PART_INDICATOR);

	/* --- the needles ------------------------------------------------ */
	if (paired) {
		lv_obj_t *n2 = lv_line_create(sc);

		lv_obj_set_style_line_width(n2, 3, 0);
		lv_obj_set_style_line_color(n2, COL_CODEX, 0);
		lv_obj_set_style_line_rounded(n2, true, 0);
		lv_scale_set_line_needle_value(sc, n2, 62, (int)s_codex);
	}

	lv_obj_t *n1 = lv_line_create(sc);

	lv_obj_set_style_line_width(n1, 4, 0);
	lv_obj_set_style_line_color(n1, COL_CLAUDE, 0);
	lv_obj_set_style_line_rounded(n1, true, 0);
	lv_scale_set_line_needle_value(sc, n1, 74, (int)s_claude);

	/* The hub, drawn last so both needles disappear under it -- which is
	 * what makes them read as pivoting rather than as lines that happen to
	 * meet. */
	lv_obj_t *hub = disc(s, 16, COL_BEZEL, DIAL_CX, DIAL_Y + DIAL_SZ / 2 - 8);

	lv_obj_set_style_border_color(hub, COL_FACE, 0);
	lv_obj_set_style_border_width(hub, 2, 0);

	/* --- the reading, printed on the face like a date window -------- */
	char buf[24];

	snprintf(buf, sizeof(buf), "%d%%", (int)s_claude);
	lbl(s, buf, sev(s_claude), &lv_font_montserrat_28, LV_ALIGN_TOP_MID,
	    DIAL_CX, DIAL_Y + 96);
	lbl(s, "SESSION 5h", COL_FAR, NULL, LV_ALIGN_TOP_MID, DIAL_CX,
	    DIAL_Y + 132);

	/* --- the right-hand stack: everything the dial cannot hold ------ */
	lbl(s, "23:47", COL_FAR, NULL, LV_ALIGN_TOP_RIGHT, -12, 10);

	lv_obj_t *rule = lv_obj_create(s);

	lv_obj_set_size(rule, 108, 1);
	lv_obj_align(rule, LV_ALIGN_TOP_RIGHT, -12, 34);
	lv_obj_set_style_bg_color(rule, COL_BEZEL, 0);
	lv_obj_set_style_border_width(rule, 0, 0);
	lv_obj_set_style_radius(rule, 0, 0);

	lbl(s, "RESETS IN", COL_FAR, NULL, LV_ALIGN_TOP_RIGHT, -12, 44);
	lbl(s, cd, COL_TEXT, NULL, LV_ALIGN_TOP_RIGHT, -12, 62);
	if (paired) {
		char c2[24];

		snprintf(c2, sizeof(c2), "codex %d%%", (int)s_codex);
		lbl(s, c2, COL_CODEX, NULL, LV_ALIGN_TOP_RIGHT, -12, 84);
	}

	/* The weekly window as a sub-dial: the same instrument language at a
	 * smaller scale, the way a chronograph nests its counters. */
	lv_obj_t *sub = disc(s, 74, COL_FACE, 108, 134);

	lv_obj_set_style_border_color(sub, COL_BEZEL, 0);
	lv_obj_set_style_border_width(sub, 2, 0);

	lv_obj_t *sarc = lv_arc_create(s);

	lv_obj_set_size(sarc, 60, 60);
	lv_obj_align(sarc, LV_ALIGN_TOP_MID, 108, 141);
	lv_arc_set_rotation(sarc, 135);
	lv_arc_set_bg_angles(sarc, 0, 270);
	lv_arc_set_range(sarc, 0, 100);
	lv_arc_set_value(sarc, (int)weekly);
	lv_obj_remove_style(sarc, NULL, LV_PART_KNOB);
	lv_obj_set_style_arc_width(sarc, 5, LV_PART_MAIN);
	lv_obj_set_style_arc_width(sarc, 5, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(sarc, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(sarc, sev(weekly), LV_PART_INDICATOR);

	snprintf(buf, sizeof(buf), "%d%%", (int)weekly);
	lbl(s, buf, COL_TEXT, NULL, LV_ALIGN_TOP_MID, 108, 162);
	lbl(s, "WEEK", COL_FAR, NULL, LV_ALIGN_TOP_MID, 108, 212);
	lbl(s, wcd, COL_DIM, NULL, LV_ALIGN_TOP_MID, 108, 112);
}

int main(int argc, char **argv)
{
	static uint8_t buf[W * 40 * 2];
	const char *dir = argc > 1 ? argv[1] : ".";
	struct { const char *n; double a, b, w; const char *cd, *wcd; bool p; }
	cases[] = {
		{ "one",  27, 0,  42, "3h 40m",  "6d 22h", false },
		{ "two",  78, 34, 91, "30m 00s", "1d 1h",  true  },
		{ "hot",  97, 88, 99, "04m 12s", "2h 30m", true  },
	};

	lv_init();
	for (unsigned i = 0; i < sizeof(cases) / sizeof(cases[0]); i++) {
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
		build(s, cases[i].a, cases[i].b, cases[i].w, cases[i].cd,
		      cases[i].wcd, cases[i].p);
		for (int k = 0; k < 30; k++) {
			lv_tick_inc(16);
			lv_timer_handler();
		}
		snprintf(path, sizeof(path), "%s/inst-%s.ppm", dir, cases[i].n);
		write_ppm(path);
		printf("wrote %s\n", path);
	}
	return 0;
}
