"""The sessions card's firmware half, held by its source (issue #18,
docs/sessions-card-design.md).

The card itself is drawn by LVGL and run by tests/board/sessions_card_touch.txt
on the touch harness and on a board; these are the promises a reader of the
source can check without either: the board says it can take rows, the wire
arm and the page request exist, the tap keeps the swipes alive, the card
never stacks on a popup, and the bench script parses.
"""
import importlib.util
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "firmware" / "src"
TOOL = ROOT / "tools" / "board_touch.py"
SCRIPT = ROOT / "tests" / "board" / "sessions_card_touch.txt"


def _src(name):
    return (SRC / name).read_text(encoding="utf-8")


def _func(src, name):
    """The body of a C function defined in `src`, from its signature to the
    closing brace at column 0. A parameter list may hold one level of
    parentheses: ui_settings_service takes a function pointer."""
    m = re.search(r"\n[a-z][a-z_ \*]*\b" + re.escape(name) +
                  r"\((?:[^()]|\([^()]*\))*\)\n\{(.*?)\n\}\n", src, re.S)
    assert m, f"{name} is gone"
    return m.group(1)


def _load_board_touch():
    spec = importlib.util.spec_from_file_location("board_touch_sc", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_board_caps_lists_sessions():
    src = _src("proto.c")
    caps = re.search(r'#define BOARD_CAPS "\\"caps\\":\[(.*?)\]"', src)
    assert caps, "BOARD_CAPS is gone from proto.c"
    assert "sessions" in re.findall(r'\\"([a-z_]+)\\"', caps.group(1))


def test_proto_has_the_srow_arm_and_sends_spage():
    src = _src("proto.c")
    dispatch = _func(src, "dispatch")
    arm = re.search(r'strcmp\(type, "srow"\) == 0\)(.*?)\} else if',
                    dispatch, re.S)
    assert arm, "dispatch has no srow arm"
    assert "msg_parse_srow(" in arm.group(1)
    assert "usage_view_set_srow(" in arm.group(1)
    # Never log a buffer: the arm prints nothing at all.
    assert "printk" not in arm.group(1)
    spage = _func(src, "proto_send_spage")
    assert '\\"spage\\"' in spage and '\\"p\\"' in spage
    assert "emit(buf)" in spage
    # HOST LOST forgets the rows.
    assert "usage_view_clear_srows();" in _func(src, "proto_service")
    assert "void proto_send_spage(int p);" in _src("proto.h")


def test_the_tap_area_keeps_gestures_bubbling():
    src = _src("usage_view.c")
    init = _func(src, "usage_view_init")
    i = init.index("sess_tap = lv_obj_create(scr);")
    j = init.index("sess_tap_cb, LV_EVENT_CLICKED", i)
    block = init[i:j + 40]
    assert "lv_obj_add_flag(sess_tap, LV_OBJ_FLAG_GESTURE_BUBBLE);" in block
    assert "LV_OBJ_FLAG_CLICKABLE" not in block, \
        "the tap area must stay clickable"
    assert "sess_tap_cb, LV_EVENT_CLICKED" in block
    # Its callback raises a flag and opens nothing itself.
    cb = _func(src, "sess_tap_cb")
    assert "sess_tap_req = true;" in cb
    assert "lv_obj_create" not in cb and "ui_settings" not in cb


def test_the_card_uses_mk_back_and_counts_as_busy():
    src = _src("ui_settings.c")
    build = _func(src, "sess_build")
    assert "mk_back(ov, sess_back_cb);" in build
    assert "LV_OBJ_FLAG_GESTURE_BUBBLE" in build
    # Neither the ask box nor the update offer may go up over it...
    ask = _func(src, "ask_tick")
    assert "sess_ov" in ask
    upd = _func(src, "upd_timer_cb")
    assert re.search(r"ask_box \|\|\s*sess_ov", upd)
    # ...and it opens over none of them.
    can = _func(src, "sess_can_open")
    for name in ("panel", "confirm", "notice", "wn", "dl_overlay", "ask_box",
                 "upd_prompt"):
        assert re.search(r"\b" + name + r" == NULL", can), name
    # Swipes on it are its own, and it closes on a doze.
    assert "sess_swipe(dir);" in _func(src, "swipe_cb")
    assert "sess_close();" in _func(src, "ui_settings_dozing")
    assert "sess_service();" in _func(src, "ui_settings_service")
    assert "sess_rebuild_owed = true;" in _func(src, "ui_settings_apply_theme")


def test_a_closed_card_goes_without_lv_async_call():
    """lv_obj_delete_async queues through lv_async_call, which allocates
    from the LVGL pool and ignores a failure; the card is hidden at once and
    deleted by the mode loop instead, and a bounce on the dots under the
    back button cannot reopen it. (First written for the platform's
    2026-11-25 board run, which it did not explain: that run failed at
    full_touch.txt's first `tap settings.open` because it began during the
    boot clip; see test_ui_state_is_busy_until_the_gauges_are_attached.)"""
    src = _src("ui_settings.c")
    assert re.search(r"^static lv_obj_t \*sess_doomed;", src, re.M)
    later = _func(src, "sess_close_later")
    assert "lv_obj_delete_async" not in later
    assert "lv_async_call" not in later
    assert "LV_OBJ_FLAG_HIDDEN" in later and "LV_OBJ_FLAG_CLICKABLE" in later
    assert "sess_doomed = o;" in later
    # Deleted on the mode loop's pass, first thing, and by the doze too.
    service = _func(src, "sess_service")
    assert service.index("sess_reap();") < service.index(
        "usage_view_take_sessions_tap()")
    assert "sess_reap();" in _func(src, "sess_close")
    assert "lv_obj_delete(o);" in _func(src, "sess_reap")
    # A card deleted with its screen leaves no dangling pointer.
    assert "sess_doomed = NULL;" in _func(src, "sess_deleted_cb")
    # Either close drops a raised tap and starts the reopen guard...
    noted = _func(src, "sess_note_closed")
    assert "usage_view_take_sessions_tap()" in noted
    assert "k_uptime_get()" in noted
    for name in ("sess_close", "sess_close_later"):
        assert "sess_note_closed();" in _func(src, name), name
    # ...which sess_can_open honours.
    assert "SESS_REOPEN_GUARD_MS" in _func(src, "sess_can_open")


def test_ui_state_is_busy_until_the_gauges_are_attached():
    """The platform's board check of 2026-11-25 failed at
    tools/bench/full_touch.txt line 21, its first `tap settings.open`, with
    the board saying scr=gauges throughout. Its console held one [boot] line
    and no boot banner: the logo line ui_boot_splash prints as the clip
    ends, not the edition line it prints as the clip starts. So the run
    began during the boot clip of a board flashed just before; ui_boot.c
    answers the protocol between frames, ui_state then read gauges and
    busy=0, and the tap landed on the splash (an inference from the counts,
    not yet measured on a desk). ui_state is busy until ui_settings_attach
    has run, so the bench's wait_state waits the clip out."""
    src = _src("ui_settings.c")
    assert "bench_attached = true;" in _func(src, "ui_settings_attach")
    bench = _func(src, "ui_settings_bench_state")
    busy = re.search(r"out->busy = (.*?);", bench, re.S).group(1)
    assert re.match(r"\s*!bench_attached\s*\|\|", busy), busy
    # main.c attaches only after the splash, in both of its boot paths.
    main = _src("main.c")
    assert main.count("ui_settings_attach(") == 2
    for m in re.finditer(r"ui_settings_attach\(", main):
        assert main.rfind("ui_boot_splash();", 0, m.start()) != -1


def _code(body):
    """A C body with its comments taken out, so a test of what the code
    does is not satisfied, or tripped, by the comment explaining it."""
    return re.sub(r"/\*.*?\*/", "", body, flags=re.S)


def test_a_swipe_straight_after_back_is_not_dropped_by_the_clock():
    """Review rejected the last round (2026-11-25) for this: for 800 ms
    after the card closed (SESS_STROKE_GUARD_MS), swipe_cb dropped every
    swipe, so a deliberate provider swipe straight after Back did nothing.
    The guard was meant for a stroke ui_swipe stitches to the press on "<",
    which nothing in ui_settings can tell from a fresh one (the comment
    after SESS_REOPEN_GUARD_MS has the reasoning), so it is gone rather
    than narrowed. Nothing on the swipe path may time a window from the
    close again; the reopen guard, which is about the tap area under "<",
    stays in sess_can_open."""
    src = _src("ui_settings.c")
    code = _code(src)
    assert "SESS_STROKE_GUARD_MS" not in code
    assert "sess_stroke_was_cards" not in code
    swipe = _code(_func(src, "swipe_cb"))
    # The card's own block still comes first...
    assert "sess_swipe(dir);" in swipe
    # ...and after it nothing asks the clock or the close.
    for term in ("k_uptime_get", "sess_closed_ms", "sess_closed_once",
                 "SESS_REOPEN_GUARD_MS"):
        assert term not in swipe, term
    # A clock could also come in through a new helper or a new constant, so
    # neither a time call nor a *_MS/*_ms name may appear in swipe_cb or in
    # the two things it hands to, and the only guard constant is the reopen
    # one.
    clock = re.compile(r"k_uptime|k_cycle|k_ticks|sys_clock|\b\w*_MS\b|\b\w*_ms\b")
    assert not clock.search(swipe), clock.search(swipe).group(0)
    assert "swipe_cb" not in _code(_func(src, "sess_note_closed"))
    assert not clock.search(_code(_func(src, "zone_was_a_tap")))
    assert re.findall(r"#define\s+(SESS_\w*GUARD\w*)", code) == [
        "SESS_REOPEN_GUARD_MS"]
    # The edge strips believe ui_swipe's drag with no exception for a close.
    tap = _code(_func(src, "zone_was_a_tap"))
    ret = re.search(r"return (.*?);", tap, re.S).group(1)
    assert "||" not in ret, ret
    for term in ("panel == NULL", "sess_ov == NULL", "!modal_up()",
                 "!ui_anim_gesture_muted()", "!ui_swipe_dragging()"):
        assert term in ret, term
    for term in ("k_uptime_get", "sess_closed_ms", "sess_closed_once"):
        assert term not in tap, term
    # The close time is read in one place only: the reopen guard.
    reads = [m.start() for m in re.finditer(r"\bsess_closed_ms\b", code)]
    can = _code(_func(src, "sess_can_open"))
    noted = _code(_func(src, "sess_note_closed"))
    assert "sess_closed_ms" in can and "SESS_REOPEN_GUARD_MS" in can
    # declaration, the write in sess_note_closed, the read in sess_can_open
    assert len(reads) == 3, len(reads)
    assert "sess_closed_ms = k_uptime_get();" in noted


def _steps():
    bt = _load_board_touch()
    steps = []
    for n, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(),
                             1):
        s = bt.parse_line(line, n)
        if s is not None:
            steps.append(s)
    return steps


