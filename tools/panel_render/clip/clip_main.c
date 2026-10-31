/*
 * The gauge screen's left half through the eye clip, a wipe and a doze, on
 * the real code: firmware/src/ui_anim.c, ui_slide.c, ui_sleep.c and
 * usage_view.c, compiled unchanged against real LVGL on this machine.
 *
 *   tools/panel_render/clip.sh [OUTDIR]
 *
 * WHY THIS EXISTS. The burn view takes the session gauge's half of the
 * screen, and the whole-branch review of feat/ultra-burn (2026-10-31) found
 * three ways for the two to end up on screen together, or for the view to
 * outlive its data, none of which the touch harness could see: it MODELS
 * the eye clip with a flat overlay and the wipes as the clock moving on, and
 * every one of these lives in what that model leaves out.
 *
 *   - The eye clip hides every child of the gauge screen for the length of
 *     the show, and its exit unhides exactly the set it hid -- a record
 *     taken when the clip STARTED. A burn line, a session reading or the
 *     view's 180 s running out mid-clip changed hands underneath it, and the
 *     restore put the session gauge back on top of the burn view.
 *   - A wipe paints the incoming screen one strip at a time with the
 *     protocol serviced between strips, so a change of hands mid-wipe left
 *     the strips already painted showing one half and the rest the other.
 *   - The 180 s expiry ran from the mode loop's one-second tick, which the
 *     doze never calls, so a peek hours into a doze showed a chart whose
 *     line had stopped long before.
 *
 * Against the firmware of 5f8446e, 15 of these checks failed: "both" after
 * every mid-clip change, the half shown under the clip, 3,843 px of the
 * wipe's left half painted from the other tree, and the stale chart in two
 * peeks. usage_view_hold_half() and burn_timer (usage_view.c) are the
 * fixes, and with them every check passes. Without the wipe's own hold
 * (ui_slide.c) the wipe scene alone fails again.
 *
 * WHAT IS REAL AND WHAT IS NOT. The four files above are the firmware's,
 * and so is everything they call on the gauge screen. The clock is
 * simulated (zephyr/kernel.h here), so a wipe takes its 650 ms and a doze
 * its minutes without this machine waiting for either. stubs.c has the
 * rest: no clip blob, so ui_anim.c holds its flat clay overlay as it does
 * on a board with no RAM for the strip; a clip player that paces and
 * services exactly as ui_boot.c's does but decodes nothing; drivers that
 * accept every write. proto_service() is this file's: it hands over the
 * lines each scene scripts, at the moment the scene asks for them, the
 * way proto.c hands over a line read off the cable.
 *
 * Every scene runs in a fresh process (clip.sh), prints PASS or FAIL per
 * check, and leaves PNGs of what the panel showed in OUTDIR.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zephyr/kernel.h>
#include <lvgl.h>

#include "burn5h.h"
#include "proto.h"
#include "theme.h"
#include "ui_anim.h"
#include "ui_sleep.h"
#include "ui_slide_geom.h"
#include "usage_layout.h"
#include "usage_view.h"

int png_write(const char *path, const uint8_t *rgb, int w, int h);

#define W SCR_W
#define H SCR_H

/* --- the clock ------------------------------------------------------------ */

static int64_t sim_ms = 100000;

int64_t k_uptime_get(void)
{
	return sim_ms;
}

int32_t k_sleep(k_timeout_t t)
{
	if (t.ms > 0) {
		sim_ms += t.ms;
		lv_tick_inc((uint32_t)t.ms);
	}
	return 0;
}

/* --- the panel ------------------------------------------------------------ */

static uint16_t fb[W * H];
static lv_display_t *disp;
static lv_obj_t *gauges;	/* the gauge screen, as usage_view_init() made it */
static const char *outdir = ".";
static const char *scene = "";
static int failures;

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

/* The framebuffer as it stands -- nothing redrawn first. */
static void png_of(const uint16_t *pix, const char *name)
{
	static uint8_t rgb[W * H * 3];
	char path[512];

	for (int i = 0; i < W * H; i++) {
		uint16_t c = pix[i];
		uint8_t r = (c >> 11) & 0x1F, g = (c >> 5) & 0x3F, b = c & 0x1F;

		rgb[i * 3 + 0] = (uint8_t)((r << 3) | (r >> 2));
		rgb[i * 3 + 1] = (uint8_t)((g << 2) | (g >> 4));
		rgb[i * 3 + 2] = (uint8_t)((b << 3) | (b >> 2));
	}
	snprintf(path, sizeof(path), "%s/clip-%s-%s.png", outdir, scene, name);
	if (png_write(path, rgb, W, H) != 0) {
		fprintf(stderr, "cannot write %s\n", path);
	}
}

