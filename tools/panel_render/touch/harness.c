/*
 * The touch targets, pressed on the real Settings code with no board.
 *
 *   tools/panel_render/touch.sh OUTDIR [SCRIPT]
 *
 * Compiles firmware/src/ui_settings.c, usage_view.c and ui_theme.c UNCHANGED
 * against real LVGL, with a pointer input device whose finger is driven by
 * the same wire lines a CONFIG_BLINK_TOUCH_INJECT board takes. stdin and
 * stdout are that wire: tools/board_touch.py --harness runs its scripts
 * against this exactly as it does against a board, and every expectation it
 * checks there it checks here. So a coordinate that misses its button, or a
 * swipe that starts on a control that swallows it, fails on this machine
 * before it is ever flashed.
 *
 * Before each stroke the screen is written to OUTDIR as a PNG with the stroke
 * marked on it -- a ring where a tap lands, a line and a ring for a swipe --
 * and after it the screen it led to, so each target can be checked by eye.
 *
 * What is modelled rather than compiled, and how:
 *
 *   - ui_swipe. Its rules are the real ones (ui_swipe_geom.h); the stroke is
 *     fed to them from the injected samples, the way the input callback
 *     would see them, and decided strokes are handed over on the LVGL loop.
 *   - the eye clip. ui_anim.c streams frames to the panel, which there is
 *     none of here; a flat overlay stands in, leaving on the same gesture
 *     direction ui_anim.c's gesture_cb leaves on, behind the same mutes.
 *   - the slides. ui_slide_run() blocks the board for a transition with no
 *     input dispatched; here it moves the clock on by SLIDE_MS without
 *     running LVGL, which is the part of that the targets can notice.
 *   - storage, the backlight, OTA and the rest of proto: stubs.c.
 *
 * Host pointers are twice the board's, so the [mem] lines printed here (one
 * per `mem` step, answered to the bench's harness-only bench_mem line) are
 * named host-*, and the board's own are the figures to trust. What they are
 * good for is pool_sample's question: whether the widget walk takes anything.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/select.h>
#include <unistd.h>
#include <zephyr/kernel.h>
#include <lvgl.h>

#include "touch_inject_geom.h"
#include "ui_swipe.h"
#include "ui_swipe_geom.h"
#include "ui_settings.h"
#include "ui_anim.h"
#include "ui_slide.h"
#include "ui_theme.h"
#include "usage_view.h"
#include "msg_parse.h"
#include "theme.h"
#include "cfg_store.h"
#include "backlight.h"
#include "touch_widgets.h"
#include "touch_widgets_fmt.h"
#include "whatsnew.h"
#include "version.h"
#include "harness.h"

#define W TI_SCREEN_W
#define H TI_SCREEN_H
#define LOOP_MS 5
#define SLIDE_MS 400

/* --- the clock ---------------------------------------------------------- */

static int64_t sim_ms;

int64_t k_uptime_get(void)
{
	return sim_ms;
}

static void tick(int ms)
{
	sim_ms += ms;
	lv_tick_inc(ms);
}

/* --- the panel ---------------------------------------------------------- */

static uint16_t fb[W * H];
static lv_display_t *disp;

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

/* The finger, as the pointer device reads it. */
static int fx, fy;
static bool fdown;

static void read_cb(lv_indev_t *in, lv_indev_data_t *data)
{
	ARG_UNUSED(in);
	data->point.x = fx;
	data->point.y = fy;
	data->state = fdown ? LV_INDEV_STATE_PRESSED : LV_INDEV_STATE_RELEASED;
}

/* --- ui_swipe, fed from the injected reports ----------------------------- */

static void (*swipe_emit)(enum ui_swipe_dir);
static void (*swipe_show)(enum ui_swipe_dir, int);
static bool sw_active, sw_fired;
static int sw_x0, sw_y0, sw_x1, sw_y1;
static int64_t sw_end_at;
static enum ui_swipe_dir sw_pending;

