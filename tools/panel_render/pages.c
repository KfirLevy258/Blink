/*
 * One provider per screen, stacked vertically.
 *
 * The merged panel had to fit four numbers -- two providers x two windows --
 * on 320x240, and that budget is what forced 14 px type, which measures 10
 * arcminutes at 60 cm and does not resolve. Splitting halves the payload, and
 * a single number per screen is what pays for the 48 px readout.
 *
 * Down is the only free axis: sideways already reaches settings and the boot
 * clip. It is also the only axis the ILI9341 CANNOT scroll in hardware here
 * (rotation 90 sets MADCTL_MV, so the chip's native scroll maps to the screen's
 * horizontal), which is why the page change is a cut and not a slide.
 *
 * The dot rail is not just a position indicator: each dot carries its own
 * provider's severity, so codex going red is visible while you are looking at
 * the claude page. That is what buys back the only thing splitting costs.
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

#define ROT   170
#define SPAN  200
#define DIA   176
#define DY    2
#define CX    (-12)		/* dial centre, shifted left of the dot rail */
#define RAIL_X 306

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

/*
 * The rail. n_pages == 1 draws nothing at all: with one provider there is
 * nowhere to go, so there is no affordance to explain and no dot to decode.
 */
static void rail(lv_obj_t *s, int n, int active, const lv_color_t *cols)
{
	int pitch = 20;
	int top = H / 2 - (n * pitch - (pitch - 6)) / 2;

	if (n < 2) {
		return;
	}
	for (int i = 0; i < n; i++) {
		lv_obj_t *d = lv_obj_create(s);
		bool on = i == active;

		/* Position is carried by SHAPE and colour by severity, so the
		 * two never compete: a red dot means codex is in trouble
		 * whether or not you are standing on that page. */
		lv_obj_set_size(d, 6, on ? 20 : 6);
		lv_obj_set_pos(d, RAIL_X, top + i * pitch + (on ? -7 : 0));
		lv_obj_set_style_radius(d, LV_RADIUS_CIRCLE, 0);
		lv_obj_set_style_bg_color(d, cols[i], 0);
		lv_obj_set_style_bg_opa(d, on ? LV_OPA_COVER : LV_OPA_60, 0);
		lv_obj_set_style_border_width(d, 0, 0);
		lv_obj_clear_flag(d, LV_OBJ_FLAG_SCROLLABLE);
	}
}

static void header(lv_obj_t *s, const char *who, const char *clock)
{
	lv_obj_t *o = lbl(s, who, COL_TEXT, &lv_font_montserrat_20,
			  LV_ALIGN_TOP_LEFT, 14, 8);

	lv_obj_set_style_text_letter_space(o, 3, 0);
	lbl(s, clock, COL_FAR, &lv_font_montserrat_20, LV_ALIGN_TOP_RIGHT, -26, 8);
}

