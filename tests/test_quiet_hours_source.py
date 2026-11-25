"""Quiet hours (issue #17): source checks, and the real C run on the host.

The firmware as a whole cannot be built here, so the first half of this file
reads the C source and pins the parts of the feature that a later edit could
quietly undo: the backlight schedule itself, the tick that drives it on both
the awake and the dozing paths, the settings record's frozen layout, the
on-screen copy on the Quiet hours page, and the bench's ui_state getter, which
board_touch.py reads and which must go on reporting the page as the brightness
screen.

The second half compiles two of the firmware's own files with the host `cc`
and runs them. A source check only proves a call is written down; a reviewer
rejected the first round because nothing showed the panel actually dims,
that a level picked during the window survives it, that the 20% is never
stored, or that a dozing board -- the laptop shut for the night, the case the
feature exists for -- moves on the schedule with nobody touching it.

  * firmware/src/backlight.c alone, against stand-in Zephyr headers (a PWM
    that records each pulse, a settable uptime) and fakes for the settings
    store and the time of day.
  * firmware/src/ui_sleep.c linked with the real backlight.c, against a fake
    lvgl.h and stand-ins for everything ui_sleep.c calls, with a clip player
    that keeps the real one's contract (ui_boot.c, bootanim_play_ex).

tests/ci/check_host_tests.sh compiles its suites with only `-I firmware/src`,
which cannot reach either file, so this is done from pytest instead. Every
generated file goes in tmp_path; the C is in the module-level constants below.
The project's own headers (backlight.h, cfg_store.h, net_time.h, ui_sleep.h,
proto.h, ui_settings.h and the rest ui_sleep.c includes) are the real ones:
the stand-ins only replace Zephyr and LVGL, so a prototype that drifts from
the firmware's fails to compile here.
"""

import glob
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware" / "src"

# A string literal or a comment, in one pass, so a quote inside a comment is
# never read as a string and a "/*" inside a string never as a comment.
_TOKEN = re.compile(
    r'"(?:\\.|[^"\\\n])*"'      # a string literal
    r"|'(?:\\.|[^'\\\n])+'"     # a character literal
    r"|/\*.*?\*/"               # a block comment
    r"|//[^\n]*",               # a line comment
    re.S,
)

# The board keeps 47 characters of an error (CLAUDE.md, "Style"); every line
# on these screens is held to the same figure.
MAX_COPY = 47


def _read(path):
    return path.read_text(encoding="utf-8")


def _quiet_section(src):
    start = src.index("/* --- Quiet hours")
    end = src.index("/* --- ", start + 1)
    return src[start:end]


# A compile-time check's message is read by whoever breaks the build, never
# by a customer, and is written as a lower-case clause like every other
# BUILD_ASSERT in the firmware. One level of parentheses inside, strings
# skipped whole (a message may hold a ';' or a ')').
_BUILD_ASSERT = re.compile(
    r'BUILD_ASSERT\((?:"(?:\\.|[^"\\\n])*"|[^"()]|\([^()]*\))*\)\s*;')


def _uncommented(code):
    """`code` with every comment replaced by a space, strings left alone."""
    return _TOKEN.sub(
        lambda m: m.group(0) if m.group(0)[0] in "\"'" else " ", code)


def _string_literals(code):
    """(literal text, the code just before it) for each string literal."""
    code = _BUILD_ASSERT.sub(" ", _uncommented(code))
    out = []
    for m in _TOKEN.finditer(code):
        tok = m.group(0)
        if tok.startswith('"'):
            out.append((tok[1:-1], code[max(0, m.start() - 40):m.start()]))
    return out


def test_backlight_no_longer_says_there_is_no_schedule():
    assert "There is no automatic schedule" not in _read(SRC / "backlight.c")


def test_the_schedule_is_ticked_awake_and_dozing():
    assert "backlight_tick(" in _read(SRC / "main.c")
    assert "backlight_tick(" in _read(SRC / "ui_sleep.c")


def test_the_settings_record_layout_is_unchanged():
    # The frozen legacy layouts are sized by it; quiet hours had to fit
    # without moving it.
    assert re.search(r"#define\s+CFG_TOKEN_MAX\s+320\b",
                     _read(SRC / "cfg_store.h"))


def test_the_stored_theme_stays_where_it_was():
    # Quiet hours took the token's last four bytes; the theme lives after
    # 1.3.4's seal and must not have moved with them. A moved theme_id
    # would read some other byte as the theme on every board that updates.
    src = _read(SRC / "cfg_store.c")
    assert re.search(
        r"_Static_assert\(offsetof\(struct rec, theme_id\) == 475\b", src)
    assert re.search(
        r"_Static_assert\(offsetof\(struct rec, focus_on\) == 501\b", src)


_THEME_WRITERS = re.compile(
    r"\b(?:cfg_set_theme|ui_theme_choose|theme_select(?:_custom)?|"
    r"ui_settings_apply_theme)\s*\(")


def _function_body(src, signature):
    start = src.index(signature)
    return src[start:src.index("\n}\n", start)]


def test_quiet_hours_never_write_a_theme():
    # The 2026-11-25 board check ended on theme 2 where its script expects
    # Dark (tools/bench/full_touch.txt line 129). Only a computer's theme
    # line sets the theme on a board; quiet hours must stay out of it, on
    # the page, in the store and in the backlight alike.
    ui = _uncommented(_quiet_section(_read(SRC / "ui_settings.c")))
    assert not _THEME_WRITERS.search(ui)
    store = _uncommented(_read(SRC / "cfg_store.c"))
    for sig in ("void cfg_get_quiet(", "int cfg_set_quiet("):
        assert not _THEME_WRITERS.search(_function_body(store, sig)), sig
    assert not _THEME_WRITERS.search(_uncommented(_read(SRC / "backlight.c")))


def test_the_settings_screen_has_quiet_hours():
    assert "Quiet hours" in _read(SRC / "ui_settings.c")


def test_quiet_hours_copy_fits_and_is_sentence_case():
    section = _quiet_section(_read(SRC / "ui_settings.c"))
    literals = _string_literals(section)
    assert literals, "no string literals found in the Quiet hours section"
    checked = []
    for text, before in literals:
        # A [mem] tag names a measurement in the board's log, in the same
        # lower-case form as "brightness-close"; it is never on screen.
        if re.search(r"mem_stats_report\(\s*$", before):
            continue
        checked.append(text)
        assert len(text) <= MAX_COPY, f"too long for the board: {text!r}"
        first = text[0] if text else ""
        assert first and not first.islower(), (
            f"on-screen copy starts with one capital: {text!r}")
    # The page's own words were read, so the exclusions above did not
    # swallow the section.
    for want in ("Quiet hours", "Off", "On", "From", "Until",
                 "Dims the screen to 20% between these times",
                 "Waits for the time from your computer"):
        assert want in checked, want


def _bench_sub_chain(src):
    start = src.index("void ui_settings_bench_state(")
    body = src[start:src.index("\n}\n", start)]
    m = re.search(r"out->sub\s*=(.*?);", body, re.S)
    assert m, "the bench getter no longer sets out->sub"
    return re.findall(r'"([^"]*)"', m.group(1))


def test_the_bench_getter_reports_the_same_screens():
    # The Quiet hours page is inside bright_ov, so the getter goes on saying
    # "brightness" for it: board_touch.py's scripts know no other name.
    assert _bench_sub_chain(_read(SRC / "ui_settings.c")) == [
        "confirm", "brightness", "theme", "focus", "connection", "about",
        "none",
    ]


def _code_only(src):
    """`src` with comments blanked and every literal emptied, so a brace or a
    call name inside a string or a comment is never counted as code."""
    def blank(m):
        tok = m.group(0)
        if tok.startswith('"'):
            return '""'
        if tok.startswith("'"):
            return "' '"
        return " "
    return _TOKEN.sub(blank, src)


def _enclosing_block(code, pos):
    """(start of the innermost `{` holding `pos`, the text that opens it)."""
    depth = 0
    i = pos
    while i > 0:
        i -= 1
        if code[i] == "}":
            depth += 1
        elif code[i] == "{":
            if depth == 0:
                head_start = max(code.rfind(c, 0, i) for c in ";{}") + 1
                return i, code[head_start:i]
            depth -= 1
    raise AssertionError("no enclosing block")


def test_main_ticks_the_schedule_in_its_one_second_block():
    # backlight_tick holds itself to once a second, so a call anywhere in the
    # main loop would "work" -- but the clock the schedule reads is shown from
    # the same block, and keeping the two together is what makes the panel
    # dim on the minute the header's clock turns to From. Pinned as: the
    # innermost block holding the net_time_local( call is the
    # `now - last_tick >= 1000` one, and backlight_tick( sits directly in it.
    code = _code_only(_read(SRC / "main.c"))
    clock = [m.start() for m in re.finditer(r"\bnet_time_local\s*\(", code)]
    ticks = [m.start() for m in re.finditer(r"\bbacklight_tick\s*\(", code)]
    assert len(ticks) == 1, f"main.c calls backlight_tick {len(ticks)} times"
    start, head = _enclosing_block(code, ticks[0])
    assert re.fullmatch(r"\s*if\s*\(\s*now\s*-\s*last_tick\s*>=\s*1000\s*\)\s*",
                        head), f"backlight_tick is inside {head.strip()!r}"
    assert any(_enclosing_block(code, c)[0] == start for c in clock), (
        "net_time_local is not read in the block that ticks the backlight")


