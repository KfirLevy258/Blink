/*
 * The bench touch injector. See touch_inject.h for what it is for and the
 * wire it speaks, and touch_inject_geom.h for the arithmetic.
 *
 * Built only with CONFIG_BLINK_TOUCH_INJECT (CMakeLists.txt), which a release
 * never has: tools/lib_selftest.sh refuses an image carrying the boot line
 * printed below.
 *
 * WHERE THE FINGER GOES IN. Into the XPT2046's own input device, through the
 * same input_report_abs()/input_report_key() calls its driver makes, in the
 * same order -- ABS_X, ABS_Y with sync off, then BTN_TOUCH with sync on, and a
 * lift as BTN_TOUCH 0 alone. Every listener keyed on that device then gets it
 * as a panel report: lvgl_pointer_input, which turns it into an LVGL press,
 * and ui_swipe's INPUT_CALLBACK_DEFINE, which reads the channels directly
 * because LVGL's replay of them was too stale to classify a stroke. A bench
 * that fed LVGL instead -- lv_indev_set_read_cb, a fake indev -- would skip
 * the second of those, and ui_swipe is where most of this panel's touch bugs
 * have lived.
 *
 * WHEN. One report per TI_STEP_MS while the finger is down, and the lift a
 * driver re-check later, played from a delayable work item on the system
 * workqueue against absolute deadlines so a late run does not push every
 * later report back with it. Not from the protocol handler: a 260 ms swipe
 * there would stall the RX drain, the ping and LVGL for its whole length,
 * which is a board no finger ever produces. And not a thread of its own: a
 * stack for a feature that only exists on the bench is RAM a bench image
 * should not differ from a release one by.
 *
 * ONE AT A TIME. A second stroke while one is playing is refused with
 * touch_refused, never queued: two strokes overlapping on one panel is not
 * something a finger can do, and the PC is waiting for touch_done anyway.
 *
 * NOBODY AT THE PANEL. A real finger is not locked out, and the two do not
 * corrupt each other -- the driver reports from the same system workqueue,
 * so each X, Y, BTN_TOUCH triple goes in whole -- but they do blend between
 * triples. A touch during a stroke puts its positions among the injected
 * ones, the driver's release can land mid-stroke, and an injected lift can
 * end a real press. The result is a wrong test result or a phantom gesture
 * on a bench board, nothing worse. So: do not touch the panel during a run
 * (tools/board_touch.py says so too).
 */
#include <zephyr/kernel.h>
#include <zephyr/device.h>
#include <zephyr/input/input.h>
#include <lvgl.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/printk.h>
#if defined(CONFIG_LOG_MODE_DEFERRED) && defined(CONFIG_LOG_PROCESS_THREAD)
#include <zephyr/logging/log_ctrl.h>
#define TI_LOG_KICK() log_thread_trigger()
#else
#define TI_LOG_KICK() ((void)0)
#endif
#include <stdio.h>
#include <string.h>

#include "touch_inject.h"
#include "touch_inject_geom.h"
#include "touch_widgets.h"
#include "touch_widgets_fmt.h"
#include "msg_parse.h"
#include "version.h"
#include "backlight.h"
#include "cfg_store.h"
#include "theme.h"
#include "ui_theme.h"
#include "ui_anim.h"
#include "ui_settings.h"
#include "usage_view.h"
#include "ui_sleep.h"

#if !DT_NODE_EXISTS(DT_NODELABEL(xpt2046))
#error "touch_inject reports through the xpt2046 node, as the panel does"
#endif

static const struct device *const panel_dev =
	DEVICE_DT_GET(DT_NODELABEL(xpt2046));

enum {
	IDLE = 0,
	PLAYING,	/* a tap or a swipe, start to lift */
	HELD,		/* a press, until its release */
};

static atomic_t state = ATOMIC_INIT(IDLE);
static atomic_t release_asked;
/* What has finished and not been said yet: the op, plus AUTO_LIFT when a
 * press let go by itself at TI_HOLD_MAX_MS. 0 is nothing. Two strokes cannot
 * finish between two service passes, since the second one cannot start until
 * the first is IDLE -- but a press says "press" when it lands and "release"
 * when it lifts, so those two are kept apart. */
#define AUTO_LIFT 0x10
/* ...and AS_CLICK when the tap was a click by index, so its answer names the
 * op the PC sent. */
#define AS_CLICK 0x20
static atomic_t done_press;
static atomic_t done;
/* dismiss and wake: their own slot, since either may land while a stroke is
 * playing and must not overwrite that stroke's answer. */
static atomic_t done_now;

/* The stroke in play. Written by dispatch only while IDLE, read only by the
 * work item after; the state change between them orders the two. */