STROKES = ("tap", "raw_tap", "swipe", "press", "click", "click_index")


def test_each_press_after_a_click_waits_out_the_stitch():
    """A precaution: ui_swipe stitches two contacts less than
    UI_SWIPE_STITCH_MS (250 ms) apart into one stroke, and a bench can be
    that quick, so `tap settings.open` straight after `click "<"` could be
    read as one right swipe and the Settings strip would take its tap for a
    drag (not yet measured on a desk). A click has no settle of its own, and
    nor has a raw tap, so every stroke after one waits first -- past the window,
    less the driver's 100 ms release lag, with room to spare. Only
    `tap <target>` is exempt as the earlier stroke: the target's own settle
    in board_touch.py already spaces it from the next press, which is why
    the final `click "<"` after `tap settings.open` needs no wait."""
    geom = _src("ui_swipe_geom.h")
    stitch = int(re.search(r"#define UI_SWIPE_STITCH_MS\s+(\d+)",
                           geom).group(1))
    steps = _steps()
    last_unsettled = None
    waited = 0.0
    for s in steps:
        if s.cmd == "wait":
            waited += s.args[0]
            continue
        if s.cmd in STROKES:
            if last_unsettled:
                assert waited * 1000 >= 2 * stitch, \
                    f"line {s.line}: {s.cmd} too soon after a {last_unsettled}"
            last_unsettled = None if s.cmd == "tap" else s.cmd
            waited = 0.0