# --- The real C on the host ----------------------------------------------------
#
# Everything below writes C into tmp_path, compiles it with the firmware's own
# backlight.c (and, for the doze, ui_sleep.c), runs it once per scenario, and
# reads a trace off its stdout. One process per scenario, because backlight.c's
# state is file-static and a fresh process is the only honest reset.
#
# The trace is one event per line:
#   pwm <pct> <pulse> t=<ms> clip=<0|1>   a pwm_set_pulse_dt call
#   printk <line>                          a console line, newline stripped
#   store <pct>                            a cfg_set_bright_pct call
#   get <pct>                              backlight_get(), asked by a driver
#   notice t=<ms> / dismiss t=<ms>         the peek's note going up and down
#   done t=<ms>                            ui_sleep_run returned
#   mark <label>                           a driver's section heading

# Stand-in Zephyr headers. Only what backlight.c and ui_sleep.c use, with the
# real signatures, so the real files compile unchanged.
_KERNEL_H = r"""
#ifndef SIM_ZEPHYR_KERNEL_H
#define SIM_ZEPHYR_KERNEL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#define ARG_UNUSED(x) (void)(x)

/* A timeout carries milliseconds and nothing else: k_sleep is the only
 * consumer, and it moves the simulated clock by exactly that much. */
typedef struct { int64_t ms; } k_timeout_t;
#define K_MSEC(m) ((k_timeout_t){ .ms = (m) })

/* The simulated uptime. Nothing in either harness waits on a real clock, so
 * a seven-minute doze runs in a few milliseconds and every instant in the
 * trace is exact. */
int64_t k_uptime_get(void);
int32_t k_sleep(k_timeout_t t);
#endif
"""

_PRINTK_H = r"""
#ifndef SIM_ZEPHYR_PRINTK_H
#define SIM_ZEPHYR_PRINTK_H
/* The format attribute keeps -Wall checking the firmware's printk calls
 * against their arguments, as the cross compiler does. */
void printk(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
#endif
"""

_PWM_H = r"""
#ifndef SIM_ZEPHYR_PWM_H
#define SIM_ZEPHYR_PWM_H
#include <stdbool.h>
#include <stdint.h>

/* The overlay's 1 kHz: a period of 1e6 ns. backlight.c multiplies it by the
 * percent and the scale in 64 bits; the trace prints the pulse it computed,
 * so the arithmetic is checked as well as the percent. */
struct pwm_dt_spec { uint32_t period; };
#define DT_NODELABEL(label) 0
#define PWM_DT_SPEC_GET(node) { .period = 1000000u }

bool pwm_is_ready_dt(const struct pwm_dt_spec *spec);
int pwm_set_pulse_dt(const struct pwm_dt_spec *spec, uint32_t pulse);
#endif
"""

# A fake lvgl.h, for ui_sleep.c and ui_settings.h. Opaque objects, the two
# flags, the one event and the setters ui_sleep.c calls, and nothing else: a
# new LVGL call in ui_sleep.c fails to compile here and has to be added on
# purpose, rather than silently doing nothing.
_LVGL_H = r"""
#ifndef SIM_LVGL_H
#define SIM_LVGL_H
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct _lv_obj_t lv_obj_t;
typedef struct _lv_event_t lv_event_t;
typedef struct _lv_display_t lv_display_t;
typedef void (*lv_event_cb_t)(lv_event_t *e);
typedef struct { uint8_t blue, green, red; } lv_color_t;
typedef uint8_t lv_opa_t;
typedef uint32_t lv_style_selector_t;
typedef enum { LV_EVENT_ALL = 0, LV_EVENT_CLICKED = 10 } lv_event_code_t;
typedef enum {
	LV_OBJ_FLAG_CLICKABLE = 1 << 1,
	LV_OBJ_FLAG_SCROLLABLE = 1 << 4,
} lv_obj_flag_t;
#define LV_OPA_COVER 255

lv_obj_t *lv_scr_act(void);
lv_obj_t *lv_obj_create(lv_obj_t *parent);
void lv_obj_del(lv_obj_t *obj);
void lv_scr_load(lv_obj_t *scr);
void lv_refr_now(lv_display_t *disp);
uint32_t lv_timer_handler(void);
void lv_obj_add_flag(lv_obj_t *obj, lv_obj_flag_t f);
void lv_obj_clear_flag(lv_obj_t *obj, lv_obj_flag_t f);
lv_color_t lv_color_hex(uint32_t c);
void lv_obj_set_style_bg_color(lv_obj_t *obj, lv_color_t c,
			       lv_style_selector_t sel);
void lv_obj_set_style_bg_opa(lv_obj_t *obj, lv_opa_t opa,
			     lv_style_selector_t sel);
void *lv_obj_add_event_cb(lv_obj_t *obj, lv_event_cb_t cb,
			  lv_event_code_t filter, void *user_data);
#endif
"""

_SIM_H = r"""
#ifndef SIM_H
#define SIM_H
#include <stdbool.h>
#include <stdint.h>

extern int64_t sim_ms;		/* the uptime k_uptime_get answers */
extern uint8_t sim_bright;	/* what the settings store holds */
extern uint8_t sim_q_on;	/* the stored quiet-hours schedule */
extern uint16_t sim_q_from, sim_q_until;
extern int sim_in_clip;		/* 1 while the fake clip player runs */

void sim_mark(const char *label);
#endif
"""

# The part both harnesses share: the clock, the PWM, the console and the
# settings store. Each is a recorder, because the questions are "what did the
# panel get, and when" and "what was written to flash".
_SIM_COMMON_C = r"""
#include <stdarg.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/drivers/pwm.h>
#include <zephyr/sys/printk.h>

#include "cfg_store.h"
#include "sim.h"

/* Not zero: backlight.c's rate limit starts from 0, and a board has been up
 * for a while before anything here matters. */
int64_t sim_ms = 100000;
uint8_t sim_bright = 100;
uint8_t sim_q_on;
uint16_t sim_q_from = 1320, sim_q_until = 420;
int sim_in_clip;

void sim_mark(const char *label)
{
	printf("mark %s\n", label);
}

int64_t k_uptime_get(void)
{
	return sim_ms;
}

int32_t k_sleep(k_timeout_t t)
{
	sim_ms += t.ms;
	return 0;
}

void printk(const char *fmt, ...)
{
	char buf[256];
	size_t n;
	va_list ap;

	va_start(ap, fmt);
	vsnprintf(buf, sizeof buf, fmt, ap);
	va_end(ap);
	n = strlen(buf);
	while (n > 0 && buf[n - 1] == '\n') {
		buf[--n] = '\0';
	}
	printf("printk %s\n", buf);
}

bool pwm_is_ready_dt(const struct pwm_dt_spec *spec)
{
	(void)spec;
	return true;
}

/* The percent is worked back from the pulse, so a wrong multiply shows up as
 * a wrong percent rather than hiding behind a right one. */
int pwm_set_pulse_dt(const struct pwm_dt_spec *spec, uint32_t pulse)
{
	printf("pwm %u %u t=%lld clip=%d\n",
	       (unsigned)((uint64_t)pulse * 100u / spec->period),
	       (unsigned)pulse, (long long)sim_ms, sim_in_clip);
	return 0;
}

uint8_t cfg_get_bright_pct(void)
{
	return sim_bright;
}

/* Every write is recorded: the dim must never be one of them. The stored
 * value is also what the next backlight_init reads, so a "reboot" in a
 * scenario sees what flash would hold. */
int cfg_set_bright_pct(uint8_t pct)
{
	printf("store %u\n", (unsigned)pct);
	sim_bright = pct;
	return 0;
}

void cfg_get_quiet(uint8_t *on, uint16_t *from_min, uint16_t *until_min)
{
	*on = sim_q_on;
	*from_min = sim_q_from;
	*until_min = sim_q_until;
}
"""