void ui_swipe_init(void (*on_swipe)(enum ui_swipe_dir),
		   void (*on_progress)(enum ui_swipe_dir, int))
{
	swipe_emit = on_swipe;
	swipe_show = on_progress;
}

bool ui_swipe_dragging(void)
{
	return sw_active && ui_swipe_is_drag(sw_x1 - sw_x0, sw_y1 - sw_y0);
}

/* One panel report with the finger down: ui_swipe.c's report(). */
static void swipe_report(int x, int y)
{
	if (!sw_active) {
		sw_active = true;
		sw_fired = false;
		sw_x0 = x;
		sw_y0 = y;
	}
	sw_x1 = x;
	sw_y1 = y;
	if (!sw_fired) {
		enum ui_swipe_dir d = ui_swipe_classify(x - sw_x0, y - sw_y0);

		if (d != UI_SWIPE_NONE) {
			sw_fired = true;
			sw_pending = d;
		}
	}
	sw_end_at = sim_ms + (sw_fired ? UI_SWIPE_REARM_MS : UI_SWIPE_STITCH_MS);
}

/* ui_swipe.c's stroke_end and drain_cb, on the loop. */
static void swipe_service(void)
{
	if (sw_active && sim_ms >= sw_end_at) {
		sw_active = false;
		sw_fired = false;
	}
	if (swipe_show) {
		swipe_show(UI_SWIPE_NONE, 0);
	}
	if (sw_pending != UI_SWIPE_NONE) {
		enum ui_swipe_dir d = sw_pending;

		sw_pending = UI_SWIPE_NONE;
		if (swipe_emit) {
			swipe_emit(d);
		}
	}
}

/* --- the eye clip, modelled ---------------------------------------------- */

static bool anim_pending, anim_showing, anim_leave;
static int64_t mute_until, enter_mute_until;
static lv_obj_t *anim_ov;

void ui_anim_request(void)
{
	anim_pending = true;
}

bool ui_anim_pending(void)
{
	return anim_pending;
}

bool ui_anim_gesture_muted(void)
{
	return sim_ms < mute_until;
}

void ui_anim_gesture_mute(int ms)
{
	if (sim_ms + ms > mute_until) {
		mute_until = sim_ms + ms;
	}
}

bool ui_anim_showing(void)
{
	return anim_showing;
}

static void anim_gesture_cb(lv_event_t *e)
{
	ARG_UNUSED(e);
	if (sim_ms < enter_mute_until) {
		return;
	}
	/* ui_anim.c gesture_cb: LV_DIR_LEFT leaves, nothing else does. */
	if (lv_indev_get_gesture_dir(lv_indev_active()) == LV_DIR_LEFT) {
		anim_leave = true;
	}
}

static void anim_service(void)
{
	if (anim_pending && !anim_showing) {
		anim_pending = false;
		anim_leave = false;
		anim_showing = true;
		anim_ov = lv_obj_create(lv_screen_active());
		lv_obj_set_size(anim_ov, LV_PCT(100), LV_PCT(100));
		lv_obj_set_style_bg_color(anim_ov, lv_color_hex(0xC9A27E), 0);
		lv_obj_set_style_bg_opa(anim_ov, LV_OPA_COVER, 0);
		lv_obj_set_style_border_width(anim_ov, 0, 0);
		lv_obj_set_style_radius(anim_ov, 0, 0);
		lv_obj_clear_flag(anim_ov, LV_OBJ_FLAG_SCROLLABLE);
		lv_obj_clear_flag(anim_ov, LV_OBJ_FLAG_GESTURE_BUBBLE);
		lv_obj_add_event_cb(anim_ov, anim_gesture_cb, LV_EVENT_GESTURE,
				    NULL);
		lv_obj_t *l = lv_label_create(anim_ov);

		lv_label_set_text(l, "eye clip\n(not drawn on host)");
		lv_obj_center(l);
		mute_until = sim_ms + UI_SLIDE_MS * 6;
		tick(SLIDE_MS);		/* the entry slide, input held */
		enter_mute_until = sim_ms + 250;
	} else if (anim_showing && anim_leave) {
		lv_obj_del(anim_ov);
		anim_ov = NULL;
		mute_until = sim_ms + UI_SLIDE_MS * 6;
		tick(SLIDE_MS);		/* the exit slide */
		mute_until = sim_ms + 250;
		anim_pending = false;
		anim_showing = false;
	}
}