/* One provider: dial for the session window, rim ring for the weekly one. */
static void provider_page(lv_obj_t *s, const char *who, const char *clock,
			  double pct, const char *cd, double wk, const char *wkcd)
{
	char buf[24];

	header(s, who, clock);

	lv_obj_t *face = lv_obj_create(s);

	lv_obj_set_size(face, DIA, DIA);
	lv_obj_align(face, LV_ALIGN_TOP_MID, CX, DY + 26);
	lv_obj_set_style_radius(face, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_bg_color(face, COL_FACE, 0);
	lv_obj_set_style_bg_opa(face, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(face, COL_BEZEL, 0);
	lv_obj_set_style_border_width(face, 2, 0);
	lv_obj_clear_flag(face, LV_OBJ_FLAG_SCROLLABLE);

	/* Weekly, as a band around the rim. Geometry, so it still says
	 * something at the distance where a second "66%" would not -- and it
	 * cannot be confused with the session arc, because it is a ring and
	 * the session is a needle. Held at half weight: it is the outermost
	 * and longest mark here, so at full strength it reads as the headline,
	 * and it is the secondary window. */
	lv_obj_t *ring = lv_arc_create(s);

	lv_obj_set_size(ring, DIA - 8, DIA - 8);
	lv_obj_align(ring, LV_ALIGN_TOP_MID, CX, DY + 30);
	lv_arc_set_rotation(ring, ROT);
	lv_arc_set_bg_angles(ring, 0, SPAN);
	lv_arc_set_range(ring, 0, 100);
	lv_arc_set_value(ring, (int)wk);
	lv_obj_remove_style(ring, NULL, LV_PART_KNOB);
	lv_obj_set_style_arc_width(ring, 4, LV_PART_MAIN);
	lv_obj_set_style_arc_width(ring, 4, LV_PART_INDICATOR);
	lv_obj_set_style_arc_color(ring, COL_BEZEL, LV_PART_MAIN);
	lv_obj_set_style_arc_color(ring, sev(wk), LV_PART_INDICATOR);
	lv_obj_set_style_arc_opa(ring, LV_OPA_50, LV_PART_INDICATOR);

	lv_obj_t *sc = lv_scale_create(s);

	lv_obj_set_size(sc, DIA - 30, DIA - 30);
	lv_obj_align(sc, LV_ALIGN_TOP_MID, CX, DY + 41);
	lv_scale_set_mode(sc, LV_SCALE_MODE_ROUND_INNER);
	lv_scale_set_range(sc, 0, 100);
	lv_scale_set_total_tick_count(sc, 21);
	lv_scale_set_major_tick_every(sc, 5);
	lv_scale_set_angle_range(sc, SPAN);
	lv_scale_set_rotation(sc, ROT);
	lv_scale_set_label_show(sc, false);
	lv_obj_set_style_bg_opa(sc, LV_OPA_TRANSP, 0);
	lv_obj_set_style_border_width(sc, 0, 0);
	lv_obj_set_style_arc_opa(sc, LV_OPA_TRANSP, LV_PART_MAIN);
	lv_obj_set_style_line_color(sc, COL_TICK, LV_PART_ITEMS);
	lv_obj_set_style_line_width(sc, 2, LV_PART_ITEMS);
	lv_obj_set_style_length(sc, 5, LV_PART_ITEMS);
	lv_obj_set_style_line_color(sc, COL_FAR, LV_PART_INDICATOR);
	lv_obj_set_style_line_width(sc, 3, LV_PART_INDICATOR);
	lv_obj_set_style_length(sc, 10, LV_PART_INDICATOR);

	/* The last tenth of the sweep, marked on the tick radius. Inboard of
	 * the weekly ring so the two never read as one mark. */
	lv_obj_t *band = lv_arc_create(s);

	lv_obj_set_size(band, DIA - 30, DIA - 30);
	lv_obj_align(band, LV_ALIGN_TOP_MID, CX, DY + 41);
	lv_arc_set_rotation(band, ROT + SPAN * 9 / 10);
	lv_arc_set_bg_angles(band, 0, SPAN / 10);
	lv_obj_remove_style(band, NULL, LV_PART_KNOB);
	lv_obj_remove_style(band, NULL, LV_PART_INDICATOR);
	lv_obj_set_style_arc_width(band, 4, LV_PART_MAIN);
	lv_obj_set_style_arc_color(band, COL_RED, LV_PART_MAIN);
	lv_obj_set_style_arc_opa(band, LV_OPA_90, LV_PART_MAIN);

	lv_obj_t *n = lv_line_create(sc);

	lv_obj_set_style_line_width(n, 5, 0);
	lv_obj_set_style_line_color(n, COL_TEXT, 0);
	lv_obj_set_style_line_rounded(n, true, 0);
	lv_scale_set_line_needle_value(sc, n, 58, (int)pct);

	/*
	 * The two things that resolve at 60 cm: 48 px is ~35 arcminutes and
	 * 28 px ~20, both inside the comfortable band. Everything else on this
	 * page is geometry.
	 *
	 * They sit below the hub, and the sweep is sized so they are safe
	 * there. At SPAN 240 the dead sector was 120 degrees and the readout's
	 * own corners reached exactly that far -- at 97% the needle drew
	 * straight through the digits. SPAN 200 leaves 160 degrees clear, so
	 * no value can reach the text.
	 */
	snprintf(buf, sizeof(buf), "%d%%", (int)pct);
	lbl(s, buf, sev(pct), &lv_font_montserrat_48, LV_ALIGN_TOP_MID, CX, DY + 118);
	lbl(s, cd, COL_FAR, &lv_font_montserrat_28, LV_ALIGN_TOP_MID, CX, DY + 166);

	lv_obj_t *hub = lv_obj_create(s);

	lv_obj_set_size(hub, 16, 16);
	lv_obj_align(hub, LV_ALIGN_TOP_MID, CX, DY + 26 + DIA / 2 - 8);
	lv_obj_set_style_radius(hub, LV_RADIUS_CIRCLE, 0);
	lv_obj_set_style_bg_color(hub, COL_BEZEL, 0);
	lv_obj_set_style_bg_opa(hub, LV_OPA_COVER, 0);
	lv_obj_set_style_border_color(hub, COL_FACE, 0);
	lv_obj_set_style_border_width(hub, 3, 0);

	/* Weekly in words, once, under the dial. The ring already carries it
	 * at a glance; this is the number you lean in for. */
	snprintf(buf, sizeof(buf), "WEEK  %s", wkcd);
	lbl(s, buf, COL_FAR, &lv_font_montserrat_16, LV_ALIGN_TOP_MID, CX, 214);
}

/* A page you navigate to on purpose, so it may carry denser type. */
static void sessions_page(lv_obj_t *s, const char *clock)
{
	struct { const char *n; int ctx; } r[] = {
		{ "LiveClaudeUi", 62 }, { "clauge-web", 31 }, { "notes", 8 },
	};

	header(s, "SESSIONS", clock);
	lbl(s, "3 active   5 agents", COL_TEXT, &lv_font_montserrat_28,
	    LV_ALIGN_TOP_LEFT, 14, 44);

	for (unsigned i = 0; i < 3; i++) {
		int y = 100 + (int)i * 44;

		lbl(s, r[i].n, COL_TEXT, &lv_font_montserrat_20,
		    LV_ALIGN_TOP_LEFT, 14, y);

		lv_obj_t *t = lv_obj_create(s);

		lv_obj_set_size(t, 260, 4);
		lv_obj_align(t, LV_ALIGN_TOP_LEFT, 14, y + 28);
		lv_obj_set_style_radius(t, 2, 0);
		lv_obj_set_style_bg_color(t, COL_BEZEL, 0);
		lv_obj_set_style_bg_opa(t, LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(t, 0, 0);

		lv_obj_t *f = lv_obj_create(t);

		lv_obj_set_size(f, 260 * r[i].ctx / 100, 4);
		lv_obj_align(f, LV_ALIGN_TOP_LEFT, 0, 0);
		lv_obj_set_style_radius(f, 2, 0);
		lv_obj_set_style_bg_color(f, sev(r[i].ctx), 0);
		lv_obj_set_style_bg_opa(f, LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(f, 0, 0);
	}
}

int main(int argc, char **argv)
{
	static uint8_t buf[W * 40 * 2];
	const char *dir = argc > 1 ? argv[1] : ".";
	lv_color_t g = COL_GREEN, a = COL_AMBER, r = COL_RED;
	lv_color_t two_ok[2]   = { a, g };
	lv_color_t two_bad[2]  = { g, r };
	lv_color_t three[3]    = { g, r, COL_FAR };
	struct {
		const char *n; int kind, np, act; const lv_color_t *cols;
		const char *who, *cd, *wkcd; double pct, wk;
	} sc[] = {
	  { "solo",    0, 1, 0, NULL,    "CLAUDE", "3h 40m", "6d 22h", 27, 42 },
	  { "pair",    0, 2, 0, two_ok,  "CLAUDE", "30 min", "1d 04h", 78, 66 },
	  { "alert",   0, 2, 0, two_bad, "CLAUDE", "4h 10m", "6d 02h", 27, 38 },
	  { "codex",   0, 2, 1, two_bad, "CODEX",  "6 min",  "0d 09h", 97, 94 },
	  { "sessions",1, 3, 2, three,   NULL,     NULL,     NULL,      0,  0 },
	};

	lv_init();
	for (unsigned i = 0; i < sizeof(sc) / sizeof(sc[0]); i++) {
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

		if (sc[i].kind == 0) {
			provider_page(s, sc[i].who, "14:05", sc[i].pct,
				      sc[i].cd, sc[i].wk, sc[i].wkcd);
		} else {
			sessions_page(s, "14:05");
		}
		rail(s, sc[i].np, sc[i].act, sc[i].cols);

		for (int k = 0; k < 30; k++) {
			lv_tick_inc(16);
			lv_timer_handler();
		}
		snprintf(path, sizeof(path), "%s/pg-%s.ppm", dir, sc[i].n);
		write_ppm(path);
		printf("wrote %s\n", path);
	}
	return 0;
}
