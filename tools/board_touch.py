#!/usr/bin/env python3
"""Drive a bench board's touch panel from the PC, and check what it shows.

    tools/board_touch.py state
    tools/board_touch.py tap settings.open
    tools/board_touch.py raw tap 298 120
    tools/board_touch.py swipe 260 120 100 120 [MS]
    tools/board_touch.py script tools/bench/full_touch.txt
    tools/board_touch.py targets
    tools/board_touch.py widgets
    tools/board_touch.py click "Later"
    tools/board_touch.py click --index 7
    tools/board_touch.py script tools/bench/popups_touch.txt

Needs a board flashed with CONFIG_BLINK_TOUCH_INJECT (firmware/Kconfig has
the build line; tools/burn.sh --bench flashes it). Such a board takes
{"t":"touch"} lines and plays them into its touch controller's own input
device, so LVGL and ui_swipe see them as a finger; and it answers
{"t":"ui_state"} with the screen and overlay it is showing
(firmware/src/touch_inject.h). This is the other end: it stands in for the
daemon -- welcome, pongs, made-up usage every 60 s, so the board is "host
connected" and on its gauges -- and plays named actions whose coordinates come
from the firmware's layout code, asserting after each one that the screen it
was meant to lead to is the screen the board reports.

Run it with the repo's test venv or the Zephyr one: stdlib and pyserial.

THE PORT. Opened with a plain pyserial open -- port, baud and a read timeout,
nothing else. DTR and RTS are never touched: on this board's cross-coupled auto-reset
circuit, setting them before the open resets the chip (CLAUDE.md). It refuses
to start while anything else holds the port, or while a Blink daemon holds
its lock -- on macOS a /dev/cu.* node is not exclusive, and two writers on one
cable interleave. Stop the daemon first:

    launchctl bootout gui/$(id -u)/com.blink.bridge      # and afterwards:
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.blink.bridge.plist
    launchctl kickstart -k gui/$(id -u)/com.blink.bridge

WHAT IT PRINTS. Parsed fields and nothing else: ui_state's fields, theme_now's
id/why/h, theme_edit, touch_done, the numbers on a [mem] or [swipe] line. A
line it does not recognise -- including every other console line the board
prints -- is never echoed: with -v it prints as "unparsed line (N bytes)",
and either way it is counted in the summary. Three things are read from a
line logged under the lvgl module, and only these: the LVGL function that
logged it (printed only when it is on _LVGL_FUNCS, a short list of names
compiled into the firmware), whether it is the Zephyr glue's input-queue
warning, and whether it carries one of the fixed out-of-memory markers
(_POOL_DRY_MARKERS). The last two print as counts, and an out-of-memory hit
fails the run. CLAUDE.md's rule names no program,
and a bench tool that echoed the wire would be the easiest place to break
it.

WIDGETS. `widgets` lists every clickable object on the panel, popups
included, with the words on it; `click "Later"` taps one by those words and
`click --index N` by its place in the listing, both through the same real
input path a tap takes (firmware/src/touch_widgets.h). A label is matched in
memory, but printed only when it is text this repository already holds -- a
string literal in firmware/src or the harness, or a reason in pc/bridge.py,
as the firmware would cut it, with typed holes for numbers and versions
(LabelAllowList). Any other label prints as "label withheld, N chars": the
OTA failure notice carries the daemon's reason and the offer carries the
host's version string, and a decoded value off the wire is exactly what the
rule above forbids printing. A label of a shape the firmware does not write
fails the whole line, which is then counted and never shown. A disabled
widget is not clicked without --force, and before "Update now" or "Yes, do
it" the port is checked again for anyone else holding it.

DO NOT TOUCH THE PANEL DURING A RUN. A real finger is not locked out: its
reports mix with the injected ones (firmware/src/touch_inject.c), and the
run then fails, or passes, for reasons that are not the code's.

IT PUTS THE BOARD BACK. The theme, brightness and Focus the board had when
the run started are restored when it ends -- failed or interrupted runs
included. A built-in theme goes back as the save line the PC would send; a
custom one too, when ~/.blink/theme.json holds the colours the board's own
hash names, and otherwise by its tile, which reselects the record the board
still keeps. Brightness and Focus go back through the panel. --no-restore
leaves the board as the run left it.

USAGE IS MADE UP. The readings are fixed invented numbers, never anything read
from this machine (CLAUDE.md: never generate usage, never ship a captured
fixture). Every line goes out through protocol.encode_checked.

Exit status: 0 when every expectation held, 1 when any failed, 2 for a usage
error or a port it could not take.
"""
import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from pc import protocol  # noqa: E402
from pc.providers.base import NormalizedUsageFrame  # noqa: E402

BAUD = 115200
SCREEN_W, SCREEN_H = 320, 240

# The LVGL pool the board can actually hand out: CONFIG_LV_Z_MEM_POOL_SIZE
# 32768 less the heap's own 88 B of metadata, as every [mem] line's used+free
# sums to (firmware/src/mem_stats.h). It was 30632 of 30720 until 2026-10-28;
# both sizes need twelve free-list buckets, so the metadata did not change.
POOL_USABLE = 32680


def _pool_of(m):
    """The pool a [mem] line is out of, usable bytes.

    For the host harness's host-* lines, the host pool the line itself names
    (64 KB of eight-byte pointers, so its numbers are not the board's). For a
    board line, its own used+free, which is the pool less the heap's metadata
    (firmware/src/mem_stats.h) -- so a build with another pool size is judged
    against its own pool. Judged against the constant, a trace build's 27648 B
    pool read as 5120 B roomier than it was (fp/pool review, S1). A line whose
    used+free is below its own peak is not a pool reading at all; for that,
    the release figure."""
    if m["where"].startswith("host-") and m.get("pool"):
        return m["pool"]
    total = m.get("used", 0) + m.get("free", 0)
    if total > 0 and total >= m.get("peak", 0):
        return total
    return POOL_USABLE

# A finger's tap and swipe, as the board will play them (touch_inject_geom.h
# reports every 13 ms, the panel's median). 120 ms is a deliberate press;
# 260 ms over 160 px is a brisk swipe -- and well past both ui_swipe's 36 px
# and LVGL's 24 px gesture floor before the lift.
TAP_MS = 120
SWIPE_MS = 260

HEARTBEAT_S = 60       # the daemon's usage poll

# --- the firmware's layout, mirrored ---------------------------------------
#
# The #defines in firmware/src/ui_settings.c that place the controls. A
# mirror rather than a parse at run time so this file stays usable from a
# copy, and tests/test_board_touch.py reads each one back out of the C so the
# two cannot drift. The handful of positions that are literals in the C --
# the edge zones, the back button, the brightness stops -- are pinned by the
# same test in the calls that set them.
FW_LAYOUT = {
    "TILE_W": 144,
    "TILE_H": 71,
    "TILE_GAP": 8,
    "TILE_TOP": 46,
    "BRIGHT_STOPS": 5,
    "THEME_SLOTS": 6,
    "THEME_TILE_W": 144,
    "THEME_TILE_H": 56,
    "THEME_TILE_GAP": 6,
    "THEME_GRID_Y": 46,
    "FOCUS_BTN_W": 92,
    "FOCUS_BTN_GAP": 10,
    "FOCUS_BTN_H": 100,
    "FOCUS_BTN_Y": 56,
}
_L = FW_LAYOUT

# firmware/src/theme.h, the built-in ids in their tile order.
THEME_IDS = {
    "dark": 0, "neon": 1, "bold": 2, "mono": 3, "ocean": 4, "synthwave": 5,
    "forest": 6, "retro_lcd": 7, "cvd_safe": 8, "light": 9,
    "light_soft": 10, "light_ink": 11,
    # Only once a custom theme has been saved: the tile is not offered
    # before (theme_list in ui_settings.c).
    "custom": 12,
}