/* --- ui_slide: a blocked transition is the clock moving on --------------- */

void ui_slide_begin(void)
{
}

void ui_slide_freeze(bool frozen)
{
	ARG_UNUSED(frozen);
}

void ui_slide_top_hide(bool hide)
{
	ARG_UNUSED(hide);
}

void ui_slide_run(int dir, void (*pump)(void))
{
	ARG_UNUSED(dir);
	ARG_UNUSED(pump);
	tick(SLIDE_MS);
}

/* --- the loop ------------------------------------------------------------ */

/* --- the pool, as LVGL's own monitor reads it ---------------------------
 *
 * The board's [mem] lines are the figures to trust (the header above says
 * why). These are the host's, from the same widgets on a pool whose
 * pointers are twice as wide, and they answer one question the board cannot
 * answer as cleanly: does the widget walk itself take anything from the
 * pool? Each walk is measured with the layout already brought up to date,
 * so what it leaves is the walk's alone, and the layout it runs first is
 * measured beside it. */
static size_t peak_since;	/* the most in use since the last bench_mem */
static size_t walk_grew, walk_rise, layout_grew;
static int walks;

static size_t pool_used(lv_mem_monitor_t *m)
{
	lv_mem_monitor(m);
	return m->total_size - m->free_size;
}

static void pool_sample(void)
{
	lv_mem_monitor_t m;
	size_t u = pool_used(&m);

	if (u > peak_since) {
		peak_since = u;
	}
}

static void walk_begin(lv_mem_monitor_t *before)
{
	lv_mem_monitor_t m0;
	size_t u0 = pool_used(&m0), u1;

	lv_obj_update_layout(lv_layer_sys());
	lv_obj_update_layout(lv_layer_top());
	lv_obj_update_layout(lv_screen_active());
	lv_obj_update_layout(lv_layer_bottom());
	u1 = pool_used(before);
	if (u1 > u0 && u1 - u0 > layout_grew) {
		layout_grew = u1 - u0;
	}
}

static void walk_end(const lv_mem_monitor_t *before)
{
	lv_mem_monitor_t a;
	size_t ub = before->total_size - before->free_size;
	size_t ua = pool_used(&a);

	walks++;
	if (ua > ub && ua - ub > walk_grew) {
		walk_grew = ua - ub;
	}
	if (a.max_used > before->max_used &&
	    a.max_used - before->max_used > walk_rise) {
		walk_rise = a.max_used - before->max_used;
	}
}

static void loop_once(void)
{
	tick(LOOP_MS);
	lv_timer_handler();
	pool_sample();
	swipe_service();
	anim_service();
	/* ui_anim_run() holds the mode loop for the whole clip: Settings is
	 * not serviced until it returns, though LVGL (and so ui_swipe's
	 * drain, and anything it latches) keeps running inside it. */
	if (!anim_showing) {
		ui_settings_service(NULL);
	}
}

static void run_for(int ms)
{
	for (int t = 0; t < ms; t += LOOP_MS) {
		loop_once();
	}
}

/* --- pictures ------------------------------------------------------------ */

static const char *outdir = ".";
static char note[48] = "stroke";
static int shot;

static void put(uint8_t *rgb, int x, int y, uint32_t c)
{
	if (x < 0 || y < 0 || x >= W || y >= H) {
		return;
	}
	rgb[(y * W + x) * 3 + 0] = (uint8_t)(c >> 16);
	rgb[(y * W + x) * 3 + 1] = (uint8_t)(c >> 8);
	rgb[(y * W + x) * 3 + 2] = (uint8_t)c;
}