/* What the panel shows now, drawn first. Thread context only: never from
 * inside a wipe, where a refresh is the wipe's to make. */
static void shot(const char *name)
{
	lv_refr_now(disp);
	png_of(fb, name);
}

static void check(bool ok, const char *what)
{
	printf("%s: %s: %s\n", ok ? "PASS" : "FAIL", scene, what);
	failures += !ok;
}

/* --- the finger ----------------------------------------------------------- */

/* A stroke: down at `at`, from one point to the other over `ms`, then up. A
 * tap is a stroke that does not move. */
static struct stroke {
	int64_t at;
	int x0, y0, x1, y1, ms;
} strokes[8];
static int n_strokes;

static void stroke(int64_t at, int x0, int y0, int x1, int y1, int ms)
{
	strokes[n_strokes++] = (struct stroke){ at, x0, y0, x1, y1, ms };
}

static void read_cb(lv_indev_t *in, lv_indev_data_t *data)
{
	ARG_UNUSED(in);
	data->state = LV_INDEV_STATE_RELEASED;
	for (int i = 0; i < n_strokes; i++) {
		const struct stroke *s = &strokes[i];
		int64_t t = sim_ms - s->at;

		if (t >= 0 && t < s->ms) {
			data->point.x = s->x0 + (int32_t)((s->x1 - s->x0) * t / s->ms);
			data->point.y = s->y0 + (int32_t)((s->y1 - s->y0) * t / s->ms);
			data->state = LV_INDEV_STATE_PRESSED;
		}
	}
}

/* --- the desk ------------------------------------------------------------- */

/*
 * render_main.c's: an account with no session window -- session_pct -1, the
 * way the daemon sends it -- and a weekly one, at 14:52 on a board told
 * UTC+2. The line's figures are tokens and every bucket is under the old
 * thousands ceiling, so the same line parses on either side of the change
 * to exact tokens; the scenes are about who holds the half, not its words.
 */
static const char burn_json[] =
	"{\"t\":\"burn\",\"v\":2,\"p\":\"claude\",\"q\":1793450700,\"b\":"
	"[90000,120000,80000,0,0,40000,110000,150000,130000,160000,140000,"
	"120000,150000,140000,170000,160000,230000,260000,290000,320000]}";

static void ultra_desk(void)
{
	usage_view_set_provider1("claude");
	usage_view_update(-1.0, -1, 42.0, 598831);
	usage_view_set_activity(USAGE_ACTIVITY_RUNNING);
	usage_view_set_sessions(1, 0);
	usage_view_set_counts(1, 1, 0, 0);
	usage_view_set_clock(14, 52);
	usage_view_set_utc_offset(120);
}

/* As proto.c hands a burn line over: parsed whole, stamped with the uptime
 * it arrived at -- `age_ms` before now, for a line that is already old. */
static void burn_line(int64_t age_ms)
{
	struct burn5h m;

	if (!burn5h_parse(burn_json, &m)) {
		fprintf(stderr, "the scene's burn line was refused\n");
		exit(2);
	}
	usage_view_set_burn5h(&m, k_uptime_get() - age_ms);
}

/* A usage line from an account whose session window is back. */
static void session_back(void)
{
	usage_view_update(37.0, 9000, 42.0, 598831);
	usage_view_set_provider1("claude");
}

/* --- the wire: proto_service() -------------------------------------------- */

enum ev_kind { EV_BURN, EV_SESSION_BACK };

/* A line the scene lands at a time, or at one strip of one wipe. */
static struct ev {
	enum ev_kind kind;
	int64_t at;		/* uptime, or 0 for a strip */
	int wipe, strip;	/* the n-th wipe of the run, its strip */
	bool done;
} evs[8];
static int n_evs;

static void at_time(int64_t at, enum ev_kind k)
{
	evs[n_evs++] = (struct ev){ .kind = k, .at = at };
}

static void at_strip(int wipe, int strip, enum ev_kind k)
{
	evs[n_evs++] = (struct ev){ .kind = k, .wipe = wipe, .strip = strip };
}

static void apply(enum ev_kind k)
{
	switch (k) {
	case EV_BURN:
		burn_line(0);
		break;
	case EV_SESSION_BACK:
		session_back();
		break;
	}
}