# Harness A's driver: the time of day is set by hand, apart from the uptime,
# so a scenario can jump from 23:30 to 03:00 in one step, or hold the time
# still while the uptime moves (the rate limit).
_BACKLIGHT_DRIVER_C = r"""
#include <stdio.h>
#include <string.h>

#include <zephyr/kernel.h>

#include "backlight.h"
#include "net_time.h"
#include "sim.h"

static bool t_known;
static int t_hh, t_mm;

/* "Outputs untouched" when unknown, as net_time.h promises. */
bool net_time_local(int *hh, int *mm)
{
	if (!t_known) {
		return false;
	}
	*hh = t_hh;
	*mm = t_mm;
	return true;
}

static void at(int hh, int mm)
{
	t_known = true;
	t_hh = hh;
	t_mm = mm;
}

static void quiet(uint8_t on, uint16_t from, uint16_t until)
{
	sim_q_on = on;
	sim_q_from = from;
	sim_q_until = until;
}

/* A second later, the main loop's tick. */
static void tick_later(void)
{
	sim_ms += 1000;
	backlight_tick();
}

static void say_level(void)
{
	printf("get %u\n", (unsigned)backlight_get());
}

static void boot_never_dims(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	backlight_init();
	for (int i = 0; i < 5; i++) {
		tick_later();
	}
	backlight_quiet_changed();
	say_level();
}

static void dims_once(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	at(21, 0);
	backlight_init();
	tick_later();
	sim_mark("window opens");
	at(23, 0);
	tick_later();
	sim_mark("inside");
	for (int i = 0; i < 5; i++) {
		tick_later();
	}
	say_level();
	/* Power lost at 23:00: the next boot reads what flash holds. */
	sim_mark("reboot");
	backlight_init();
	say_level();
}

static void set_in_window(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	at(23, 0);
	backlight_init();
	tick_later();
	sim_mark("set");
	backlight_set(60);
	say_level();
	tick_later();
	tick_later();
	sim_mark("window closes");
	at(7, 0);
	tick_later();
	say_level();
}

static void step_in_window(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	at(23, 0);
	backlight_init();
	tick_later();
	sim_mark("step");
	backlight_step(-1);
	say_level();
	tick_later();
	sim_mark("window closes");
	at(7, 0);
	tick_later();
	say_level();
}

static void twenty_stays_twenty(void)
{
	sim_bright = 20;
	quiet(1, 1320, 420);
	at(21, 0);
	backlight_init();
	tick_later();
	sim_mark("window");
	at(23, 0);
	tick_later();
	tick_later();
	backlight_quiet_changed();
	at(8, 0);
	tick_later();
	say_level();
}

static void unknown_time_never_dims(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	backlight_init();
	sim_mark("ticks");
	for (int i = 0; i < 100; i++) {
		tick_later();
	}
	backlight_quiet_changed();
	say_level();
}

static void across_midnight(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	at(21, 0);
	backlight_init();
	tick_later();
	sim_mark("23:30");
	at(23, 30);
	tick_later();
	sim_mark("12:00");
	at(12, 0);
	tick_later();
	sim_mark("03:00");
	at(3, 0);
	tick_later();
	sim_mark("06:59");
	at(6, 59);
	tick_later();
	sim_mark("07:00");
	at(7, 0);
	tick_later();
	sim_mark("22:00");
	at(22, 0);
	tick_later();
}

static void rate_limit(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	at(21, 59);
	backlight_init();
	tick_later();		/* looked: not quiet yet */
	sim_mark("half a second later");
	at(22, 0);
	sim_ms += 500;
	backlight_tick();
	sim_mark("a second after the last look");
	sim_ms += 500;
	backlight_tick();
}

static void quiet_changed_is_immediate(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	at(21, 59);
	backlight_init();
	tick_later();
	sim_mark("changed");
	at(22, 0);
	sim_ms += 10;
	backlight_quiet_changed();
}

static void quiet_off_restores(void)
{
	sim_bright = 80;
	quiet(1, 1320, 420);
	at(23, 0);
	backlight_init();
	tick_later();
	sim_mark("off");
	quiet(0, 1320, 420);
	backlight_quiet_changed();
	sim_mark("on again");
	quiet(1, 1320, 420);
	backlight_quiet_changed();
}

static const struct {
	const char *name;
	void (*run)(void);
} scenarios[] = {
	{ "boot_never_dims", boot_never_dims },
	{ "dims_once", dims_once },
	{ "set_in_window", set_in_window },
	{ "step_in_window", step_in_window },
	{ "twenty_stays_twenty", twenty_stays_twenty },
	{ "unknown_time_never_dims", unknown_time_never_dims },
	{ "across_midnight", across_midnight },
	{ "rate_limit", rate_limit },
	{ "quiet_changed_is_immediate", quiet_changed_is_immediate },
	{ "quiet_off_restores", quiet_off_restores },
};

int main(int argc, char **argv)
{
	for (size_t i = 0; argc == 2 && i < sizeof scenarios / sizeof scenarios[0];
	     i++) {
		if (strcmp(argv[1], scenarios[i].name) == 0) {
			scenarios[i].run();
			return 0;
		}
	}
	fprintf(stderr, "no such scenario\n");
	return 2;
}
"""

# Harness B's driver: the real ui_sleep.c and the real backlight.c, with every
# other thing ui_sleep.c calls stood in for. The time of day is the uptime
# counted from a chosen local time, as net_time.c counts it forward from the
# daemon's last time message -- so the window opens because simulated time
# passed under the doze, never because a driver said so.
_DOZE_DRIVER_C = r"""
#include <stdio.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <lvgl.h>

#include "backlight.h"
#include "bootclip.h"
#include "net_time.h"
#include "proto.h"
#include "sleep_gate.h"
#include "ui_boot.h"
#include "ui_settings.h"
#include "ui_sleep.h"
#include "usage_freshness.h"
#include "usage_view.h"
#include "sim.h"

/* Local seconds since midnight at sim_ms == 0. */
static int64_t base_s;

bool net_time_local(int *hh, int *mm)
{
	int64_t s = (base_s + sim_ms / 1000) % 86400;

	*hh = (int)(s / 3600);
	*mm = (int)(s / 60 % 60);
	return true;
}

/* --- LVGL: objects are slots, and the click callback is kept ------------ */

struct _lv_obj_t { int id; };
struct _lv_event_t { int unused; };

static struct _lv_obj_t objs[8];
static int nobjs = 1;		/* objs[0] is the dashboard the doze leaves */
static lv_event_cb_t click_cb;	/* tap_cb, once ui_sleep_run registers it */
static struct _lv_event_t click_event;

lv_obj_t *lv_scr_act(void) { return &objs[0]; }
lv_obj_t *lv_obj_create(lv_obj_t *parent)
{
	(void)parent;
	objs[nobjs].id = nobjs;
	return &objs[nobjs++];
}
void lv_obj_del(lv_obj_t *obj) { (void)obj; }
void lv_scr_load(lv_obj_t *scr) { (void)scr; }
void lv_refr_now(lv_display_t *disp) { (void)disp; }
uint32_t lv_timer_handler(void) { return 0; }
void lv_obj_add_flag(lv_obj_t *obj, lv_obj_flag_t f) { (void)obj; (void)f; }
void lv_obj_clear_flag(lv_obj_t *obj, lv_obj_flag_t f) { (void)obj; (void)f; }
lv_color_t lv_color_hex(uint32_t c)
{
	lv_color_t out = { (uint8_t)c, (uint8_t)(c >> 8), (uint8_t)(c >> 16) };
	return out;
}
void lv_obj_set_style_bg_color(lv_obj_t *obj, lv_color_t c,
			       lv_style_selector_t sel)
{
	(void)obj; (void)c; (void)sel;
}
void lv_obj_set_style_bg_opa(lv_obj_t *obj, lv_opa_t opa,
			     lv_style_selector_t sel)
{
	(void)obj; (void)opa; (void)sel;
}
void *lv_obj_add_event_cb(lv_obj_t *obj, lv_event_cb_t cb,
			  lv_event_code_t filter, void *user_data)
{
	(void)obj; (void)user_data;
	if (filter == LV_EVENT_CLICKED) {
		click_cb = cb;
	}
	return NULL;
}

/* --- The rest of the board, quiet ----------------------------------------
 * The protocol says nothing, settings is never busy, and there is no reading
 * to stamp stale on waking: the only reasons this doze ends are the driver's
 * awake callback, and the only thing on the panel that changes is the
 * backlight. The peek's note is traced by time, so a scenario can show the
 * dim landed inside it. */

void proto_service(void) {}
bool ui_settings_busy(void) { return false; }
void ui_settings_notice(const char *txt)
{
	(void)txt;
	printf("notice t=%lld\n", (long long)sim_ms);
}
void ui_settings_notice_dismiss(void)
{
	printf("dismiss t=%lld\n", (long long)sim_ms);
}
void ui_settings_whatsnew_dismiss(void) {}
void ui_settings_dozing(bool dozing) { (void)dozing; }
void usage_view_set_sleeping(bool sleeping) { (void)sleeping; }
bool usage_view_have_data(void) { return false; }
void usage_view_set_status(enum usage_status status) { (void)status; }
int32_t usage_freshness_age_s(int64_t now_ms) { (void)now_ms; return -1; }
bool sleep_reading_is_stale(int32_t age_s) { (void)age_s; return false; }

/* --- The clips -------------------------------------------------------------
 * Told apart by blob pointer; the length in frames is this harness's own,
 * since the real blob carries it in its header. 12 fps, as the shipped sleep
 * clips are: a 2 s close, a 3 s loop, a 2 s open. */

static const uint8_t close_blob[1], loop_blob[1], open_blob[1];
static const struct bootclip clips[3] = {
	{ close_blob, sizeof close_blob, 0x202020, "close" },
	{ loop_blob, sizeof loop_blob, 0x202020, "loop" },
	{ open_blob, sizeof open_blob, 0x202020, "open" },
};

const struct bootclip *sleepclip_active(enum sleep_part part)
{
	return &clips[part];
}

static bool loop_undecodable;	/* the loop clip fails its header */
static int64_t tap_at = -1;	/* a finger lands on the first loop frame at
				 * or after this uptime; -1 never */

/*
 * ui_boot_play_clip as ui_boot.c's bootanim_play_ex plays it: false at once
 * for a blob that does not decode; otherwise, per frame, wait out the frame
 * time (1000 / fps ms) pumping the protocol and LVGL in 5 ms sleeps, then
 * ask `stop` if there is one and return true the moment it says so; true
 * once every frame has played. It does not call ui_sleep.c's service() and
 * does not tick the backlight: that is exactly what the stop callbacks have
 * to make up for. The tap is delivered where LVGL would deliver it, from the
 * pump between frames, through the callback ui_sleep_run registered.
 */
bool ui_boot_play_clip(const uint8_t *blob, size_t len, bool (*stop)(void))
{
	int frames = blob == loop_blob ? 36 : 24;
	int64_t next = k_uptime_get();

	(void)len;
	if (blob == loop_blob && loop_undecodable) {
		return false;
	}
	sim_in_clip = 1;
	for (int i = 0; i < frames; i++) {
		next += 1000 / 12;
		while (k_uptime_get() < next) {
			proto_service();
			lv_timer_handler();
			k_sleep(K_MSEC(5));
		}
		if (blob == loop_blob && tap_at >= 0 && sim_ms >= tap_at &&
		    click_cb != NULL) {
			tap_at = -1;
			click_cb(&click_event);
		}
		if (stop != NULL && stop()) {
			sim_in_clip = 0;
			return true;
		}
	}
	sim_in_clip = 0;
	return true;
}

/* --- Scenarios ------------------------------------------------------------- */

static int64_t wake_at;

static bool awake_cb(void)
{
	return sim_ms >= wake_at;
}

static void setup(int hh, int mm, uint8_t on)
{
	sim_ms = 0;
	base_s = (int64_t)hh * 3600 + (int64_t)mm * 60;
	sim_bright = 80;
	sim_q_on = on;
	sim_q_from = 1320;
	sim_q_until = 420;
	backlight_init();
}

static void doze_for_min(int minutes)
{
	sim_mark("doze");
	wake_at = sim_ms + (int64_t)minutes * 60000;
	ui_sleep_run(awake_cb, "Waiting for the app on your computer.");
	printf("done t=%lld\n", (long long)sim_ms);
}

static void dozing_dims(void)
{
	setup(21, 58, 1);
	doze_for_min(7);
}

static void dozing_restores(void)
{
	setup(6, 58, 1);
	sim_ms += 1000;
	backlight_tick();	/* the main loop's last tick: dimmed */
	doze_for_min(5);
}

static void undecodable_loop_dims(void)
{
	setup(21, 58, 1);
	loop_undecodable = true;
	doze_for_min(7);
}

static void peek_dims(void)
{
	setup(21, 58, 1);
	tap_at = 115000;	/* 21:59:55: the window opens 5 s into the peek */
	doze_for_min(7);
}

static void quiet_off_doze(void)
{
	setup(21, 58, 0);
	doze_for_min(7);
}

static const struct {
	const char *name;
	void (*run)(void);
} scenarios[] = {
	{ "dozing_dims", dozing_dims },
	{ "dozing_restores", dozing_restores },
	{ "undecodable_loop_dims", undecodable_loop_dims },
	{ "peek_dims", peek_dims },
	{ "quiet_off_doze", quiet_off_doze },
};

int main(int argc, char **argv)
{
	for (size_t i = 0; argc == 2 && i < sizeof scenarios / sizeof scenarios[0];
	     i++) {
		if (strcmp(argv[1], scenarios[i].name) == 0) {
			scenarios[i].run();
			return 0;
		}
	}
	fprintf(stderr, "no such scenario\n");
	return 2;
}
"""