/* A ring a fingertip wide (9 mm is about 50 px on this panel; 10 px radius
 * is the aim, not the contact) and a dot at its centre. */
static void ring(uint8_t *rgb, int cx, int cy, uint32_t c)
{
	for (int dy = -12; dy <= 12; dy++) {
		for (int dx = -12; dx <= 12; dx++) {
			int r2 = dx * dx + dy * dy;

			if ((r2 >= 81 && r2 <= 144) || r2 <= 4) {
				put(rgb, cx + dx, cy + dy, c);
			}
		}
	}
}

static void line(uint8_t *rgb, int x0, int y0, int x1, int y1, uint32_t c)
{
	int n = abs(x1 - x0) > abs(y1 - y0) ? abs(x1 - x0) : abs(y1 - y0);

	for (int i = 0; i <= n; i++) {
		int x = x0 + (n ? (x1 - x0) * i / n : 0);
		int y = y0 + (n ? (y1 - y0) * i / n : 0);

		put(rgb, x, y, c);
		put(rgb, x, y + 1, c);
	}
}

static void snapshot(const char *suffix, const struct ti_stroke *s)
{
	static uint8_t rgb[W * H * 3];
	char path[512];

	lv_refr_now(disp);
	for (int i = 0; i < W * H; i++) {
		uint16_t c = fb[i];
		uint8_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;

		rgb[i * 3 + 0] = (uint8_t)((r << 3) | (r >> 2));
		rgb[i * 3 + 1] = (uint8_t)((g << 2) | (g >> 4));
		rgb[i * 3 + 2] = (uint8_t)((b << 3) | (b >> 2));
	}
	if (s != NULL) {
		const uint32_t mark = 0xFF2D95;	/* on every theme's ground */

		if (s->op == TI_OP_SWIPE) {
			line(rgb, s->x0, s->y0, s->x1, s->y1, mark);
			ring(rgb, s->x0, s->y0, mark);
			ring(rgb, s->x1, s->y1, 0x2DE1FF);
		} else if (s->op != TI_OP_RELEASE) {
			ring(rgb, s->x0, s->y0, mark);
		}
	}
	snprintf(path, sizeof(path), "%s/%02d-%s%s.png", outdir, shot, note,
		 suffix);
	if (png_write(path, rgb, W, H) != 0) {
		fprintf(stderr, "cannot write %s\n", path);
	}
}

/* --- the wire ------------------------------------------------------------ */

static void reply(const char *json)
{
	fputs(json, stdout);
	fputc('\n', stdout);
	fflush(stdout);
}

void harness_emit(const char *json)
{
	reply(json);
}

static int field(const char *json, const char *key)
{
	double v;

	if (!msg_get_double(json, key, &v) || v != v || v < -1 || v > 100000) {
		return -1;
	}
	return (int)v;
}

static void play(const struct ti_stroke *s)
{
	int n = ti_samples(s);
	int64_t t0 = sim_ms;
	struct ti_sample p;

	if (s->op == TI_OP_PRESS) {
		n = 1;		/* held until the release line */
	}
	for (int i = 0; i < n; i++) {
		ti_sample(s, i, &p);
		while (sim_ms < t0 + p.t_ms) {
			loop_once();
		}
		fx = p.x;
		fy = p.y;
		fdown = true;
		swipe_report(p.x, p.y);
	}
	if (s->op == TI_OP_PRESS) {
		return;
	}
	while (sim_ms < t0 + ti_release_ms(s)) {
		loop_once();
	}
	fdown = false;
}

static bool held;

