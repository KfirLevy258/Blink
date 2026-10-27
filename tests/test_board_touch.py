"""tools/board_touch.py, the PC half of the bench touch injector.

No board and no serial port here: the tool's Link is replaced by a fake that
records what was written and answers the way a CONFIG_BLINK_TOUCH_INJECT board
does. Everything that can go wrong without a board is pinned:

  - the script parser, line numbers and all, and the shipped default script;
  - that every coordinate it taps is the one the firmware layout code puts
    the control at (the constants are read back out of the C source);
  - that nothing read from the board is ever printed raw -- an unparsed line
    is a byte count and nothing more (CLAUDE.md: never log a serial buffer);
  - that every line it writes went through protocol.encode_checked, and
    that the source never sets DTR or RTS, which would reset the board.
"""
import ast
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
    spec = importlib.util.spec_from_file_location("board_touch", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bt = _load()


# --- the script language ---------------------------------------------------

def test_a_script_parses_into_steps_with_their_line_numbers():
    steps = bt.parse_script(
        "# a comment\n"
        "\n"
        "tap settings.open\n"
        "raw tap 10 20\n"
        "raw tap 10 20 300\n"
        "swipe 260 120 100 120\n"
        "swipe 260 120 100 120 400\n"
        "press 5 6\n"
        "release\n"
        "expect ui_state scr=settings ov=none\n"
        "expect theme_now why=user id=9\n"
        "expect theme_edit\n"
        "expect notice none\n"
        "wait 1.5\n"
        "mem\n"
        "state\n")
    got = [(s.line, s.cmd, s.args) for s in steps]
    assert got == [
        (3, "tap", ("settings.open",)),
        (4, "raw_tap", (10, 20, bt.TAP_MS)),
        (5, "raw_tap", (10, 20, 300)),
        (6, "swipe", (260, 120, 100, 120, bt.SWIPE_MS)),
        (7, "swipe", (260, 120, 100, 120, 400)),
        (8, "press", (5, 6)),
        (9, "release", ()),
        (10, "expect", ("ui_state", {"scr": "settings", "ov": "none"})),
        (11, "expect", ("theme_now", {"why": "user", "id": "9"})),
        (12, "expect", ("theme_edit", {})),
        (13, "expect", ("notice", {"nt": "none"})),
        (14, "wait", (1.5,)),
        (15, "mem", ()),
        (16, "state", ()),
    ]


@pytest.mark.parametrize("line, why", [
    ("tap no.such.target", "unknown target"),
    ("raw tap 10", "raw tap X Y"),
    ("raw tap 400 20", "off the panel"),
    ("swipe 1 2 3", "swipe X0 Y0 X1 Y1"),
    ("expect ui_state scr", "key=value"),
    ("expect nonsense a=b", "expect ui_state"),
    ("wait soon", "wait S"),
    ("dance", "unknown command"),
])
def test_a_bad_line_is_refused_with_its_number(line, why):
    with pytest.raises(bt.ScriptError) as e:
        bt.parse_script("mem\n" + line + "\n")
    assert "line 2" in str(e.value)
    assert why in str(e.value)


def test_the_default_script_parses_and_covers_the_whole_list():
    path = ROOT / "tools" / "bench" / "full_touch.txt"
    steps = bt.parse_script(path.read_text(encoding="utf-8"))
    taps = [s.args[0] for s in steps if s.cmd == "tap"]
    # Settings opened at least three times, by both routes.
    assert sum(t in ("settings.open", "settings.swipe") for t in taps) >= 3
    assert "settings.swipe" in taps
    for name in ("brightness.open", "brightness.level3", "theme.open",
                 "theme.light", "theme.dark", "theme.edit_on_computer",
                 "focus.open", "focus.session", "focus.weekly", "focus.off",
                 "eye.swipe", "eye.exit", "back.swipe", "settings.back"):
        assert name in taps, name
    assert taps.index("theme.light") < taps.index("theme.dark")
    # Ends with Focus off and then Theme Dark, and says so.
    last_focus = max(i for i, t in enumerate(taps) if t.startswith("focus."))
    assert taps[last_focus] == "focus.off"
    assert taps.index("theme.dark") > last_focus
    assert any(s.cmd == "mem" for s in steps)
    assert any(s.cmd == "expect" and s.args[0] == "theme_now"
               and s.args[1] == {"why": "user", "id": "9"} for s in steps)
    assert any(s.cmd == "expect" and s.args[0] == "theme_edit"
               for s in steps)
    # Focus is checked on the gauges, not only in the store.
    gf = [s.args[1]["gf"] for s in steps if s.cmd == "expect"
          and s.args[0] == "ui_state" and "gf" in s.args[1]]
    assert {"session", "weekly", "off"} <= set(gf)
    assert any(s.cmd == "expect" and s.args[0] == "notice" for s in steps)
    # The full eye swipe stays last.
    assert [t for t in taps if t.startswith("eye.")][-2:] == [
        "eye.swipe", "eye.exit"]
    assert taps[-1] == "eye.exit"


# --- coordinates from the firmware ----------------------------------------

def _define(name, path):
    m = re.search(rf"^#define {name}\s+(\d+)", path.read_text(), re.M)
    assert m, name
    return int(m.group(1))


def test_the_layout_mirror_matches_the_firmware_defines():
    settings = SRC / "ui_settings.c"
    for name, value in bt.FW_LAYOUT.items():
        assert _define(name, settings) == value, name


def test_the_layout_literals_are_still_where_the_targets_assume():
    """The numbers that are literals in the C rather than defines, found in
    the calls that place them. If one of these moves, a target moves with it."""
    c = (SRC / "ui_settings.c").read_text()
    assert "lv_obj_set_size(z, 44, 150);" in c                 # edge zones
    assert re.search(r"lv_obj_set_size\(b, 60, 36\);\s*"
                     r"lv_obj_align\(b, LV_ALIGN_TOP_LEFT, 6, 2\);", c)
    assert "lv_obj_set_size(seg[i], 56, 116);" in c            # bright stops
    assert "LV_ALIGN_TOP_LEFT, 12 + i * 60, 86);" in c
    assert "uint8_t lvl = 20 + i * 20;" in c
    assert "12 + (k % 2) * (THEME_TILE_W + 8)," in c           # theme grid
    assert "(k / 2) * (THEME_TILE_H + THEME_TILE_GAP));" in c
    assert re.search(r"lv_obj_set_size\(pg, 60, 36\);\s*"
                     r"lv_obj_align\(pg, LV_ALIGN_TOP_RIGHT, -6, 2\);", c)
    assert "12 + i * (FOCUS_BTN_W + FOCUS_BTN_GAP), FOCUS_BTN_Y);" in c
    assert "LV_ALIGN_TOP_LEFT, 12 + (k % 2) * (TILE_W + TILE_GAP)," in c
    assert "TILE_TOP + (k / 2) * (TILE_H + TILE_GAP));" in c  # tiles


def test_the_theme_ids_match_theme_h():
    h = (SRC / "theme.h").read_text()
    for name, tid in bt.THEME_IDS.items():
        assert re.search(rf"THEME_{name.upper()}\s*=\s*{tid},", h), name


def test_every_tap_target_lands_inside_the_panel():
    for name, t in bt.TARGETS.items():
        for x, y in t.points():
            assert 0 <= x < 320 and 0 <= y < 240, name


def test_a_theme_is_found_on_its_page_and_slot():
    # Five a page and the Edit slot sixth, when the app can open the builder.
    assert bt.theme_slot(bt.THEME_IDS["dark"]) == (0, 0)
    assert bt.theme_slot(bt.THEME_IDS["light"]) == (1, 4)
    assert bt.theme_slot(bt.THEME_IDS["light_ink"]) == (2, 1)
    assert bt.slot_center(0) == (84, 74)
    assert bt.slot_center(4) == (84, 198)
    assert bt.slot_center(5) == (236, 198)


def test_strokes_on_the_wire_are_the_firmware_s_shape():
    assert bt.touch_tap(298, 120, 120) == {
        "t": "touch", "v": 2, "op": "tap", "x": 298, "y": 120, "ms": 120}
    assert bt.touch_swipe(260, 120, 100, 120, 260) == {
        "t": "touch", "v": 2, "op": "swipe", "x0": 260, "y0": 120,
        "x1": 100, "y1": 120, "ms": 260}


# --- the fake board -----------------------------------------------------

class FakeLink:
    """Records writes; answers touch and ui_state the way the board does."""

    def __init__(self, bench_ref, state=None):
        self.writes = []
        self.bench_ref = bench_ref
        self.state = dict(scr="gauges", ov="none", modal="none", busy=0,
                          theme=0, stored=0, tp=0, focus="off", bl=60,
                          edit="name", slp=0, nt="none", gf="off")
        if state:
            self.state.update(state)

    def write(self, raw):
        assert isinstance(raw, bytes)
        self.writes.append(raw)
        msg = json.loads(raw)
        b = self.bench_ref()
        if msg["t"] == "touch":
            if msg["op"] == "dismiss":
                self.state.update(modal="none", nt="none")
            elif msg["op"] == "wake":
                self.state.update(slp=0)
            b.on_line(json.dumps({"t": "touch_done", "v": 2,
                                  "op": msg["op"], "ok": 1}).encode())
        elif msg["t"] == "ui_state":
            b.on_line(json.dumps(dict(t="ui_state", v=2,
                                      **self.state)).encode())


def _bench(state=None, out=None):
    holder = {}
    link = FakeLink(lambda: holder["b"], state)
    b = bt.Bench(link, out=out or io.StringIO(), settle_scale=0)
    holder["b"] = b
    return b, link


def test_nothing_raw_from_the_board_is_ever_printed():
    out = io.StringIO()
    b, _ = _bench(out=out)
    secret = "sk-ant-PRIVATE-session-content-7f3a"
    b.verbose = True
    for raw in (
        secret.encode(),
        ("[usage] session 42% " + secret).encode(),
        json.dumps({"t": "mystery", "v": 2, "x": secret}).encode(),
        json.dumps({"t": "theme_now", "v": 2, "id": 9, "why": secret}).encode(),
        json.dumps({"t": "ui_state", "v": 2, "scr": secret}).encode(),
        b"\xff\xfe" + secret.encode() + b"\x00",
        ("[mem] lvgl " + secret + ": used=1 free=2").encode(),
    ):
        b.on_line(raw)
    text = out.getvalue()
    assert secret not in text
    assert "PRIVATE" not in text
    assert "unparsed line (" in text


def test_a_mem_line_is_read_into_numbers():
    b, _ = _bench()
    b.on_line(b"[mem] lvgl theme-close: used=18000 free=12632 largest=9000 "
              b"peak=26640 boot_peak=27000 pool=30632")
    (m,) = b.mem_lines
    assert m == {"where": "theme-close", "used": 18000, "free": 12632,
                 "largest": 9000, "peak": 26640, "boot_peak": 27000,
                 "pool": 30632}


def test_a_ui_state_line_keeps_only_known_fields_in_range():
    st = bt.parse_ui_state({"t": "ui_state", "v": 2, "scr": "settings",
                            "ov": "theme", "modal": "none", "busy": 0,
                            "theme": 9, "stored": 9, "tp": 1,
                            "focus": "off", "bl": 60, "edit": "opened",
                            "slp": 0, "nt": "none", "gf": "off",
                            "extra": "dropped"})
    assert st == {"scr": "settings", "ov": "theme", "modal": "none",
                  "busy": 0, "theme": 9, "stored": 9, "tp": 1,
                  "focus": "off", "bl": 60, "edit": "opened",
                  "slp": 0, "nt": "none", "gf": "off"}
    assert bt.parse_ui_state({"t": "ui_state", "scr": "x" * 99}) is None


def test_every_write_went_through_encode_checked(monkeypatch):
    seen = []
    real = bt.protocol.encode_checked

    def spy(msg):
        raw, why = real(msg)
        seen.append(raw)
        return raw, why

    monkeypatch.setattr(bt.protocol, "encode_checked", spy)
    b, link = _bench()
    b.greet()
    b.send_frames()
    b.run_target("settings.open")
    b.query_state()
    assert link.writes and all(w in seen for w in link.writes)


def test_an_over_long_line_is_not_written(monkeypatch):
    b, link = _bench()
    monkeypatch.setattr(bt.protocol, "MAX_LINE_BYTES", 10)
    assert b.send({"t": "touch", "v": 2, "op": "tap", "x": 1, "y": 1,
                   "ms": 1}) is False
    assert link.writes == []


def test_the_source_writes_only_through_send_and_never_sets_dtr_or_rts():
    text = TOOL.read_text(encoding="utf-8")
    tree = ast.parse(text)
    for node in ast.walk(tree):
        # No .dtr / .rts attribute anywhere, read or written, and no
        # keyword of either name to a call (serial.Serial(dtr=...)).
        if isinstance(node, ast.Attribute):
            assert node.attr not in ("dtr", "rts", "setDTR", "setRTS",
                                     "dsrdtr", "rtscts"), node.attr
        if isinstance(node, ast.keyword):
            assert node.arg not in ("dtr", "rts", "dsrdtr", "rtscts"), node.arg
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == "encode":
                # protocol.encode would skip the line budget.
                assert not (isinstance(f.value, ast.Name)
                            and f.value.id == "protocol")
    # The one open: plain Serial(port, baud, timeout=...).
    opens = re.findall(r"serial\.Serial\(([^)]*)\)", text)
    assert opens == ["port, baud, timeout=0.2"]
    # Exactly one place writes to the port (Link.write), and its one caller
    # is Bench.send, which is fed by encode_checked.
    assert len(re.findall(r"\bser\.write\(", text)) == 1
    assert len(re.findall(r"\blink\.write\(", text)) == 1
    send = text[text.index("    def send(self, msg):"):]
    send = send[:send.index("\n    def ", 1)]
    assert "protocol.encode_checked(msg)" in send
    assert "self.link.write(raw)" in send


def test_a_named_target_checks_the_state_it_leads_to():
    b, link = _bench()
    link.state.update(scr="gauges")
    # The fake board does not change screens, so the expectation fails.
    assert b.run_target("settings.open") is False
    assert b.failures
    b2, link2 = _bench(state={"scr": "settings"})
    # A target that needs the gauges refuses before touching anything.
    assert b2.run_target("settings.open") is False
    assert not [w for w in link2.writes if b'"touch"' in w]


def test_expect_ui_state_passes_and_fails_on_the_fields_it_names():
    b, _ = _bench(state={"scr": "settings", "ov": "focus", "focus": "weekly"})
    assert b.expect_state({"scr": "settings", "focus": "weekly"}, timeout=0)
    assert not b.expect_state({"focus": "off"}, timeout=0)
    assert len(b.failures) == 1


def test_expect_theme_now_matches_what_arrived_since_the_last_action():
    b, _ = _bench()
    b.mark()
    b.on_line(b'{"t":"theme_now","v":2,"id":9,"why":"user"}')
    assert b.expect_event("theme_now", {"why": "user", "id": "9"}, timeout=0)
    assert not b.expect_event("theme_now", {"why": "user", "id": "0"},
                              timeout=0)


def test_a_theme_edit_from_the_board_is_answered(monkeypatch):
    b, link = _bench()
    b.board_caps = frozenset({"theme"})
    b.on_line(b'{"t":"theme_edit","v":2}')
    assert json.loads(link.writes[-1]) == {"t": "theme_edit", "v": 2, "ok": 1}


def test_the_ping_is_answered():
    b, link = _bench()
    b.on_line(b'{"t":"ping","v":2,"up_ms":5}')
    assert json.loads(link.writes[-1]) == {"t": "pong", "v": 2}


def test_the_usage_is_made_up_and_fits_the_line():
    b, link = _bench()
    b.send_frames()
    usage = [json.loads(w) for w in link.writes
             if json.loads(w)["t"] == "usage"]
    assert usage and usage[0]["provider"] == "claude"
    assert usage[0]["p2"] == "codex"
    assert all(len(w) <= 512 for w in link.writes)


def test_the_summary_lists_each_screen_s_peak_against_the_pool():
    out = io.StringIO()
    b, _ = _bench(out=out)
    for where, peak in (("gauges", 21000), ("settings", 25000),
                        ("theme-close", 26640), ("settings", 25496)):
        b.on_line(f"[mem] lvgl {where}: used=1 free=2 largest=3 peak={peak} "
                  f"boot_peak=27000 pool=30632".encode())
    b.summary()
    text = out.getvalue()
    assert "30632" in text
    row = next(l for l in text.splitlines() if l.strip().startswith("settings "))
    assert "25496" in row and "83.2%" in row
    assert "theme-close" in text


def test_preflight_refuses_a_port_someone_else_holds():
    def fake_run(cmd, **kw):
        class R:
            returncode = 0
            stdout = "4242\n"
        return R()

    why = bt.port_holders("/dev/cu.usbserial-1", run=fake_run)
    assert why == [4242]


# --- staying awake, and notices -------------------------------------------

def test_a_notice_is_dismissed_before_an_action_and_said_so():
    out = io.StringIO()
    b, link = _bench(state={"modal": "notice", "nt": "ota"}, out=out)
    b.run_target("settings.open")
    ops = [json.loads(w).get("op") for w in link.writes
           if json.loads(w)["t"] == "touch"]
    assert ops[0] == "dismiss"
    assert "notice dismissed (ota)" in out.getvalue()


def test_a_dozing_board_is_woken_before_an_action():
    out = io.StringIO()
    b, link = _bench(state={"slp": 1, "modal": "notice", "nt": "sleep"},
                     out=out)
    b.run_target("settings.open")
    ops = [json.loads(w).get("op") for w in link.writes
           if json.loads(w)["t"] == "touch"]
    assert ops[:2] == ["wake", "dismiss"] or ops[:2] == ["dismiss", "wake"]
    assert "notice dismissed (sleep)" in out.getvalue()


def test_expect_notice_none_fails_while_one_is_up():
    b, _ = _bench(state={"modal": "notice", "nt": "other"})
    assert not b.run_step(bt.parse_line("expect notice none"))
    assert b.failures
    b2, _ = _bench()
    assert b2.run_step(bt.parse_line("expect notice none"))


def test_the_ota_query_is_answered_as_a_daemon_with_nothing_new_does():
    """Unanswered, the board's own 30 s watchdog puts up "Couldn't check
    for updates" over the gauges -- the notice the first board run hit."""
    b, link = _bench()
    b.on_line(b'{"t":"ota_query","v":2,"cur":"1.3.4"}')
    assert json.loads(link.writes[-1]) == {"t": "ota_none", "v": 2}


def test_the_made_up_usage_moves_a_little_each_minute_and_stays_fresh():
    b, link = _bench()
    b.send_frames()
    b.send_frames()
    usage = [json.loads(w) for w in link.writes
             if json.loads(w)["t"] == "usage"]
    assert usage[0]["session_pct"] != usage[1]["session_pct"]
    # Fresh by the board's rule: active_age_s 0, nowhere near the 4 h
    # SLEEP_ABSENT_AFTER_S that the stale doze waits for.
    assert all(u["active_age_s"] == 0 for u in usage)


# --- putting the owner's board back ----------------------------------------

START = dict(scr="gauges", ov="none", modal="none", busy=0, theme=3,
             stored=3, tp=0, focus="session", bl=20, edit="name", slp=0,
             nt="none", gf="session")


def test_restore_puts_back_theme_brightness_and_focus():
    now = dict(START, theme=0, stored=0, focus="off", gf="off", bl=60)
    plan = bt.restore_plan(START, now, start_h=None)
    # A built-in theme goes back the way the PC sends one: a save line.
    assert plan[0] == ("line", {"t": "theme", "v": 2, "op": "save", "id": 3})
    ui = [name for kind, name in plan if kind == "target"]
    assert ui == ["settings.open", "brightness.open", "brightness.level1",
                  "settings.back", "focus.open", "focus.session",
                  "settings.back", "settings.back"]


def test_restore_does_nothing_when_nothing_changed():
    assert bt.restore_plan(START, dict(START), start_h=None) == []


def test_a_custom_theme_goes_back_as_the_line_the_pc_would_send():
    from pc import theme as pc_theme
    from pc.cli import blink_home
    t = {"id": 12, "c": ["14121C", "F2EEF8", "7C9CFF", "5ED6A0", "5ED6A0",
                         "F0B04A", "FF5A5A"], "w": 22, "ends": "flat",
         "dot": 0, "by": "gauge"}
    pc_theme.save(blink_home(), t)
    h = pc_theme.theme_hash(t)
    start = dict(START, theme=12, stored=12)
    now = dict(START, theme=0, stored=0)
    plan = bt.restore_plan(start, now, start_h=h)
    assert plan == [("line", pc_theme.message("save", t))]


def test_a_custom_theme_nobody_here_saved_is_reselected_on_the_panel():
    """No theme.json to match the board's hash: the Custom tile reselects
    the record the board itself still holds."""
    start = dict(START, theme=12, stored=12)
    now = dict(START, theme=0, stored=0)
    plan = bt.restore_plan(start, now, start_h="deadbeef")
    assert [n for k, n in plan if k == "target"] == [
        "settings.open", "theme.open", "theme.custom", "settings.back",
        "settings.back"]


def test_restore_runs_after_a_failed_and_an_interrupted_script():
    for boom in (None, KeyboardInterrupt):
        b, _ = _bench()
        calls = []
        b.restore = lambda start, start_h=None: calls.append(start)
        if boom:
            def explode(s, boom=boom):
                raise boom()
            b.run_step = explode
            with pytest.raises(boom):
                b.run_with_restore(bt.parse_script("mem\n"))
        else:
            b.run_with_restore(bt.parse_script("expect ui_state scr=eyes\n"))
            assert b.failures
        assert len(calls) == 1 and calls[0]["scr"] == "gauges"


def test_no_restore_skips_it():
    b, _ = _bench()
    calls = []
    b.restore = lambda start, start_h=None: calls.append(start)
    b.run_with_restore(bt.parse_script("mem\n"), restore=False)
    assert calls == []


def test_the_tool_tells_the_operator_not_to_touch_the_panel():
    assert "do not touch the panel" in TOOL.read_text().lower()


def test_the_custom_theme_has_a_target():
    assert bt.THEME_IDS["custom"] == 12
    assert "theme.custom" in bt.TARGETS
    assert bt.theme_slot(12) == (2, 2)


def test_the_bench_notice_tag_is_cleared_wherever_a_notice_comes_or_goes():
    """ui_settings.c tags a notice by setting bench_nt right after it is put
    up. Keyed on the object's address it could outlive its popup: LVGL hands
    a freed block straight back to the next lv_obj_create of the same size,
    so a later notice read as the old kind (review, SHOULD-1). Every place
    that creates or deletes `notice` must clear the tag."""
    c = (SRC / "ui_settings.c").read_text()
    sites = [m.start() for m in re.finditer(
        r"notice = lv_obj_create\(|lv_obj_del\(notice\);", c)]
    assert len(sites) >= 4
    for at in sites:
        window = c[max(0, at - 400):at + 400]
        assert "bench_nt_clear()" in window, c[at - 80:at + 80]
    assert "bench_nt_obj" not in c


def test_ui_swipe_reads_the_channels_the_way_the_injector_assumes():
    """The C host test's SWIPE_SCREEN_DX/DY is a copy; this is what pins the
    copy to ui_swipe.c (review, NICE-4)."""
    c = (SRC / "ui_swipe.c").read_text()
    assert re.search(r"#define SCREEN_DX\(chan_x, chan_y\)\s+\(chan_y\)", c)
    assert re.search(r"#define SCREEN_DY\(chan_x, chan_y\)\s+\(chan_x\)", c)
    h = (ROOT / "tests" / "touch_inject" / "host_test.c").read_text()
    assert "#define SWIPE_SCREEN_DX(cx, cy) (cy)" in h
    assert "#define SWIPE_SCREEN_DY(cx, cy) (cx)" in h


def test_the_glue_model_matches_the_zephyr_glue_when_it_is_here():
    """ti_chan_to_screen() models lvgl_pointer_input.c's invert-x branch and
    its ROTATED_90 case. Pinned against the real file when a Zephyr
    workspace is on this machine; skipped where there is none (CI)."""
    import glob
    import os
    # Read-only, and from the account database: under pytest ~ is tmp_path.
    try:
        import pwd
        real = pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError):
        pytest.skip("no account home to look in")
    roots = [os.environ.get("BLINK_ZEPHYR", "")] + glob.glob(
        os.path.join(real, "Projects", "zephyr-v4.3*")) + glob.glob(
        os.path.join(real, "zephyr-v4.3*"))
    found = [os.path.join(r, "zephyr", "modules", "lvgl", "input",
                          "lvgl_pointer_input.c") for r in roots if r]
    found = [f for f in found if os.path.isfile(f)]
    if not found:
        pytest.skip("no Zephyr 4.3 workspace here")
    g = Path(found[0]).read_text()
    assert "tmp_point.x = cap->y_resolution - tmp_point.x;" in g
    assert re.search(r"case DISPLAY_ORIENTATION_ROTATED_90:\s*"
                     r"point->x = tmp_point\.y;\s*"
                     r"point->y = cap->y_resolution - tmp_point\.x;", g)