/*
 * Which wipe is painting, and which strip of it. ui_slide_run() freezes
 * invalidation for the whole run and calls the pump once after each strip,
 * and nothing else here calls the pump frozen, so a frozen pump is a strip
 * and the first one after an unfrozen pump starts a wipe.
 */
static int wipe_no, strip_no;
static bool was_frozen;
#define WIPE_STRIPS	(SCR_W / UI_SLIDE_STEP_COLS)

/* The frame the strips of one wipe left behind, before its settle. */
static int snap_wipe;
static uint16_t fb_strips[W * H];
static bool have_strips;

/*
 * The show: the clip's overlay up and the wipes done. Checked from the
 * pump, because that is where a line lands mid-show, and only for the left
 * half: an object shown under the overlay is repainted under it, and that
 * repaint is a flat clay patch over the streamed eyes. (A usage line also
 * shows the pips and the clock under the clip, through set_status(); that
 * is older than the burn view and not what these scenes are about.)
 */
static bool hidden_under_clip_ok = true;
static int show_checks;
static char shown_under_clip[24];

/* The doze's peeks: when each began and what its left half was. */
static bool dozing;
static bool on_gauges;
static int peeks;
static int64_t peek_from;
static char peek_saw[4][2][24];

static const char *half(void);

void proto_service(void)
{
	bool frozen = !lv_display_is_invalidation_enabled(disp);

	if (frozen && !was_frozen) {
		wipe_no++;
		strip_no = 0;
	}
	if (frozen) {
		strip_no++;
	}
	was_frozen = frozen;

	for (int i = 0; i < n_evs; i++) {
		struct ev *e = &evs[i];
		bool due = e->at ? sim_ms >= e->at
				 : frozen && wipe_no == e->wipe &&
				   strip_no == e->strip;

		if (!e->done && due) {
			e->done = true;
			apply(e->kind);
		}
	}

	if (frozen && wipe_no == snap_wipe && strip_no == WIPE_STRIPS) {
		memcpy(fb_strips, fb, sizeof(fb));
		have_strips = true;
	}

	/* The show itself, between the two wipes. */
	if (ui_anim_showing() && !frozen && wipe_no == 1) {
		const char *h = half();

		show_checks++;
		if (strcmp(h, "none") != 0) {
			if (hidden_under_clip_ok) {
				snprintf(shown_under_clip,
					 sizeof(shown_under_clip), "%s", h);
			}
			hidden_under_clip_ok = false;
		}
	}

	if (dozing) {
		bool on = lv_screen_active() == gauges;

		if (on && !on_gauges) {
			peeks++;
			peek_from = sim_ms;
		}
		on_gauges = on;
		if (on && peeks >= 1 && peeks <= 4) {
			char *a = peek_saw[peeks - 1][0];
			char *b = peek_saw[peeks - 1][1];
			char name[32];

			if (!a[0] && sim_ms >= peek_from + 1000) {
				snprintf(a, 24, "%s", half());
				snprintf(name, sizeof(name), "peek%d-start", peeks);
				shot(name);
			}
			if (!b[0] && sim_ms >= peek_from + 8000) {
				snprintf(b, 24, "%s", half());
				snprintf(name, sizeof(name), "peek%d-end", peeks);
				shot(name);
			}
		}
	}
}

/* --- what holds the left half --------------------------------------------- */

/*
 * From the gauge screen's own children, the way the eye sees them: the
 * session ring (the arc on the left), its caption, and the burn view's one
 * object (usage_layout.h's BURN_OBJ_* box). "both" is the defect.
 */
static const char *half(void)
{
	uint32_t n = lv_obj_get_child_count(gauges);
	bool ring = false, name = false, burn = false;

	lv_obj_update_layout(gauges);
	for (uint32_t i = 0; i < n; i++) {
		lv_obj_t *c = lv_obj_get_child(gauges, i);
		lv_area_t a;

		if (lv_obj_has_flag(c, LV_OBJ_FLAG_HIDDEN)) {
			continue;
		}
		lv_obj_get_coords(c, &a);
		if (lv_obj_check_type(c, &lv_arc_class) && a.x2 < SCR_MID_X) {
			ring = true;
		} else if (lv_obj_check_type(c, &lv_label_class) &&
			   a.x2 < SCR_MID_X &&
			   strcmp(lv_label_get_text(c), "SESSION 5h") == 0) {
			name = true;
		} else if (lv_obj_check_type(c, &lv_obj_class) &&
			   a.x1 == BURN_OBJ_X && a.y1 == BURN_OBJ_Y &&
			   lv_area_get_width(&a) == BURN_OBJ_W &&
			   lv_area_get_height(&a) == BURN_OBJ_H) {
			burn = true;
		}
	}
	if ((ring || name) && burn) {
		return "both";
	}
	if (burn) {
		return "burn";
	}
	return ring && name ? "gauge" : ring || name ? "half a gauge" : "none";
}