static void touch_line(const char *json)
{
	char op[12], buf[96];
	struct ti_stroke s = { 0 };

	if (!msg_get_str(json, "op", op, sizeof(op))) {
		reply("{\"t\":\"touch_refused\",\"v\":2,\"why\":\"args\"}");
		return;
	}
	if (strcmp(op, "click") == 0) {
		/* touch_inject.c's click_line, the same function: it parses the
		 * line and finds the aim, and from there it is a tap like any
		 * other. Only the busy test is this file's, from the same
		 * getters the board's uses. */
		struct ui_settings_bench bs;
		lv_mem_monitor_t mb;
		const char *why;

		ui_settings_bench_state(&bs);
		walk_begin(&mb);
		why = tw_click_parse(json, bs.busy || anim_pending, &s);
		walk_end(&mb);
		if (why) {
			snprintf(buf, sizeof(buf),
				 "{\"t\":\"touch_refused\",\"v\":%d,"
				 "\"why\":\"%s\"}", BLINK_PROTO_VERSION, why);
			reply(buf);
			return;
		}
	} else {
		s.op = ti_op_from_name(op);
	}
	/* Not strokes, as on the board: answered at once, nothing drawn.
	 * Nothing dozes here, so a wake has nothing to end. */
	if (s.op == TI_OP_DISMISS || s.op == TI_OP_WAKE) {
		if (s.op == TI_OP_DISMISS) {
			ui_settings_notice_dismiss();
		}
		snprintf(buf, sizeof(buf),
			 "{\"t\":\"touch_done\",\"v\":2,\"op\":\"%s\",\"ok\":1}",
			 op);
		reply(buf);
		return;
	}
	if (s.op == TI_OP_SWIPE) {
		s.x0 = field(json, "x0");
		s.y0 = field(json, "y0");
		s.x1 = field(json, "x1");
		s.y1 = field(json, "y1");
		s.ms = field(json, "ms");
	} else if (s.op != TI_OP_RELEASE && strcmp(op, "click") != 0) {
		s.x0 = field(json, "x");
		s.y0 = field(json, "y");
		s.ms = s.op == TI_OP_TAP ? field(json, "ms") : 0;
	}
	if (!ti_valid(&s) || (s.op == TI_OP_RELEASE && !held)) {
		reply(s.op == TI_OP_RELEASE
			      ? "{\"t\":\"touch_refused\",\"v\":2,\"why\":\"not_held\"}"
			      : "{\"t\":\"touch_refused\",\"v\":2,\"why\":\"args\"}");
		return;
	}
	shot++;
	snapshot("", &s);
	if (s.op == TI_OP_RELEASE) {
		run_for(TI_RELEASE_LAG_MS);
		fdown = false;
		held = false;
	} else {
		play(&s);
		held = s.op == TI_OP_PRESS;
	}
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"touch_done\",\"v\":2,\"op\":\"%s\",\"ok\":1}", op);
	reply(buf);
	/* The screen it led to, once the slide it started has had time. */
	run_for(600);
	snapshot("-after", NULL);
	snprintf(note, sizeof(note), "stroke");
}

static void ui_state_line(void)
{
	struct ui_settings_bench b;
	char buf[256];
	const char *scr;

	ui_settings_bench_state(&b);
	scr = anim_showing ? "eyes" : b.open ? "settings" :
	      usage_view_takeover_active() ? "connecting" : "gauges";
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"ui_state\",\"v\":2,\"scr\":\"%s\",\"ov\":\"%s\","
		 "\"modal\":\"%s\",\"busy\":%d,\"theme\":%d,\"stored\":%d,"
		 "\"tp\":%d,\"focus\":\"%s\",\"bl\":%d,\"edit\":\"%s\","
		 "\"slp\":0,\"nt\":\"%s\",\"gf\":\"%s\"}",
		 scr, b.open ? b.sub : "none", b.modal,
		 (b.busy || anim_pending ||
		  lv_display_get_screen_prev(NULL) != NULL) ? 1 : 0,
		 theme_active_id(),
		 ui_theme_stored_id(), b.theme_page,
		 !cfg_get_focus_on() ? "off" :
		 cfg_get_focus_win() ? "weekly" : "session",
		 (int)backlight_get(), b.edit, b.nt, usage_view_bench_focus());
	reply(buf);
}