def test_the_harness_keeps_its_loop_after_every_stroke():
    """The harness runs 600 ms of loop after every stroke, and that run ages
    ui_swipe's stitch window, so a stroke has ended before the next begins.
    An earlier round of this branch moved the window out of that run and
    charged serial-wire time instead (`sw_end_at += AFTER_MS`, wire_charge),
    to stand in for a board's timing. With that change
    tools/bench/full_touch.txt -- the script that passes on real boards, and
    that CI runs on this harness -- failed: `click "Focus"` then
    `click "Session"` were stitched into one stroke and Focus stayed off
    (line 144, 2026-11-25). So it was withdrawn; this keeps it out."""
    src = (ROOT / "tools" / "panel_render" / "touch" /
           "harness.c").read_text(encoding="utf-8")
    touch = _func(src, "touch_line")
    assert "sw_end_at += " not in touch, \
        "the after run must age the stitch window, not be kept out of it"
    assert "wire_charge" not in src
    after = re.search(r"#define AFTER_MS\s+(\d+)", src)
    assert ("run_for(600);" in touch or
            ("run_for(AFTER_MS);" in touch and after and
             int(after.group(1)) == 600)), \
        "touch_line must run the loop for 600 ms after a stroke"


def test_the_card_does_not_disable_its_page_buttons():
    """The bench clicks Previous and Next with no sessions; a disabled
    button would be refused there."""
    src = _src("ui_settings.c")
    for name in ("sess_build", "sess_paint", "sess_btn"):
        assert "LV_STATE_DISABLED" not in _func(src, name)
    build = _func(src, "sess_build")
    assert '"Previous"' in build and '"Next"' in build


