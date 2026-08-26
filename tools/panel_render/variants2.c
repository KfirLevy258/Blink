/*
 * A wider set of layout explorations. NOT shipping code.
 *
 * The first round produced four ways to arrange the same numbers, which was
 * the wrong axis: all four were dashboards, and none of them asked whether a
 * thing that sits on a desk all day should be a dashboard at all.
 *
 * These four question that instead. Same data throughout.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <lvgl.h>

#define W 320
#define H 240

#define COL_BG      lv_color_hex(0x0E1116)
#define COL_TRACK   lv_color_hex(0x272C34)
#define COL_FAINT   lv_color_hex(0x1A1F27)
#define COL_TEXT    lv_color_hex(0xE6E8EB)
#define COL_DIM     lv_color_hex(0x8A9199)
#define COL_FAR     lv_color_hex(0x4A525C)
#define COL_GREEN   lv_color_hex(0x0DA243)
#define COL_AMBER   lv_color_hex(0xBA8107)
#define COL_RED     lv_color_hex(0xFF1900)
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

/* ---- 1. Headroom. One number, and it is a duration, not a percentage. --
 *
 * Nobody wants to be told they are at 78%. The question is whether there is
 * time to finish what they are doing, so that is the only thing said loudly.
 */
static void v_headroom(lv_obj_t *s)
{
	lbl(s, "23:47", COL_FAR, NULL, LV_ALIGN_TOP_LEFT, 12, 10);

	lv_obj_t *dot = lv_obj_create(s);

	lv_obj_set_size(dot, 10, 10);
	lv_obj_align(dot, LV_ALIGN_TOP_RIGHT, -12, 12);
	lv_obj_set_style_radius(dot, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_border_width(dot, 0, 0);
	lv_obj_set_style_bg_color(dot, COL_AMBER, 0);

	lbl(s, "30 min", COL_TEXT, &lv_font_montserrat_48, LV_ALIGN_TOP_MID, 0, 62);
	lbl(s, "OF SESSION LEFT", COL_DIM, NULL, LV_ALIGN_TOP_MID, 0, 122);

	/* A single hairline showing the session window draining. No numbers on
	 * it -- the number above already said the only one that matters. */
	lv_obj_t *tr = lv_obj_create(s);

	lv_obj_set_size(tr, 240, 4);
	lv_obj_align(tr, LV_ALIGN_TOP_MID, 0, 158);
	lv_obj_set_style_bg_color(tr, COL_TRACK, 0);
	lv_obj_set_style_border_width(tr, 0, 0);
	lv_obj_set_style_radius(tr, 2, 0);

	lv_obj_t *fi = lv_obj_create(s);

	lv_obj_set_size(fi, (int)(240 * 0.78), 4);
	lv_obj_align(fi, LV_ALIGN_TOP_MID, -(int)(240 * 0.11), 158);
	lv_obj_set_style_bg_color(fi, sev(78), 0);
	lv_obj_set_style_border_width(fi, 0, 0);
	lv_obj_set_style_radius(fi, 2, 0);

	lbl(s, "weekly 91%  ends in 1d 1h", COL_FAR, NULL, LV_ALIGN_BOTTOM_MID,
	    0, -14);
}

/* ---- 2. An instrument. Ticks and a needle, not a progress bar. --------
 *
 * A thing on a desk is furniture. Every dashboard idea so far has looked like
 * software running on a screen; this looks like a meter that happens to be
 * lit, which is a different kind of object to own.
 */
static void v_meter(lv_obj_t *s)
{
	lv_obj_t *sc = lv_scale_create(s);

	lv_obj_set_size(sc, 250, 250);
	lv_obj_align(sc, LV_ALIGN_TOP_MID, 0, 26);
	lv_scale_set_mode(sc, LV_SCALE_MODE_ROUND_INNER);
	lv_scale_set_range(sc, 0, 100);
	lv_scale_set_total_tick_count(sc, 21);
	lv_scale_set_major_tick_every(sc, 5);
	lv_scale_set_angle_range(sc, 200);
	lv_scale_set_rotation(sc, 170);
	lv_obj_set_style_bg_opa(sc, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_width(sc, 0, 0);
	lv_obj_set_style_line_color(sc, COL_FAR, LV_PART_ITEMS);
	lv_obj_set_style_line_width(sc, 2, LV_PART_ITEMS);
	lv_obj_set_style_length(sc, 6, LV_PART_ITEMS);
	lv_obj_set_style_line_color(sc, COL_DIM, LV_PART_INDICATOR);
	lv_obj_set_style_line_width(sc, 3, LV_PART_INDICATOR);
	lv_obj_set_style_length(sc, 12, LV_PART_INDICATOR);
	lv_obj_set_style_text_color(sc, COL_FAR, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(sc, COL_TRACK, LV_PART_MAIN);
	lv_obj_set_style_arc_width(sc, 2, LV_PART_MAIN);

	/* The red zone is painted on the dial, so "nearly out" is a place on
	 * the face rather than a colour the needle turns. */
	static lv_style_t red_zone;

	lv_style_init(&red_zone);
	lv_style_set_line_color(&red_zone, COL_RED);
	lv_style_set_line_width(&red_zone, 3);
	lv_style_set_length(&red_zone, 12);

	lv_scale_section_t *sect = lv_scale_add_section(sc);

	lv_scale_section_set_range(sect, 90, 100);
	lv_scale_section_set_style(sect, LV_PART_INDICATOR, &red_zone);
	lv_scale_section_set_style(sect, LV_PART_ITEMS, &red_zone);

	lv_obj_t *needle = lv_line_create(sc);

	lv_obj_set_style_line_width(needle, 4, 0);
	lv_obj_set_style_line_color(needle, COL_TEXT, 0);
	lv_obj_set_style_line_rounded(needle, true, 0);
	lv_scale_set_line_needle_value(sc, needle, 88, 78);

	lbl(s, "23:47", COL_FAR, NULL, LV_ALIGN_TOP_LEFT, 12, 10);
	lbl(s, "78%", COL_TEXT, &lv_font_montserrat_28, LV_ALIGN_TOP_MID, 0, 150);
	lbl(s, "SESSION  30m 00s", COL_DIM, NULL, LV_ALIGN_BOTTOM_MID, 0, -30);
	lbl(s, "weekly 91%   codex 34%", COL_FAR, NULL, LV_ALIGN_BOTTOM_MID, 0, -10);
}

/* ---- 3. The window as a clock. ---------------------------------------
 *
 * A five-hour window IS a span of time, and a desk already has a language for
 * that. The consumed part is a filled sector; the gap is what is left. No
 * percentage anywhere -- the shape is the quantity.
 */
static void v_clock(lv_obj_t *s)
{
	lv_obj_t *face = lv_arc_create(s);

	lv_obj_set_size(face, 168, 168);
	lv_obj_align(face, LV_ALIGN_TOP_MID, 0, 24);
	lv_arc_set_rotation(face, 270);
	lv_arc_set_bg_angles(face, 0, 360);
	lv_arc_set_range(face, 0, 100);
	lv_arc_set_value(face, 78);
	lv_obj_remove_style(face, NULL, LV_PART_KNOB);
	lv_obj_set_style_arc_width(face, 84, LV_PART_MAIN);
	lv_obj_set_style_arc_width(face, 84, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(face, COL_FAINT, LV_PART_MAIN);
	lv_obj_set_style_arc_color(face, sev(78), LV_PART_INDICATOR);

	/* A second, thinner ring inside for the weekly window: the same
	 * language at a different scale, the way a watch nests its dials. */
	lv_obj_t *inner = lv_arc_create(s);

	lv_obj_set_size(inner, 74, 74);
	lv_obj_align(inner, LV_ALIGN_TOP_MID, 0, 71);
	lv_arc_set_rotation(inner, 270);
	lv_arc_set_bg_angles(inner, 0, 360);
	lv_arc_set_range(inner, 0, 100);
	lv_arc_set_value(inner, 91);
	lv_obj_remove_style(inner, NULL, LV_PART_KNOB);
	lv_obj_set_style_arc_width(inner, 37, LV_PART_MAIN);
	lv_obj_set_style_arc_width(inner, 37, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(inner, COL_BG, LV_PART_MAIN);
	lv_obj_set_style_arc_color(inner, sev(91), LV_PART_INDICATOR);

	lbl(s, "23:47", COL_FAR, NULL, LV_ALIGN_TOP_LEFT, 12, 10);
	lbl(s, "30m", COL_TEXT, NULL, LV_ALIGN_TOP_MID, 0, 96);
	lbl(s, "SESSION  outer", COL_DIM, NULL, LV_ALIGN_BOTTOM_MID, 0, -30);
	lbl(s, "WEEKLY  inner  1d 1h", COL_FAR, NULL, LV_ALIGN_BOTTOM_MID, 0, -12);
}

/* ---- 4. Resting. What the thing looks like 95% of the time. ----------
 *
 * A desk object is not read; it is noticed. This is the state a healthy
 * machine sits in all day -- almost nothing, one soft mark carrying the only
 * fact that matters, and the detail arriving only when someone reaches for it.
 */
static void v_resting(lv_obj_t *s)
{
	lv_obj_t *ring = lv_arc_create(s);

	lv_obj_set_size(ring, 132, 132);
	lv_obj_center(ring);
	lv_arc_set_rotation(ring, 270);
	lv_arc_set_bg_angles(ring, 0, 360);
	lv_arc_set_range(ring, 0, 100);
	lv_arc_set_value(ring, 78);
	lv_obj_remove_style(ring, NULL, LV_PART_KNOB);
	lv_obj_set_style_arc_width(ring, 5, LV_PART_MAIN);
	lv_obj_set_style_arc_width(ring, 5, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(ring, COL_FAINT, LV_PART_MAIN);
	lv_obj_set_style_arc_color(ring, sev(78), LV_PART_INDICATOR);

	lbl(s, "30m", COL_TEXT, &lv_font_montserrat_28, LV_ALIGN_CENTER, 0, -4);
	lbl(s, "left", COL_FAR, NULL, LV_ALIGN_CENTER, 0, 26);
	lbl(s, "23:47", COL_FAR, NULL, LV_ALIGN_BOTTOM_MID, 0, -14);
}

typedef void (*builder)(lv_obj_t *);

int main(int argc, char **argv)
{
	static uint8_t buf[W * 40 * 2];
	const char *dir = argc > 1 ? argv[1] : ".";
	struct { const char *name; builder fn; } v[] = {
		{ "headroom", v_headroom },
		{ "meter",    v_meter },
		{ "clock",    v_clock },
		{ "resting",  v_resting },
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
		snprintf(path, sizeof(path), "%s/alt-%s.ppm", dir, v[i].name);
		write_ppm(path);
		printf("wrote %s\n", path);
	}
	return 0;
}