_STUBS = {
    "zephyr/kernel.h": _KERNEL_H,
    "zephyr/sys/printk.h": _PRINTK_H,
    "zephyr/drivers/pwm.h": _PWM_H,
    "lvgl.h": _LVGL_H,
    "sim.h": _SIM_H,
}


def _build(tmp_path, name, driver, firmware):
    """Compile `driver` with the shared simulation and the named real firmware
    files; return the executable.

    -Wall -Werror, as tests/ci/check_host_tests.sh compiles its suites: a
    warning in a firmware file is one the cross build would show too. The
    stand-in include dir comes before firmware/src, but it holds only Zephyr,
    LVGL and sim.h, so every project header is the firmware's own.
    """
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler")
    work = tmp_path / name
    inc = work / "include"
    for rel, text in _STUBS.items():
        path = inc / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    common = work / "sim_common.c"
    common.write_text(_SIM_COMMON_C, encoding="utf-8")
    drv = work / "driver.c"
    drv.write_text(driver, encoding="utf-8")
    exe = work / name
    cmd = [cc, "-Wall", "-Werror", "-DCONFIG_BLINK_BL_SCALE_PCT=100",
           "-I", str(inc), "-I", str(SRC), str(common), str(drv),
           *[str(SRC / f) for f in firmware], "-o", str(exe)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    assert r.returncode == 0, f"{name} did not compile:\n{r.stderr}"
    return exe


def _run(exe, scenario):
    r = subprocess.run([str(exe), scenario], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, f"{scenario}: exit {r.returncode}\n{r.stderr}"
    return r.stdout


def _segments(out):
    """The trace cut at its marks: {label: [event lines]}, "start" first."""
    segs = {"start": []}
    cur = "start"
    for line in out.splitlines():
        if line.startswith("mark "):
            cur = line[5:]
            segs[cur] = []
        else:
            segs[cur].append(line)
    return segs


_PWM_LINE = re.compile(r"pwm (\d+) (\d+) t=(-?\d+) clip=([01])")


def _pwm(lines):
    """[{pct, t, clip}] for each PWM write, checking the pulse is the percent
    of the 1e6 ns period at a scale of 100 -- backlight.c's own multiply."""
    out = []
    for line in lines:
        m = _PWM_LINE.fullmatch(line)
        if m:
            pct, pulse = int(m.group(1)), int(m.group(2))
            assert pulse == pct * 10000, line
            out.append({"pct": pct, "t": int(m.group(3)),
                        "clip": m.group(4) == "1"})
    return out


def _pcts(lines):
    return [p["pct"] for p in _pwm(lines)]


def _logs(lines):
    return [ln[len("printk "):] for ln in lines if ln.startswith("printk ")]


def _stores(out):
    return [int(ln.split()[1]) for ln in out.splitlines()
            if ln.startswith("store ")]


def _gets(lines):
    return [int(ln.split()[1]) for ln in lines if ln.startswith("get ")]


def _backlight(tmp_path, scenario):
    exe = _build(tmp_path, "backlight", _BACKLIGHT_DRIVER_C, ["backlight.c"])
    out = _run(exe, scenario)
    return out, _segments(out)


def test_backlight_boot_never_dims(tmp_path):
    # Quiet on, but no time message yet -- every boot. The panel shows the
    # owner's 80 and nothing a tick or a schedule edit does changes that.
    out, segs = _backlight(tmp_path, "boot_never_dims")
    assert _pcts(segs["start"]) == [80], f"boot_never_dims:\n{out}"
    assert _logs(segs["start"]) == ["[bl] 80%"], f"boot_never_dims:\n{out}"
    assert _gets(segs["start"]) == [80], f"boot_never_dims:\n{out}"
    assert _stores(out) == [], f"boot_never_dims stored:\n{out}"


def test_backlight_dims_once_and_never_stores_the_dim(tmp_path):
    out, segs = _backlight(tmp_path, "dims_once")
    msg = f"dims_once:\n{out}"
    assert _pcts(segs["start"]) == [80], msg
    assert _pcts(segs["window opens"]) == [20], msg
    assert _logs(segs["window opens"]) == ["[bl] 20%"], msg
    # Five more ticks inside the window: no write, so no console line a
    # second either.
    assert _pwm(segs["inside"]) == [], msg
    assert _logs(segs["inside"]) == [], msg
    # Dimmed, the settings screen still reads the owner's choice...
    assert _gets(segs["inside"]) == [80], msg
    # ...and the dim never reached flash: a board that loses power inside the
    # window boots at 80, not at 20.
    assert _stores(out) == [], msg
    assert _pcts(segs["reboot"]) == [80], msg


def test_backlight_a_level_set_in_the_window_is_kept_for_after_it(tmp_path):
    out, segs = _backlight(tmp_path, "set_in_window")
    msg = f"set_in_window:\n{out}"
    assert _pcts(segs["start"]) == [80, 20], msg
    # Stored as the owner's, answered as theirs, and the panel stays dim.
    assert _stores(out) == [60], msg
    assert _gets(segs["set"]) == [60], msg
    assert _pwm(segs["set"]) == [], msg
    # Until: the latest choice, not the level the window opened on.
    assert _pcts(segs["window closes"]) == [60], msg
    assert _logs(segs["window closes"]) == ["[bl] 60%"], msg
    assert _gets(segs["window closes"]) == [60], msg


def test_backlight_a_step_in_the_window_is_kept_for_after_it(tmp_path):
    out, segs = _backlight(tmp_path, "step_in_window")
    msg = f"step_in_window:\n{out}"
    assert _pcts(segs["start"]) == [80, 20], msg
    assert _stores(out) == [60], msg
    assert _gets(segs["step"]) == [60], msg
    assert _pwm(segs["step"]) == [], msg
    assert _pcts(segs["window closes"]) == [60], msg
    assert _logs(segs["window closes"]) == ["[bl] 60%"], msg


def test_backlight_twenty_is_left_at_twenty(tmp_path):
    # The window holds the panel at no MORE than 20: an owner already there
    # sees no write at From or at Until, and nothing is stored.
    out, segs = _backlight(tmp_path, "twenty_stays_twenty")
    msg = f"twenty_stays_twenty:\n{out}"
    assert _pcts(segs["start"]) == [20], msg
    assert _pwm(segs["window"]) == [], msg
    assert _logs(segs["window"]) == [], msg
    assert _gets(segs["window"]) == [20], msg
    assert _stores(out) == [], msg


def test_backlight_an_unknown_time_never_dims(tmp_path):
    out, segs = _backlight(tmp_path, "unknown_time_never_dims")
    msg = f"unknown_time_never_dims:\n{out}"
    assert _pcts(segs["start"]) == [80], msg
    assert _pwm(segs["ticks"]) == [], msg
    assert _logs(segs["ticks"]) == [], msg


def test_backlight_the_window_crosses_midnight(tmp_path):
    out, segs = _backlight(tmp_path, "across_midnight")
    msg = f"across_midnight:\n{out}"
    assert _pcts(segs["start"]) == [80], msg
    assert {k: _pcts(v) for k, v in segs.items() if k != "start"} == {
        "23:30": [20],
        "12:00": [80],
        "03:00": [20],
        "06:59": [],        # still inside: Until is exclusive
        "07:00": [80],
        "22:00": [20],      # From is inclusive
    }, msg


def test_backlight_ticks_are_once_a_second(tmp_path):
    # The window opens between two calls 500 ms apart: the second is not a
    # look, so the panel stays bright until a full second has passed since
    # the last one.
    out, segs = _backlight(tmp_path, "rate_limit")
    msg = f"rate_limit:\n{out}"
    assert _pcts(segs["start"]) == [80], msg
    assert _pwm(segs["half a second later"]) == [], msg
    assert _pcts(segs["a second after the last look"]) == [20], msg


def test_backlight_a_schedule_edit_answers_at_once(tmp_path):
    # 10 ms after the last tick, so only backlight_quiet_changed can have
    # looked: the page's tap is answered without waiting for the rate limit.
    out, segs = _backlight(tmp_path, "quiet_changed_is_immediate")
    msg = f"quiet_changed_is_immediate:\n{out}"
    assert _pcts(segs["start"]) == [80], msg
    assert _pcts(segs["changed"]) == [20], msg


def test_backlight_turning_quiet_off_restores_at_once(tmp_path):
    out, segs = _backlight(tmp_path, "quiet_off_restores")
    msg = f"quiet_off_restores:\n{out}"
    assert _pcts(segs["start"]) == [80, 20], msg
    assert _pcts(segs["off"]) == [80], msg
    assert _logs(segs["off"]) == ["[bl] 80%"], msg
    assert _pcts(segs["on again"]) == [20], msg
    assert _stores(out) == [], msg


# The doze. Each scenario starts at uptime 0 at a chosen local time, so From
# (22:00) or Until (07:00) arrives at uptime 120000 ms. backlight_tick looks at
# most once a second, from a frame callback asked every 83 ms or a service()
# that sleeps 5 ms, so a look lands within ~1.1 s of the minute; 2 s is the
# margin, and the change can never come before the minute.
_ON_THE_MINUTE = 120000
_SLACK_MS = 2000


def _doze(tmp_path, scenario):
    exe = _build(tmp_path, "doze", _DOZE_DRIVER_C,
                 ["backlight.c", "ui_sleep.c"])
    out = _run(exe, scenario)
    segs = _segments(out)
    msg = f"{scenario}:\n{out}"
    # The doze ran and ended: closing clip announced, woken, returned.
    assert "[sleep] dozing (close)" in _logs(segs["doze"]), msg
    assert "[sleep] waking" in _logs(segs["doze"]), msg
    assert any(ln.startswith("done t=") for ln in segs["doze"]), msg
    assert _stores(out) == [], f"{scenario} stored a level:\n{out}"
    return out, segs, msg


def _on_time(event):
    return _ON_THE_MINUTE <= event["t"] <= _ON_THE_MINUTE + _SLACK_MS


def test_doze_dims_on_time_through_the_clips(tmp_path):
    # 21:58 to 22:05, nobody touching the board: the clip callbacks are the
    # only thing running at 22:00, and they must move the schedule.
    out, segs, msg = _doze(tmp_path, "dozing_dims")
    assert _pcts(segs["start"]) == [80], msg
    dims = _pwm(segs["doze"])
    assert [d["pct"] for d in dims] == [20], msg
    assert _on_time(dims[0]), msg
    assert dims[0]["clip"], f"dozing_dims: dimmed outside a clip\n{out}"


def test_doze_restores_on_time_through_the_clips(tmp_path):
    out, segs, msg = _doze(tmp_path, "dozing_restores")
    assert _pcts(segs["start"]) == [80, 20], msg
    ups = _pwm(segs["doze"])
    assert [u["pct"] for u in ups] == [80], msg
    assert _on_time(ups[0]), msg
    assert ups[0]["clip"], msg


def test_doze_dims_on_time_when_the_loop_will_not_decode(tmp_path):
    # The fallback spins service() a second at a time with no clip at all,
    # so the dim at 22:00 can only have come from service()'s own tick.
    out, segs, msg = _doze(tmp_path, "undecodable_loop_dims")
    dims = _pwm(segs["doze"])
    assert [d["pct"] for d in dims] == [20], msg
    assert _on_time(dims[0]), msg
    assert not dims[0]["clip"], msg


def test_doze_dims_on_time_during_a_peek(tmp_path):
    # A tap at 21:59:55 starts the ten-second peek, which spans 22:00.
    out, segs, msg = _doze(tmp_path, "peek_dims")
    dims = _pwm(segs["doze"])
    assert [d["pct"] for d in dims] == [20], msg
    assert _on_time(dims[0]), msg
    assert not dims[0]["clip"], msg
    notice = [int(ln.split("=")[1]) for ln in segs["doze"]
              if ln.startswith("notice t=")]
    dismiss = [int(ln.split("=")[1]) for ln in segs["doze"]
               if ln.startswith("dismiss t=")]
    assert len(notice) == 1 and len(dismiss) == 1, msg
    assert notice[0] < dims[0]["t"] < dismiss[0], (
        f"peek_dims: the dim was not inside the peek\n{out}")


def test_doze_with_quiet_off_never_touches_the_panel(tmp_path):
    out, segs, msg = _doze(tmp_path, "quiet_off_doze")
    assert _pcts(segs["start"]) == [80], msg
    assert _pwm(segs["doze"]) == [], msg


# =========================================================================
# Harness C: the Quiet hours controls, tapped on the real screen
# =========================================================================
#
# A review rejected the round before this one because nothing operated the
# page: the source checks above read text and symbols, and nothing showed that
# On, Off, From, Until and the back arrow do anything when pressed. These tests
# press them.
#
# They build tools/panel_render/touch.sh's harness -- the real ui_settings.c on
# the real LVGL, on the host, with the bench's input path -- and play taps and
# swipes into it over the same stdin wire tools/board_touch.py uses
# (harness.c, "the wire"). The lines are written all at once and stdin is then
# closed: harness.c handles one line at a time and, after every stroke, runs
# its loop 600 ms of simulated time before reading the next, so a batch plays
# exactly as a script would, only without board_touch.py's wall-clock settles.
#
# What is read back, and from where:
#
#   * ui_state, the bench getter (ov=brightness on both pages, bl= for the
#     level stops);
#   * the widget listing (touch_widgets.c): which buttons are there, where
#     LVGL's own hit test reaches them, and the CHECKED state the page puts
#     on Off or On from what cfg_get_quiet() returns -- which here is
#     stubs.c's RAM store, written only by cfg_set_quiet();
#   * every label's text, which neither of those offers: the listing names a
#     control by its first label child only, and the times, the hint and the
#     row's value are not controls. So the build adds one translation unit of
#     this test's own (_QH_LABEL_DUMP_C, through BLINK_TOUCH_CFLAGS, which
#     touch.sh hands to the compiler unquoted). It registers an atexit
#     handler, and when the harness returns from main at the end of its
#     input, walks the screen and its layers and prints every label's text
#     (hex) with its place in the object tree and its coordinates. It reads
#     LVGL's objects as ui_settings.c left them; it calls nothing in
#     ui_settings.c and changes nothing. One reading per run, at the end, so
#     each checkpoint below is a run of its own, replayed from boot.
#
# The levels page's row is built from cfg_get_quiet() each time the page is
# (quiet_row), so its value label after a round trip is what was stored.
#
# The hint: stubs.c's net_time_local() always answers false ("a board that has
# not been told the time"), and there is no wire line or flag to give it one,
# so only "Waits for the time from your computer" can be shown here. The other
# two lines are covered by the source checks above, not by a tap.
#
# LVGL is LVGL_DIR's, when set, or a Zephyr workspace's modules/lib/gui/lvgl;
# with neither, or no `cc`, these skip. Everything built or drawn goes under
# tmp_path: touch.sh puts its build in $TMPDIR/blink-touch-render, and TMPDIR
# is pointed there for it.

_QH_TOUCH_SH = ROOT / "tools" / "panel_render" / "touch.sh"
# One cc over every LVGL source, serially: about a minute on an M-series Mac.
_QH_BUILD_TIMEOUT_S = 900
_QH_RUN_TIMEOUT_S = 120
# Pages of four asked for after each listing request; a screen here has under
# twenty clickable objects. A page past the end comes back empty.
_QH_WIDGET_PAGES = 6
_QH_HHMM = re.compile(r"\d\d:\d\d")
_QH_WAITS = "Waits for the time from your computer"
_QH_DIMS = "Dims the screen to 20% between these times"
# LV_SYMBOL_MINUS and LV_SYMBOL_PLUS (lv_symbol_def.h), as decoded text.
_QH_MINUS, _QH_PLUS = "\uf068", "\uf067"
W_CHECKED_FLAG, W_HIT_FLAG = 1, 4    # touch_widgets_fmt.h, TW_F_*

# Linked into the harness by the build fixture; see the section comment. Only
# prints when QH_LABEL_DUMP is set, so touch.sh's own smoke run (which
# board_touch.py reads) never sees it.
_QH_LABEL_DUMP_C = r"""
#include <stdio.h>
#include <stdlib.h>
#include <lvgl.h>

static void qh_walk(lv_obj_t *o, const char *path, int hidden)
{
	char sub[192];
	uint32_t n;

	if (o == NULL) {
		return;
	}
	hidden = hidden || lv_obj_has_flag(o, LV_OBJ_FLAG_HIDDEN);
	if (lv_obj_check_type(o, &lv_label_class)) {
		const char *t = lv_label_get_text(o);
		lv_area_t a;

		lv_obj_get_coords(o, &a);
		printf("QH_LABEL %s %d %d %d %d %d ", path, hidden, (int)a.x1,
		       (int)a.y1, (int)a.x2, (int)a.y2);
		for (; t != NULL && *t; t++) {
			printf("%02x", (unsigned char)*t);
		}
		printf("\n");
	}
	n = lv_obj_get_child_count(o);
	for (uint32_t i = 0; i < n; i++) {
		snprintf(sub, sizeof(sub), "%s/%u", path, (unsigned)i);
		qh_walk(lv_obj_get_child(o, (int32_t)i), sub, hidden);
	}
}

static void qh_dump(void)
{
	if (getenv("QH_LABEL_DUMP") == NULL) {
		return;
	}
	lv_obj_update_layout(lv_screen_active());
	lv_obj_update_layout(lv_layer_top());
	lv_obj_update_layout(lv_layer_sys());
	printf("QH_DUMP_BEGIN\n");
	qh_walk(lv_screen_active(), "s", 0);
	qh_walk(lv_layer_top(), "t", 0);
	qh_walk(lv_layer_sys(), "y", 0);
	printf("QH_DUMP_END\n");
	fflush(stdout);
}

__attribute__((constructor)) static void qh_register(void)
{
	atexit(qh_dump);
}
"""


def _qh_tail(text, n=40):
    return "\n".join(text.splitlines()[-n:])


def _qh_lvgl_ok(d):
    return bool(d) and os.path.isdir(os.path.join(d, "src")) and \
        os.path.isfile(os.path.join(d, "lv_conf_template.h"))


def _qh_lvgl_dir():
    """LVGL_DIR when set (CI style), else the LVGL of a Zephyr workspace found
    where tools/lib_zephyr.sh looks, or None. Under pytest HOME is tmp_path,
    so the account's home comes from the account database, as in
    tests/test_board_touch.py; it is only read, never written."""
    env = os.environ.get("LVGL_DIR", "")
    if env:
        if not _qh_lvgl_ok(env):
            pytest.fail(f"LVGL_DIR is set but holds no LVGL checkout: {env}")
        return env
    roots = [os.environ.get("BLINK_ZEPHYR", "")]
    try:
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        real = None
    if real:
        for pat in ("zephyrproject", "zephyr-v*",
                    os.path.join("Projects", "zephyrproject"),
                    os.path.join("Projects", "zephyr-v*")):
            roots += sorted(glob.glob(os.path.join(real, pat)))
    found = [os.path.join(r, "modules", "lib", "gui", "lvgl")
             for r in roots if r]
    found = [d for d in found if _qh_lvgl_ok(d)]
    # Zephyr 4.4.0's LVGL cannot build this firmware (CLAUDE.md, "Hardware"),
    # so a 4.3 workspace goes first when there are several.
    found.sort(key=lambda d: "4.3" not in d)
    return found[0] if found else None


def _qh_board_touch():
    spec = importlib.util.spec_from_file_location(
        "board_touch_for_quiet", ROOT / "tools" / "board_touch.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def qh(tmp_path_factory):
    """The harness, built once for this section, with what to drive it."""
    lvgl = _qh_lvgl_dir()
    if lvgl is None:
        pytest.skip("no LVGL checkout: set LVGL_DIR, or keep a Zephyr 4.3 "
                    "workspace (modules/lib/gui/lvgl) where "
                    "tools/lib_zephyr.sh looks")
    cc = os.environ.get("CC") or "cc"
    if shutil.which(cc) is None:
        pytest.skip(f"no C compiler ({cc}) on PATH")
    sh = shutil.which("sh")
    if sh is None:
        pytest.skip("no sh on PATH to run tools/panel_render/touch.sh")
    work = tmp_path_factory.mktemp("quiet_touch")
    spy = work / "qh_label_dump.c"
    if any(c.isspace() for c in str(spy)):
        pytest.skip("touch.sh word-splits BLINK_TOUCH_CFLAGS, and the "
                    "temporary directory's path has a space in it")
    spy.write_text(_QH_LABEL_DUMP_C, encoding="utf-8")
    # touch.sh always plays a script after it builds; this one only proves
    # the harness boots to the gauges under board_touch.py.
    smoke = work / "smoke.txt"
    smoke.write_text("expect ui_state scr=gauges modal=none\n",
                     encoding="utf-8")
    env = dict(os.environ)
    env.update({
        "LVGL_DIR": lvgl,
        "TMPDIR": str(work / "tmp"),
        "BLINK_PYTHON": sys.executable,
        "BLINK_TOUCH_CFLAGS": str(spy),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    env.pop("QH_LABEL_DUMP", None)
    t0 = time.monotonic()
    p = subprocess.run([sh, str(_QH_TOUCH_SH), str(work / "smoke-renders"),
                        str(smoke)], cwd=str(ROOT), env=env,
                       capture_output=True, text=True, errors="replace",
                       timeout=_QH_BUILD_TIMEOUT_S)
    took = time.monotonic() - t0
    exe = work / "tmp" / "blink-touch-render" / "touch"
    tail = _qh_tail(p.stdout + "\n" + p.stderr)
    assert exe.is_file(), (
        f"touch.sh built no harness (exit {p.returncode}, LVGL {lvgl}):\n"
        f"{tail}")
    assert p.returncode == 0, (
        f"the harness built but its smoke script failed "
        f"(exit {p.returncode}):\n{tail}")
    print(f"\nquiet hours touch harness: LVGL {lvgl}, built in {took:.1f} s")
    return {"exe": exe, "bt": _qh_board_touch(), "build_s": took}


def _qh_geom():
    """The Quiet hours page's layout, from ui_settings.c's own #defines, and
    the aim point of each control. The listing cross-checks every aim in
    test_quiet_touch_the_row_opens_the_quiet_page."""
    src = _read(SRC / "ui_settings.c")
    g = {k: int(v) for k, v in
         re.findall(r"#define (QUIET_\w+)\s+(\d+)\b", src)}
    mid = g["QUIET_CTL_H"] // 2
    on_x = 12 + g["QUIET_SW_W"] + g["QUIET_SW_GAP"]
    g["pts"] = {
        # quiet_row: 296 wide at x 12.
        "row": (12 + 296 // 2, g["QUIET_ROW_Y"] + g["QUIET_ROW_H"] // 2),
        "off": (12 + g["QUIET_SW_W"] // 2, g["QUIET_SW_Y"] + mid),
        "on": (on_x + g["QUIET_SW_W"] // 2, g["QUIET_SW_Y"] + mid),
        "from-": (g["QUIET_MINUS_X"] + g["QUIET_STEP_W"] // 2,
                  g["QUIET_FROM_Y"] + mid),
        "from+": (g["QUIET_PLUS_X"] + g["QUIET_STEP_W"] // 2,
                  g["QUIET_FROM_Y"] + mid),
        "until-": (g["QUIET_MINUS_X"] + g["QUIET_STEP_W"] // 2,
                   g["QUIET_UNTIL_Y"] + mid),
        "until+": (g["QUIET_PLUS_X"] + g["QUIET_STEP_W"] // 2,
                   g["QUIET_UNTIL_Y"] + mid),
    }
    return g


_QH_G = _qh_geom()


def _qh_tap(name):
    return ("tap", _QH_G["pts"][name], name)


def _qh_open(bt):
    """Gauges -> Settings -> Brightness, as tools/bench/full_touch.txt does
    it (settings.open, brightness.open)."""
    return [("tap", bt.P_SETTINGS_ZONE, "settings.open"),
            ("tap", bt.P_BRIGHT_ROW, "brightness.open")]


def _qh_quiet(bt):
    return _qh_open(bt) + [_qh_tap("row")]


def _qh_back(bt):
    return ("tap", bt.P_BACK, "back")


def _qh_back_swipe(bt):
    return ("swipe", bt.SW_BACK, "back.swipe")


class _QhRun:
    def __init__(self, states, widgets, labels, diag):
        self.states, self.widgets, self.labels = states, widgets, labels
        self.diag = diag


def _qh_run(qh, renders, steps):
    """Play `steps` into a fresh harness and read everything back.

    A step is ("tap", (x, y), name), ("swipe", ((x0, y0), (x1, y1)), name),
    ("state", tag) or ("widgets", tag). Every stroke must be answered
    touch_done; each state and listing is kept under its tag; the labels are
    the screen as it was after the last step."""
    bt = qh["bt"]
    msgs, want = [], []
    for st in steps:
        if st[0] == "tap":
            msgs.append(bt.touch_tap(*st[1]))
            want.append(("touch", st[2]))
        elif st[0] == "swipe":
            (x0, y0), (x1, y1) = st[1]
            msgs.append(bt.touch_swipe(x0, y0, x1, y1))
            want.append(("touch", st[2]))
        elif st[0] == "state":
            msgs.append(bt.ui_state_query())
            want.append(("state", st[1]))
        elif st[0] == "widgets":
            for page in range(_QH_WIDGET_PAGES):
                msgs.append(bt.widgets_query(page))
                want.append(("widgets", st[1]))
        else:
            raise AssertionError(f"unknown step {st[0]!r}")
    renders.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env["QH_LABEL_DUMP"] = "1"
    p = subprocess.run([str(qh["exe"]), str(renders)],
                       input="".join(json.dumps(m) + "\n" for m in msgs),
                       capture_output=True, text=True, errors="replace",
                       timeout=_QH_RUN_TIMEOUT_S, env=env)
    replies, labels, dumped = [], [], False
    for ln in p.stdout.splitlines():
        if ln == "QH_DUMP_END":
            dumped = True
        elif ln.startswith("QH_LABEL "):
            _, path, hidden, x1, y1, x2, y2, hx = ln.split(" ")
            labels.append({
                "path": path, "hidden": hidden == "1",
                "x1": int(x1), "y1": int(y1), "x2": int(x2), "y2": int(y2),
                "text": bytes.fromhex(hx).decode("utf-8", "replace"),
            })
        elif ln.startswith("{"):
            try:
                obj = json.loads(ln)
            except ValueError:
                continue
            if obj.get("t") in ("touch_done", "touch_refused", "ui_state",
                                "widgets"):
                replies.append(obj)
    # The strokes played and the reply kinds: never the listing's labels
    # (the widget replies' text is read below, by the assertions).
    diag = (f"steps: {[s[-1] for s in steps]}\n"
            f"exit {p.returncode}; replies: {[r['t'] for r in replies]}\n"
            f"stderr:\n{_qh_tail(p.stderr)}")
    assert p.returncode == 0, diag
    assert dumped, "the label dump never ran\n" + diag
    assert len(replies) == len(want), diag
    states, widgets = {}, {}
    for (kind, tag), r in zip(want, replies):
        if kind == "touch":
            assert r["t"] == "touch_done" and r.get("ok") == 1, (
                f"stroke {tag} refused: {r.get('why')}\n{diag}")
        elif kind == "state":
            assert r["t"] == "ui_state", diag
            states[tag] = r
        else:
            rows = widgets.setdefault(tag, [])
            if r["t"] == "widgets":
                rows.extend(r.get("w", []))
    return _QhRun(states, widgets, labels, diag)


def _qh_button_at(rows, xy):
    """The listed button whose box holds xy, or None. Rows are touch_widgets'
    [i, kind, label, x, y, w, h, ax, ay, f]. The listing also has what lies
    under the open screen (the gauges' own buttons, listed as not hit), so a
    button LVGL's hit test reaches goes first, then the smallest."""
    x, y = xy
    hits = [r for r in rows if r[1] == "button"
            and r[3] <= x < r[3] + r[5] and r[4] <= y < r[4] + r[6]]
    hits.sort(key=lambda r: (not r[9] & W_HIT_FLAG, r[5] * r[6]))
    return hits[0] if hits else None


def _qh_checked(rows, name):
    b = _qh_button_at(rows, _QH_G["pts"][name])
    assert b is not None, f"no button under {name}"
    return bool(b[9] & W_CHECKED_FLAG)


def _qh_page(labels, title):
    """The visible labels of the page whose title is `title`: everything
    under the title's parent (bright_ov), the title included."""
    tops = [lb for lb in labels if lb["text"] == title and not lb["hidden"]
            and lb["y1"] < 40]
    assert len(tops) == 1, (
        f"wanted one {title!r} title, found {len(tops)}; titles up: "
        f"{[lb['text'] for lb in labels if lb['y1'] < 40 and not lb['hidden']]}")
    parent = tops[0]["path"].rsplit("/", 1)[0]
    return [lb for lb in labels if lb["path"].startswith(parent + "/")
            and not lb["hidden"]]


def _qh_row_value(labels):
    """The levels page's Quiet hours row: what its value label says."""
    page = _qh_page(labels, "Brightness")
    names = [lb for lb in page if lb["text"] == "Quiet hours"
             and lb["y1"] >= _QH_G["QUIET_ROW_Y"]]
    assert len(names) == 1, [lb["text"] for lb in page]
    row = names[0]["path"].rsplit("/", 1)[0]
    vals = [lb["text"] for lb in page
            if lb["path"].rsplit("/", 1)[0] == row and lb is not names[0]]
    assert len(vals) == 1, vals
    return vals[0]


def _qh_quiet_view(labels):
    """The Quiet hours page as read off its labels."""
    page = _qh_page(labels, "Quiet hours")
    h = _QH_G["QUIET_CTL_H"]

    def time_in(y):
        return [lb["text"] for lb in page if y <= lb["y1"] < y + h
                and _QH_HHMM.fullmatch(lb["text"])]

    return {
        "texts": [lb["text"] for lb in page],
        "from": time_in(_QH_G["QUIET_FROM_Y"]),
        "until": time_in(_QH_G["QUIET_UNTIL_Y"]),
        "hint": [lb["text"] for lb in page
                 if lb["y1"] >= _QH_G["QUIET_HINT_Y"]],
    }


def _qh_short_enough(texts):
    long = [t for t in texts if len(t) > MAX_COPY]
    assert not long, f"labels over {MAX_COPY} characters: {long}"


def test_quiet_touch_brightness_opens_with_the_row_reading_off(qh, tmp_path):
    bt = qh["bt"]
    r = _qh_run(qh, tmp_path / "png", _qh_open(bt) + [
        ("state", "open"), ("widgets", "open")])
    st = r.states["open"]
    assert (st["scr"], st["ov"], st["busy"], st["bl"]) == (
        "settings", "brightness", 0, 60), r.diag
    row = _qh_button_at(r.widgets["open"], _QH_G["pts"]["row"])
    assert row is not None and row[2] == "Quiet hours", r.diag
    assert row[9] & W_HIT_FLAG, "nothing reaches the Quiet hours row"
    # The five stops are still there, above it.
    for i in range(5):
        assert _qh_button_at(r.widgets["open"], bt.bright_stop(i)), i
    assert _qh_row_value(r.labels) == "Off"
    _qh_short_enough([lb["text"] for lb in _qh_page(r.labels, "Brightness")])


def test_quiet_touch_the_row_opens_the_quiet_page(qh, tmp_path):
    bt = qh["bt"]
    r = _qh_run(qh, tmp_path / "png", _qh_quiet(bt) + [
        ("state", "quiet"), ("widgets", "quiet")])
    st = r.states["quiet"]
    # The bench getter has no value of its own for the page.
    assert (st["scr"], st["ov"], st["busy"]) == (
        "settings", "brightness", 0), r.diag
    rows = r.widgets["quiet"]
    # Every aim point lands on the control it is named for, and LVGL's own
    # hit test reaches it there.
    for name in ("off", "on", "from-", "from+", "until-", "until+"):
        b = _qh_button_at(rows, _QH_G["pts"][name])
        assert b is not None, f"no button under {name}"
        assert b[9] & W_HIT_FLAG, f"{name} is covered"
    assert _qh_button_at(rows, _QH_G["pts"]["off"])[2] == "Off"
    assert _qh_button_at(rows, _QH_G["pts"]["on"])[2] == "On"
    back = _qh_button_at(rows, bt.P_BACK)
    assert back is not None and back[2] == "<", "no back arrow"
    # Off is the stub's stored state, and the page shows it.
    assert _qh_checked(rows, "off") and not _qh_checked(rows, "on")
    # The levels page went: no row, no stops. (bright_ov itself is listed,
    # as an obj named by its first label, which is now this page's title.)
    assert not [w for w in rows if w[1] == "button"
                and w[2] == "Quiet hours"]
    assert not [w for w in rows if w[5] == 56 and w[6] == 116]
    v = _qh_quiet_view(r.labels)
    for word in ("Quiet hours", "Off", "On", "From", "Until"):
        assert word in v["texts"], word
    assert v["texts"].count(_QH_MINUS) == 2, v["texts"]
    assert v["texts"].count(_QH_PLUS) == 2, v["texts"]
    assert (v["from"], v["until"]) == (["22:00"], ["07:00"])
    # stubs.c's net_time_local() has no time to give, so the page says it is
    # waiting for one; "Dims the screen..." cannot be reached on this harness.
    assert v["hint"] == [_QH_WAITS]
    assert _QH_DIMS not in v["texts"]
    assert not [lb for lb in r.labels if lb["text"] == "Brightness"
                and not lb["hidden"]], "the levels page's title survived"
    _qh_short_enough(v["texts"])


def test_quiet_touch_on_and_off_switch_the_setting(qh, tmp_path):
    bt = qh["bt"]
    r = _qh_run(qh, tmp_path / "png", _qh_quiet(bt) + [
        _qh_tap("on"), ("widgets", "on"), ("state", "on"),
        _qh_tap("on"), ("widgets", "on again"),
        _qh_tap("off"), ("widgets", "off"),
        _qh_tap("off"), ("widgets", "off again"), ("state", "off")])
    for tag, on in (("on", True), ("on again", True), ("off", False),
                    ("off again", False)):
        rows = r.widgets[tag]
        assert (_qh_checked(rows, "on"), _qh_checked(rows, "off")) == (
            on, not on), tag
    for tag in ("on", "off"):
        st = r.states[tag]
        # Still the page, and the switch leaves the owner's level alone.
        assert (st["ov"], st["busy"], st["bl"]) == ("brightness", 0, 60), (
            tag, r.diag)
    v = _qh_quiet_view(r.labels)
    assert (v["from"], v["until"], v["hint"]) == (
        ["22:00"], ["07:00"], [_QH_WAITS])


# (case, taps, From, Until): 30 minutes a press, both ways round midnight.
_QH_STEPS = [
    ("from plus", ["from+"], "22:30", "07:00"),
    ("from plus minus", ["from+", "from-"], "22:00", "07:00"),
    ("from to 23:30", ["from+"] * 3, "23:30", "07:00"),
    ("from 23:30 plus wraps", ["from+"] * 4, "00:00", "07:00"),
    ("from 00:00 minus wraps", ["from+"] * 4 + ["from-"], "23:30", "07:00"),
    ("until plus", ["until+"], "22:00", "07:30"),
    ("until plus minus", ["until+", "until-"], "22:00", "07:00"),
    ("until to 00:00", ["until-"] * 14, "22:00", "00:00"),
    ("until 00:00 minus wraps", ["until-"] * 15, "22:00", "23:30"),
    ("until 23:30 plus wraps", ["until-"] * 15 + ["until+"], "22:00",
     "00:00"),
]


@pytest.mark.parametrize("case,taps,want_from,want_until", _QH_STEPS,
                         ids=[c[0] for c in _QH_STEPS])
def test_quiet_touch_from_and_until_step(qh, tmp_path, case, taps,
                                         want_from, want_until):
    bt = qh["bt"]
    r = _qh_run(qh, tmp_path / "png", _qh_quiet(bt) + [
        _qh_tap(t) for t in taps] + [("state", "after")])
    assert r.states["after"]["ov"] == "brightness", r.diag
    v = _qh_quiet_view(r.labels)
    assert (v["from"], v["until"]) == ([want_from], [want_until]), case
    assert v["hint"] == [_QH_WAITS], case
    _qh_short_enough(v["texts"])


def test_quiet_touch_back_shows_the_window_on_the_row(qh, tmp_path):
    bt = qh["bt"]
    r = _qh_run(qh, tmp_path / "png", _qh_quiet(bt) + [
        _qh_tap("on"), _qh_tap("from+"), _qh_back(bt),
        ("state", "levels"), ("widgets", "levels"),
        ("tap", bt.bright_stop(0), "brightness.level1"), ("state", "level")])
    st = r.states["levels"]
    assert (st["scr"], st["ov"], st["busy"], st["bl"]) == (
        "settings", "brightness", 0, 60), r.diag
    rows = r.widgets["levels"]
    assert _qh_button_at(rows, _QH_G["pts"]["row"])[2] == "Quiet hours"
    assert not [w for w in rows if w[2] in ("Off", "On")
                and w[9] & W_HIT_FLAG]
    # The stops work after the round trip.
    assert r.states["level"]["bl"] == 20, r.diag
    assert r.states["level"]["ov"] == "brightness", r.diag
    assert _qh_row_value(r.labels) == "22:30 - 07:00"
    _qh_short_enough([lb["text"] for lb in _qh_page(r.labels, "Brightness")])


def test_quiet_touch_off_reads_off_on_the_row_and_keeps_the_times(qh,
                                                                  tmp_path):
    bt = qh["bt"]
    set_off = _qh_quiet(bt) + [_qh_tap("on"), _qh_tap("from+"),
                               _qh_tap("until+"), _qh_tap("off"),
                               _qh_back(bt), ("state", "levels")]
    r = _qh_run(qh, tmp_path / "levels", set_off)
    assert r.states["levels"]["ov"] == "brightness", r.diag
    assert _qh_row_value(r.labels) == "Off"
    # Off forgets nothing: the page opened again has the times it was left
    # with, and Off still chosen.
    r = _qh_run(qh, tmp_path / "again", set_off + [
        _qh_tap("row"), ("widgets", "again")])
    v = _qh_quiet_view(r.labels)
    assert (v["from"], v["until"]) == (["22:30"], ["07:30"])
    rows = r.widgets["again"]
    assert _qh_checked(rows, "off") and not _qh_checked(rows, "on")


def test_quiet_touch_the_back_swipe_on_the_page_goes_to_the_levels(qh,
                                                                   tmp_path):
    bt = qh["bt"]
    r = _qh_run(qh, tmp_path / "png", _qh_quiet(bt) + [
        _qh_tap("on"), _qh_back_swipe(bt), ("state", "swiped")])
    st = r.states["swiped"]
    assert (st["scr"], st["ov"], st["busy"]) == (
        "settings", "brightness", 0), r.diag
    assert _qh_row_value(r.labels) == "22:00 - 07:00"


# Out of Brightness from the Quiet hours page, two ways: the arrow twice, or
# the back swipe twice (the first goes to the levels, the second closes, as
# from the levels page). Then Brightness opened again.
_QH_CLOSES = [
    ("back arrow", "back"),
    ("back swipe", "swipe"),
]


@pytest.mark.parametrize("case,how", _QH_CLOSES,
                         ids=[c[0] for c in _QH_CLOSES])
def test_quiet_touch_closing_keeps_the_setting_and_reopens_on_the_levels(
        qh, tmp_path, case, how):
    bt = qh["bt"]
    out = _qh_back(bt) if how == "back" else _qh_back_swipe(bt)
    steps = _qh_quiet(bt) + [
        _qh_tap("on"), _qh_tap("from+"), out, ("state", "levels"),
        out, ("state", "closed"),
        ("tap", bt.P_BRIGHT_ROW, "brightness.open"), ("state", "reopened"),
        ("widgets", "reopened")]
    r = _qh_run(qh, tmp_path / "reopen", steps)
    assert r.states["levels"]["ov"] == "brightness", (case, r.diag)
    closed = r.states["closed"]
    assert (closed["scr"], closed["ov"], closed["busy"]) == (
        "settings", "none", 0), (case, r.diag)
    st = r.states["reopened"]
    assert (st["ov"], st["busy"]) == ("brightness", 0), (case, r.diag)
    rows = r.widgets["reopened"]
    assert _qh_button_at(rows, _QH_G["pts"]["row"])[2] == "Quiet hours", case
    assert not [w for w in rows if w[2] in ("Off", "On")
                and w[9] & W_HIT_FLAG], (
        f"{case}: Brightness reopened on the Quiet hours page")
    assert _qh_row_value(r.labels) == "22:30 - 07:00", case
    # And the page itself, opened from there, has what was set.
    r = _qh_run(qh, tmp_path / "page", steps + [
        _qh_tap("row"), ("widgets", "page")])
    v = _qh_quiet_view(r.labels)
    assert (v["from"], v["until"]) == (["22:30"], ["07:00"]), case
    rows = r.widgets["page"]
    assert _qh_checked(rows, "on") and not _qh_checked(rows, "off"), case