static struct ti_stroke cur;
static bool cur_click;
static int next_i;
static int last_t;
static bool lifting;
static int64_t t0;

static void play(struct k_work *work);
static K_WORK_DELAYABLE_DEFINE(play_work, play);

static void finger_down(int sx, int sy)
{
	int cx, cy;

	ti_screen_to_chan(sx, sy, &cx, &cy);
	input_report_abs(panel_dev, INPUT_ABS_X, cx, false, K_FOREVER);
	input_report_abs(panel_dev, INPUT_ABS_Y, cy, false, K_FOREVER);
	input_report_key(panel_dev, INPUT_BTN_TOUCH, 1, true, K_FOREVER);
}

static void finger_up(void)
{
	/* The driver's release carries no position; the glue reuses the last
	 * one it saw, and so does ui_swipe. */
	input_report_key(panel_dev, INPUT_BTN_TOUCH, 0, true, K_FOREVER);
}

static void at(int t_ms)
{
	k_work_schedule(&play_work, K_TIMEOUT_ABS_MS(t0 + t_ms));
}

static void finish(int what)
{
	atomic_set(&done, what | (cur_click ? AS_CLICK : 0));
	atomic_set(&state, IDLE);
}

static void play(struct k_work *work)
{
	struct ti_sample p;
	int n = ti_samples(&cur);

	ARG_UNUSED(work);

	if (lifting) {
		finger_up();
		finish(cur.op == TI_OP_PRESS ? TI_OP_RELEASE : cur.op);
		return;
	}
	/* A release asked for: stop re-reporting, and lift a driver
	 * re-check after the last report, as a real finger would read. */
	if (cur.op == TI_OP_PRESS && atomic_get(&release_asked)) {
		lifting = true;
		at(last_t + TI_RELEASE_LAG_MS);
		return;
	}
	if (next_i >= n) {
		/* A press nobody released: let go rather than leave a finger
		 * on the panel for good. */
		finger_up();
		finish(cur.op == TI_OP_PRESS ? (TI_OP_RELEASE | AUTO_LIFT)
					     : cur.op);
		return;
	}
	ti_sample(&cur, next_i, &p);
	finger_down(p.x, p.y);
	last_t = p.t_ms;
	if (cur.op == TI_OP_PRESS && next_i == 0) {
		atomic_set(&done_press, 1);
	}
	next_i++;
	if (next_i < n) {
		ti_sample(&cur, next_i, &p);
		at(p.t_ms);
	} else if (cur.op == TI_OP_PRESS) {
		at(last_t + TI_STEP_MS);	/* the cap: the auto lift above */
	} else {
		lifting = true;
		at(ti_release_ms(&cur));
	}
}

static const char *op_name(int op)
{
	switch (op) {
	case TI_OP_TAP:
		return "tap";
	case TI_OP_SWIPE:
		return "swipe";
	case TI_OP_PRESS:
		return "press";
	case TI_OP_RELEASE:
		return "release";
	case TI_OP_DISMISS:
		return "dismiss";
	case TI_OP_WAKE:
		return "wake";
	case AS_CLICK:
		return "click";
	default:
		return "?";
	}
}

static void refuse(const char *why)
{
	printk("{\"t\":\"touch_refused\",\"v\":%d,\"why\":\"%s\"}\n",
	       BLINK_PROTO_VERSION, why);
	TI_LOG_KICK();
}

/* An integer field, or -1 when it is absent or not a number: every field
 * here is a coordinate or a duration, and -1 is outside both, so ti_valid()
 * turns a missing field into a refusal. */
static int field(const char *json, const char *key)
{
	double v;

	if (!msg_get_double(json, key, &v) || v != v || v < -1 || v > 100000) {
		return -1;
	}
	return (int)v;
}

static void click_line(const char *json);
static void start(const struct ti_stroke *s, bool click);

