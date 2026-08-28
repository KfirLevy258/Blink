/*
 * Layout explorations for the gauge screen. NOT shipping code.
 *
 * Draws several completely different arrangements of the SAME data, straight
 * onto an LVGL framebuffer, so they can be compared as pixels instead of as
 * descriptions. Nothing here touches usage_view.c -- the point is to try
 * shapes cheaply before committing one to the firmware.
 *
 *   cc ... variants.c -o variants && ./variants out/ 
 *
 * The data is identical across every variant, deliberately: two providers,
 * one of them close to its weekly limit, so each layout is judged on the same
 * awkward case rather than on a flattering one.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <lvgl.h>

#define W 320
#define H 240

#define COL_BG      lv_color_hex(0x0E1116)
#define COL_TRACK   lv_color_hex(0x272C34)
#define COL_TEXT    lv_color_hex(0xE6E8EB)
#define COL_DIM     lv_color_hex(0x8A9199)
#define COL_GREEN   lv_color_hex(0x0DA243)
#define COL_AMBER   lv_color_hex(0xBA8107)
#define COL_RED     lv_color_hex(0xFF1900)
#define COL_CLAUDE  lv_color_hex(0xC6653B)
#define COL_CODEX   lv_color_hex(0x21B6A7)

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

static lv_color_t severity(double pct)
{
	return pct >= 90 ? COL_RED : pct >= 60 ? COL_AMBER : COL_GREEN;
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

/* A bullet: a track, a filled measure, and a threshold marker at 90%. */
static void bullet(lv_obj_t *p, int x, int y, int w, int h, double pct,
		   lv_color_t fill)
{
	lv_obj_t *tr = lv_obj_create(p);

	lv_obj_set_size(tr, w, h);
	lv_obj_align(tr, LV_ALIGN_TOP_LEFT, x, y);
	lv_obj_set_style_bg_color(tr, COL_TRACK, 0);
	lv_obj_set_style_bg_opa(tr, LV_OPA_COVER, 0);
	lv_obj_set_style_border_width(tr, 0, 0);
	lv_obj_set_style_radius(tr, h / 2, 0);
	lv_obj_set_style_pad_all(tr, 0, 0);

	int fw = (int)(w * pct / 100.0 + 0.5);

	if (fw > 0) {
		lv_obj_t *fi = lv_obj_create(p);

		lv_obj_set_size(fi, fw, h);
		lv_obj_align(fi, LV_ALIGN_TOP_LEFT, x, y);
		lv_obj_set_style_bg_color(fi, fill, 0);
		lv_obj_set_style_bg_opa(fi, LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(fi, 0, 0);
		lv_obj_set_style_radius(fi, h / 2, 0);
		lv_obj_set_style_pad_all(fi, 0, 0);
	}
	/* The threshold marker -- what a bullet chart has and a bar does not:
	 * where "nearly out" begins, so a length becomes a judgement. */
	lv_obj_t *mk = lv_obj_create(p);

	lv_obj_set_size(mk, 2, h + 6);
	lv_obj_align(mk, LV_ALIGN_TOP_LEFT, x + (int)(w * 0.9), y - 3);
	lv_obj_set_style_bg_color(mk, COL_TEXT, 0);
	lv_obj_set_style_bg_opa(mk, LV_OPA_50, 0);
	lv_obj_set_style_border_width(mk, 0, 0);
	lv_obj_set_style_radius(mk, 0, 0);
	lv_obj_set_style_pad_all(mk, 0, 0);
}

static lv_obj_t *ring(lv_obj_t *p, int cx, int y, int sz, int wdt, double pct,
		      lv_color_t c)
{
	lv_obj_t *a = lv_arc_create(p);

	lv_obj_set_size(a, sz, sz);
	lv_obj_align(a, LV_ALIGN_TOP_MID, cx, y);
	lv_arc_set_rotation(a, 135);
	lv_arc_set_bg_angles(a, 0, 270);
	lv_arc_set_range(a, 0, 100);
	lv_arc_set_value(a, (int)(pct + 0.5));
	lv_obj_remove_style(a, NULL, LV_PART_KNOB);
	lv_obj_clear_flag(a, LV_OBJ_FLAG_CLICKABLE);
	lv_obj_set_style_arc_width(a, wdt, LV_PART_MAIN);
	lv_obj_set_style_arc_width(a, wdt, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(a, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_color(a, c, LV_PART_INDICATOR);
	return a;
}

static void header(lv_obj_t *s, const char *note)
{
	lbl(s, "23:47", COL_DIM, NULL, LV_ALIGN_TOP_LEFT, 10, 8);
	lv_obj_t *b = lbl(s, "BLINK", COL_DIM, NULL, LV_ALIGN_TOP_MID, 0, 8);

	lv_obj_set_style_text_letter_space(b, 2, 0);

	lv_obj_t *d = lv_obj_create(s);

	lv_obj_set_size(d, 12, 12);
	lv_obj_align(d, LV_ALIGN_TOP_RIGHT, -12, 8);
	lv_obj_set_style_radius(d, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_border_width(d, 0, 0);
	lv_obj_set_style_bg_color(d, COL_AMBER, 0);
	if (note) {
		lbl(s, note, COL_DIM, NULL, LV_ALIGN_BOTTOM_MID, 0, -6);
	}
}

/* ---- A: bullet rows. One line per provider per window. ---------------- */
static void variant_bullets(lv_obj_t *s)
{
	header(s, NULL);

	struct { const char *win, *prov, *cd; double pct; lv_color_t c; int y; }
	rows[] = {
		{ "SESSION 5h", "claude", "30m 00s", 78, COL_CLAUDE,  56 },
		{ NULL,         "codex",  "1h 12m",  34, COL_CODEX,   84 },
		{ "WEEKLY 7d",  "claude", "1d 1h",   91, COL_CLAUDE, 136 },
		{ NULL,         "codex",  "3d 0h",   61, COL_CODEX,  164 },
	};
	for (unsigned i = 0; i < 4; i++) {
		char buf[16];

		if (rows[i].win) {
			lbl(s, rows[i].win, COL_TEXT, NULL, LV_ALIGN_TOP_LEFT,
			    12, rows[i].y - 22);
		}
		lbl(s, rows[i].prov, rows[i].c, NULL, LV_ALIGN_TOP_LEFT, 12,
		    rows[i].y - 2);
		bullet(s, 78, rows[i].y + 3, 112, 10, rows[i].pct,
		       severity(rows[i].pct));
		snprintf(buf, sizeof(buf), "%d%%", (int)rows[i].pct);
		/* Fixed columns. The first pass let the percentage and the
		 * countdown share whatever space was left and "78%30m 00s"
		 * ran together at three digits. */
		lv_obj_t *pc = lbl(s, buf, COL_TEXT, NULL, LV_ALIGN_TOP_LEFT,
				   198, rows[i].y - 2);
		lv_obj_set_width(pc, 40);
		lv_obj_set_style_text_align(pc, LV_TEXT_ALIGN_RIGHT, 0);
		lbl(s, rows[i].cd, COL_DIM, NULL, LV_ALIGN_TOP_RIGHT, -10,
		    rows[i].y - 2);
	}
}

/* ---- B: session dominant. The 5h window is what bites. --------------- */
static void variant_dominant(lv_obj_t *s)
{
	header(s, NULL);

	ring(s, -76, 44, 150, 14, 78, severity(78));
	ring(s, -76, 60, 118, 7, 34, severity(34));
	lbl(s, "78%", COL_TEXT, &lv_font_montserrat_20, LV_ALIGN_TOP_MID, -76, 100);
	lbl(s, "34%", COL_DIM, NULL, LV_ALIGN_TOP_MID, -76, 126);
	lbl(s, "SESSION 5h", COL_DIM, NULL, LV_ALIGN_TOP_MID, -76, 178);

	lbl(s, "WEEKLY 7d", COL_DIM, NULL, LV_ALIGN_TOP_LEFT, 178, 60);
	lbl(s, "claude", COL_CLAUDE, NULL, LV_ALIGN_TOP_LEFT, 178, 82);
	bullet(s, 178, 102, 126, 10, 91, severity(91));
	lbl(s, "91%   1d 1h", COL_DIM, NULL, LV_ALIGN_TOP_LEFT, 178, 118);
	lbl(s, "codex", COL_CODEX, NULL, LV_ALIGN_TOP_LEFT, 178, 144);
	bullet(s, 178, 164, 126, 10, 61, severity(61));
	lbl(s, "61%   3d 0h", COL_DIM, NULL, LV_ALIGN_TOP_LEFT, 178, 180);

	lbl(s, "claude  30m 00s", COL_CLAUDE, NULL, LV_ALIGN_TOP_MID, -76, 198);
	lbl(s, "codex   1h 12m", COL_CODEX, NULL, LV_ALIGN_TOP_MID, -76, 218);
}

/* ---- C: time first. The question is "how long have I got". ----------- */
static void variant_timefirst(lv_obj_t *s)
{
	header(s, NULL);

	lbl(s, "30 min", COL_TEXT, &lv_font_montserrat_20, LV_ALIGN_TOP_MID, 0, 44);
	lbl(s, "of session left", COL_DIM, NULL, LV_ALIGN_TOP_MID, 0, 74);

	struct { const char *n, *cd; double pct; lv_color_t c; int y; } r[] = {
		{ "SESSION 5h", "30m 00s", 78, COL_CLAUDE, 116 },
		{ "WEEKLY 7d",  "1d 1h",   91, COL_CLAUDE, 152 },
		{ "codex 5h",   "1h 12m",  34, COL_CODEX,  188 },
	};
	for (unsigned i = 0; i < 3; i++) {
		char buf[16];

		lbl(s, r[i].n, r[i].c, NULL, LV_ALIGN_TOP_LEFT, 12, r[i].y);
		bullet(s, 106, r[i].y + 5, 92, 8, r[i].pct, severity(r[i].pct));
		snprintf(buf, sizeof(buf), "%d%%", (int)r[i].pct);
		lv_obj_t *pc = lbl(s, buf, COL_TEXT, NULL, LV_ALIGN_TOP_LEFT,
				   204, r[i].y);
		lv_obj_set_width(pc, 40);
		lv_obj_set_style_text_align(pc, LV_TEXT_ALIGN_RIGHT, 0);
		lbl(s, r[i].cd, COL_DIM, NULL, LV_ALIGN_TOP_RIGHT, -10, r[i].y);
	}
}

typedef void (*builder)(lv_obj_t *);

int main(int argc, char **argv)
{
	static uint8_t buf[W * 40 * 2];
	const char *dir = argc > 1 ? argv[1] : ".";
	struct { const char *name; builder fn; } v[] = {
		{ "bullets",   variant_bullets },
		{ "dominant",  variant_dominant },
		{ "timefirst", variant_timefirst },
	};

	lv_init();
	for (unsigned i = 0; i < sizeof(v) / sizeof(v[0]); i++) {
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
		v[i].fn(s);
		for (int k = 0; k < 30; k++) {
			lv_tick_inc(16);
			lv_timer_handler();
		}
		snprintf(path, sizeof(path), "%s/variant-%s.ppm", dir, v[i].name);
		write_ppm(path);
		printf("wrote %s\n", path);
	}
	return 0;
}