static void dispatch(const char *json)
{
	char t[16];

	if (!msg_get_str(json, "t", t, sizeof(t))) {
		return;
	}
	if (strcmp(t, "touch") == 0) {
		touch_line(json);
	} else if (strcmp(t, "ui_state") == 0) {
		ui_state_line();
	} else if (strcmp(t, "widgets") == 0) {
		static char wbuf[TW_LINE_MAX];
		lv_mem_monitor_t mb;
		int n;

		/* touch_inject.c's widgets_line, the same function. */
		walk_begin(&mb);
		n = tw_widgets_reply(json, wbuf, sizeof(wbuf),
				     BLINK_PROTO_VERSION);
		walk_end(&mb);
		if (n > 0) {
			reply(wbuf);
		} else {
			reply("{\"t\":\"touch_refused\",\"v\":2,\"why\":\"args\"}");
		}
	} else if (strcmp(t, "bench_mem") == 0) {
		/* A [mem] line in mem_stats.h's own shape, named for the screen
		 * up now, then what the walks have taken (see pool_sample). */
		struct ui_settings_bench b;
		lv_mem_monitor_t m;
		size_t used = pool_used(&m);
		char where[24], buf[200];

		ui_settings_bench_state(&b);
		snprintf(where, sizeof(where), "host-%s",
			 anim_showing ? "eyes" :
			 strcmp(b.modal, "none") ? b.modal : !b.open ? "gauges" :
			 strcmp(b.sub, "none") ? b.sub : "settings");
		if (peak_since < used) {
			peak_since = used;
		}
		snprintf(buf, sizeof(buf),
			 "[mem] lvgl %s: used=%zu free=%zu largest=%zu peak=%zu "
			 "boot_peak=%zu pool=%zu", where, used, m.free_size,
			 m.free_biggest_size, peak_since, m.max_used,
			 m.total_size);
		reply(buf);
		peak_since = used;
		snprintf(buf, sizeof(buf),
			 "{\"t\":\"bench_walk\",\"v\":2,\"walks\":%d,\"grew\":%zu,"
			 "\"rise\":%zu,\"layout\":%zu}", walks, walk_grew,
			 walk_rise, layout_grew);
		reply(buf);
	} else if (strcmp(t, "ota_avail") == 0) {
		/* proto.c's checks, then its state change (stubs.c). */
		char ver[16], sha[65], app[16] = "";
		double sz = 0;

		if (msg_get_str(json, "version", ver, sizeof(ver)) &&
		    msg_get_str(json, "sha256", sha, sizeof(sha)) &&
		    msg_get_double(json, "size", &sz) && sz > 0) {
			msg_get_str(json, "app", app, sizeof(app));
			harness_ota_avail(ver, (uint32_t)sz, app);
		} else {
			harness_ota_error("");
		}
	} else if (strcmp(t, "ota_none") == 0) {
		harness_ota_none();
	} else if (strcmp(t, "ota_error") == 0) {
		char why[48] = "";

		msg_get_str(json, "why", why, sizeof(why));
		harness_ota_error(why);
	} else if (strcmp(t, "bench_notice") == 0) {
		/*
		 * Harness only: the notice main.c puts up on the first boot
		 * after an update, which no wire line can raise. "update" is
		 * the landed one, with What's new behind it when the table has
		 * an entry between `from` and this version; "reverted" is the
		 * other.
		 */
		char kind[12] = "", from[16] = "1.3.3", summary[64];

		msg_get_str(json, "kind", kind, sizeof(kind));
		msg_get_str(json, "from", from, sizeof(from));
		if (strcmp(kind, "reverted") == 0) {
			ui_settings_notice("Update failed, previous version "
					   "restored.");
		} else {
			whatsnew_summary(from, BLINK_FW_VERSION, summary,
					 sizeof(summary));
			ui_settings_notice_update(BLINK_FW_VERSION, summary, from,
						  BLINK_FW_VERSION);
		}
	} else if (strcmp(t, "note") == 0) {
		/* The target about to be pressed, for the next file name. */
		char n[40];

		if (msg_get_str(json, "name", n, sizeof(n))) {
			snprintf(note, sizeof(note), "%s", n);
		}
	} else if (strcmp(t, "welcome") == 0) {
		reply("{\"t\":\"pref\",\"v\":2,\"provider\":\"claude\","
		      "\"caps\":[\"svc\",\"theme\",\"edit_done\"]}");
		harness_theme_now("boot");
	} else if (strcmp(t, "theme") == 0) {
		/* As proto.c does it: the real parser and ui_theme.c's wire
		 * path, which the bench's restore sends a save through. */
		struct theme_msg m;

		if (theme_msg_parse(json, &m)) {
			ui_theme_wire(&m);
		}
	} else if (strcmp(t, "theme_edit") == 0) {
		/* As proto.c does it, done (Addendum 6) first. */
		double ok = 0, done = 0;

		msg_get_double(json, "done", &done);
		if (done == 1) {
			ui_settings_theme_edit_done();
			return;
		}
		msg_get_double(json, "ok", &ok);
		ui_settings_theme_edit_reply(ok == 1);
	}
	/* time, usage, svc, pong, ota_none: the desk below is fixed. */
}