static void expect_half(const char *when, const char *want)
{
	const char *got = half();
	char what[160];

	snprintf(what, sizeof(what), "%s: the left half is %s (%s)", when,
		 want, got);
	check(strcmp(got, want) == 0, what);
}

/* --- the mode loop -------------------------------------------------------- */

/* main.c's run_usb() as far as the gauge screen can tell: the protocol, the
 * one-second tick, LVGL, a 10 ms sleep. */
static int64_t last_tick;

static void run_loop(int ms)
{
	int64_t end = sim_ms + ms;

	while (sim_ms < end) {
		proto_service();
		if (sim_ms - last_tick >= 1000) {
			usage_view_tick_1s();
			last_tick = sim_ms;
		}
		lv_timer_handler();
		k_sleep(K_MSEC(10));
	}
}

/* --- the eye clip ---------------------------------------------------------- */

/*
 * How long the eyes play, and when the middle of that is. The entry wipe
 * takes ui_slide.c's 650 ms floor and ui_anim.c then mutes gestures for
 * 250 ms more, so the swipe out lands after both.
 */
#define CLIP_SHOW_MS	3000
#define CLIP_SHOW_FROM	(650 + 250)

static int64_t clip_t0;

static int64_t clip_mid(void)
{
	return clip_t0 + CLIP_SHOW_FROM + CLIP_SHOW_MS / 2;
}

/* main.c's ui_anim_run(usb_anim_pump), with the swipe out scripted: right
 * to left across the overlay, the direction gesture_cb leaves on. */
static void eye_clip(void)
{
	stroke(clip_t0 + CLIP_SHOW_FROM + CLIP_SHOW_MS, 260, 120, 100, 120,
	       200);
	ui_anim_run(proto_service);
}

/* --- the scenes ----------------------------------------------------------- */

/* The first burn line of a reconnected Ultra desk lands while the eyes
 * play: the clip's exit used to put the session gauge back over it. */
static void scene_on(void)
{
	ultra_desk();
	run_loop(1000);
	expect_half("before the clip, no line yet", "gauge");
	clip_t0 = sim_ms;
	at_time(clip_mid(), EV_BURN);
	eye_clip();
	expect_half("as the clip hands back", "burn");
	run_loop(1000);
	expect_half("a second later", "burn");
	shot("after");
}

/* The session window comes back while the eyes play: the clip's exit used
 * to put the burn view back over the gauge. */
static void scene_off(void)
{
	ultra_desk();
	burn_line(0);
	run_loop(1000);
	expect_half("before the clip, a fresh line", "burn");
	clip_t0 = sim_ms;
	at_time(clip_mid(), EV_SESSION_BACK);
	eye_clip();
	expect_half("as the clip hands back", "gauge");
	run_loop(1000);
	expect_half("a second later", "gauge");
	shot("after");
}

/* The line's 180 s run out while the eyes play, with no other line. */
static void scene_expiry(void)
{
	ultra_desk();
	burn_line(178 * 1000);
	run_loop(500);
	expect_half("before the clip, a line 178 s old", "burn");
	clip_t0 = sim_ms;
	eye_clip();
	expect_half("as the clip hands back, the line 183 s old", "gauge");
	run_loop(1000);
	expect_half("a second later", "gauge");
	shot("after");
}

/*
 * The first burn line lands while the clip's exit wipe is painting the left
 * half -- strip 60 of 80, right to left, so x 80..159 is already painted.
 * Whatever the strips painted must be the screen the wipe settles on, and
 * the line takes the half straight after.
 */
static void scene_wipe(void)
{
	ultra_desk();
	run_loop(1000);
	expect_half("before the clip, no line yet", "gauge");
	clip_t0 = sim_ms;
	at_strip(2, 60, EV_BURN);
	snap_wipe = 2;
	eye_clip();
	if (!have_strips) {
		check(false, "the exit wipe was never seen");
		return;
	}
	{
		int left = 0, right = 0;

		for (int y = 0; y < H; y++) {
			for (int x = 0; x < W; x++) {
				if (fb[y * W + x] != fb_strips[y * W + x]) {
					*(x < SCR_MID_X ? &left : &right) += 1;
				}
			}
		}
		char what[160];

		snprintf(what, sizeof(what),
			 "the wipe's strips are the screen it settled on "
			 "(%d px differ on the left, %d on the right)", left,
			 right);
		check(left == 0 && right == 0, what);
		png_of(fb_strips, "strips");
		png_of(fb, "settled");
	}
	run_loop(1000);
	expect_half("a second later, the line that landed mid-wipe", "burn");
	shot("after");
}