def rect_center(x, y, w, h):
    """The middle of an LVGL object, the way a finger aims at it."""
    return (x + w // 2, y + h // 2)


# The two invisible 44 x 150 strips on the gauge screen (mk_edge_zone,
# LV_ALIGN_RIGHT_MID / LEFT_MID): the right one opens Settings, the left one
# plays the eye clip.
P_SETTINGS_ZONE = rect_center(SCREEN_W - 44, (SCREEN_H - 150) // 2, 44, 150)
P_EYE_ZONE = rect_center(0, (SCREEN_H - 150) // 2, 44, 150)
# mk_back: 60 x 36 at (6, 2), the same on Settings and every screen in it.
P_BACK = rect_center(6, 2, 60, 36)
# mk_tile: tile k of four, two by two from (12, TILE_TOP) -- Brightness,
# Theme, then Focus and Update.
def _tile(k):
    x = 12 + (k % 2) * (_L["TILE_W"] + _L["TILE_GAP"])
    y = _L["TILE_TOP"] + (k // 2) * (_L["TILE_H"] + _L["TILE_GAP"])
    return rect_center(x, y, _L["TILE_W"], _L["TILE_H"])


P_BRIGHT_ROW, P_THEME_ROW, P_FOCUS_ROW = _tile(0), _tile(1), _tile(2)
# show_theme: the page button, 60 x 36 in the top right corner (-6, 2).
P_THEME_PAGE = rect_center(SCREEN_W - 6 - 60, 2, 60, 36)


def bright_stop(i):
    """show_bright: stop i of five, 56 x 116 at (12 + 60 i, 86); level
    20 + 20 i percent."""
    return rect_center(12 + i * 60, 86, 56, 116)


def slot_center(k):
    """mk_slot: theme slot k of six, two across, inside the grid at
    THEME_GRID_Y. Slot 5 is "Edit on computer" when the app can open the
    builder, which this tool always says it can."""
    x = 12 + (k % 2) * (_L["THEME_TILE_W"] + 8)
    y = _L["THEME_GRID_Y"] + (k // 2) * (_L["THEME_TILE_H"] +
                                         _L["THEME_TILE_GAP"])
    return rect_center(x, y, _L["THEME_TILE_W"], _L["THEME_TILE_H"])


def focus_btn(i):
    """show_focus: Off, Session, Weekly, side by side."""
    return rect_center(12 + i * (_L["FOCUS_BTN_W"] + _L["FOCUS_BTN_GAP"]),
                       _L["FOCUS_BTN_Y"],
                       _L["FOCUS_BTN_W"], _L["FOCUS_BTN_H"])


def theme_slot(theme_id):
    """(page, slot) of a theme on the theme screen: ids in order, five a
    page because the sixth slot is Edit (theme_per_page with caps theme)."""
    per = _L["THEME_SLOTS"] - 1
    return (theme_id // per, theme_id % per)


# Swipes. Each starts on the ground of the screen it is meant for, not on a
# control: a row or a brightness stop clears GESTURE_BUBBLE, so a back swipe
# that started on one would never reach the handler that closes the screen.
# y 230 is under every control on Settings and its three sub-screens (the
# lowest, the theme screen's last row, ends at 226). Settings' footer runs to
# 235, but it is two labels, and a label takes no press.
SW_LEFT = ((260, 120), (100, 120))       # gauges -> Settings; leaves the clip
SW_RIGHT = ((60, 120), (220, 120))       # gauges -> the eye clip
SW_BACK = ((40, 230), (200, 230))        # closes whatever Settings shows
# 30 px: past LVGL's 24 px gesture floor (tune_gestures), short of
# ui_swipe's 36 px one (UI_SWIPE_MIN_PX). See eye.exit_short.
SW_LEFT_SHORT = ((200, 120), (170, 120))
SHORT_SWIPE_MS = 130

FOCUS_NAMES = ("off", "session", "weekly")


class Target:
    """A named action: what to press, where it may be pressed from, and the
    state it should leave behind."""

    def __init__(self, name, kind, pts, needs, expect, settle, doc,
                 ms=None):
        self.name = name
        self.ms = ms                # stroke length; None is the default
        self.kind = kind            # "tap" | "swipe" | "theme"
        self.pts = pts
        self.needs = needs          # ui_state fields required beforehand
        self.expect = expect        # f(before) -> fields required after
        self.settle = settle        # seconds the screen keeps moving after
        self.doc = doc

    def points(self):
        return list(self.pts)


def _back_expect(before):
    if before.get("ov", "none") != "none":
        return {"scr": "settings", "ov": "none"}
    return {"scr": "gauges"}


def _targets():
    t = {}

    def add(name, kind, pts, needs, expect, settle, doc, ms=None):
        t[name] = Target(name, kind, pts, needs,
                         expect if callable(expect) else (lambda b, e=expect: e),
                         settle, doc, ms)

    gauges = {"scr": "gauges", "modal": "none"}
    on_settings = {"scr": "settings", "ov": "none", "modal": "none"}
    # Opening and closing Settings slide the whole screen and then mute
    # gestures for UI_SLIDE_MS * 6 (ui_settings.c do_open/do_close); the
    # settle covers both so the next stroke is not the one that gets muted.
    add("settings.open", "tap", [P_SETTINGS_ZONE], gauges,
        {"scr": "settings", "ov": "none"}, 1.6,
        "tap the right edge zone on the gauges")
    add("settings.swipe", "swipe", list(SW_LEFT), gauges,
        {"scr": "settings", "ov": "none"}, 1.6,
        "swipe left on the gauges")
    add("settings.back", "tap", [P_BACK], {"scr": "settings"}, _back_expect,
        1.6, "the back button: closes a sub-screen, else Settings")
    add("back.swipe", "swipe", list(SW_BACK), {"scr": "settings"},
        _back_expect, 1.6,
        "swipe right along the bottom: closes a sub-screen, else Settings")
    add("brightness.open", "tap", [P_BRIGHT_ROW], on_settings,
        {"ov": "brightness"}, 0.6, "the Brightness tile")
    for i in range(_L["BRIGHT_STOPS"]):
        add(f"brightness.level{i + 1}", "tap", [bright_stop(i)],
            {"ov": "brightness"}, {"ov": "brightness", "bl": 20 + 20 * i},
            0.3, f"brightness stop {i + 1} ({20 + 20 * i}%)")
    add("theme.open", "tap", [P_THEME_ROW], on_settings, {"ov": "theme"},
        0.6, "the Theme tile")
    add("theme.next_page", "tap", [P_THEME_PAGE], {"ov": "theme"},
        {"ov": "theme"}, 0.3, "the theme screen's page button")
    for name, tid in THEME_IDS.items():
        _, slot = theme_slot(tid)
        add(f"theme.{name}", "theme", [slot_center(slot)], {"ov": "theme"},
            {"ov": "theme", "theme": tid, "stored": tid}, 0.6,
            f"theme tile {name} (id {tid}), paging to it first")
    add("theme.edit_on_computer", "tap", [slot_center(5)], {"ov": "theme"},
        {"ov": "theme", "edit": "opened"}, 0.6,
        "the Edit on computer slot (answered ok 1, as a daemon would)")
    add("focus.open", "tap", [P_FOCUS_ROW], on_settings, {"ov": "focus"},
        0.6, "the Focus tile")
    for i, name in enumerate(FOCUS_NAMES):
        add(f"focus.{name}", "tap", [focus_btn(i)], {"ov": "focus"},
            {"ov": "focus", "focus": name}, 0.4, f"Focus: {name}")
    # The clip slides in and then holds a 250 ms entry mute (ui_anim.c).
    add("eye.swipe", "swipe", list(SW_RIGHT), gauges, {"scr": "eyes"}, 1.2,
        "swipe right on the gauges: the eye clip")
    add("eye.tap", "tap", [P_EYE_ZONE], gauges, {"scr": "eyes"}, 1.2,
        "tap the left edge zone: the eye clip")
    # ui_anim.c's gesture_cb leaves on LV_DIR_LEFT only. The injector hands
    # LVGL exactly the screen point it was given (touch_inject_geom.h), so
    # that is a leftward stroke on the screen.
    add("eye.exit", "swipe", list(SW_LEFT), {"scr": "eyes"},
        {"scr": "gauges"}, 1.6, "swipe left on the clip: back to the gauges")
    # The same exit, too short for ui_swipe to call it a swipe: LVGL's
    # gesture alone. The full one also reaches ui_swipe as a LEFT stroke
    # while the clip holds the mode loop, and swipe_cb used to latch
    # want_open from it once the clip's entry mute had run out, so Settings
    # opened as the clip returned (host harness, 2026-10-28). swipe_cb now
    # ignores strokes while the clip shows; this stays for a script that
    # wants an exit ui_swipe never sees.
    add("eye.exit_short", "swipe", list(SW_LEFT_SHORT), {"scr": "eyes"},
        {"scr": "gauges"}, 1.6,
        "a 30 px left swipe on the clip: LVGL's gesture, not ui_swipe's",
        ms=SHORT_SWIPE_MS)
    return t


TARGETS = _targets()


# --- the wire ---------------------------------------------------------------

def touch_tap(x, y, ms=TAP_MS):
    return {"t": "touch", "v": protocol.VERSION, "op": "tap", "x": int(x),
            "y": int(y), "ms": int(ms)}


def touch_swipe(x0, y0, x1, y1, ms=SWIPE_MS):
    return {"t": "touch", "v": protocol.VERSION, "op": "swipe", "x0": int(x0),
            "y0": int(y0), "x1": int(x1), "y1": int(y1), "ms": int(ms)}


def touch_press(x, y):
    return {"t": "touch", "v": protocol.VERSION, "op": "press", "x": int(x),
            "y": int(y)}


def touch_release():
    return {"t": "touch", "v": protocol.VERSION, "op": "release"}


def touch_dismiss():
    return {"t": "touch", "v": protocol.VERSION, "op": "dismiss"}


def touch_wake():
    return {"t": "touch", "v": protocol.VERSION, "op": "wake"}


def ui_state_query():
    return {"t": "ui_state", "v": protocol.VERSION}


def widgets_query(page=0):
    return {"t": "widgets", "v": protocol.VERSION, "page": int(page)}


def touch_click(index, gen, ms=TAP_MS, force=False):
    msg = {"t": "touch", "v": protocol.VERSION, "op": "click",
           "i": int(index), "gen": int(gen), "ms": int(ms)}
    if force:
        msg["force"] = 1        # press it even though it is disabled
    return msg


# --- the widget listing (firmware/src/touch_widgets.h) ----------------------
#
# The board's answer to {"t":"widgets","page":P}: every clickable object on
# the panel, four a line, each [i,"kind","label",x,y,w,h,ax,ay,f]. The
# firmware writes a label as printable ASCII with nothing to escape
# (touch_widgets_fmt.h, tw_label_copy), cut at 28; anything else in that
# position is not a label this firmware wrote, and the whole line is then
# treated as unparsed -- counted, never printed.

LABEL_MAX = 28
PER_PAGE = 4
WIDGET_KINDS = ("obj", "button", "switch", "slider", "list", "check",
                "screen", "layer")
W_CHECKED, W_DISABLED, W_HIT, W_EVENTS, W_TOP, W_SYS = 1, 2, 4, 8, 16, 32
# fullmatch, not match with $: `$` also matches before a trailing newline, so
# a 29-character label ending in one used to pass (review, S5).
_LABEL_RE = re.compile(r"[ !#-\[\]-~]{0,%d}" % LABEL_MAX)
# Kinds a person would call a button, preferred when a name matches more than
# one reachable widget: a box and a button inside it can both contain the
# words asked for, and the one meant is the one that can be pressed.
_PRESSABLE = ("button", "switch", "slider", "list", "check")
# The two presses that consent to an install (ui_settings.c, upd_prompt_show
# and show_install_confirm). Before either, the port is checked again: see
# Bench.click.
INSTALL_WORDS = ("Update now", "Yes, do it")
# How long the end of a run waits for the ota_flash an install press sets off
# (Bench.end_install). It is one main-loop pass behind the press on a board;
# 2 s is far past that and short enough not to be noticed.
INSTALL_FLASH_WAIT_S = 2.0


class Widget:
    __slots__ = ("i", "kind", "label", "x", "y", "w", "h", "ax", "ay", "f")

    def __init__(self, i, kind, label, x, y, w, h, ax, ay, f):
        self.i, self.kind, self.label = i, kind, label
        self.x, self.y, self.w, self.h = x, y, w, h
        self.ax, self.ay, self.f = ax, ay, f

    @property
    def hit(self):
        return bool(self.f & W_HIT)

    @property
    def disabled(self):
        return bool(self.f & W_DISABLED)

    def __repr__(self):
        # Never the label: a repr ends up in assertion messages and logs.
        return f"Widget({self.i}, {self.kind!r})"


def parse_widgets(obj):
    """A widgets reply as {page, n, more, gen, items}, or None if any part of
    it is not what touch_widgets.c writes."""
    page, n, more, gen = (obj.get(k) for k in ("page", "n", "more", "gen"))
    rows = obj.get("w")
    if not (_int_in(page, 0, 999) and _int_in(n, 0, 10000)
            and _int_in(more, 0, 1) and _int_in(gen, 0, 2 ** 32 - 1)
            and isinstance(rows, list) and len(rows) <= PER_PAGE):
        return None
    items = []
    for r in rows:
        if not (isinstance(r, list) and len(r) == 10):
            return None
        i, kind, label, x, y, w, h, ax, ay, f = r
        if not (_int_in(i, 0, 9999) and kind in WIDGET_KINDS
                and isinstance(label, str) and _LABEL_RE.fullmatch(label)
                and _int_in(x, 0, SCREEN_W - 1) and _int_in(y, 0, SCREEN_H - 1)
                and _int_in(w, 1, SCREEN_W) and _int_in(h, 1, SCREEN_H)
                and _int_in(ax, -1, SCREEN_W - 1)
                and _int_in(ay, -1, SCREEN_H - 1)
                and _int_in(f, 0, 63)):
            return None
        items.append(Widget(i, kind, label, x, y, w, h, ax, ay, f))
    return {"page": page, "n": n, "more": more, "gen": gen, "items": items}


# --- which labels may be printed --------------------------------------------
#
# CLAUDE.md: never log the contents of a serial buffer. A label is a decoded
# value off the wire, and some carry text the board was sent rather than text
# it was built with -- an OTA failure notice ends with the daemon's reason,
# the offer names whatever version the host offered. Shape is not enough: any
# 28 printable characters pass parse_widgets (review, M1).
#
# So a label is printed only when it is text this repository already holds:
# a string literal in firmware/src (and the host harness's own), or one of the
# daemon's reasons in pc/bridge.py, put through label_copy() -- a port of the
# firmware's tw_label_copy, held to the C by tests/test_board_touch_widgets.py
# -- exactly as the panel would show it. A literal with printf holes becomes a
# template: %d is a number, a %s listed in VERSION_HOLES is a version (or the
# plural "s"), and a literal with any other hole is no template at all, so a
# label built from it is never shown. Anything else prints as "label
# withheld, N chars". Read from the source at run time, never from a capture.
#
# A decision, recorded: the typed holes do print values the host sent -- the
# offered version, a page number -- when they fit the type (digits, or
# N.N.N). They are the reason a listing is readable at all, and a version or
# a count cannot carry session content; anything wider than the type is
# withheld (review r2, N1).
#
# Matching a click is a different question and stays on the label in memory:
# `click "X"` works on anything the board shows, it just never echoes it.

# Every firmware format whose %s is a version (or a plural "s"), by the whole
# literal as joined in the source. tests/test_board_touch_widgets.py checks
# each is still there.
VERSION_HOLES = {
    "Updated to %s": ("ver",),
    "Version %s is available.": ("ver",),
    "Version %s is available.\nThis also updates the app on your computer.":
        ("ver",),
    "Install version %s?\nThe screen restarts when done.": ("ver",),
    "Updating to %s...": ("ver",),
    "Downloading version %s...": ("ver",),
    "Install %s": ("ver",),
    "Blink %s": ("ver",),
    "Blink %s  |  App %s": ("ver", "ver"),
    "%d change%s since %s": ("plural", "ver"),
    "%d change%s": ("plural",),
}
_HOLE_RE = {
    "num": (re.compile(r"-?\d{1,5}"), re.compile(r"-?\d{0,5}")),
    "ver": (re.compile(r"\d{1,3}(\.\d{1,3}){2}"),
            re.compile(r"\d{0,3}(\.(\d{0,3}(\.\d{0,3})?)?)?")),
    "plural": (re.compile(r"s?"), re.compile(r"s?")),
}
# The LVGL symbols the panel's controls use, as their UTF-8 bytes (lv_symbol_def.h).
_LV_SYMBOLS = {"LV_SYMBOL_LEFT": b"\xef\x81\x93",
               "LV_SYMBOL_RIGHT": b"\xef\x81\x94",
               "LV_SYMBOL_OK": b"\xef\x80\x8c"}
_C_CONV = re.compile(rb"%(%|[-+ #0]*\d*(?:\.\d+)?(?:hh|h|ll|l|z|j|t)?([a-zA-Z]))")


def _label_map(raw, limit=None):
    """touch_widgets_fmt.h's tw_label_copy, byte for byte: printable ASCII
    through, " to ', \\ to /, newline and tab to space, the three symbols to
    < > *, any other UTF-8 character to one ?, other control bytes to ?. Cut
    at `limit` characters when given."""
    out = []
    i = 0
    while i < len(raw) and (limit is None or len(out) < limit):
        c = raw[i]
        if c == 0:
            break
        if c >= 0x80:
            seq = raw[i:i + 3]
            out.append({b"\xef\x81\x93": "<", b"\xef\x81\x94": ">",
                        b"\xef\x80\x8c": "*"}.get(seq, "?"))
            i += 1
            while i < len(raw) and (raw[i] & 0xC0) == 0x80:
                i += 1
            continue
        ch = chr(c)
        out.append("'" if ch == '"' else "/" if ch == "\\" else
                   " " if ch in "\n\t" else
                   "?" if c < 0x20 or c == 0x7F else ch)
        i += 1
    return "".join(out)


def label_copy(raw):
    """What the board puts on the wire for a label whose bytes are `raw`."""
    return _label_map(raw, LABEL_MAX)


def _c_unescape(body):
    """A C string literal's body as bytes, read as a C compiler reads it.

    Octal (\\302, one to three digits), hex (\\x, every hex digit that
    follows, as C takes them) and universal names (\\u, \\U, as UTF-8, the
    build's execution character set). The first version knew none of
    octal or \\u and took two digits of \\x, so "\\302\\260" -- a degree
    sign -- read as the text "302260" and anything spelt that way passed
    tests/test_panel_strings.py unseen (review of fp/audit-nice, NICE 4).
    An escape C would refuse, or one this does not know, raises ValueError
    rather than being guessed at."""
    out = bytearray()
    i = 0
    simple = {"n": 10, "t": 9, "r": 13, "\\": 92, '"': 34, "'": 39,
              "a": 7, "b": 8, "f": 12, "v": 11, "?": 63}
    while i < len(body):
        ch = body[i]
        if ch != "\\":
            out += ch.encode("utf-8")
            i += 1
            continue
        nxt = body[i + 1:i + 2]
        if nxt in simple:
            out.append(simple[nxt])
            i += 2
            continue
        m = re.match(r"[0-7]{1,3}", body[i + 1:])
        if m:
            v = int(m.group(0), 8)
            if v > 0xFF:
                raise ValueError(f"octal escape \\{m.group(0)} is out of"
                                 " range")
            out.append(v)
            i += 1 + len(m.group(0))
            continue
        if nxt == "x":
            m = re.match(r"[0-9a-fA-F]+", body[i + 2:])
            if not m:
                raise ValueError("\\x with no hex digits after it")
            if int(m.group(0), 16) > 0xFF:
                raise ValueError(f"\\x{m.group(0)} is more than a byte")
            out.append(int(m.group(0), 16))
            i += 2 + len(m.group(0))
            continue
        if nxt in ("u", "U"):
            n = 4 if nxt == "u" else 8
            digits = body[i + 2:i + 2 + n]
            if not re.fullmatch(r"[0-9a-fA-F]{%d}" % n, digits):
                raise ValueError(f"\\{nxt} needs {n} hex digits")
            cp = int(digits, 16)
            # C11 6.4.3: a universal name below U+00A0 is allowed only for
            # $, @ and `; the others are refused, as cc refuses them
            # (review of fp/audit-nice r2, NICE 7).
            if cp < 0xA0 and cp not in (0x24, 0x40, 0x60):
                raise ValueError(f"\\{nxt}{digits} names a character C"
                                 " spells only as itself")
            out += chr(cp).encode("utf-8")
            i += 2 + n
            continue
        raise ValueError(f"unknown escape \\{nxt}")
    return bytes(out)


_C_TOKEN = re.compile(r'"((?:[^"\\\n]|\\.)*)"|(LV_SYMBOL_\w+)|(\S)')


def c_literals(text):
    """Every string literal in C source `text`, adjacent ones joined and the
    LVGL symbols spelled as their bytes; comments and character constants
    left out."""
    text = re.sub(r"/\*.*?\*/|//[^\n]*", " ", text, flags=re.S)
    text = re.sub(r"'(?:[^'\\\n]|\\.)+'", " ", text)
    out, cur = [], None
    for m in _C_TOKEN.finditer(text):
        lit, sym, other = m.groups()
        if lit is not None or sym is not None:
            piece = _c_unescape(lit) if lit is not None else \
                _LV_SYMBOLS.get(sym, b"\xef\x80\x80")
            cur = piece if cur is None else cur + piece
        elif cur is not None:
            out.append(cur)
            cur = None
    if cur is not None:
        out.append(cur)
    return out


def _template(raw):
    """A literal's tokens: ("lit", char) and ("hole", kind), or None when it
    has a hole that is not a number and not a listed version."""
    holes = VERSION_HOLES.get(raw.decode("utf-8", "replace"))
    s_seen = 0
    toks = []
    pos = 0
    for m in _C_CONV.finditer(raw):
        toks += [("lit", c) for c in _label_map(raw[pos:m.start()])]
        pos = m.end()
        if m.group(1) == b"%":
            toks.append(("lit", "%"))
            continue
        conv = m.group(2)
        if conv in b"diu" and len(conv) == 1:
            toks.append(("hole", "num"))
        elif conv == b"s" and holes and s_seen < len(holes):
            toks.append(("hole", holes[s_seen]))
            s_seen += 1
        else:
            return None
    toks += [("lit", c) for c in _label_map(raw[pos:])]
    return tuple(toks)


def _tmatch(label, i, toks, j, prefix_ok):
    """Does label[i:] match toks[j:]? With prefix_ok, running out of label
    part way through is a match: the board cut it at LABEL_MAX."""
    if j == len(toks):
        return i == len(label)
    if i == len(label):
        # Out of label: fine if it was cut, or if all that is left are holes
        # that may be empty (the plural "s" of "1 change").
        return prefix_ok or all(k == "hole" and _HOLE_RE[v][0].fullmatch("")
                                for k, v in toks[j:])
    kind, v = toks[j]
    if kind == "lit":
        return label[i] == v and _tmatch(label, i + 1, toks, j + 1, prefix_ok)
    full, part = _HOLE_RE[v]
    for k in range(len(label) - i, -1, -1):
        seg = label[i:i + k]
        if full.fullmatch(seg) and _tmatch(label, i + k, toks, j + 1,
                                           prefix_ok):
            return True
        if prefix_ok and i + k == len(label) and part.fullmatch(seg):
            return True
    return False


class LabelAllowList:
    """The labels this repository can account for (see above)."""

    def __init__(self, root=None):
        root = Path(root) if root else ROOT
        raws = []
        # Headers too: the gauge screen's "BLINK" is a #define in
        # usage_layout.h, and a label from a macro is a literal all the same.
        fw = root / "firmware" / "src"
        for src in sorted(list(fw.glob("*.c")) + list(fw.glob("*.h"))):
            raws += c_literals(src.read_text(encoding="utf-8"))
        harness = root / "tools" / "panel_render" / "touch"
        for src in sorted(harness.glob("*.c")):
            raws += c_literals(src.read_text(encoding="utf-8"))
        raws += self._py_literals(root / "pc" / "bridge.py")
        self.plain = set()
        self.templates = set()
        for raw in raws:
            toks = _template(raw)
            if toks is None:
                continue
            if all(k == "lit" for k, _ in toks):
                text = "".join(v for _, v in toks)
                if text:
                    self.plain.add(text)
                    # ui_settings.c capitalises the first letter of the
                    # reason it puts under a failure notice.
                    self.plain.add(text[0].upper() + text[1:])
            else:
                self.templates.add(toks)
        self._plain_sorted = sorted(self.plain)
        self._cache = {}

    @staticmethod
    def _py_literals(path):
        import ast
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError):
            return []
        return [n.value.encode("utf-8") for n in ast.walk(tree)
                if isinstance(n, ast.Constant) and isinstance(n.value, str)]

    def _one(self, label, prefix_ok):
        if label in self.plain:
            return True
        if prefix_ok and any(p.startswith(label) for p in self._plain_sorted):
            return True
        return any(_tmatch(label, 0, t, 0, prefix_ok) for t in self.templates)

    def allowed(self, label):
        """True when `label` is text the repository holds, whole, cut at
        LABEL_MAX, or as two such texts joined by the space a newline
        becomes (a notice's sentence and the reason under it)."""
        if label in self._cache:
            return self._cache[label]
        cut = len(label) >= LABEL_MAX
        ok = label == "" or self._one(label, cut)
        if not ok:
            for p, ch in enumerate(label):
                if ch == " " and self._one(label[:p], False) and \
                        _tail_ok(label[p + 1:]) and \
                        (p + 1 == len(label) or self._one(label[p + 1:], cut)):
                    ok = True
                    break
        self._cache[label] = ok
        return ok


def _tail_ok(tail):
    """The second half of a joined label: nothing, or something that reads
    as a sentence -- four characters or more, a letter among them. Without
    this the tail matched the bare-hole templates (%d, %d:%d) and the one-
    and two-letter literals, so "Updated to 1.3.4 12345" and "OK OK" printed
    (review r2, N1): never free text, but wider than a reason under a notice
    needs to be."""
    return tail == "" or (len(tail) >= 4 and any(c.isalpha() for c in tail))


_ALLOW = None


def label_allow():
    """The allow-list, built once per run from the source beside this file."""
    global _ALLOW
    if _ALLOW is None:
        _ALLOW = LabelAllowList()
    return _ALLOW


def shown_label(label):
    """A label as it may be printed: itself when the repository accounts for
    it, otherwise only its length."""
    if label_allow().allowed(label):
        return repr(label)
    return f"<label withheld, {len(label)} chars>"


def reachable_matches(items, text, force=False):
    """The widgets `text` names that a finger can press, as (list, None), or
    ([], why).

    Exact label first, then a label that starts with it (labels are cut at
    LABEL_MAX, so a longer name is matched on its first LABEL_MAX), then one
    that contains it; the first of those that matches anything decides, and
    of what it matched, only what the hit test reaches and LVGL would take a
    press on (not disabled, unless forced) is kept. A reason names indices
    and kinds and the operator's own words, never a label."""
    q = text[:LABEL_MAX]
    tiers = (lambda l: l == text or (len(text) > LABEL_MAX and l == q),
             lambda l: l.startswith(q),
             lambda l: q in l)
    cands = []
    for tier in tiers:
        cands = [w for w in items if w.label and tier(w.label)]
        if cands:
            break
    if not cands:
        return [], f"no widget says {text!r}"
    reach = [w for w in cands if w.hit]
    if not reach:
        return [], (f"{text!r} is on the panel but covered "
                    f"(widget {', '.join(str(w.i) for w in cands)}): "
                    "something is on top of it")
    live = reach if force else [w for w in reach if not w.disabled]
    if not live:
        return [], (f"{text!r} is disabled (widget "
                    f"{', '.join(str(w.i) for w in reach)}): LVGL drops a "
                    "press on it; --force presses it anyway")
    return live, None


def find_widget(items, text, force=False):
    """The one widget `text` names, as (widget, None), or (None, why): of
    reachable_matches, a button over a box, and more than one left is
    ambiguous and refused, never guessed."""
    reach, why = reachable_matches(items, text, force)
    if not reach:
        return None, why
    if len(reach) > 1:
        press = [w for w in reach if w.kind in _PRESSABLE]
        if len(press) == 1:
            return press[0], None

        def one(w):
            lab = (" " + repr(w.label)
                   if label_allow().allowed(w.label) else "")
            return f"{w.i} {w.kind}{lab}"
        return None, (f"{text!r} names {len(reach)} widgets ("
                      + ", ".join(one(w) for w in reach)
                      + "); use click --index")
    return reach[0], None


def widget_table(items):
    """The listing as lines of text: index, kind, label (when allowed; see
    above), rectangle, aim and what the flags say."""
    width = LABEL_MAX + 2
    out = [f"  {'i':>3} {'kind':<7} {'label':<{width}} "
           f"{'x,y':>8} {'w x h':>9} {'aim':>9}  flags"]
    for w in items:
        aim = f"{w.ax},{w.ay}" if w.hit else "covered"
        fl = [n for b, n in ((W_CHECKED, "checked"), (W_DISABLED, "disabled"),
                             (W_EVENTS, "cb"), (W_TOP, "top"), (W_SYS, "sys"))
              if w.f & b]
        out.append(f"  {w.i:>3} {w.kind:<7} {shown_label(w.label):<{width}} "
                   f"{f'{w.x},{w.y}':>8} {f'{w.w}x{w.h}':>9} {aim:>9}  "
                   + " ".join(fl))
    return out


def _mem_wheres():
    """The screen names a [mem] line may carry: those firmware/src passes to
    mem_stats_report, and the host harness's host-* ones (harness.c,
    bench_mem: a modal, a sub-screen, or the screen)."""
    names = set()
    for src in (ROOT / "firmware" / "src").glob("*.c"):
        names.update(re.findall(r'mem_stats_report\("([a-z0-9-]+)"\)',
                                src.read_text(encoding="utf-8")))
    for w in (("eyes", "gauges", "settings") + _STATE_WORDS["modal"]
              + _STATE_WORDS["ov"]):
        names.add("host-" + w)
    return names


_WHERES = None


def shown_where(where):
    """A [mem] line's screen name as it may be printed (review, N6)."""
    global _WHERES
    if _WHERES is None:
        _WHERES = _mem_wheres()
    return where if where in _WHERES else f"<screen withheld, {len(where)} chars>"


# What ui_state may say, field by field (touch_inject.c, ui_state_line). A
# reply with anything outside this is not printed and not believed.
_STATE_WORDS = {
    "scr": ("gauges", "settings", "connecting", "eyes"),
    "ov": ("none", "brightness", "theme", "focus", "confirm"),
    "modal": ("none", "notice", "whatsnew", "prompt", "download"),
    "focus": FOCUS_NAMES,
    "edit": ("name", "waiting", "opened", "run"),
    "nt": ("none", "sleep", "ota", "other"),
    "gf": FOCUS_NAMES,
}
_STATE_INTS = {"busy": (0, 1), "theme": (0, 12), "stored": (0, 12),
               "tp": (0, 9), "bl": (0, 100), "slp": (0, 1)}
STATE_KEYS = ("scr", "ov", "modal", "busy", "theme", "stored", "tp",
              "focus", "bl", "edit", "slp", "nt", "gf")


def _int_in(v, lo, hi):
    return (isinstance(v, int) and not isinstance(v, bool)
            and lo <= v <= hi)


def parse_ui_state(obj):
    """The fields of a ui_state reply, or None if any is missing or not one
    of the values the firmware can send."""
    out = {}
    for k in STATE_KEYS:
        v = obj.get(k)
        if k in _STATE_WORDS:
            if v not in _STATE_WORDS[k]:
                return None
        elif not _int_in(v, *_STATE_INTS[k]):
            return None
        out[k] = v
    return out


_MEM_RE = re.compile(
    rb"^\[mem\] lvgl ([a-z][a-z0-9-]{0,23}): used=(\d{1,6}) free=(\d{1,6}) "
    rb"largest=(\d{1,6}) peak=(\d{1,6}) boot_peak=(\d{1,6}) pool=(\d{1,6})$")
_MEM_KEYS = ("used", "free", "largest", "peak", "boot_peak", "pool")
_SWIPE_RE = re.compile(
    rb"^\[swipe\] dx (-?\d{1,4}) dy (-?\d{1,4}) in (\d{1,6}) ms, "
    rb"(\d{1,5}) samples -> (\d)$")
# What an unparsed line is, in words this file chose, never the line itself:
# a Zephyr log line's level and module (the module only when it is one of
# these names, else "other"), a printk's leading [tag] (only tags
# firmware/src prints), else "other". Enough to say "LVGL is warning on every
# frame" without reading a byte of what it said.
_LOG_DROPPED_RE = re.compile(rb"--- (\d{1,9}) messages dropped ---")
# The shape of a Zephyr 4.3 log line, read from the source rather than
# guessed. subsys/logging/log_output.c:prefix_print() writes the timestamp
# first ("[%02u:%02u:%02u.%03u,%03u] ", timestamp_print), THEN the colour
# (color_prefix), THEN "<wrn> module: " (ids_print); postfix_print() ends it
# with the colour reset "\x1b[0m" and "\r\n". With
# CONFIG_LOG_BACKEND_SHOW_COLOR=y every level gets a code at the front -- red
# or yellow for err and wrn, the reset "\x1b[0m" for inf and dbg, whose
# colours this build leaves off. This pattern used to expect the colour before
# the timestamp, the order the board never sends, so every coloured line read
# as "other": the 2026-10-28 bench run filed ~1,700 lines that way and the
# question of what LVGL was warning about could not be answered. The hours
# field is %02u, so it widens past 99 h of uptime rather than wrapping.
_ZLOG_RE = re.compile(
    rb"^\[\d{2,5}:\d{2}:\d{2}\.\d{3},\d{3}\] (?:\x1b\[[0-9;]*m)?"
    rb"<(err|wrn|inf|dbg)> ([a-z][a-z0-9_]{0,31}):")
# A line that is nothing but colour codes. lv_log_add() (lvgl
# src/misc/lv_log.c) ends the text it hands to Zephyr with its own "\n", and
# log_output.c's out_func passes that through untouched, so the colour reset
# and CRLF that postfix_print() adds land on a line of their own: one LVGL
# warning reached this tool as two lines and was counted twice. The tail is
# part of the line before it, so it is folded in -- not counted, not shown.
_COLOUR_ONLY_RE = re.compile(rb"^(?:\x1b\[[0-9;]*m)+$")
# What follows "lvgl: " in a line LVGL itself logged. Zephyr's lvgl_log()
# (modules/lvgl/lvgl.c) strips LVGL's "[Warn]" and the one character after
# it, and with CONFIG_LV_LOG_USE_TIMESTAMP=y (this build) what is left starts
# "(12.345, +5)\t " -- LOG_TIMESTAMP_FMT, which only lv_log_add()
# (src/misc/lv_log.c) writes -- then the " %s: " that LV_LOG_WARN fills with
# __func__. The timestamp is REQUIRED: without it the first word of any
# Zephyr glue line under the same "lvgl" module (lvgl_pointer_input.c's
# "Could not put input data into queue", say) could be read as a function
# name, and the rule below would hold by the luck of how those lines are
# worded rather than by construction.
_LVGL_FUNC_RE = re.compile(
    rb"lvgl: \(\d{1,9}\.\d{3}, \+\d{1,9}\)\t ([^:\s]{1,64}): ")
# The function names worth telling apart, and the only ones ever printed:
# LVGL 9 functions that log an allocation failure, found by grepping the LVGL
# tree Zephyr 4.3.1 pins (modules/lib/gui/lvgl/src: core, draw, draw/sw,
# display, indev, font, misc, stdlib, and the label, arc and image widgets)
# for LV_ASSERT_MALLOC and for LV_LOG_WARN/ERROR text about memory, and
# taking the enclosing function (2026-10-28). It covers the allocators this
# firmware reaches on every frame -- slides, text updates, the usage rings --
# but not every allocator LVGL has, so a dry pool can still show as
# "(other LVGL function)". It is counted and fails the run all the same; the
# name only says where. A list rather than a shape check, because a list is
# airtight. tests/test_board_touch_widgets.py pins every name to a definition
# in that tree.
_LVGL_FUNCS = frozenset({
    # draw: buffers, layers, tasks
    "lv_draw_buf_create_ex", "lv_draw_buf_destroy", "lv_draw_layer_create",
    "lv_draw_layer_alloc_buf", "lv_draw_add_task", "lv_draw_create_unit",
    "cleanup_task",
    # software renderer: gradients (the theme chips draw LV_GRAD_DIR_HOR),
    # masks
    "allocate_item", "lv_draw_sw_grad_get", "lv_draw_sw_mask_radius_init",
    "circ_calc_aa4",
    # images and their cache
    "lv_image_decoder_create", "lv_image_decoder_post_process",
    "lv_cache_create", "lv_cache_entry_alloc", "alloc_cb",
    # objects, styles, refresh
    "lv_obj_class_create_obj", "lv_obj_allocate_spec_attr",
    "lv_obj_add_style", "lv_obj_style_create_transition", "get_local_style",
    "lv_obj_set_transform", "lv_obj_assign_id", "lv_style_init",
    "lv_style_register_prop", "refr_area", "lv_group_create",
    "lv_group_add_obj",
    # display, input, the allocator
    "lv_display_create", "lv_indev_create", "lv_realloc", "lv_mem_add_pool",
    # the busy ones (re-review of fp/fix-tools, SHOULD-1): every slide starts
    # animations, every reading sets label text, the rings are arcs
    "lv_anim_start", "lv_timer_create", "lv_label_set_text",
    "lv_label_ins_text", "lv_text_set_text_vfmt", "lv_draw_label",
    "lv_draw_label_iterate_characters", "lv_draw_unit_draw_letter",
    "lv_draw_sw_arc", "lv_draw_sw_box_shadow", "transform_and_recolor",
    "lv_image_set_src",
    # containers under them
    "lv_array_init", "lv_circle_buf_create", "rb_create_node",
    "lv_iter_create",
})
# Lines Zephyr's LVGL glue logs under the same module, with no LVGL
# timestamp or function: fixed messages from zephyr/modules/lvgl/input/*.c,
# each counted under a label of this file's. The queue one is the benign
# reading of M1 -- touches arriving faster than LVGL reads them during a long
# slide -- and it has to be told apart from a dry pool by a number, not left
# in a bucket with everything else.
_LVGL_GLUE = (
    (b"Could not put input data into queue", "input queue full"),
    (b"Could not put input data into keypad queue", "input queue full"),
)
# Every way LVGL 9 or its Zephyr glue says an allocation failed, as fixed
# substrings, matched case-folded; only how many lines carry one is printed.
# Derived from the source, not from memory: LV_LOG_WARN/ERROR messages
# grepped out of modules/lib/gui/lvgl/src and LOG_ERR out of
# zephyr/modules/lvgl (Zephyr 4.3.1, 2026-10-28).
#   "out of memory"  LV_ASSERT_MALLOC (src/misc/lv_assert.h), on with
#                    CONFIG_LV_USE_ASSERT_MALLOC=y: "Asserted at expression:
#                    new_task != NULL (Out of memory)" from lv_draw_add_task,
#                    lv_obj_class_create_obj and the rest. This is the line
#                    that actually reaches the console, because lv_malloc's
#                    own "couldn't allocate memory" is LV_LOG_INFO and is
#                    filtered at WARN. Also the decoders' plain "Out of
#                    memory".
#   "no memory"      lv_draw_buf.c's "No memory: WxH, ..." and the decoders'
#                    "No memory for ...".
#   "allocating layer buffer failed"   lv_draw.c.
#   "failed to allocate"   lv_draw_sw_grad.c's "Failed to allocate item for
#                    the gradient", the glue's "Failed to allocate memory for
#                    rendering buffer".
#   "malloc failed"  the cache and display classes.
#   "allocate memory"  "Could not allocate memory for ...", "couldn't
#                    allocate memory for ...".
#   "alloc failed", "allocation failed", "alloc grad item failed",
#   "max_memory was reached"   the draw buffer, file and layer variants.
# Any one means a screen drew with a failed allocation. With
# LV_ASSERT_HANDLER a no-op and CONFIG_ASSERT off, the next instruction uses
# the NULL, which on a customer's panel is a reboot -- so a hit fails the run
# (Bench.summary), and the exit code says so.
_POOL_DRY_MARKERS = (
    b"out of memory", b"no memory", b"allocating layer buffer failed",
    b"failed to allocate", b"malloc failed", b"allocate memory",
    b"alloc failed", b"allocation failed", b"alloc grad item failed",
    b"max_memory was reached",
)
_ASSERT_MARK = b"Asserted at expression"
_LOG_MODULES = frozenset({"lvgl", "os", "uart", "input", "xpt2046", "spi",
                          "gpio", "flash", "display", "mcuboot", "net",
                          "esp32", "heap", "kernel"})
_TAG_RE = re.compile(rb"^\[([a-z]{1,12})\] ")
_TAGS = None


def _printk_tags():
    global _TAGS
    if _TAGS is None:
        tags = set()
        for src in (ROOT / "firmware" / "src").glob("*.c"):
            tags.update(re.findall(r'"\[([a-z]{1,12})\] ',
                                   src.read_text(encoding="utf-8")))
        _TAGS = frozenset(tags)
    return _TAGS


def unparsed_kind(raw):
    """A category for an unparsed line, from this file's own words."""
    m = _ZLOG_RE.match(raw)
    if m:
        mod = m.group(2).decode("ascii")
        return (f"log {m.group(1).decode('ascii')} "
                f"{mod if mod in _LOG_MODULES else 'other'}")
    m = _TAG_RE.match(raw)
    if m and m.group(1).decode("ascii") in _printk_tags():
        return f"printk [{m.group(1).decode('ascii')}]"
    if raw.startswith(b"{"):
        return "json not understood"
    return "other"


def lvgl_source(raw):
    """(level, label) for a line logged under the lvgl module, else None.
    The label is an LVGL function on _LVGL_FUNCS, "(other LVGL function)",
    a glue label from _LVGL_GLUE, or "(other glue line)" -- never a word of
    the line that is not on one of those lists."""
    m = _ZLOG_RE.match(raw)
    if not m or m.group(2) != b"lvgl":
        return None
    level = m.group(1).decode("ascii")
    f = _LVGL_FUNC_RE.match(raw, m.start(2))
    if f:
        name = f.group(1)
        try:
            name = name.decode("ascii")
        except UnicodeDecodeError:
            name = ""
        return level, name if name in _LVGL_FUNCS else "(other LVGL function)"
    for mark, label in _LVGL_GLUE:
        if mark in raw:
            return level, f"(glue: {label})"
    return level, "(other glue line)"


def pool_dry(raw):
    """True when a line carries one of the out-of-memory markers."""
    low = raw.lower()
    return any(k in low for k in _POOL_DRY_MARKERS)


def input_queue_full(raw):
    """True for the Zephyr glue's input-queue overflow warning."""
    return any(mark in raw for mark, label in _LVGL_GLUE
               if label == "input queue full")


_INJECT_LINE = (b"[touch] INJECT BUILD: the PC can drive this panel"
                b" -- bench only, never ship this")
_HEX8 = re.compile(r"^[0-9a-f]{8}$")
_CAP_WORDS = frozenset({"svc", "theme"})
_TOUCH_OPS = ("tap", "swipe", "press", "release", "dismiss", "wake", "click")
_REFUSALS = ("busy", "args", "not_held", "stale", "covered", "range",
             "disabled")
_THEME_WHY = ("boot", "user", "saved")

MAX_RX_LINE = 2048     # longer than anything the board sends on purpose


# --- the script language ----------------------------------------------------

class ScriptError(ValueError):
    pass


class Step:
    def __init__(self, line, cmd, args):
        self.line = line
        self.cmd = cmd
        self.args = args

    def __repr__(self):
        return f"Step({self.line}, {self.cmd!r}, {self.args!r})"


def _ints(words, n_min, n_max, usage, lineno):
    if not n_min <= len(words) <= n_max:
        raise ScriptError(f"line {lineno}: usage: {usage}")
    try:
        vals = [int(w) for w in words]
    except ValueError:
        raise ScriptError(f"line {lineno}: usage: {usage}") from None
    return vals


def _on_panel(pts, lineno):
    for x, y in pts:
        if not (0 <= x < SCREEN_W and 0 <= y < SCREEN_H):
            raise ScriptError(f"line {lineno}: ({x}, {y}) is off the panel "
                              f"({SCREEN_W} x {SCREEN_H})")


# The commands that name a widget by its words, which have spaces in them:
# these lines are split shell-style, so `click "Update now"` is one name and a
# `#` inside the quotes is not a comment.
_QUOTED_CMDS = ("click", "expect-widget", "wait-widget", "widgets", "fake")
FAKE_KINDS = ("ota_avail", "ota_none", "ota_error", "boot_notice",
              "reverted_notice", "edit_done")


def _parse_quoted(cmd, rest, lineno):
    if cmd == "widgets":
        if rest:
            raise ScriptError(f"line {lineno}: usage: widgets")
        return Step(lineno, "widgets", ())
    if cmd == "click":
        # --force: press a disabled widget anyway (it is refused otherwise,
        # because LVGL would drop the press and the step would pass).
        forced = rest[:1] == ["--force"]
        extra = (True,) if forced else ()
        if forced:
            rest = rest[1:]
        if len(rest) == 2 and rest[0] in ("index", "--index"):
            try:
                return Step(lineno, "click_index", (int(rest[1]),) + extra)
            except ValueError:
                pass
        elif len(rest) == 1 and rest[0]:
            return Step(lineno, "click", (rest[0],) + extra)
        raise ScriptError(f'line {lineno}: usage: click [--force] "LABEL"  '
                          "or  click [--force] index N")
    if cmd == "expect-widget":
        if len(rest) == 1 and rest[0]:
            return Step(lineno, "expect_widget", (rest[0],))
        raise ScriptError(f'line {lineno}: usage: expect-widget "LABEL"')
    if cmd == "wait-widget":
        try:
            label, secs = rest
            if label:
                return Step(lineno, "wait_widget", (label, float(secs)))
        except ValueError:
            pass
        raise ScriptError(f'line {lineno}: usage: wait-widget "LABEL" S')
    # fake: a line the PC would not normally send, to raise a popup.
    kind = rest[0] if rest else ""
    if kind not in FAKE_KINDS:
        raise ScriptError(f"line {lineno}: usage: fake "
                          + "|".join(FAKE_KINDS) + " [ARG]")
    if kind == "ota_avail" and len(rest) in (2, 3):
        return Step(lineno, "fake", tuple(rest))
    if kind in ("ota_none", "reverted_notice", "edit_done") and \
            len(rest) == 1:
        return Step(lineno, "fake", tuple(rest))
    if kind == "ota_error":
        # The reason is the rest of the line, as the daemon's sentence is.
        return Step(lineno, "fake", (kind,) + ((" ".join(rest[1:]),)
                                               if rest[1:] else ()))
    if kind == "boot_notice" and len(rest) in (1, 2):
        return Step(lineno, "fake", tuple(rest))
    raise ScriptError(f"line {lineno}: usage: fake ota_avail VERSION [APP] | "
                      "fake ota_none | fake ota_error [WHY] | "
                      "fake boot_notice [FROM] | fake reverted_notice")


def parse_line(text, lineno=1):
    """One script line as a Step, or None for a blank line or a comment."""
    head = text.split("#", 1)[0].split()
    if head[:2] == ["fake", "ota_error"]:
        # The reason is the rest of the line as written, the way the
        # daemon's sentence is: shlex would call "it's late" an unbalanced
        # quote (review r2, N5). Quotes around the whole of it come off.
        rest = text.split("#", 1)[0].split(None, 2)[2:]
        why = rest[0].strip() if rest else ""
        if len(why) >= 2 and why[0] == why[-1] and why[0] in "'\"":
            why = why[1:-1]
        return Step(lineno, "fake", ("ota_error",) + ((why,) if why else ()))
    if head and head[0] in _QUOTED_CMDS:
        try:
            words = shlex.split(text, comments=True)
        except ValueError:
            raise ScriptError(f"line {lineno}: unbalanced quote") from None
        return _parse_quoted(words[0], words[1:], lineno)
    words = head
    if not words:
        return None
    cmd, rest = words[0], words[1:]
    if cmd in ("tap", "swipe") and len(rest) == 1:
        if rest[0] not in TARGETS:
            raise ScriptError(f"line {lineno}: unknown target {rest[0]!r} "
                              "(board_touch.py targets lists them)")
        return Step(lineno, "tap", (rest[0],))
    if cmd == "raw" and rest[:1] == ["tap"]:
        v = _ints(rest[1:], 2, 3, "raw tap X Y [MS]", lineno)
        _on_panel([v[:2]], lineno)
        return Step(lineno, "raw_tap", (v[0], v[1], v[2] if len(v) > 2
                                        else TAP_MS))
    if cmd == "swipe":
        v = _ints(rest, 4, 5, "swipe X0 Y0 X1 Y1 [MS]  or  swipe NAME", lineno)
        _on_panel([v[0:2], v[2:4]], lineno)
        return Step(lineno, "swipe", (v[0], v[1], v[2], v[3],
                                      v[4] if len(v) > 4 else SWIPE_MS))
    if cmd == "press":
        v = _ints(rest, 2, 2, "press X Y", lineno)
        _on_panel([v], lineno)
        return Step(lineno, "press", (v[0], v[1]))
    if cmd in ("release", "mem", "state") and not rest:
        return Step(lineno, cmd, ())
    if cmd == "harness-only":
        if rest:
            raise ScriptError(f"line {lineno}: usage: harness-only")
        return Step(lineno, "harness_only", ())
    if cmd == "wait":
        try:
            (s,) = rest
            return Step(lineno, "wait", (float(s),))
        except ValueError:
            raise ScriptError(f"line {lineno}: usage: wait S") from None
    if cmd == "expect":
        what = rest[0] if rest else ""
        if what == "notice":
            kinds = _STATE_WORDS["nt"]
            if len(rest) != 2 or rest[1] not in kinds:
                raise ScriptError(f"line {lineno}: usage: expect notice "
                                  + "|".join(kinds))
            return Step(lineno, "expect", ("notice", {"nt": rest[1]}))
        if what == "no":
            # The board must NOT say this, within QUIET_S of the last
            # action: a guard that holds is the absence of a line. ota_flash
            # is the install consent: "Update now" on an offer that changed
            # under it must not send one (full review #3, N2).
            if len(rest) != 2 or rest[1] not in ("theme_edit", "ota_flash"):
                raise ScriptError(f"line {lineno}: usage: expect no"
                                  " theme_edit|ota_flash")
            return Step(lineno, "expect_none", (rest[1],))
        if what not in ("ui_state", "theme_now", "theme_edit"):
            raise ScriptError(
                f"line {lineno}: usage: expect ui_state K=V ... | "
                "expect theme_now K=V ... | expect theme_edit | "
                "expect notice none")
        fields = {}
        for w in rest[1:]:
            k, sep, v = w.partition("=")
            if not sep or not k or not v:
                raise ScriptError(f"line {lineno}: expect wants key=value, "
                                  f"got {w!r}")
            fields[k] = v
        if what == "ui_state":
            bad = [k for k in fields if k not in STATE_KEYS]
            if bad:
                raise ScriptError(f"line {lineno}: ui_state has no field "
                                  f"{bad[0]!r}")
        return Step(lineno, "expect", (what, fields))
    raise ScriptError(f"line {lineno}: unknown command {cmd!r}")


def parse_script(text):
    steps = []
    for n, line in enumerate(text.splitlines(), 1):
        s = parse_line(line, n)
        if s is not None:
            steps.append(s)
    return steps


# --- the port ---------------------------------------------------------------

def port_holders(port, run=subprocess.run):
    """PIDs other than ours with `port` open (and its /dev/tty. twin on
    macOS), from lsof. None when lsof is not there to ask."""
    if run is subprocess.run and shutil.which("lsof") is None:
        return None
    paths = [port]
    if port.startswith("/dev/cu."):
        paths.append("/dev/tty." + port[len("/dev/cu."):])
    pids = set()
    for p in paths:
        r = run(["lsof", "-t", p], capture_output=True, text=True)
        for tok in (r.stdout or "").split():
            if tok.isdigit():
                pids.add(int(tok))
    pids.discard(os.getpid())
    return sorted(pids)


def take_daemon_lock():
    """Hold the daemon's single-instance lock (claude_usage_bridge.py,
    hold_single_instance) for as long as this runs. (handle, None), or
    (None, why) when a daemon already has it. A daemon that starts while we
    hold it waits instead of opening the port under us.

    Resolved here, not at import: a module-level home path would point at the
    real home inside the test sandbox (CLAUDE.md)."""
    try:
        import fcntl
    except ImportError:
        return None, None          # Windows: the port itself is exclusive
    from pc.cli import blink_home
    home = blink_home()
    # Made if it is not there, as hold_single_instance does. It used to
    # mean "no daemon has ever run here, so no lock", and a daemon that
    # was then installed or kickstarted mid-run took the lock and the port
    # beside this tool -- a /dev/cu.* node is not exclusive on macOS -- and
    # could answer the board's next check with a real offer (review, S2).
    # A home that cannot be written refuses the run with a sentence, as
    # hold_single_instance catches OSError too: without the lock nothing
    # keeps a daemon off the port (review r2, N3).
    try:
        os.makedirs(home, exist_ok=True)
        fh = open(os.path.join(home, "bridge.lock"), "a", encoding="utf-8")
    except OSError as e:
        return None, (f"cannot take the daemon's lock in {home} "
                      f"({type(e).__name__}); without it a daemon could "
                      "open the port beside this tool")
    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        fh.close()
        return None, ("a Blink daemon is running and has the board. Stop it "
                      "first (see the top of this file).")
    return fh, None


def autodetect():
    from claude_usage_bridge import candidate_ports
    ports = candidate_ports()
    return ports[0] if ports else None


def open_port(port, baud):
    import serial
    # Plain open, nothing pre-set: see "THE PORT" at the top.
    return serial.Serial(port, baud, timeout=0.2)


class Link:
    """The serial port, one writer at a time. The only write to the port in
    this file is here, and the only caller is Bench.send."""

    def __init__(self, ser):
        self.ser = ser
        self._wlock = threading.Lock()
        self._stop = False

    def write(self, raw):
        with self._wlock:
            self.ser.write(raw)

    def read_loop(self, on_line, on_overlong):
        buf = b""
        dropping = 0
        while not self._stop:
            try:
                chunk = self.ser.read(256)
            except Exception:
                return
            if not chunk:
                continue
            buf += chunk
            while True:
                i = buf.find(b"\n")
                if i < 0:
                    break
                line, buf = buf[:i], buf[i + 1:]
                if dropping:
                    on_overlong(dropping + len(line))
                    dropping = 0
                    continue
                on_line(line)
            if len(buf) > MAX_RX_LINE:
                dropping += len(buf)
                buf = b""

    def stop(self):
        self._stop = True


class HarnessLink:
    """tools/panel_render/touch.sh's harness in place of a board: the real
    Settings code on host LVGL, speaking this wire on its stdin and stdout,
    writing a PNG of each stroke into `renders`. Everything else -- targets,
    expectations, the summary -- is the same code that runs against a board,
    which is the point of it."""

    def __init__(self, path, renders):
        os.makedirs(renders, exist_ok=True)
        self.proc = subprocess.Popen(
            [path, renders], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)

    def write(self, raw):
        self.proc.stdin.write(raw)
        self.proc.stdin.flush()

    def note(self, name):
        """Name the next stroke's PNG after the target being pressed."""
        raw, _ = protocol.encode_checked(
            {"t": "note", "v": protocol.VERSION, "name": name})
        if raw is not None:
            self.write(raw)

    def read_loop(self, on_line, on_overlong):
        for line in self.proc.stdout:
            if len(line) > MAX_RX_LINE:
                on_overlong(len(line))
            else:
                on_line(line.rstrip(b"\n"))

    def stop(self):
        try:
            self.proc.stdin.close()
        except OSError:
            pass
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()

    def close(self):
        pass


# --- the bench --------------------------------------------------------------

def _fake_frames(now, n=0):
    """Two providers of invented numbers, so the board has both pages and a
    reason to show its gauges. Never a reading from this machine.

    The n-th heartbeat moves the session figures by a tenth of a percent
    each, cycling over a few points, the way a desk in use does. What keeps
    the board awake is the ages: observed_at and active_at are now, so the
    board's active age is 0 against the 4 h SLEEP_ABSENT_AFTER_S its stale
    doze waits for (sleep_gate.h), and the pongs keep host_lost false for
    the other doze. The movement is so the gauges are seen to update."""
    drift = (n % 30) / 10.0
    claude = NormalizedUsageFrame(
        provider="claude", src="bench", observed_at=now, active_at=now,
        session_pct=42.0 + drift, session_resets_at=now + 2 * 3600,
        weekly_pct=17.0, weekly_resets_at=now + 4 * 86400,
        state="running", n_run=1)
    codex = NormalizedUsageFrame(
        provider="codex", src="bench", observed_at=now, active_at=now,
        session_pct=63.0 + drift, session_resets_at=now + 3 * 3600,
        weekly_pct=28.0, weekly_resets_at=now + 5 * 86400,
        state="idle", n_idle=1)
    return claude, codex


def _utc_offset_min(now):
    lt = time.localtime(now)
    return int((lt.tm_gmtoff or 0) // 60)


class Bench:
    def __init__(self, link, out=None, settle_scale=1.0, verbose=False,
                 edit_reply=True):
        self.link = link
        self.out = out or sys.stdout
        self.scale = settle_scale
        self.verbose = verbose
        self.edit_reply = edit_reply
        self.board_caps = frozenset()
        self.failures = []
        self.mem_lines = []
        self._mem_seen = 0
        self.unparsed = 0
        self.unparsed_kinds = {}   # unparsed_kind() -> count
        self.lvgl_sources = {}     # "wrn lv_func" -> count (lvgl_source())
        self.pool_dry = 0          # lines with an LVGL out-of-memory marker
        self.pool_dry_asserts = 0  # ...of them, LV_ASSERT_MALLOC's shape
        self.input_queue_full = 0  # the glue's input-queue overflow lines
        self.log_dropped = 0       # messages Zephyr's log said it dropped
        self.withheld = 0
        self.events = []           # parsed, in arrival order
        self._mark = 0
        self._cv = threading.Condition()
        self._last_frames = None
        self._frames_sent = 0
        self.board_h = None        # the custom theme's hash, last theme_now
        self.walk_stats = None     # host harness: what the widget walks took
        self.force = False         # --force: click a disabled widget anyway
        # f() -> pids other than ours on the port, or None when it cannot be
        # asked; None here means there is no port (the host harness).
        self.port_check = None
        self.ota_flash_pending = False  # a consent nobody has answered
        # An install press landed (INSTALL_WORDS). Its ota_flash comes a loop
        # pass after the touch_done, behind the next ui_state reply, so a run
        # ending on the press can end before the line arrives (review r2, S1).
        self.install_pressed = False

    # -- output, parsed fields only --
    def say(self, text):
        print(text, file=self.out, flush=True)

    def fail(self, text):
        self.failures.append(text)
        self.say("FAIL " + text)

    # -- writing --
    def send(self, msg):
        raw, why = protocol.encode_checked(msg)
        if raw is None:
            self.say(f"not sent: {why}")
            self.withheld += 1
            return False
        self.link.write(raw)
        return True

    def greet(self):
        from pc.version import RELEASE_VERSION
        self.send(protocol.welcome("blink-bench", RELEASE_VERSION))

    def send_frames(self):
        now = time.time()
        claude, codex = _fake_frames(now, self._frames_sent)
        self._frames_sent += 1
        self.send(protocol.time_msg(int(now), _utc_offset_min(now)))
        self.send(protocol.frame_to_usage(claude, now, secondary=codex))
        # svc only to a board that listed it, as Bridge gates it.
        if protocol.BOARD_CAP_SVC in self.board_caps:
            self.send(protocol.svc("claude", "ok"))
            self.send(protocol.svc("codex", "ok"))
        self._last_frames = time.monotonic()

    def maybe_heartbeat(self):
        if (self._last_frames is not None and
                time.monotonic() - self._last_frames >= HEARTBEAT_S):
            self.send_frames()

    # -- reading --
    def _event(self, ev):
        with self._cv:
            self.events.append(ev)
            self._cv.notify_all()

    def _unparsed(self, n, raw=None):
        self.unparsed += 1
        kind = unparsed_kind(raw) if raw is not None else "over-long"
        self.unparsed_kinds[kind] = self.unparsed_kinds.get(kind, 0) + 1
        if raw is not None:
            src = lvgl_source(raw)
            if src is not None:
                key = " ".join(src)
                self.lvgl_sources[key] = self.lvgl_sources.get(key, 0) + 1
            # Only on a Zephyr log line: a [proto] printk can carry text the
            # host sent ("ota failed: %s"), which must not read as LVGL's.
            if _ZLOG_RE.match(raw) and pool_dry(raw):
                self.pool_dry += 1
                if _ASSERT_MARK in raw:
                    self.pool_dry_asserts += 1
            if input_queue_full(raw):
                self.input_queue_full += 1
        if self.verbose:
            self.say(f"unparsed line ({n} bytes, {kind})")

    def on_overlong(self, n):
        self._unparsed(n)

    def on_line(self, raw):
        raw = raw.rstrip(b"\r")
        if not raw or _COLOUR_ONLY_RE.match(raw):
            # Empty, or the colour-reset tail of an LVGL line that carried
            # its own newline (see _COLOUR_ONLY_RE): part of the line before.
            return
        if raw.startswith(b"{"):
            if not self._on_json(raw):
                self._unparsed(len(raw), raw)
            return
        m = _MEM_RE.match(raw)
        if m:
            ev = {"t": "[mem]", "where": m.group(1).decode("ascii")}
            for k, g in zip(_MEM_KEYS, m.groups()[1:]):
                ev[k] = int(g)
            self.mem_lines.append({k: v for k, v in ev.items() if k != "t"})
            self.say("[mem] {w}: peak={peak} used={used} free={free} "
                     "largest={largest} boot_peak={boot_peak}".format(
                         w=shown_where(ev["where"]), **ev))
            self._event(ev)
            return
        m = _SWIPE_RE.match(raw)
        if m:
            dx, dy, ms, n, d = (int(g) for g in m.groups())
            self.say(f"[swipe] dx={dx} dy={dy} ms={ms} samples={n} dir={d}")
            return
        if raw == _INJECT_LINE:
            self.say("board: touch inject build")
            return
        m = _LOG_DROPPED_RE.search(raw)
        if m:
            # Zephyr's deferred log threw messages away. Every protocol line
            # the board sends goes through it, so a reply this tool is
            # waiting on may be among them: said when it happens, with the
            # number, because it is the cause of a "no touch_done" that
            # follows (the 2026-10-28 board run lost 22 replies this way).
            n = int(m.group(1))
            self.log_dropped += n
            self.say(f"board log dropped {n} message(s): a reply may be "
                     "lost")
            self._event({"t": "log_dropped", "n": n})
            return
        self._unparsed(len(raw), raw)

    def _on_json(self, raw):
        try:
            obj = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
            return False
        t = obj["t"]
        if t == "ping":
            self.send(protocol.pong())
            return True
        if t == "ota_query":
            # As a daemon with nothing newer answers. Unanswered, the
            # board's own 30 s watchdog (main.c, OTA_UI_CHECKING) fails the
            # check and upd_timer_cb puts "Couldn't check for updates" over
            # the gauges -- the notice that stopped the first board run.
            self.say("ota_query (answered ota_none)")
            self.send(protocol.ota_none())
            return True
        if t == "ui_state":
            st = parse_ui_state(obj)
            if st is None:
                return False
            self.say("ui_state " + " ".join(f"{k}={st[k]}" for k in STATE_KEYS))
            self._event(dict(st, t="ui_state"))
            return True
        if t == "bench_walk":
            # The host harness only: what the widget walks have left in the
            # pool, worst case over every one so far (harness.c,
            # pool_sample). The walk must take nothing; the layout it runs
            # first does what the next refresh would have.
            vals = [obj.get(k) for k in ("walks", "grew", "rise", "layout")]
            if not all(_int_in(v, 0, 10 ** 7) for v in vals):
                return False
            walks, grew, rise, layout = vals
            self.walk_stats = {"walks": walks, "grew": grew, "rise": rise,
                               "layout": layout}
            self.say(f"host walk: {walks} walks, +{grew} B left and "
                     f"+{rise} B high-water by the walk, +{layout} B by the "
                     "layout before it")
            if grew or rise:
                self.fail(f"the widget walk took {max(grew, rise)} B "
                          "from the pool; it must take nothing")
            return True
        if t == "ota_flash":
            # The board's consent to an install. Never answered: a daemon
            # would run esptool now, and this tool installs nothing. A
            # `fake ota_error` takes the download screen down again.
            self.say("ota_flash (not answered: the bench installs nothing)")
            self.ota_flash_pending = True
            self._event({"t": "ota_flash"})
            return True
        if t == "widgets":
            page = parse_widgets(obj)
            if page is None:
                return False
            if self.verbose:
                self.say(f"widgets page {page['page']}: "
                         f"{len(page['items'])} of {page['n']}")
            self._event(dict(page, t="widgets"))
            return True
        if t == "touch_done":
            op, ok = obj.get("op"), obj.get("ok")
            if op not in _TOUCH_OPS or not _int_in(ok, 0, 1):
                return False
            auto = obj.get("auto") == 1
            self.say(f"touch_done op={op} ok={ok}" + (" auto=1" if auto else ""))
            self._event({"t": t, "op": op, "ok": ok})
            return True
        if t == "touch_refused":
            why = obj.get("why")
            if why not in _REFUSALS:
                return False
            self.say(f"touch_refused why={why}")
            self._event({"t": t, "why": why})
            return True
        if t == "theme_now":
            tid, why, h = obj.get("id"), obj.get("why"), obj.get("h")
            if not _int_in(tid, 0, 12) or why not in _THEME_WHY:
                return False
            if h is not None and not (isinstance(h, str) and _HEX8.match(h)):
                return False
            self.say(f"theme_now id={tid} why={why}" + (f" h={h}" if h else ""))
            ev = {"t": t, "id": str(tid), "why": why}
            if h:
                ev["h"] = h
            self.board_h = h
            self._event(ev)
            return True
        if t == "theme_edit":
            self.say("theme_edit")
            self._event({"t": t})
            # Answered as the daemon does: ok only to a board that can
            # theme, and only "ok 1" when we are told to pretend a browser
            # opened. No browser is opened here.
            if protocol.BOARD_CAP_THEME in self.board_caps:
                self.send(protocol.theme_edit_reply(self.edit_reply))
            else:
                self.send(protocol.theme_edit_reply(False))
            return True
        if t in ("hello", "pref"):
            caps = obj.get("caps")
            if caps is not None and not (
                    isinstance(caps, list)
                    and all(isinstance(c, str) for c in caps)):
                return False
            known = sorted(set(caps or ()) & _CAP_WORDS)
            self.board_caps = frozenset(known)
            self.say(f"{t} caps={','.join(known) or '-'}")
            self._event({"t": t})
            if t == "hello":
                # A board that just booted: greet it the way the daemon
                # answers a hello, so it is host-connected again.
                self.greet()
                self.send_frames()
            return True
        return False

    # -- waiting --
    def mark(self):
        with self._cv:
            self._mark = len(self.events)

    def _wait(self, pred, timeout):
        """Wait until pred(events since the mark) returns something, or the
        timeout; always looks at least once."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            with self._cv:
                got = pred(self.events[self._mark:])
                if got is not None:
                    return got
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.5))
            self.maybe_heartbeat()

    def sleep(self, seconds):
        end = time.monotonic() + seconds * self.scale
        while True:
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(left, 0.5))
            self.maybe_heartbeat()

    def query_state(self, timeout=2.0):
        with self._cv:
            n = len(self.events)
        if not self.send(ui_state_query()):
            return None
        deadline = time.monotonic() + max(0.0, timeout * self.scale)
        while True:
            with self._cv:
                for ev in self.events[n:]:
                    if ev["t"] == "ui_state":
                        return {k: ev[k] for k in STATE_KEYS}
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.5))
            self.maybe_heartbeat()

    def wait_state(self, want, timeout):
        """Poll ui_state until it carries every field of `want` (compared as
        text, the way a script spells them) and busy is 0."""
        deadline = time.monotonic() + max(0.0, timeout * self.scale)
        st = None
        while True:
            st = self.query_state()
            if st is not None and st["busy"] == 0 and all(
                    str(st.get(k)) == str(v) for k, v in want.items()):
                return True, st
            if time.monotonic() >= deadline:
                return False, st
            time.sleep(0.15)

    # -- acting --
    def stroke(self, msg, timeout=12.0):
        """Send one stroke and wait for the board to finish playing it."""
        self.mark()
        drops = self.log_dropped   # before the send: a drop can beat it back
        if not self.send(msg):
            self.fail(f"{msg['op']}: not sent")
            return False
        want_op = msg["op"]

        def done(evs):
            for ev in evs:
                if ev["t"] == "touch_refused":
                    return ev
                if ev["t"] == "touch_done" and ev["op"] == want_op:
                    return ev
            return None

        ev = self._wait(done, timeout)
        if ev is None:
            self.fail(f"{want_op}: " + self._lost_why(drops))
            return False
        if ev["t"] == "touch_refused":
            self.fail(f"{want_op}: refused ({ev['why']})")
            return False
        return True

    def _lost_why(self, drops_before):
        """Why a reply never came, as far as this side can tell. The first
        board run of the widget image blamed the image ("is this an inject
        build?") for 22 replies that Zephyr's log had dropped."""
        if self.log_dropped > drops_before:
            return ("no touch_done: the board's log dropped "
                    f"{self.log_dropped - drops_before} message(s) meanwhile, "
                    "and the reply was probably one of them")
        return ("no touch_done from the board -- is this a "
                "CONFIG_BLINK_TOUCH_INJECT image?")

    def _play(self, target, pts):
        if target.kind == "swipe":
            (x0, y0), (x1, y1) = pts
            return self.stroke(touch_swipe(x0, y0, x1, y1,
                                           target.ms or SWIPE_MS))
        (x, y), = pts
        return self.stroke(touch_tap(x, y))

    def _note(self, name):
        """Tell a harness link which target the next stroke is, for its
        PNG's name. A serial link has no such method and is told nothing."""
        note = getattr(self.link, "note", None)
        if note is not None:
            note(name)

    def _goto_theme_page(self, page):
        for _ in range(4):
            st = self.query_state()
            if st is None:
                return False
            if st["tp"] == page:
                return True
            x, y = P_THEME_PAGE
            self._note("theme.next_page")
            if not self.stroke(touch_tap(x, y)):
                return False
            self.sleep(0.3)
        return False

    def clear_the_way(self, st):
        """Wake a dozing board and take down a notice, saying so, before an
        action. Neither is a target's business: both are things a person at
        the desk would see and deal with before reaching for the panel, and
        each, left alone, turns every later press into a press on it. Returns
        the state afterwards (None if the board stopped answering)."""
        if st["slp"]:
            if not self.stroke(touch_wake()):
                return None
            ok, st = self.wait_state({"slp": 0}, timeout=8.0)
            if not ok:
                self.fail("the board would not wake")
                return None
            self.say("board woken")
        if st["modal"] == "notice":
            kind = st["nt"]
            if not self.stroke(touch_dismiss()):
                return None
            ok, st = self.wait_state({"modal": "none"}, timeout=2.0)
            if not ok:
                self.fail(f"the notice ({kind}) would not go")
                return None
            self.say(f"notice dismissed ({kind})")
        return st

    def run_target(self, name):
        t = TARGETS[name]
        before = self.query_state()
        if before is not None:
            before = self.clear_the_way(before)
        if before is None:
            self.fail(f"{name}: no ui_state from the board")
            return False
        miss = {k: v for k, v in t.needs.items() if str(before[k]) != str(v)}
        if miss:
            self.fail(f"{name}: needs " +
                      " ".join(f"{k}={v}" for k, v in t.needs.items()) +
                      ", board is at " +
                      " ".join(f"{k}={before[k]}" for k in t.needs))
            return False
        self.say(f"> {name}")
        if t.kind == "theme":
            tid = THEME_IDS[name.split(".", 1)[1]]
            page, _ = theme_slot(tid)
            if not self._goto_theme_page(page):
                self.fail(f"{name}: could not page to theme page {page + 1}")
                return False
        self._note(name)
        if not self._play(t, t.points()):
            return False
        want = t.expect(before)
        ok, st = self.wait_state(want, timeout=t.settle + 3.0)
        if not ok:
            got = (" ".join(f"{k}={st[k]}" for k in want) if st
                   else "no ui_state")
            self.fail(f"{name}: expected " +
                      " ".join(f"{k}={v}" for k, v in want.items()) +
                      f", board says {got}")
            return False
        # The screen keeps moving after it reports the new state: a slide
        # still running, a gesture mute still open (see each settle).
        self.sleep(t.settle)
        return True

    def expect_state(self, fields, timeout=3.0):
        ok, st = self.wait_state(fields, timeout)
        if not ok:
            got = (" ".join(f"{k}={st[k]}" for k in fields) if st
                   else "no ui_state")
            self.fail("expect ui_state " +
                      " ".join(f"{k}={v}" for k, v in fields.items()) +
                      f": board says {got}")
        return ok

    QUIET_S = 1.5

    def expect_none(self, kind):
        """Fail if the board sends `kind` within QUIET_S of the last
        action. Long enough for a tap's line to cross the wire on a board,
        which answers inside a few hundred ms."""
        def seen(evs):
            return next((ev for ev in evs if ev["t"] == kind), None)

        if self._wait(seen, self.QUIET_S * self.scale) is not None:
            self.fail(f"expect no {kind}: the board sent one")
            return False
        if kind == "ota_flash":
            # The install press this follows was refused, so there is no
            # consent for end_install to wait out and answer: left set, the
            # run would end by raising a failure notice nothing caused.
            self.install_pressed = False
        return True

    def expect_event(self, kind, fields, timeout=3.0):
        def seen(evs):
            for ev in evs:
                if ev["t"] == kind and all(
                        str(ev.get(k)) == str(v) for k, v in fields.items()):
                    return ev
            return None

        if self._wait(seen, timeout * self.scale) is None:
            self.fail(f"expect {kind} " + " ".join(
                f"{k}={v}" for k, v in fields.items()) +
                ": not seen since the last action")
            return False
        return True

    # -- widgets --
    def _widgets_page(self, page, timeout=2.0):
        with self._cv:
            n = len(self.events)
        if not self.send(widgets_query(page)):
            return None
        deadline = time.monotonic() + max(0.0, timeout * self.scale)
        while True:
            with self._cv:
                for ev in self.events[n:]:
                    if ev["t"] == "widgets" and ev["page"] == page:
                        return ev
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._cv.wait(min(left, 0.5))
            self.maybe_heartbeat()

    def _list(self):
        """((gen, [Widget]), None) for the whole panel, or (None, why). Page
        by page, and all of one generation: a page that comes back under
        another one means the panel changed while it was being read, so the
        listing starts again -- three times, then it is called a panel that
        will not hold still. Records no failure: a caller that polls may
        try again, and only it knows when trying is over (review, S3)."""
        # Not mid-transition: a listing taken while a slide or a queued
        # close is still running is of a screen that is about to go.
        self.wait_state({}, timeout=3.0)
        for _ in range(3):
            items, gen, page = [], None, 0
            while True:
                ev = self._widgets_page(page)
                if ev is None:
                    return None, ("no widgets reply from the board -- is this "
                                  "a CONFIG_BLINK_TOUCH_INJECT image with the "
                                  "widget walk?")
                if gen is None:
                    gen = ev["gen"]
                elif ev["gen"] != gen:
                    break
                items += ev["items"]
                if not ev["more"]:
                    return (gen, items), None
                page += 1
                if page > 999:
                    break
        return None, "the panel kept changing while its widgets were listed"

    def list_widgets(self):
        """(gen, [Widget]), or None with the failure recorded."""
        got, why = self._list()
        if got is None:
            self.fail(why)
        return got

    def show_widgets(self):
        got = self.list_widgets()
        if got is None:
            return False
        gen, items = got
        self.say(f"widgets: {len(items)} (gen {gen})")
        for line in widget_table(items):
            self.say(line)
        return True

    def _install_press_ok(self, w):
        """Before a press that consents to an install: is the port still
        ours alone? A daemon that started mid-run has the board's next
        ota_query to answer with a real offer, and would take the ota_flash
        this press sends as its cue to install a release over the bench
        image (review, S2). _connect checked once, at the start; this is the
        check at the moment it matters."""
        if self.port_check is None or w.label not in INSTALL_WORDS:
            return True
        holders = self.port_check()
        if holders is None:
            self.fail("refusing an install press: lsof is not here to say "
                      "who else holds the port")
            return False
        if holders:
            self.fail("refusing an install press: pid(s) "
                      f"{', '.join(map(str, holders))} also hold the port, "
                      "and a daemon there would install a release over this "
                      "bench image")
            return False
        return True

    def click(self, text=None, index=None, ms=TAP_MS, force=None):
        """Tap a widget by its words or by its index in a fresh listing,
        through the real input path (touch_widgets.c, tw_click_parse). A
        listing gone stale between reading it and pressing is read again
        once; a board busy with a transition is given a few seconds. What is
        said names the index, kind and aim -- and the label only by
        shown_label, and never for a click by index (review, M1, N7)."""
        force = self.force if force is None else force
        tries_busy = 10
        attempt = 0
        while True:
            attempt += 1
            got = self.list_widgets()
            if got is None:
                return False
            gen, items = got
            if index is not None:
                w = next((w for w in items if w.i == index), None)
                if w is None:
                    self.fail(f"click index {index}: no such widget "
                              f"({len(items)} listed)")
                    return False
                if w.disabled and not force:
                    self.fail(f"click index {index}: widget {w.i} is "
                              "disabled; LVGL drops a press on it (--force "
                              "presses it anyway)")
                    return False
                what = f"index {index}"
                self.say(f"> click {what}: widget {w.i} {w.kind} at "
                         f"{w.ax},{w.ay}")
            else:
                w, why = find_widget(items, text, force)
                if w is None:
                    self.fail(f"click {text!r}: {why}")
                    return False
                what = repr(text)
                self.say(f"> click {what}: widget {w.i} {w.kind} "
                         f"{shown_label(w.label)} at {w.ax},{w.ay}")
            if not self._install_press_ok(w):
                return False
            self._note(f"click-{w.i}")
            self.mark()
            drops = self.log_dropped
            if not self.send(touch_click(w.i, gen, ms, force)):
                self.fail(f"click {what}: not sent")
                return False

            def done(evs):
                for ev in evs:
                    if ev["t"] == "touch_refused":
                        return ev
                    if ev["t"] == "touch_done" and ev["op"] == "click":
                        return ev
                return None

            ev = self._wait(done, 12.0)
            if ev is None:
                self.fail(f"click {what}: " + self._lost_why(drops))
                return False
            if ev["t"] == "touch_done":
                if w.label in INSTALL_WORDS:
                    self.install_pressed = True
                # The press is in; let what it started (a slide, a screen
                # load) finish before the next step looks (review, N3).
                self.wait_state({}, timeout=3.0)
                return True
            if ev["why"] == "stale" and attempt == 1:
                self.say("listing went stale before the press; reading again")
                continue
            if ev["why"] == "busy" and tries_busy > 0:
                tries_busy -= 1
                time.sleep(0.3)
                continue
            self.fail(f"click {what}: refused ({ev['why']})")
            return False

    def wait_widget(self, text, timeout):
        """Until a reachable widget says `text`, or the timeout: the first
        such widget, or None. Polls, and records a failure only at the
        deadline; expect-widget is this with a short timeout."""
        deadline = time.monotonic() + max(0.0, timeout * self.scale)
        why = "no listing"
        while True:
            got, lwhy = self._list()
            if got is not None:
                reach, why = reachable_matches(got[1], text, force=True)
                if reach:
                    return reach[0]
            else:
                why = lwhy
            if time.monotonic() >= deadline:
                self.fail(f"widget {text!r}: {why}")
                return None
            time.sleep(0.3)

    def fake(self, kind, *args):
        """A line that raises a popup a run cannot otherwise reach. The OTA
        ones are real protocol lines, the board's own answer path: an offer,
        a check that found nothing, a failure. Pressing "Update now" after
        one sends ota_flash to this tool, which never answers it -- nothing
        is written to the board -- and a later `fake ota_error` takes the
        download screen down the way a daemon that gave up would; so does
        the restore, if the run ends first. The two notices are the
        harness's alone: only a boot after a real update raises them on a
        board."""
        if kind == "ota_avail":
            ver = args[0]
            app = args[1] if len(args) > 1 else None
            # Invented size and digest: nothing will ever be downloaded.
            return self.send(protocol.ota_avail(ver, 1_200_000, "0" * 64,
                                                app=app))
        if kind == "ota_none":
            return self.send(protocol.ota_none())
        if kind == "edit_done":
            # The daemon's word that the builder it opened has ended: a
            # real protocol line, so a board takes it as well as the
            # harness does.
            return self.send(protocol.theme_edit_done())
        if kind == "ota_error":
            self.ota_flash_pending = False
            self.install_pressed = False
            return self.send(protocol.ota_error(args[0] if args else ""))
        if getattr(self.link, "note", None) is None:
            self.fail(f"fake {kind}: the host harness only (--harness); a "
                      "board raises it only on the boot after an update")
            return False
        msg = {"t": "bench_notice", "v": protocol.VERSION,
               "kind": "reverted" if kind == "reverted_notice" else "update"}
        if kind == "boot_notice" and args:
            msg["from"] = args[0]
        return self.send(msg)

    def end_install(self):
        """A consent the bench never answered leaves the board on the
        download screen, which has no timeout of its own and holds off the
        doze until a power cycle (review, S2). Answered as a daemon that gave
        up would: ota_error, then ota_none, so the board goes back to having
        nothing on offer.

        After an install press whose ota_flash has not been seen, it is
        waited for, up to INSTALL_FLASH_WAIT_S: on a board main.c prints it
        on the loop pass after the one that answered the bench's ui_state,
        so a run that ends on the press used to find nothing pending and
        leave the board waiting (review r2, S1). Then the two lines go out
        either way: on a board that never reached the download screen they
        only put up a failure notice, on a bench board."""
        if not (self.ota_flash_pending or self.install_pressed):
            return
        if not self.ota_flash_pending:
            self._wait(lambda evs: next(
                (e for e in evs if e["t"] == "ota_flash"), None),
                INSTALL_FLASH_WAIT_S)
        self.say("ending the install the board was left waiting on")
        self.send(protocol.ota_error("the bench run ended"))
        self.sleep(0.5)
        self.send(protocol.ota_none())
        self.ota_flash_pending = False
        self.install_pressed = False
        self.sleep(0.5)

    def report_mem(self):
        new = self.mem_lines[self._mem_seen:]
        self._mem_seen = len(self.mem_lines)
        if not new:
            self.say("mem: no [mem] line since the last one")
        for m in new:
            pool = _pool_of(m)
            self.say(f"mem {shown_where(m['where'])}: peak {m['peak']} "
                     f"of {pool} "
                     f"({100.0 * m['peak'] / pool:.1f}%), "
                     f"largest free {m['largest']}")

    def run_step(self, s):
        if s.cmd == "tap":
            return self.run_target(s.args[0])
        if s.cmd == "raw_tap":
            return self.stroke(touch_tap(*s.args))
        if s.cmd == "swipe":
            return self.stroke(touch_swipe(*s.args))
        if s.cmd == "press":
            return self.stroke(touch_press(*s.args))
        if s.cmd == "release":
            return self.stroke(touch_release())
        if s.cmd == "wait":
            self.sleep(s.args[0])
            return True
        if s.cmd == "mem":
            # The harness prints a [mem] line only when asked (its pool is
            # the host's; see harness.c), and says what the walks took.
            if getattr(self.link, "note", None) is not None:
                self.send({"t": "bench_mem", "v": protocol.VERSION})
            # Give a [mem] line printed by the last action a moment to land.
            self.sleep(0.3)
            self.report_mem()
            return True
        if s.cmd == "state":
            return self.query_state() is not None
        if s.cmd == "widgets":
            return self.show_widgets()
        if s.cmd == "click":
            return self.click(text=s.args[0], force=len(s.args) > 1 or None)
        if s.cmd == "click_index":
            return self.click(index=s.args[0], force=len(s.args) > 1 or None)
        if s.cmd == "expect_widget":
            return self.wait_widget(s.args[0], 3.0) is not None
        if s.cmd == "wait_widget":
            return self.wait_widget(*s.args) is not None
        if s.cmd == "fake":
            self.mark()
            return self.fake(*s.args)
        if s.cmd == "harness_only":
            # A script whose test is only meaningful on the host harness's
            # clock. On a board it would run and pass without testing
            # anything (eye_mute_touch.txt: the board's entry slide
            # swallows the second stroke), so it stops here instead
            # (review of fp/audit-nice r2, NICE 8).
            if getattr(self.link, "note", None) is None:
                self.fail("this script runs on the host harness only"
                          " (--harness); on a board it would test nothing")
                return False
            return True
        if s.cmd == "expect_none":
            return self.expect_none(s.args[0])
        if s.cmd == "expect":
            kind, fields = s.args
            if kind in ("ui_state", "notice"):
                return self.expect_state(fields)
            return self.expect_event(kind, fields)
        raise AssertionError(s.cmd)

    def run_script(self, steps, keep_going=False):
        """Run each step. Stops at the first failure unless keep_going:
        every later target's precondition is the screen the failed one was
        meant to reach, so carrying on reports one fault twenty times."""
        for s in steps:
            if s.cmd in ("tap", "raw_tap", "swipe", "press", "release",
                         "click", "click_index"):
                self.mark()
            n = len(self.failures)
            self.run_step(s)
            if len(self.failures) > n:
                self.failures[-1] = f"line {s.line}: {self.failures[-1]}"
                if not keep_going:
                    self.say(f"stopped at line {s.line} "
                             "(--keep-going runs the rest)")
                    return

    def run_with_restore(self, steps, keep_going=False, restore=True):
        """run_script, with the board put back as it was found afterwards,
        however the script ends."""
        start = self.query_state()
        start_h = self.board_h
        try:
            self.run_script(steps, keep_going=keep_going)
        finally:
            # Even with --no-restore: the download screen is not a setting
            # the run chose, it is a board left waiting for a daemon.
            self.end_install()
            if restore and start is not None:
                self.restore(start, start_h=start_h)

    def restore(self, start, start_h=None):
        now = self.query_state()
        if now is None:
            self.fail("restore: no ui_state from the board")
            return False
        now = self.clear_the_way(now)
        plan = restore_plan(start, now, start_h) if now else []
        if not plan:
            self.say("restore: nothing to put back")
            return True
        self.say("restore: theme {} bl {} focus {}".format(
            start["stored"], start["bl"], start["focus"]))
        n = len(self.failures)
        # From wherever the run stopped to the gauges, which every UI step
        # of the plan starts from.
        for _ in range(4):
            st = self.query_state()
            if st is None or st["scr"] == "gauges":
                break
            if st["scr"] == "eyes":
                self.run_target("eye.exit_short")
            elif st["scr"] == "settings":
                self.run_target("settings.back")
            else:
                break
        for kind, what in plan:
            if kind == "line":
                self.mark()
                self.send(what)
                self.expect_event("theme_now",
                                  {"why": "saved", "id": str(what["id"])})
            elif not self.run_target(what):
                break
        for i in range(n, len(self.failures)):
            self.failures[i] = "restore: " + self.failures[i]
        return len(self.failures) == n

    def summary(self):
        peaks = {}
        for m in self.mem_lines:
            p = peaks.setdefault(m["where"], m)
            if m["peak"] > p["peak"]:
                peaks[m["where"]] = m
        self.say("")
        self.say(f"[mem] peak per screen, of the pool each line reports "
                 f"({POOL_USABLE} B usable on a release board; host-* lines: "
                 "of the host pool they name):")
        self.say(f"  {'screen':<18}{'peak':>7}{'of pool':>9}{'headroom':>10}")
        for where, m in peaks.items():
            pool = _pool_of(m)
            self.say(f"  {shown_where(where):<18}{m['peak']:>7}"
                     f"{100.0 * m['peak'] / pool:>8.1f}%"
                     f"{pool - m['peak']:>10}")
        if not peaks:
            self.say("  (no [mem] lines seen)")
        self.say(f"unparsed lines: {self.unparsed} (never printed)")
        if self.unparsed_kinds:
            self.say("  by kind: " + ", ".join(
                f"{k} {v}" for k, v in sorted(
                    self.unparsed_kinds.items(), key=lambda kv: -kv[1])))
        if self.lvgl_sources:
            self.say("  LVGL lines by function: " + ", ".join(
                f"{k} {v}" for k, v in sorted(
                    self.lvgl_sources.items(), key=lambda kv: -kv[1])))
        self.say(f"input queue full (Zephyr LVGL input glue): "
                 f"{self.input_queue_full}")
        if self.pool_dry:
            self.say(f"LVGL OUT OF MEMORY: {self.pool_dry} line(s), "
                     f"{self.pool_dry_asserts} of them LV_ASSERT_MALLOC "
                     "-- a screen drew with a failed allocation")
            # A failure, not a remark: the run must not end "all
            # expectations held", rc 0, over a pool that ran dry. Recorded
            # once, however often the summary is printed.
            oom = (f"LVGL out of memory: {self.pool_dry} line(s) carried an "
                   "allocation-failure marker")
            self.failures = [f for f in self.failures
                             if not f.startswith("LVGL out of memory: ")]
            self.failures.append(oom)
        else:
            self.say("LVGL out-of-memory lines: 0")
        if self.log_dropped:
            self.say(f"the board's log dropped {self.log_dropped} "
                     "message(s); a reply that never came was probably one "
                     "of them")
        if self.withheld:
            self.say(f"lines not sent (over the line limit): {self.withheld}")
        if self.failures:
            self.say(f"{len(self.failures)} expectation(s) FAILED:")
            for f in self.failures:
                self.say("  " + f)
        else:
            self.say("all expectations held")


def _saved_custom_matching(h):
    """The custom theme ~/.blink/theme.json holds, if it is the one the
    board's hash names; None otherwise. Read, never written."""
    if not h:
        return None
    from pc import theme as pc_theme
    from pc.cli import blink_home
    t = pc_theme.saved_custom(blink_home())
    if t is None or pc_theme.theme_hash(t) != h:
        return None
    return t


def restore_plan(start, now, start_h=None):
    """What puts the board back from `now` to `start`: ("line", msg) for a
    theme line, ("target", name) for a press, in order."""
    from pc import theme as pc_theme
    plan, ui = [], []
    tid = start["stored"]
    if now["stored"] != tid:
        if tid != THEME_IDS["custom"]:
            plan.append(("line", pc_theme.message("save", {"id": tid})))
        else:
            t = _saved_custom_matching(start_h)
            if t is not None:
                plan.append(("line", pc_theme.message("save", t)))
            else:
                ui += ["theme.open", "theme.custom", "settings.back"]
    if now["bl"] != start["bl"]:
        k = min(5, max(1, round(start["bl"] / 20)))
        ui += ["brightness.open", f"brightness.level{k}", "settings.back"]
    if now["focus"] != start["focus"]:
        ui += ["focus.open", f"focus.{start['focus']}", "settings.back"]
    if ui:
        plan += ([("target", "settings.open")] +
                 [("target", n) for n in ui] +
                 [("target", "settings.back")])
    return plan


# --- main -------------------------------------------------------------------

def _connect(args):
    port = args.port or autodetect()
    if not port:
        print("no board found; pass --port", file=sys.stderr)
        return None
    holders = port_holders(port)
    if holders:
        print(f"{port} is held by pid(s) {', '.join(map(str, holders))}; "
              "stop that first (the daemon: see the top of this file)",
              file=sys.stderr)
        return None
    if holders is None:
        print("warning: lsof not found; cannot check who holds the port",
              file=sys.stderr)
    lock, why = take_daemon_lock()
    if why:
        print(why, file=sys.stderr)
        return None
    try:
        ser = open_port(port, args.baud)
    except Exception as e:
        # The exception's text names the port and the OS error, never data.
        print(f"cannot open {port}: {type(e).__name__}", file=sys.stderr)
        return None
    return port, ser, lock


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Drive a CONFIG_BLINK_TOUCH_INJECT board's touch panel. "
                    "Do not touch the panel during a run.")
    ap.add_argument("--port")
    ap.add_argument("--baud", type=int, default=BAUD)
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="say 'unparsed line (N bytes)' for each line "
                         "this does not recognise")
    ap.add_argument("--no-restore", action="store_true",
                    help="leave the theme, brightness and Focus as the run "
                         "left them")
    ap.add_argument("--keep-going", action="store_true",
                    help="run a script to the end after a failure")
    ap.add_argument("--harness", metavar="BIN",
                    help="run against tools/panel_render/touch.sh's host "
                         "harness instead of a board")
    ap.add_argument("--renders", metavar="DIR", default=".",
                    help="with --harness: where the PNGs go")
    ap.add_argument("--force", action="store_true",
                    help="click a widget even when it is disabled (LVGL "
                         "drops the press; refused without this)")
    ap.add_argument("--edit-reply", choices=("ok", "no"), default="ok",
                    help="answer the board's theme_edit with ok 1 (as if a "
                         "browser opened) or ok 0")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("state")
    sub.add_parser("targets")
    p = sub.add_parser("tap")
    p.add_argument("name")
    p = sub.add_parser("raw")
    p.add_argument("kind", choices=("tap",))
    p.add_argument("xy", nargs="+", type=int)
    p = sub.add_parser("swipe")
    p.add_argument("coords", nargs="+")
    p = sub.add_parser("script")
    p.add_argument("file")
    sub.add_parser("widgets", help="list every clickable widget on the panel")
    p = sub.add_parser("click", help="tap a widget by its label, or by "
                                     "--index from the listing")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("label", nargs="?")
    g.add_argument("--index", type=int)
    args = ap.parse_args(argv)

    if args.cmd == "targets":
        for name, t in TARGETS.items():
            pts = " -> ".join(f"({x},{y})" for x, y in t.points())
            print(f"{name:<24} {t.kind:<6} {pts:<22} {t.doc}")
        return 0

    try:
        if args.cmd == "script":
            steps = parse_script(Path(args.file).read_text(encoding="utf-8"))
        elif args.cmd == "tap":
            steps = [parse_line("tap " + args.name)]
        elif args.cmd == "raw":
            steps = [parse_line("raw tap " + " ".join(map(str, args.xy)))]
        elif args.cmd == "swipe":
            steps = [parse_line("swipe " + " ".join(args.coords))]
        elif args.cmd == "widgets":
            steps = [Step(1, "widgets", ())]
        elif args.cmd == "click":
            steps = [Step(1, "click_index", (args.index,))
                     if args.index is not None
                     else Step(1, "click", (args.label,))]
        else:
            steps = [parse_line("state")]
    except ScriptError as e:
        print(f"{args.cmd}: {e}", file=sys.stderr)
        return 2

    if args.harness:
        port, ser, lock = "the host harness", None, None
        link = HarnessLink(args.harness, args.renders)
    else:
        conn = _connect(args)
        if conn is None:
            return 2
        port, ser, lock = conn
        link = Link(ser)
    bench = Bench(link, verbose=args.verbose,
                  edit_reply=args.edit_reply == "ok")
    bench.force = args.force
    if ser is not None:
        bench.port_check = lambda: port_holders(port)
    reader = threading.Thread(
        target=link.read_loop, args=(bench.on_line, bench.on_overlong),
        daemon=True)
    reader.start()
    try:
        print(f"bench on {port}")
        bench.greet()
        # The board answers welcome with pref (its caps) and theme_now.
        bench.mark()
        bench._wait(lambda evs: next((e for e in evs if e["t"] == "pref"),
                                     None), 3.0)
        bench.send_frames()
        bench.sleep(1.0)
        if bench.query_state() is None:
            bench.fail("the board does not answer ui_state -- is this a "
                       "CONFIG_BLINK_TOUCH_INJECT image?")
        else:
            bench.run_with_restore(steps, keep_going=args.keep_going,
                                   restore=not args.no_restore)
        bench.summary()
    finally:
        link.stop()
        reader.join(timeout=1.0)
        if ser is not None:
            ser.close()
        if lock is not None:
            lock.close()
    return 1 if bench.failures else 0


if __name__ == "__main__":
    sys.exit(main())