static void touch_line(const char *json)
{
	char opname[12];
	struct ti_stroke s = { 0 };

	if (!msg_get_str(json, "op", opname, sizeof(opname))) {
		refuse("args");
		return;
	}
	if (strcmp(opname, "click") == 0) {
		click_line(json);
		return;
	}
	s.op = ti_op_from_name(opname);
	/*
	 * The two that are not strokes, answered at once and never refused as
	 * busy: they touch no input device, and a stroke in flight is not
	 * disturbed by either. Both are what a person at the desk does by eye
	 * and a bench cannot -- read a popup and close it, or notice the panel
	 * has dozed -- and both act on the main thread, where the popup and
	 * the doze both live, because this is called from proto's dispatch.
	 *
	 * dismiss: the notice, whichever put it up. The first board run lost
	 * twenty steps to one: a tap or swipe while it is up only reaches what
	 * is under it in a 10 px strip down each side (ui_settings.c,
	 * zone_was_a_tap), so every later target was pressed on the popup.
	 * wake: the doze, as a fresh reading would. A no-op when awake.
	 */
	if (s.op == TI_OP_DISMISS) {
		ui_settings_notice_dismiss();
		atomic_set(&done_now, TI_OP_DISMISS);
		return;
	}
	if (s.op == TI_OP_WAKE) {
		ui_sleep_bench_wake();
		atomic_set(&done_now, TI_OP_WAKE);
		return;
	}
	if (s.op == TI_OP_RELEASE) {
		/* A second release while the first is still lifting would
		 * reschedule the lift to now and skip the 100 ms the driver's
		 * re-check takes, which ui_swipe's re-arm and the zones' tap
		 * test were tuned against (review, NICE-2). The press is as
		 * good as released already. */
		if (atomic_get(&state) != HELD || atomic_get(&release_asked)) {
			refuse("not_held");
			return;
		}
		atomic_set(&release_asked, 1);
		k_work_reschedule(&play_work, K_NO_WAIT);
		return;
	}
	if (s.op == TI_OP_SWIPE) {
		s.x0 = field(json, "x0");
		s.y0 = field(json, "y0");
		s.x1 = field(json, "x1");
		s.y1 = field(json, "y1");
		s.ms = field(json, "ms");
	} else {
		s.x0 = field(json, "x");
		s.y0 = field(json, "y");
		s.ms = s.op == TI_OP_TAP ? field(json, "ms") : 0;
	}
	if (!ti_valid(&s)) {
		refuse("args");
		return;
	}
	start(&s, false);
}

/*
 * A click: a tap at a widget of the latest listing (touch_widgets.h), played
 * exactly as a tap is -- into the panel's input device, through LVGL's own hit
 * test -- at the point the listing found that test reaches the widget. Never
 * lv_obj_send_event(): an event sent to the object proves its handler and
 * nothing else, and what a bench is for is the press a finger makes, which
 * can land on something else.
 *
 *   {"t":"touch","op":"click","i":N,"gen":G[,"ms":HOLD][,"force":1]}
 *
 * Refused "stale" when the panel no longer shows the listing G named, so
 * index N of a screen that has since been replaced is never pressed on the
 * new one; "range" when it does and has no index N; "disabled" for a control
 * LVGL would drop the press on, unless force; "covered" when the widget is
 * still there but no point of it is reachable -- something else is on top;
 * "busy" while a transition is latched or a screen load is animating, when
 * LVGL drops input altogether. The aim is found again here, from the panel
 * as it is now, not taken from the listing the PC read. The parsing and the
 * aim are tw_click_parse(), which the host harness runs too.
 */
static void click_line(const char *json)
{
	struct ui_settings_bench b;
	struct ti_stroke s;
	const char *why;

	ui_settings_bench_state(&b);
	why = tw_click_parse(json, b.busy || ui_anim_pending(), &s);
	if (why) {
		refuse(why);
		return;
	}
	start(&s, true);
}

/* Widgets, one page a line (touch_widgets_fmt.h has why four). The buffer is
 * static rather than on the main stack, which also runs LVGL's handlers and
 * the walk's recursion: this is bench-image RAM, 448 B of .bss, and a release
 * does not have it. */
static void widgets_line(const char *json)
{
	static char buf[TW_LINE_MAX];

	if (tw_widgets_reply(json, buf, sizeof(buf), BLINK_PROTO_VERSION) <= 0) {
		refuse("args");
		return;
	}
	printk("%s\n", buf);
	/* Out now rather than at the log thread's next wake. With Zephyr's
	 * default trigger of ten queued messages it slept up to
	 * CONFIG_LOG_PROCESS_THREAD_SLEEP_MS, 1000, so a listing of six pages
	 * was six seconds rather than six round trips. prj.conf's threshold of
	 * 1 now wakes it on the first line anyway; the kick stays so this
	 * reply never depends on that setting. */
	TI_LOG_KICK();
}

static void start(const struct ti_stroke *s, bool click)
{
	if (!atomic_cas(&state, IDLE, s->op == TI_OP_PRESS ? HELD : PLAYING)) {
		refuse("busy");
		return;
	}
	cur = *s;
	cur_click = click;
	next_i = 0;
	last_t = 0;
	lifting = false;
	atomic_set(&release_asked, 0);
	t0 = k_uptime_get();
	k_work_schedule(&play_work, K_NO_WAIT);
}

/*
 * What is on the panel, from the code that put it there. 197 bytes with
 * every field at its longest name and a realistic number, 242 with every %d
 * at INT_MIN -- inside the buffer below either way, and snprintf bounds it
 * regardless. The PC reads lines of up to 2048.
 *
 * slp is 1 for the whole of ui_sleep_run(), the peek included, which is
 * when nothing pressed reaches the screen it looks like it reaches. nt says
 * whose notice is up. gf is the Focus layout the GAUGES are drawing, from
 * usage_view's own state, where focus is what the store says: the two are
 * meant to agree, and only gf shows that the screen was told.
 */