/*
 * The host goes quiet and the board dozes over a burn view. A tap's peek
 * shows the dashboard as it is: the view while its line is under 180 s
 * old, "--%" after, including when 180 s passes during the peek itself.
 */
static int64_t wake_at;

static bool host_back(void)
{
	return sim_ms >= wake_at;
}

static void scene_doze(void)
{
	int64_t t0;

	ultra_desk();
	burn_line(0);
	t0 = sim_ms;
	run_loop(1000);
	expect_half("before the doze, a fresh line", "burn");
	/* What proto.c does when the host goes quiet. */
	usage_view_set_status(USAGE_STATUS_DISCONNECTED);
	run_loop(1000);
	stroke(t0 + 60 * 1000, 160, 120, 160, 120, 80);
	stroke(t0 + 175 * 1000, 160, 120, 160, 120, 80);
	stroke(t0 + 300 * 1000, 160, 120, 160, 120, 80);
	wake_at = t0 + 400 * 1000;
	dozing = true;
	ui_sleep_run(host_back, "Your computer may be asleep.");
	dozing = false;

	static const struct {
		const char *when, *start, *end;
	} want[] = {
		{ "the peek at 60 s", "burn", "burn" },
		{ "the peek at 175 s, across 180", "burn", "gauge" },
		{ "the peek at 300 s", "gauge", "gauge" },
	};
	char what[160];

	snprintf(what, sizeof(what), "three peeks (%d)", peeks);
	check(peeks == 3, what);
	for (int i = 0; i < 3; i++) {
		snprintf(what, sizeof(what), "%s, a second in: %s (%s)",
			 want[i].when, want[i].start, peek_saw[i][0]);
		check(strcmp(peek_saw[i][0], want[i].start) == 0, what);
		snprintf(what, sizeof(what), "%s, eight seconds in: %s (%s)",
			 want[i].when, want[i].end, peek_saw[i][1]);
		check(strcmp(peek_saw[i][1], want[i].end) == 0, what);
	}
	run_loop(1000);
	expect_half("awake again, the line long gone", "gauge");
}

int main(int argc, char **argv)
{
	/* LVGL asserts its draw buffer is aligned, and hangs in the assert. */
	static uint8_t buf[W * 40 * 2] __attribute__((aligned(64)));

	scene = argc > 1 ? argv[1] : "on";
	outdir = argc > 2 ? argv[2] : ".";

	lv_init();
	disp = lv_display_create(W, H);
	lv_display_set_flush_cb(disp, flush_cb);
	lv_display_set_buffers(disp, buf, NULL, sizeof(buf),
			       LV_DISPLAY_RENDER_MODE_PARTIAL);
	lv_indev_t *in = lv_indev_create();

	lv_indev_set_type(in, LV_INDEV_TYPE_POINTER);
	lv_indev_set_read_cb(in, read_cb);
	/* The board reads its pointer every CONFIG_LV_DEF_REFR_PERIOD, 16 ms. */
	lv_timer_set_period(lv_indev_get_read_timer(in), 16);

	theme_select(THEME_DARK);
	usage_view_set_focus(false, false);
	usage_view_init();
	gauges = lv_screen_active();
	last_tick = sim_ms;
	run_loop(100);
	usage_view_set_status(USAGE_STATUS_OK);

	if (strcmp(scene, "on") == 0) {
		scene_on();
	} else if (strcmp(scene, "off") == 0) {
		scene_off();
	} else if (strcmp(scene, "expiry") == 0) {
		scene_expiry();
	} else if (strcmp(scene, "wipe") == 0) {
		scene_wipe();
	} else if (strcmp(scene, "doze") == 0) {
		scene_doze();
	} else {
		fprintf(stderr, "no scene %s\n", scene);
		return 2;
	}
	if (strcmp(scene, "doze") != 0) {
		char what[160];

		snprintf(what, sizeof(what),
			 "the left half stayed hidden under the clip "
			 "(%d looks%s%s)", show_checks,
			 shown_under_clip[0] ? "; shown: " : "",
			 shown_under_clip);
		check(show_checks > 0 && hidden_under_clip_ok, what);
	}
	return failures ? 1 : 0;
}
