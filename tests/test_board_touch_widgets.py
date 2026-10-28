"""tools/board_touch.py's widget listing and click, against a fake board.

The walk itself is firmware (firmware/src/touch_widgets.c) and is proved on
the host harness by tools/bench/popups_touch.txt; its wire format is held by
tests/touch_widgets/host_test.c. What is pinned here is the PC half:

  - the script commands that name a widget by its words;
  - the reply parser, which is the only thing standing between a board line
    and the terminal, so a label of any shape the firmware does not write
    must fail the whole line and never be printed (CLAUDE.md);
  - paging, a listing that changes under it, and a click refused as stale
    or covered;
  - that the constants it mirrors are the firmware's.

No board, no port, no home directory: every reply below is made up here.
"""
import importlib.util
import io
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "board_touch.py"
SRC = ROOT / "firmware" / "src"


def _load():
    spec = importlib.util.spec_from_file_location("board_touch_w", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _load()


def _w(i, label, kind="button", hit=True, x=10, y=20, w=100, h=36, f=8):
    ax, ay = (x + w // 2, y + h // 2) if hit else (-1, -1)
    return [i, kind, label, x, y, w, h, ax, ay, f | (bt.W_HIT if hit else 0)]


class WidgetBoard:
    """Answers ui_state, widgets (paged, PER_PAGE a line) and touch the way
    a CONFIG_BLINK_TOUCH_INJECT board does. `screens` is a list of
    (gen, rows); each widgets request reads the current one, and `advance`
    moves on to the next after N page requests, to model a panel that
    changes while it is being read."""

    def __init__(self, bench_ref, rows, gen=1234):
        self.bench_ref = bench_ref
        self.screens = [(gen, rows)]
        self.writes = []
        self.page_requests = 0
        self.change_after = None
        self.click_answers = []     # touch_refused whys, then touch_done
        self.state = dict(scr="gauges", ov="none", modal="none", busy=0,
                          theme=0, stored=0, tp=0, focus="off", bl=60,
                          edit="name", slp=0, nt="none", gf="off")

    def now(self):
        return self.screens[0]

    def write(self, raw):
        self.writes.append(raw)
        msg = json.loads(raw)
        b = self.bench_ref()
        if msg["t"] == "ui_state":
            b.on_line(json.dumps(dict(t="ui_state", v=2,
                                      **self.state)).encode())
        elif msg["t"] == "widgets":
            self.page_requests += 1
            gen, rows = self.now()
            p = msg["page"]
            chunk = rows[p * bt.PER_PAGE:(p + 1) * bt.PER_PAGE]
            b.on_line(json.dumps({
                "t": "widgets", "v": 2, "page": p, "w": chunk,
                "n": len(rows),
                "more": int(len(rows) > (p + 1) * bt.PER_PAGE),
                "gen": gen}).encode())
            if (self.change_after is not None
                    and self.page_requests == self.change_after
                    and len(self.screens) > 1):
                self.screens.pop(0)
        elif msg["t"] == "touch":
            if msg["op"] == "click" and self.click_answers:
                why = self.click_answers.pop(0)
                if why:
                    b.on_line(json.dumps({"t": "touch_refused", "v": 2,
                                          "why": why}).encode())
                    return
            b.on_line(json.dumps({"t": "touch_done", "v": 2,
                                  "op": msg["op"], "ok": 1}).encode())


def _bench(rows, gen=1234, out=None):
    holder = {}
    link = WidgetBoard(lambda: holder["b"], rows, gen)
    b = bt.Bench(link, out=out or io.StringIO(), settle_scale=0)
    holder["b"] = b
    return b, link


def _clicks(link):
    return [json.loads(w) for w in link.writes
            if json.loads(w).get("op") == "click"]


# --- the script language ----------------------------------------------------

def test_the_widget_commands_parse_with_their_words_whole():
    steps = bt.parse_script(
        'widgets\n'
        'click "Update now"   # the offer\n'
        "click \"What's new\"\n"
        'click index 7\n'
        'click --index 3\n'
        'expect-widget "Opened on your computer"\n'
        'wait-widget "Later" 2.5\n'
        'fake ota_avail 1.4.0\n'
        'fake ota_avail 1.4.0 1.4.0\n'
        'fake ota_none\n'
        'fake ota_error the computer stopped responding\n'
        'fake ota_error\n'
        'fake boot_notice 1.3.1\n'
        'fake reverted_notice\n'
        'click "#1 spot"\n')
    got = [(s.cmd, s.args) for s in steps]
    assert got == [
        ("widgets", ()),
        ("click", ("Update now",)),
        ("click", ("What's new",)),
        ("click_index", (7,)),
        ("click_index", (3,)),
        ("expect_widget", ("Opened on your computer",)),
        ("wait_widget", ("Later", 2.5)),
        ("fake", ("ota_avail", "1.4.0")),
        ("fake", ("ota_avail", "1.4.0", "1.4.0")),
        ("fake", ("ota_none",)),
        ("fake", ("ota_error", "the computer stopped responding")),
        ("fake", ("ota_error",)),
        ("fake", ("boot_notice", "1.3.1")),
        ("fake", ("reverted_notice",)),
        ("click", ("#1 spot",)),
    ]


@pytest.mark.parametrize("line, why", [
    ("click", 'click [--force] "LABEL"'),
    ("click a b", 'click [--force] "LABEL"'),
    ("click index seven", 'click [--force] "LABEL"'),
    ('click "unbalanced', "unbalanced quote"),
    ("expect-widget", "expect-widget"),
    ('wait-widget "Later"', "wait-widget"),
    ('wait-widget "Later" soon', "wait-widget"),
    ("widgets now", "usage: widgets"),
    ("fake ota_avail", "fake ota_avail VERSION"),
    ("fake confetti", "fake ota_avail|"),
])
def test_a_bad_widget_line_is_refused_with_its_number(line, why):
    with pytest.raises(bt.ScriptError) as e:
        bt.parse_script("mem\n" + line + "\n")
    assert "line 2" in str(e.value)
    assert why in str(e.value)


def test_the_old_commands_still_split_the_old_way():
    (s,) = bt.parse_script("tap settings.open # a comment\n")
    assert (s.cmd, s.args) == ("tap", ("settings.open",))


@pytest.mark.parametrize("name", ["full_touch.txt", "popups_touch.txt"])
def test_the_shipped_scripts_parse(name):
    steps = bt.parse_script((ROOT / "tools" / "bench" / name).read_text(
        encoding="utf-8"))
    assert any(s.cmd == "click" for s in steps)


def test_the_popup_script_reaches_every_popup_it_says_it_does():
    steps = bt.parse_script((ROOT / "tools" / "bench" / "popups_touch.txt")
                            .read_text(encoding="utf-8"))
    clicked = {s.args[0] for s in steps if s.cmd == "click"}
    assert {"Later", "Update now", "OK", "Update", "Cancel",
            "Yes, do it", "What's new", ">", "<", "Back"} <= clicked
    modals = {s.args[1].get("modal") for s in steps
              if s.cmd == "expect" and s.args[0] == "ui_state"}
    assert {"prompt", "download", "notice", "whatsnew"} <= modals
    assert any(s.cmd == "expect" and s.args[1].get("ov") == "confirm"
               for s in steps)
    # Harness-only lines come last, so a board runs everything before them.
    fakes = [i for i, s in enumerate(steps) if s.cmd == "fake"]
    host = [i for i in fakes
            if steps[i].args[0] in ("boot_notice", "reverted_notice")]
    board = [i for i in fakes if i not in host]
    assert host and max(board) < min(host)
    # And the board is left with nothing on offer.
    assert ("fake", ("ota_none",)) in [(steps[i].cmd, steps[i].args)
                                       for i in board[-1:]]


def test_the_default_script_has_a_click_by_label_pass():
    steps = bt.parse_script((ROOT / "tools" / "bench" / "full_touch.txt")
                            .read_text(encoding="utf-8"))
    clicked = [s.args[0] for s in steps if s.cmd == "click"]
    for label in ("Focus", "Session", "Off", "Theme", "Edit on computer",
                  "Dark", "Brightness", "<"):
        assert label in clicked, label
    assert any(s.cmd == "wait_widget"
               and s.args[0] == "Opened on your computer" for s in steps)
    # The full eye swipe is still last.
    taps = [s.args[0] for s in steps if s.cmd == "tap"]
    assert taps[-1] == "eye.exit"


# --- the reply parser -------------------------------------------------------

def _reply(rows, **kw):
    obj = {"t": "widgets", "v": 2, "page": 0, "w": rows, "n": len(rows),
           "more": 0, "gen": 77}
    obj.update(kw)
    return obj


def test_a_widgets_reply_parses_into_widgets():
    p = bt.parse_widgets(_reply([_w(0, "OK"), _w(1, "", kind="screen",
                                                 hit=False)]))
    assert p["gen"] == 77 and p["n"] == 2 and p["more"] == 0
    a, s = p["items"]
    assert (a.i, a.kind, a.label, a.hit, a.ax, a.ay) == (
        0, "button", "OK", True, 60, 38)
    assert (s.kind, s.hit, s.ax) == ("screen", False, -1)


@pytest.mark.parametrize("row", [
    _w(0, 'say "hi"'),                  # a quote the firmware turns to '
    _w(0, "a\\b"),                      # a backslash it turns to /
    _w(0, "x" * (bt.LABEL_MAX + 1)),    # longer than it cuts
    _w(0, "café"),                 # not ASCII
    _w(0, "tab\there"),                 # a control character
    _w(0, "OK", kind="slider-ish"),
    [0, "button", "OK", 10, 20, 100, 36, 60, 38],          # nine fields
    [0, "button", "OK", 320, 20, 100, 36, 60, 38, 4],      # x off the panel
    [0, "button", "OK", 10, 20, 0, 36, 60, 38, 4],         # no width
    [0, "button", "OK", 10, 20, 100, 36, 60, 38, 64],      # unknown flag
    [True, "button", "OK", 10, 20, 100, 36, 60, 38, 4],    # a bool index
])
def test_anything_the_firmware_does_not_write_fails_the_line(row):
    assert bt.parse_widgets(_reply([row])) is None


@pytest.mark.parametrize("kw", [
    {"gen": -1}, {"gen": 2 ** 32}, {"more": 2}, {"page": "0"},
    {"w": "rows"}, {"w": [_w(i, "x") for i in range(bt.PER_PAGE + 1)]},
])
def test_a_bad_head_or_tail_fails_the_line(kw):
    assert bt.parse_widgets(_reply([_w(0, "OK")], **kw)) is None


def test_a_label_off_the_board_is_printed_only_once_it_has_parsed():
    out = io.StringIO()
    b, _ = _bench([_w(0, "Brightness")], out=out)
    b.verbose = True
    secret = "sk-ant-PRIVATE-7f3a"
    for bad in (secret + "é", secret + '"', secret + "\n" + secret,
                secret * 3):
        b.on_line(json.dumps(_reply([_w(0, bad)])).encode())
    assert secret not in out.getvalue()
    assert b.unparsed == 4
    # A good one is listed, by its parsed fields.
    assert b.show_widgets()
    assert "'Brightness'" in out.getvalue()


# --- matching ---------------------------------------------------------------

def _items(*rows):
    return bt.parse_widgets(_reply(list(rows)))["items"]


def test_exact_beats_prefix_beats_substring():
    items = _items(_w(0, "Light soft"), _w(1, "Light"), _w(2, "Light ink"))
    assert bt.find_widget(items, "Light")[0].i == 1
    items = _items(_w(0, "Couldn't check for updates."), _w(1, "OK"))
    assert bt.find_widget(items, "Couldn't check")[0].i == 0
    assert bt.find_widget(items, "check for")[0].i == 0


def test_a_long_name_matches_the_label_it_was_cut_to():
    cut = "Run `blink theme` on your computer"[:bt.LABEL_MAX]
    items = _items(_w(4, cut))
    assert bt.find_widget(items, "Run `blink theme` on your computer"
                          )[0].i == 4


def test_a_covered_widget_is_not_clicked_and_says_so():
    items = _items(_w(0, "Brightness", hit=False))
    w, why = bt.find_widget(items, "Brightness")
    assert w is None and "covered" in why


def test_a_button_beats_the_box_it_sits_in():
    items = _items(_w(0, "Update now or later", kind="obj"),
                   _w(1, "Update now"))
    assert bt.find_widget(items, "Update")[0].i == 1


def test_two_buttons_with_one_name_are_ambiguous_never_guessed():
    items = _items(_w(3, "OK"), _w(9, "OK"))
    w, why = bt.find_widget(items, "OK")
    assert w is None and "click --index" in why and "3" in why and "9" in why


def test_no_such_label():
    w, why = bt.find_widget(_items(_w(0, "OK")), "Install")
    assert w is None and "no widget says" in why


# --- paging, generations, refusals -------------------------------------------

def _many(n):
    return [_w(i, f"Item {i}") for i in range(n)]


def test_a_listing_is_read_page_by_page_to_the_end():
    b, link = _bench(_many(10))
    gen, items = b.list_widgets()
    assert gen == 1234 and [w.i for w in items] == list(range(10))
    pages = [json.loads(w)["page"] for w in link.writes
             if json.loads(w)["t"] == "widgets"]
    assert pages == [0, 1, 2]


def test_a_panel_that_changes_mid_listing_is_read_again():
    b, link = _bench(_many(10), gen=1)
    link.screens.append((2, _many(6)))
    link.change_after = 1           # after page 0 of gen 1
    gen, items = b.list_widgets()
    assert gen == 2 and len(items) == 6
    assert not b.failures


def test_a_panel_that_never_holds_still_fails():
    b, link = _bench(_many(10), gen=1)
    for g in range(2, 12):
        link.screens.append((g, _many(10)))
    link.change_after = 1

    real = link.write

    def write(raw):
        real(raw)
        if json.loads(raw)["t"] == "widgets" and len(link.screens) > 1:
            link.screens.pop(0)
    link.write = write
    assert b.list_widgets() is None
    assert "kept changing" in b.failures[-1]


def test_a_click_names_the_index_and_generation_it_was_listed_under():
    b, link = _bench([_w(0, "Later"), _w(1, "Update now")], gen=99)
    assert b.click(text="Update now")
    (c,) = _clicks(link)
    assert c == {"t": "touch", "v": 2, "op": "click", "i": 1, "gen": 99,
                 "ms": bt.TAP_MS}
    assert b.click(index=0)
    assert _clicks(link)[-1]["i"] == 0


def test_a_stale_click_is_listed_again_once_then_refused():
    b, link = _bench([_w(0, "OK")])
    link.click_answers = ["stale", None]
    assert b.click(text="OK")
    assert len(_clicks(link)) == 2 and not b.failures
    b2, link2 = _bench([_w(0, "OK")])
    link2.click_answers = ["stale", "stale"]
    assert not b2.click(text="OK")
    assert "refused (stale)" in b2.failures[-1]


def test_a_covered_refusal_fails_the_click():
    b, link = _bench([_w(0, "OK")])
    link.click_answers = ["covered"]
    assert not b.click(text="OK")
    assert "refused (covered)" in b.failures[-1]
    assert len(_clicks(link)) == 1


def test_a_click_on_an_index_that_is_not_listed_sends_nothing():
    b, link = _bench([_w(0, "OK")])
    assert not b.click(index=5)
    assert _clicks(link) == []


def test_wait_widget_polls_until_it_appears():
    b, link = _bench([_w(0, "Update now")], gen=1)
    link.screens.append((2, [_w(0, "Opened on your computer")]))
    link.change_after = 1
    b.scale = 1.0       # a real timeout: the first listing is the old one
    w = b.wait_widget("Opened on your computer", 5)
    assert w is not None and not b.failures
    b2, _ = _bench([_w(0, "OK")])
    assert b2.wait_widget("Opened", 0) is None
    assert "no widget says" in b2.failures[-1]


def test_a_click_runs_from_a_script_and_its_refusal_names_the_line():
    b, link = _bench([_w(0, "OK")])
    link.click_answers = ["covered"]
    b.run_script(bt.parse_script('mem\nclick "OK"\nmem\n'))
    assert b.failures and b.failures[0].startswith("line 2:")


def test_the_harness_notices_are_refused_on_a_board():
    b, link = _bench([])
    assert not b.fake("boot_notice", "1.3.1")
    assert "host harness only" in b.failures[-1]
    assert not [w for w in link.writes
                if json.loads(w)["t"] == "bench_notice"]


def test_the_fake_ota_lines_are_the_daemon_s_own_and_fit():
    b, link = _bench([])
    assert b.fake("ota_avail", "1.4.0")
    assert b.fake("ota_error", "the computer stopped responding")
    assert b.fake("ota_none")
    sent = [json.loads(w) for w in link.writes]
    assert [m["t"] for m in sent] == ["ota_avail", "ota_error", "ota_none"]
    assert sent[0]["version"] == "1.4.0" and sent[0]["size"] > 0
    assert re.fullmatch(r"[0-9a-f]{64}", sent[0]["sha256"])


def test_the_bench_never_answers_an_ota_flash():
    out = io.StringIO()
    b, link = _bench([], out=out)
    b.on_line(b'{"t":"ota_flash","v":2}')
    assert link.writes == []
    assert "installs nothing" in out.getvalue()


def test_a_walk_that_took_pool_fails_the_run():
    b, _ = _bench([])
    b.on_line(b'{"t":"bench_walk","v":2,"walks":3,"grew":0,"rise":0,'
              b'"layout":40}')
    assert not b.failures
    b.on_line(b'{"t":"bench_walk","v":2,"walks":4,"grew":16,"rise":0,'
              b'"layout":40}')
    assert "must take nothing" in b.failures[-1]


def test_a_host_mem_line_is_held_to_its_own_pool():
    out = io.StringIO()
    b, _ = _bench([], out=out)
    b.on_line(b"[mem] lvgl host-notice: used=30000 free=29000 largest=20000 "
              b"peak=38536 boot_peak=39000 pool=59440")
    b.report_mem()
    assert "of 59440" in out.getvalue()
    b.on_line(b"[mem] lvgl theme: used=20000 free=10000 largest=8000 "
              b"peak=27000 boot_peak=27400 pool=30632")
    b.report_mem()
    assert f"of {bt.POOL_USABLE}" in out.getvalue()


# --- the firmware's own constants --------------------------------------------

def _define(name, path):
    m = re.search(rf"^#define {name}\s+(\w+)", path.read_text(), re.M)
    assert m, name
    return int(m.group(1), 0)


def test_the_mirrored_constants_are_the_firmware_s():
    fmt = SRC / "touch_widgets_fmt.h"
    assert bt.PER_PAGE == _define("TW_PER_PAGE", fmt)
    assert bt.LABEL_MAX == _define("TW_LABEL_MAX", fmt)
    for name, val in (("TW_F_CHECKED", bt.W_CHECKED),
                      ("TW_F_DISABLED", bt.W_DISABLED),
                      ("TW_F_HIT", bt.W_HIT), ("TW_F_EVENTS", bt.W_EVENTS),
                      ("TW_F_TOP", bt.W_TOP), ("TW_F_SYS", bt.W_SYS)):
        assert _define(name, fmt) == val, name
    text = fmt.read_text()
    table = text[text.index("names[TW_K_N] = {"):]
    table = table[:table.index("};")]
    assert tuple(re.findall(r'"(\w+)"', table)) == bt.WIDGET_KINDS


def _c_body(path, head):
    code = path.read_text()
    body = code[code.index(head):]
    return body[:body.index("\n}\n")]


def test_the_click_wire_is_the_one_the_firmware_reads():
    body = _c_body(SRC / "touch_widgets.c", "const char *tw_click_parse(")
    keys = set(re.findall(r'"(\w+)"', body))
    assert {"i", "gen", "ms", "force"} <= keys
    assert set(bt.touch_click(1, 2)) == {"t", "v", "op", "i", "gen", "ms"}
    assert bt.touch_click(1, 2, force=True)["force"] == 1
    # Refusal words the tool knows are the ones the firmware sends: the
    # injector's own, and the walk's.
    sent = set(re.findall(r'refuse\("(\w+)"\)',
                          (SRC / "touch_inject.c").read_text()))
    words = _c_body(SRC / "touch_widgets.c", "static const char *aim_word(")
    sent |= set(re.findall(r'return "(\w+)"', words))
    sent |= set(re.findall(r'return "(\w+)"', body))
    assert sent <= set(bt._REFUSALS)
    assert {"stale", "covered", "range", "disabled", "busy"} <= sent


def test_the_board_and_the_harness_parse_a_click_with_one_function():
    """The harness used to carry its own copy of the click and widgets
    parsing, and it had drifted (review, S6)."""
    inject = _c_body(SRC / "touch_inject.c", "static void click_line(const char *json)\n{")
    assert "tw_click_parse(json," in inject
    assert "tw_widgets_reply(json," in (SRC / "touch_inject.c").read_text()
    harness = (ROOT / "tools" / "panel_render" / "touch" /
               "harness.c").read_text()
    assert "tw_click_parse(json," in harness
    assert "tw_widgets_reply(json," in harness
    assert "tw_aim(" not in harness and "tw_page_line(" not in harness
    # Its refusal carries the firmware's protocol version, not a literal.
    click = harness[harness.index("tw_click_parse(json,"):]
    click = click[:click.index("return;")]
    assert "BLINK_PROTO_VERSION" in click


def test_a_click_is_played_as_a_tap_never_sent_as_an_event():
    """The point of a click is LVGL's own hit test: the firmware must play
    it into the input device like a tap, never deliver it with
    lv_obj_send_event (which proves only the handler)."""
    for name in ("touch_inject.c", "touch_widgets.c"):
        code = re.sub(r"/\*.*?\*/", "", (SRC / name).read_text(), flags=re.S)
        assert "lv_obj_send_event" not in code, name
        assert "lv_event_send" not in code, name
    parse = _c_body(SRC / "touch_widgets.c", "const char *tw_click_parse(")
    assert "s->op = TI_OP_TAP" in parse
    assert "start(&s, true)" in _c_body(SRC / "touch_inject.c",
                                        "static void click_line(const char *json)\n{")


def test_the_walk_allocates_nothing():
    code = re.sub(r"/\*.*?\*/", "", (SRC / "touch_widgets.c").read_text(),
                  flags=re.S)
    for call in ("lv_malloc", "lv_obj_create", "lv_label_create",
                 "lv_timer_create", "malloc(", "lv_strdup", "printk"):
        assert call not in code, call


def test_the_widget_files_are_bench_only():
    cmake = (ROOT / "firmware" / "CMakeLists.txt").read_text()
    m = re.search(r"target_sources_ifdef\(CONFIG_BLINK_TOUCH_INJECT app "
                  r"PRIVATE([^)]*)\)", cmake)
    assert m and "src/touch_widgets.c" in m.group(1)
    # Not listed anywhere else in the build.
    assert cmake.count("touch_widgets.c") == 1
    for f in SRC.glob("*.c"):
        if f.name in ("touch_inject.c", "touch_widgets.c"):
            continue
        assert "touch_widgets" not in f.read_text(), f.name


# --- review fixes: what may be printed (M1, N6, N7) -------------------------

SECRET = "sk-ant-PRIVATE-7f3a"      # well shaped: passes parse_widgets


def test_a_well_shaped_label_nobody_wrote_is_withheld_everywhere():
    """The shape filter alone printed this (review, M1). It is 19 printable
    ASCII characters, so it parses -- and must still never be shown."""
    out = io.StringIO()
    b, link = _bench([_w(0, SECRET), _w(1, SECRET), _w(2, "Brightness")],
                     out=out)
    assert bt.parse_widgets(_reply([_w(0, SECRET)])) is not None
    assert b.show_widgets()
    assert "<label withheld, 19 chars>" in out.getvalue()
    assert "'Brightness'" in out.getvalue()
    # Ambiguous: the reason names indices and kinds, not the words.
    assert not b.click(text="sk-ant")
    assert "0 button" in b.failures[-1] and "1 button" in b.failures[-1]
    # A click by index never echoes the label at all (N7).
    b2, _ = _bench([_w(0, SECRET)], out=out)
    assert b2.click(index=0)
    # A click by the operator's own words says those, and nothing else.
    b3, _ = _bench([_w(0, SECRET + "x")], out=out)
    assert b3.click(text="sk-ant")
    text = out.getvalue() + "\n".join(b.failures + b2.failures + b3.failures)
    assert SECRET not in text
    assert "PRIVATE" not in text


def test_a_daemon_reason_under_a_notice_is_shown_only_if_it_is_ours():
    allow = bt.label_allow()
    # ui_settings.c's notice: "Couldn't check for updates.\n" + the reason,
    # cut at 28. The base and its newline fill the cut exactly.
    assert allow.allowed("Couldn't check for updates. ")
    assert allow.allowed("Update failed. The current v")
    # A version where a version goes; nothing else there.
    assert allow.allowed("Version 1.4.0 is available.")
    assert allow.allowed("Updating to 1.4.0...")
    assert not allow.allowed("Updating to evil...")
    assert not allow.allowed("Version " + SECRET[:12] + " is")
    assert allow.allowed("6 changes since 1.3.1")
    assert allow.allowed("1 change")
    assert allow.allowed("1/3 >")
    assert not allow.allowed(SECRET)
    assert not allow.allowed("OK " + SECRET)


def test_every_label_the_shipped_scripts_click_is_printable():
    allow = bt.label_allow()
    for name in ("full_touch.txt", "popups_touch.txt"):
        steps = bt.parse_script((ROOT / "tools" / "bench" / name)
                                .read_text(encoding="utf-8"))
        for s in steps:
            if s.cmd == "click":
                assert allow.allowed(s.args[0]), (name, s.args[0])
    for label in ("BLINK", "Settings", "Update", "Theme", "Focus",
                  "Brightness", "Edit on computer", "Opened on your computer",
                  "Run `blink theme` on your co", "What's new", "Back",
                  "Yes, do it", "Cancel", "Later", "Update now", "OK", "*",
                  "Updated to 1.3.4", "Couldn't check for updates."):
        assert allow.allowed(label), label


def test_the_version_formats_are_still_in_the_firmware():
    lits = set()
    for src in SRC.glob("*.c"):
        lits.update(r.decode("utf-8", "replace")
                    for r in bt.c_literals(src.read_text()))
    for fmt in bt.VERSION_HOLES:
        assert fmt in lits, fmt


def test_the_label_port_is_the_firmware_s_byte_for_byte(tmp_path):
    """label_copy is a port of tw_label_copy; feed both the same table."""
    import shutil
    import subprocess
    cc = shutil.which("cc")
    if cc is None:
        pytest.skip("no C compiler")
    cases = [b"Update now", b'say "hi" a\\b', b"a\nb\tc", b"\xef\x81\x93",
             b"1/3 \xef\x81\x94", b"\xef\x80\x8c ok", b"caf\xc3\xa9!",
             b"a\x01b\x7fc", b"x" * 60, b"\xe2\x80\x94dash",
             b"Run `blink theme` on your computer", b"",
             bytes(range(32, 127)), bytes(range(128, 256)),
             b"\xef\x81", b"\xef"]
    src = tmp_path / "t.c"
    src.write_text(
        '#include <stdio.h>\n#include <stdlib.h>\n'
        '#include "touch_widgets_fmt.h"\n'
        "int main(void){char line[4096],in[2048],out[TW_LABEL_MAX+1];\n"
        "while(fgets(line,sizeof line,stdin)){size_t n=0;\n"
        "for(char*p=line;p[0]&&p[1]&&p[0]!='\\n';p+=2){unsigned v;"
        "sscanf(p,\"%2x\",&v);in[n++]=(char)v;}\n"
        "in[n]=0;tw_label_copy(out,sizeof out,in);printf(\"[%s]\\n\",out);}"
        "return 0;}\n")
    exe = tmp_path / "t"
    subprocess.run([cc, "-I", str(SRC), "-o", str(exe), str(src)],
                   check=True, capture_output=True)
    feed = "".join(c.hex() + "\n" for c in cases)
    r = subprocess.run([str(exe)], input=feed, capture_output=True,
                       text=True, check=True)
    got = r.stdout.split("\n")[:len(cases)]
    want = [f"[{bt.label_copy(c)}]" for c in cases]
    assert got == want


def test_an_unknown_mem_screen_name_is_withheld():
    out = io.StringIO()
    b, _ = _bench([], out=out)
    b.on_line(b"[mem] lvgl settings: used=1 free=2 largest=3 peak=4 "
              b"boot_peak=5 pool=30632")
    b.on_line(b"[mem] lvgl sk-ant-private: used=1 free=2 largest=3 peak=4 "
              b"boot_peak=5 pool=30632")
    b.report_mem()
    b.summary()
    text = out.getvalue()
    assert "settings" in text
    assert "sk-ant" not in text and "<screen withheld, 14 chars>" in text
    assert bt.shown_where("host-confirm") == "host-confirm"


# --- review fixes: S2-S5, N1 ---------------------------------------------------

def test_a_trailing_newline_does_not_sneak_a_long_label_through():
    assert bt.parse_widgets(_reply([_w(0, "x" * 28 + "\n")])) is None
    assert bt.parse_widgets(_reply([_w(0, "x" * 28)])) is not None


def test_a_poll_that_later_succeeds_records_no_failure():
    b, link = _bench([_w(0, "Later")], gen=1)
    b.scale = 1.0
    real = link.write
    calls = {"n": 0}

    def flaky(raw):
        # The first listing gets no answer at all; the next one does.
        if json.loads(raw)["t"] == "widgets":
            calls["n"] += 1
            if calls["n"] == 1:
                return
        real(raw)
    link.write = flaky
    w = b.wait_widget("Later", 10)
    assert w is not None
    assert b.failures == []


def test_a_disabled_widget_is_refused_unless_forced():
    dis = _w(0, "Update now", f=8 | bt.W_DISABLED)
    w, why = bt.find_widget(_items(dis), "Update now")
    assert w is None and "disabled" in why and "--force" in why
    assert bt.find_widget(_items(dis), "Update now", force=True)[0].i == 0
    b, link = _bench([dis])
    assert not b.click(text="Update now")
    assert not b.click(index=0)
    assert _clicks(link) == []
    b.force = True
    assert b.click(index=0)
    assert _clicks(link)[-1]["force"] == 1


def test_the_script_can_force_a_click():
    (a, c) = bt.parse_script('click --force "Update now"\n'
                             'click --force index 3\n')
    assert (a.cmd, a.args) == ("click", ("Update now", True))
    assert (c.cmd, c.args) == ("click_index", (3, True))


def test_an_install_press_is_refused_while_anyone_else_holds_the_port():
    b, link = _bench([_w(0, "Update now"), _w(1, "Later")])
    b.port_check = lambda: [4242]
    assert not b.click(text="Update now")
    assert "4242" in b.failures[-1]
    assert _clicks(link) == []
    # Another press is not an install, and goes ahead.
    assert b.click(text="Later")
    b.port_check = lambda: None          # lsof missing: cannot say
    assert not b.click(text="Update now")
    b.port_check = lambda: []
    assert b.click(text="Update now")


def test_an_unanswered_install_is_ended_when_the_run_ends():
    b, link = _bench([])
    b.on_line(b'{"t":"ota_flash","v":2}')
    assert b.ota_flash_pending
    b.run_with_restore([], restore=False)
    sent = [json.loads(w)["t"] for w in link.writes]
    assert sent.index("ota_error") < sent.index("ota_none")
    assert not b.ota_flash_pending
    # A fake ota_error already answered it: nothing more is sent.
    b2, link2 = _bench([])
    b2.on_line(b'{"t":"ota_flash","v":2}')
    b2.fake("ota_error")
    n = len(link2.writes)
    b2.end_install()
    assert len(link2.writes) == n


def test_the_daemon_lock_is_taken_even_on_a_first_run(tmp_path):
    """No ~/.blink used to mean no lock at all, leaving a daemon free to
    start beside the bench (review, S2). conftest puts HOME in tmp_path."""
    import os
    import shutil
    from pc.cli import blink_home
    home = blink_home()
    assert str(home).startswith(str(tmp_path))
    if os.path.isdir(home):
        shutil.rmtree(home)
    fh, why = bt.take_daemon_lock()
    try:
        if os.name == "nt":
            assert fh is None and why is None
            return
        assert why is None and fh is not None
        assert os.path.exists(os.path.join(home, "bridge.lock"))
        # A second taker -- a daemon starting now -- is shut out.
        fh2, why2 = bt.take_daemon_lock()
        assert fh2 is None and "daemon" in why2
    finally:
        if fh is not None:
            fh.close()


def test_an_index_past_the_end_is_range_not_stale():
    src = (SRC / "touch_widgets.c").read_text()
    body = src[src.index("enum tw_aim tw_aim("):]
    body = body[:body.index("\n}\n")]
    assert body.index("TW_AIM_STALE") < body.index("TW_AIM_RANGE")
    assert "range" in bt._REFUSALS


def test_the_harness_says_main_c_s_words_for_a_refused_install():
    main = (SRC / "main.c").read_text()
    stubs = (ROOT / "tools" / "panel_render" / "touch" / "stubs.c").read_text()
    words = re.findall(r'ota_ui_set_error\("([^"]+)"\)', main)
    assert "The app stopped answering" in words
    assert 'ota_ui_set_error("The app stopped answering")' in stubs


# --- review round 2 -----------------------------------------------------------

def test_an_ota_flash_that_lands_after_the_click_is_still_ended(monkeypatch):
    """On a board the ota_flash comes a loop pass after the touch_done,
    behind the ui_state reply click() waits on, so a run ending on the
    press used to find nothing pending (review r2, S1)."""
    import threading
    b, link = _bench([_w(0, "Update now")])
    real = link.write

    def late(raw):
        real(raw)
        msg = json.loads(raw)
        if msg["t"] == "touch" and msg.get("op") == "click":
            threading.Timer(0.05, b.on_line,
                            (b'{"t":"ota_flash","v":2}',)).start()
    link.write = late
    b.run_with_restore(bt.parse_script('click "Update now"\n'),
                       restore=False)
    sent = [json.loads(w)["t"] for w in link.writes]
    assert "ota_error" in sent and "ota_none" in sent
    assert sent.index("ota_error") < sent.index("ota_none")
    assert not b.ota_flash_pending and not b.install_pressed


def test_an_install_press_is_ended_even_if_no_ota_flash_ever_comes(
        monkeypatch):
    monkeypatch.setattr(bt, "INSTALL_FLASH_WAIT_S", 0.05)
    b, link = _bench([_w(0, "Yes, do it")])
    assert b.click(text="Yes, do it")
    assert b.install_pressed
    b.end_install()
    sent = [json.loads(w)["t"] for w in link.writes]
    assert sent[-2:] == ["ota_error", "ota_none"]
    # And nothing for a run that never pressed one.
    b2, link2 = _bench([_w(0, "Later")])
    assert b2.click(text="Later")
    n = len(link2.writes)
    b2.end_install()
    assert len(link2.writes) == n


def test_the_walk_lays_the_panel_out_before_it_looks():
    """The harness lays out before every walk to keep its pool figure
    clean, so it would not notice this loop going (review r2, S2). Without
    it a popup raised by a wire line is listed where it was before it was
    centred, and a click presses the wrong control while saying ok."""
    code = re.sub(r"/\*.*?\*/", "", (SRC / "touch_widgets.c").read_text(),
                  flags=re.S)
    body = code[code.index("static void walk(struct walk *w)"):]
    body = body[:body.index("\n}\n")]
    layout = body.index("lv_obj_update_layout(root)")
    visit = body.index("visit(w, root")
    assert layout < visit
    # Every layer, the same loop bound as the visit.
    assert body.count("for (int i = 0; i < TW_LAYERS; i++)") == 2


def test_a_joined_label_s_tail_must_read_as_words():
    allow = bt.label_allow()
    assert not allow.allowed("Updated to 1.3.4 12345")
    assert not allow.allowed("OK OK")
    assert not allow.allowed("Settings x")
    assert allow.allowed("Couldn't check for updates. ")
    assert bt._tail_ok("") and bt._tail_ok("Back") and not bt._tail_ok("x1")


def test_an_unwritable_home_refuses_with_a_sentence(tmp_path, monkeypatch):
    import os
    if os.name == "nt":
        pytest.skip("POSIX lock only")
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("x", encoding="utf-8")
    import pc.cli
    monkeypatch.setattr(pc.cli, "blink_home", lambda: str(blocker / "home"))
    fh, why = bt.take_daemon_lock()
    assert fh is None and "cannot take the daemon's lock" in why


def test_a_fake_ota_error_reason_keeps_its_apostrophe():
    (a, b, c) = bt.parse_script("fake ota_error it's late\n"
                                'fake ota_error "quoted reason"  # c\n'
                                "fake ota_error\n")
    assert a.args == ("ota_error", "it's late")
    assert b.args == ("ota_error", "quoted reason")
    assert c.args == ("ota_error",)


def test_a_click_s_touch_done_is_named_click():
    """The board's naming of a finished click (touch_inject.c, AS_CLICK):
    the PC waits for touch_done op=click and for nothing else."""
    code = (SRC / "touch_inject.c").read_text()
    names = code[code.index("static const char *op_name(int op)"):]
    names = names[:names.index("\n}\n")]
    assert re.search(r'case AS_CLICK:\s*return "click";', names)
    fin = code[code.index("static void finish(int what)"):]
    fin = fin[:fin.index("\n}\n")]
    assert "cur_click ? AS_CLICK : 0" in fin
    svc = code[code.index("void touch_inject_service(void)"):]
    svc = svc[:svc.index("\n}\n")]
    assert "(what & AS_CLICK) ? AS_CLICK" in svc
    click = code[code.index("static void click_line(const char *json)\n{"):]
    assert "start(&s, true)" in click[:click.index("\n}\n")]
    assert "click" in bt._TOUCH_OPS


def test_ui_state_busy_covers_a_screen_load_on_board_and_harness():
    for path in (SRC / "touch_inject.c",
                 ROOT / "tools" / "panel_render" / "touch" / "harness.c"):
        code = path.read_text()
        st = code[code.index("static void ui_state_line(void)"):]
        st = st[:st.index("\n}\n")]
        assert "lv_display_get_screen_prev(NULL) != NULL" in st, path.name


# --- the first board run: replies lost in the board's log ---------------------

# The bytes a Zephyr 4.3.1 board puts on the cable, built the way the source
# builds them rather than typed from memory: an earlier fixture put the colour
# before the timestamp, the board never did, and the test agreed with the bug.
#   subsys/logging/log_output.c prefix_print(): timestamp_print()
#     "[%02u:%02u:%02u.%03u,%03u] ", then color_prefix() (colors[level], or
#     LOG_COLOR_CODE_DEFAULT when the level has none: inf and dbg in this
#     build), then ids_print() "<%s> " and "%s: ";
#   postfix_print(): color_postfix() "\x1b[0m", then newline_print() "\r\n".
_Z_COLOUR = {"err": b"\x1b[1;31m", "wrn": b"\x1b[1;33m",
             "inf": b"\x1b[0m", "dbg": b"\x1b[0m"}


def _zephyr_line(level, module, text, secs=62.345):
    s, us = int(secs), round((secs % 1) * 1e6)
    stamp = b"[%02u:%02u:%02u.%03u,%03u] " % (
        s // 3600, s // 60 % 60, s % 60, us // 1000, us % 1000)
    return (stamp + _Z_COLOUR[level] + b"<" + level.encode() + b"> "
            + module.encode() + b": " + text + b"\x1b[0m\r\n")


def _lvgl_line(level, func, msg, t_ms=62345, since=5):
    """An LVGL message through Zephyr's glue. lv_log_add() (lvgl
    src/misc/lv_log.c) formats "[Warn]" LOG_TIMESTAMP_FMT " %s: %s"
    LOG_FILE_LINE_FMT "\n" with this build's CONFIG_LV_LOG_USE_TIMESTAMP and
    _FILE_LINE on; modules/lvgl/lvgl.c lvgl_log() drops sizeof("[Warn] ")-1
    characters of it and LOG_WRNs the rest. The trailing "\n" is LVGL's own."""
    tag = {"wrn": b"[Warn]", "err": b"[Error]"}[level]
    buf = (tag + b"\t(%d.%03d, +%d)\t" % (t_ms // 1000, t_ms % 1000, since)
           + b" " + func + b": " + msg + b" lv_draw_buf.c:325\n")
    return _zephyr_line(level, "lvgl", buf[len(tag) + 1:])


def _cable(bench, data):
    """What Link.read_loop does with the bytes: split on LF, one line each."""
    *lines, rest = data.split(b"\n")
    assert rest == b""
    for line in lines:
        bench.on_line(line)


def test_the_fixture_is_the_shape_zephyr_4_3_sends():
    raw = _lvgl_line("wrn", b"lv_draw_buf_create_ex", b"No memory: 1x1")
    assert raw == (b"[00:01:02.345,000] \x1b[1;33m<wrn> lvgl: "
                   b"(62.345, +5)\t lv_draw_buf_create_ex: No memory: 1x1"
                   b" lv_draw_buf.c:325\n\x1b[0m\r\n")


def test_an_unparsed_line_is_counted_by_kind_never_shown():
    out = io.StringIO()
    b, _ = _bench([], out=out)
    b.verbose = True
    _cable(b, b"".join([
        _lvgl_line("wrn", b"lv_draw_buf_create_ex",
                   b"No memory: " + SECRET.encode()),
        _zephyr_line("err", "sk_private_mod", SECRET.encode()),
        _zephyr_line("inf", "os", SECRET.encode()),
        b"[proto] host status: " + SECRET.encode() + b"\r\n",
        b"[zzz] " + SECRET.encode() + b"\r\n",
        SECRET.encode() + b"\r\n",
    ]))
    b.summary()
    text = out.getvalue()
    assert SECRET not in text and "PRIVATE" not in text
    assert "sk_private_mod" not in text
    # One LVGL warning is one line: its colour-reset tail is folded in.
    assert b.unparsed_kinds == {"log wrn lvgl": 1, "log err other": 1,
                                "log inf os": 1, "printk [proto]": 1,
                                "other": 2}
    assert "by kind:" in text and "log wrn lvgl 1" in text


def _glue_line(level, text):
    """A line Zephyr's LVGL glue logs itself (zephyr/modules/lvgl/input/
    lvgl_pointer_input.c: LOG_WRN("Could not put input data into queue")):
    module lvgl, but no LVGL timestamp and no function."""
    return _zephyr_line(level, "lvgl", text)


def test_lvgl_lines_are_told_apart_by_function_and_pool_markers():
    out = io.StringIO()
    b, _ = _bench([], out=out)
    _cable(b, b"".join([
        _lvgl_line("wrn", b"lv_draw_buf_create_ex",
                   b"No memory: 320x40, cf: 18, " + SECRET.encode()),
        _lvgl_line("wrn", b"lv_draw_buf_create_ex", b"No memory: 8x8"),
        _lvgl_line("wrn", b"lv_draw_layer_alloc_buf",
                   b"Allocating layer buffer failed. Try later"),
        _lvgl_line("err", b"lv_obj_delete", b"x"),
        # Identifier-shaped, but not on the list: counted, not named.
        _lvgl_line("wrn", b"sk_private_fn", b"y"),
        _lvgl_line("wrn", SECRET.encode(), b"y"),
        _lvgl_line("wrn", b"Bad Name", b"z"),
        # The markers count wherever they appear, but only as a number.
        b"No memory " + SECRET.encode() + b"\r\n",
    ]))
    b.summary()
    text = out.getvalue()
    assert SECRET not in text and "PRIVATE" not in text
    assert "Bad Name" not in text and "sk_private_fn" not in text
    assert "lv_obj_delete" not in text
    assert b.lvgl_sources == {"wrn lv_draw_buf_create_ex": 2,
                              "wrn lv_draw_layer_alloc_buf": 1,
                              "err (other LVGL function)": 1,
                              "wrn (other LVGL function)": 2,
                              # Not LVGL's shape at all: read as glue.
                              "wrn (other glue line)": 1}
    assert b.pool_dry == 4
    assert "wrn lv_draw_buf_create_ex 2" in text
    assert "LVGL OUT OF MEMORY: 4 line(s)" in text
    # The message text itself is never shown, markers included.
    assert "320x40" not in text and "Try later" not in text


# Every allocation-failure line, in the shape it reaches the console. The
# texts are LVGL 9's own (modules/lib/gui/lvgl/src in Zephyr 4.3.1) and the
# glue's, each one a case the marker list must catch.
_OOM_LINES = [
    # LV_ASSERT_MALLOC (src/misc/lv_assert.h), on in this build: the line
    # that actually reaches the console, since lv_malloc's own message is
    # LV_LOG_INFO and filtered at WARN.
    ("err", b"lv_draw_add_task",
     b"Asserted at expression: new_task != NULL (Out of memory)"),
    ("err", b"lv_obj_class_create_obj",
     b"Asserted at expression: obj != NULL (Out of memory)"),
    ("wrn", b"allocate_item", b"Failed to allocate item for the gradient"),
    ("wrn", b"lv_cache_entry_alloc", b"malloc failed"),
    ("err", b"lv_realloc", b"couldn't reallocate memory"),
    ("err", b"lv_image_decoder_post_process",
     b"No memory for premultiplying."),
    ("wrn", b"lv_draw_layer_alloc_buf",
     b"LV_DRAW_LAYER_MAX_MEMORY was reached when allocating the layer."),
    ("wrn", b"lv_draw_sw_grad_get", b"alloc grad item failed, no free slot"),
]


@pytest.mark.parametrize("level,func,msg", _OOM_LINES)
def test_every_allocation_failure_fails_the_run(level, func, msg):
    out = io.StringIO()
    b, _ = _bench([], out=out)
    _cable(b, _lvgl_line(level, func, msg))
    b.summary()
    assert b.pool_dry == 1, msg
    assert any(f.startswith("LVGL out of memory: 1 line") for f in b.failures)
    assert "all expectations held" not in out.getvalue()
    assert msg.decode() not in out.getvalue()


def test_the_assert_shape_is_counted_on_its_own():
    out = io.StringIO()
    b, _ = _bench([], out=out)
    _cable(b, b"".join(_lvgl_line(*l) for l in _OOM_LINES[:3]))
    b.summary()
    assert (b.pool_dry, b.pool_dry_asserts) == (3, 2)
    assert "3 line(s), 2 of them LV_ASSERT_MALLOC" in out.getvalue()


def test_the_glue_s_rendering_buffer_failure_counts_too():
    b, _ = _bench([], out=io.StringIO())
    _cable(b, _glue_line(
        "err", b"Failed to allocate memory for rendering buffer"))
    assert b.pool_dry == 1


def test_an_out_of_memory_run_exits_non_zero(monkeypatch):
    """main()'s exit code is `1 if bench.failures`; the hit has to be one."""
    b, _ = _bench([], out=io.StringIO())
    _cable(b, _lvgl_line(*_OOM_LINES[0]))
    b.summary()
    b.summary()     # printed twice, recorded once
    assert sum(f.startswith("LVGL out of memory") for f in b.failures) == 1
    assert "return 1 if bench.failures else 0" in TOOL.read_text()


def test_the_input_queue_warning_has_a_count_of_its_own():
    """The benign reading of M1, told apart from a dry pool by a number."""
    out = io.StringIO()
    b, _ = _bench([], out=out)
    _cable(b, b"".join(
        [_glue_line("wrn", b"Could not put input data into queue")] * 3
        + [_glue_line("wrn", b"something: " + SECRET.encode())]))
    b.summary()
    text = out.getvalue()
    assert b.input_queue_full == 3
    assert "input queue full (Zephyr LVGL input glue): 3" in text
    assert b.lvgl_sources == {"wrn (glue: input queue full)": 3,
                              "wrn (other glue line)": 1}
    # A glue line's first word is never read as a function name.
    assert "something" not in text and SECRET not in text
    assert b.pool_dry == 0 and b.failures == []


def test_without_the_lvgl_timestamp_no_name_is_taken():
    b, _ = _bench([], out=io.StringIO())
    _cable(b, _zephyr_line("wrn", "lvgl", b"lv_draw_buf_create_ex: x"))
    assert b.lvgl_sources == {"wrn (other glue line)": 1}


def _zephyr_43():
    """The Zephyr 4.3 workspace, read-only, from the account's real home
    (under pytest ~ is tmp_path), as test_board_touch.py's glue check finds
    it. None where there is none (CI)."""
    import glob
    import os
    try:
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        return None
    roots = [os.environ.get("BLINK_ZEPHYR", "")] + glob.glob(
        os.path.join(real, "Projects", "zephyr-v4.3*")) + glob.glob(
        os.path.join(real, "zephyr-v4.3*"))
    for r in roots:
        if r and (Path(r) / "modules" / "lib" / "gui" / "lvgl" / "src").is_dir():
            return Path(r)
    return None


def test_the_lists_match_lvgl_and_the_glue_when_they_are_here():
    """Every name on _LVGL_FUNCS is a function LVGL defines, every glue
    message is one the glue logs, and every out-of-memory line in
    _OOM_LINES is text LVGL carries -- pinned to the source the lists were
    derived from, where it is on this machine."""
    for name in bt._LVGL_FUNCS:
        assert re.fullmatch(r"[a-z_][a-z0-9_]{0,63}", name), name
    ws = _zephyr_43()
    if ws is None:
        pytest.skip("no Zephyr 4.3 workspace here")
    src = ws / "modules" / "lib" / "gui" / "lvgl" / "src"
    lv = "\n".join(f.read_text(encoding="utf-8", errors="replace")
                   for f in src.rglob("*.[ch]"))
    for name in bt._LVGL_FUNCS:
        assert re.search(rf"^\S[^\n;]*\b{name}\(", lv, re.M), name
    glue = "\n".join(f.read_text(encoding="utf-8")
                     for f in (ws / "zephyr" / "modules" / "lvgl").rglob("*.c"))
    for mark, _ in bt._LVGL_GLUE:
        assert f'LOG_WRN("{mark.decode()}")' in glue, mark
    assert '"Failed to allocate memory for rendering buffer"' in glue
    assert 'LV_LOG_ERROR("Asserted at expression: %s (%s)"' in lv
    assert 'LV_ASSERT_MSG(p != NULL, "Out of memory")' in lv
    for _, _, msg in _OOM_LINES:
        text = msg.decode().replace("new_task != NULL", "%s").replace(
            "obj != NULL", "%s")
        if text.startswith("Asserted at expression"):
            continue
        assert text in lv, text


def test_a_clean_run_says_no_lvgl_pool_failures():
    out = io.StringIO()
    b, _ = _bench([], out=out)
    _cable(b, _glue_line("wrn", b"Could not put input data into queue"))
    b.summary()
    assert b.pool_dry == 0
    assert "LVGL out-of-memory lines: 0" in out.getvalue()
    assert "all expectations held" in out.getvalue()


def test_a_colour_only_line_is_not_counted():
    b, _ = _bench([], out=io.StringIO())
    for raw in (b"\x1b[0m", b"\x1b[0m\r", b"\x1b[1;33m\x1b[0m"):
        b.on_line(raw)
    assert b.unparsed == 0


def test_a_dropped_log_message_is_said_and_explains_a_lost_reply():
    out = io.StringIO()
    b, link = _bench([], out=out)
    real = link.write

    def lossy(raw):
        # The board played the tap, but its touch_done was pushed out of
        # the deferred log: only Zephyr's drop marker reaches the cable.
        if json.loads(raw)["t"] == "touch":
            b.on_line(b"--- 3 messages dropped ---")
            return
        real(raw)
    link.write = lossy
    assert not b.stroke(bt.touch_tap(10, 10), timeout=0.2)
    assert "dropped 3 message(s)" in b.failures[-1]
    assert "CONFIG_BLINK_TOUCH_INJECT image" not in b.failures[-1]
    assert b.log_dropped == 3
    b.summary()
    assert "the board's log dropped 3 message(s)" in out.getvalue()


def test_the_printk_tags_come_from_the_firmware():
    tags = bt._printk_tags()
    assert {"proto", "ota", "mem", "touch"} <= tags


def test_the_bench_image_alone_gets_a_bigger_log_buffer():
    k = (ROOT / "firmware" / "Kconfig").read_text(encoding="utf-8")
    block = k[k.index("config LOG_BUFFER_SIZE"):]
    block = block[:block.index("source \"Kconfig.zephyr\"")]
    defaults = re.findall(r"^\s*default\s+(.*)$", block, re.M)
    assert defaults == ["8192 if BLINK_TOUCH_INJECT"]
    # Declared before Zephyr's own, so its default is the one taken.
    assert k.index("config LOG_BUFFER_SIZE") < k.rindex('source "Kconfig.zephyr"')
    for conf in (ROOT / "firmware").glob("*.conf"):
        assert "LOG_BUFFER_SIZE" not in conf.read_text(encoding="utf-8")


def test_every_reply_kicks_the_log_thread():
    code = (SRC / "touch_inject.c").read_text()
    for head in ("static void refuse(const char *why)\n{",
                 "static void ui_state_line(void)\n{",
                 "static void widgets_line(const char *json)\n{",
                 "void touch_inject_service(void)\n{"):
        body = code[code.index(head):]
        body = body[:body.index("\n}\n")]
        assert "TI_LOG_KICK()" in body, head