static void ui_state_line(void)
{
	struct ui_settings_bench b;
	const char *scr;
	const char *focus = !cfg_get_focus_on() ? "off" :
			    cfg_get_focus_win() ? "weekly" : "session";
	char buf[256];

	ui_settings_bench_state(&b);
	if (ui_anim_showing()) {
		scr = "eyes";
	} else if (b.open) {
		scr = "settings";
	} else if (usage_view_takeover_active()) {
		scr = "connecting";
	} else {
		scr = "gauges";
	}
	snprintf(buf, sizeof(buf),
		 "{\"t\":\"ui_state\",\"v\":%d,\"scr\":\"%s\",\"ov\":\"%s\","
		 "\"modal\":\"%s\",\"busy\":%d,\"theme\":%d,\"stored\":%d,"
		 "\"tp\":%d,\"focus\":\"%s\",\"bl\":%d,\"edit\":\"%s\","
		 "\"slp\":%d,\"nt\":\"%s\",\"gf\":\"%s\"}",
		 BLINK_PROTO_VERSION, scr, b.open ? b.sub : "none", b.modal,
		 /* A screen load still animating is busy too: tw_click_parse
		  * refuses a click then, so wait_state must be able to wait it
		  * out (review r2, N4). */
		 (b.busy || ui_anim_pending() ||
		  lv_display_get_screen_prev(NULL) != NULL) ? 1 : 0,
		 theme_active_id(),
		 ui_theme_stored_id(), b.theme_page, focus,
		 (int)backlight_get(), b.edit, ui_sleep_bench_dozing() ? 1 : 0,
		 b.nt, usage_view_bench_focus());
	printk("%s\n", buf);
	TI_LOG_KICK();		/* see touch_inject_service */
}

bool touch_inject_dispatch(const char *type, const char *json)
{
	if (strcmp(type, "touch") == 0) {
		touch_line(json);
		return true;
	}
	if (strcmp(type, "ui_state") == 0) {
		ui_state_line();
		return true;
	}
	if (strcmp(type, "widgets") == 0) {
		widgets_line(json);
		return true;
	}
	return false;
}

/*
 * Every reply here goes out through printk, which on this board is the
 * deferred log (CONFIG_LOG_PRINTK, LOG_MODE_DEFERRED), drained by a thread at
 * the lowest application priority. While the main thread is busy -- a slide,
 * a panel build, LVGL drawing -- nothing drains it. Until 2026-10-28 the log
 * then dropped its OLDEST message to make room, so whatever else was printed
 * pushed the reply out: the first board run of the widget image lost 22
 * touch_done and ui_state replies that way, every one on a move into the
 * Settings panel, and the bench reported each as a missing reply. prj.conf
 * now has a writer wait for room instead (LOG_BLOCK_IN_THREAD),
 * which every image shares. Each reply still kicks the log thread, so it
 * leaves at the main loop's next sleep, and the bench image's log buffer is
 * still the larger one (firmware/Kconfig, LOG_BUFFER_SIZE), so the waits it
 * can now cause stay rare at the bench's reply rate.
 */
void touch_inject_service(void)
{
	int what;
	bool said = false;

	if (atomic_clear(&done_press)) {
		printk("{\"t\":\"touch_done\",\"v\":%d,\"op\":\"press\","
		       "\"ok\":1}\n", BLINK_PROTO_VERSION);
		said = true;
	}
	what = (int)atomic_clear(&done_now);
	if (what) {
		printk("{\"t\":\"touch_done\",\"v\":%d,\"op\":\"%s\",\"ok\":1}\n",
		       BLINK_PROTO_VERSION, op_name(what));
		said = true;
	}
	what = (int)atomic_clear(&done);
	if (what) {
		printk("{\"t\":\"touch_done\",\"v\":%d,\"op\":\"%s\",\"ok\":1%s}\n",
		       BLINK_PROTO_VERSION,
		       op_name((what & AS_CLICK) ? AS_CLICK : (what & ~AUTO_LIFT)),
		       (what & AUTO_LIFT) ? ",\"auto\":1" : "");
		said = true;
	}
	if (said) {
		TI_LOG_KICK();
	}
}

void touch_inject_init(void)
{
	/* The phrase tools/lib_selftest.sh looks for in an image. Reword it
	 * and tests/test_lib_selftest.py fails, rather than every inject
	 * image passing the release check without a word. */
	printk("[touch] INJECT BUILD: the PC can drive this panel"
	       " -- bench only, never ship this\n");
}