int main(int argc, char **argv)
{
	/* LVGL asserts its draw buffer is aligned, and hangs in the assert. */
	static uint8_t buf[W * 40 * 2] __attribute__((aligned(64)));
	static char lbuf[1024];
	size_t llen = 0;

	if (argc > 1) {
		outdir = argv[1];
	}
	lv_init();
	disp = lv_display_create(W, H);
	lv_display_set_flush_cb(disp, flush_cb);
	lv_display_set_buffers(disp, buf, NULL, sizeof(buf),
			       LV_DISPLAY_RENDER_MODE_PARTIAL);
	lv_indev_t *in = lv_indev_create();

	lv_indev_set_type(in, LV_INDEV_TYPE_POINTER);
	lv_indev_set_read_cb(in, read_cb);
	/* The board reads its pointer every CONFIG_LV_DEF_REFR_PERIOD, 16 ms;
	 * the template's 33 would make LVGL see half the reports it does. */
	lv_timer_set_period(lv_indev_get_read_timer(in), 16);

	theme_select(THEME_DARK);
	usage_view_set_focus(false, false);
	usage_view_init();
	run_for(100);
	/* The desk the bench's made-up usage describes: two providers. */
	usage_view_set_status(USAGE_STATUS_OK);
	usage_view_set_provider1("claude");
	usage_view_update(42.0, 7200, 17.0, 345600);
	usage_view_set_provider2("codex", 63.0, 28.0, 10800, 432000, false);
	usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
	usage_view_set_sessions(2, 0);
	usage_view_set_counts(2, 1, 0, 0);
	usage_view_set_clock(14, 5);
	ui_settings_attach(lv_screen_active());
	run_for(200);

	/* The wire: one line at a time, the loop running between them. */
	for (;;) {
		fd_set fds;
		struct timeval tv = { 0, LOOP_MS * 1000 };

		FD_ZERO(&fds);
		FD_SET(0, &fds);
		if (select(1, &fds, NULL, NULL, &tv) > 0) {
			char c;
			ssize_t r = read(0, &c, 1);

			if (r <= 0) {
				return 0;	/* the bench has gone */
			}
			if (c == '\n') {
				lbuf[llen] = '\0';
				dispatch(lbuf);
				llen = 0;
			} else if (llen < sizeof(lbuf) - 1) {
				lbuf[llen++] = c;
			}
			continue;
		}
		loop_once();
	}
}