# What a real board may differ in from the harness, whatever the script does:
# the owner's theme (and the one stored), brightness, Focus, its gesture
# Focus, the theme page, the link and the browser-edit state. The platform's
# board check of 2026-11-25 stopped at tools/bench/full_touch.txt line 129,
# `expect ui_state ... theme=0 stored=0`, because the board had theme 2
# stored; the harness always reads 0, so it had passed there.
OWNER_KEYS = ("theme", "stored", "bl", "focus", "gf", "tp", "ln", "edit")


def test_no_expectation_depends_on_the_owners_settings():
    """board_touch.py's wait_state compares only the fields a line names,
    on a board and on the harness alike, so naming none of these is what
    keeps every line true on any desk."""
    for s in _steps():
        if s.cmd == "expect" and s.args[0] in ("ui_state", "notice"):
            named = set(s.args[1]) & set(OWNER_KEYS)
            assert not named, f"line {s.line} pins {sorted(named)}"


def test_a_named_target_clears_the_way_before_the_raw_tap():
    """A raw tap neither wakes a dozing board nor dismisses a notice (only
    run_target's clear_the_way does), so on a desk in either state it would
    land on that and the card would not open. A named target must come
    first, and the raw tap must be preceded by a check that the board is
    awake on the bare gauges."""
    steps = _steps()
    cmds = [s.cmd for s in steps]
    raw = cmds.index("raw_tap")
    before = steps[raw - 1]
    # The target must come before that check, not merely before the raw tap:
    # a target after the check would clear the way for nothing it vouches for.
    assert "tap" in cmds[:raw - 1], "no named target before the check"
    assert before.cmd == "expect" and before.args[0] == "ui_state"
    assert before.args[1].get("slp") == "0"
    assert before.args[1].get("modal") == "none"


def test_the_bench_script_exists_and_parses():
    assert SCRIPT.is_file()
    bt = _load_board_touch()
    steps = []
    for n, line in enumerate(SCRIPT.read_text(encoding="utf-8").splitlines(),
                             1):
        s = bt.parse_line(line, n)
        if s is not None:
            steps.append(s)
    cmds = [s.cmd for s in steps]
    # Runs on a board as well as the harness, so nothing harness-only.
    assert "harness_only" not in cmds and "fake" not in cmds
    assert "raw_tap" in cmds
    clicks = [s.args[0] for s in steps if s.cmd == "click"]
    assert clicks[:3] == ["Next", "Previous", "<"]
