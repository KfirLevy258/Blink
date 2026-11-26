"""The board's protocol lines must never be thrown away by its own log.

Every line the board sends the daemon is a printk, and with CONFIG_LOG_PRINTK
every printk is a message in Zephyr's deferred log, beside LVGL's warnings and
the "[tag]" diagnostics. Zephyr's defaults drop the OLDEST message when that
buffer is full, which on 2026-10-28 cost the bench image 22 replies in one
board run (25912f7) and left a release board able to lose a hello, a
theme_edit or an OTA reply with no error on either side. prj.conf now makes a
thread wait for room instead, and keeps the drop-oldest default only for the
writers that cannot wait, the fault dump among them. None of these settings
shows up as a build error when it is lost, so they are pinned here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"


def _conf(name):
    m = re.search(rf"^CONFIG_{name}=(\S+)$",
                  (FW / "prj.conf").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


# Settled by prj.conf alone. An overlay (trace.conf, or any later one) that
# set one of these would quietly change it for that image only.
PINNED = ("LOG_BLOCK_IN_THREAD", "LOG_BLOCK_IN_THREAD_TIMEOUT_MS",
          "LOG_MODE_OVERFLOW", "LOG_PROCESS_TRIGGER_THRESHOLD",
          "LOG_MODE_DEFERRED", "LOG_MODE_IMMEDIATE", "LOG_PRINTK",
          "LV_USE_LOG", "LV_LOG_LEVEL", "LV_Z_LOG_LEVEL")


def test_no_overlay_resets_the_log_settings():
    overlays = [c for c in FW.glob("*.conf") if c.name != "prj.conf"]
    assert overlays, "no firmware/*.conf overlays found to check"
    for conf in overlays:
        text = conf.read_text(encoding="utf-8")
        for name in PINNED:
            assert not re.search(rf"^CONFIG_{name}(_\w+)?=", text, re.M), (
                conf.name, name)


def test_a_thread_waits_for_room_rather_than_lose_a_line():
    assert _conf("LOG_BLOCK_IN_THREAD") == "y"
    # Finite: -1 can deadlock the log thread on its own message. 1000 ms is
    # twenty times the drain of the widest (512 B) line at 115200 baud.
    t = int(_conf("LOG_BLOCK_IN_THREAD_TIMEOUT_MS"))
    assert 100 <= t <= 5000


def test_only_a_writer_that_cannot_wait_may_evict_the_oldest_line():
    # BLOCK_IN_THREAD is the guarantee for every protocol line: Zephyr
    # 4.3.1's mpsc_pbuf_alloc waits before its drop path, so a thread never
    # evicts a queued line whatever OVERFLOW says. OVERFLOW decides only
    # for a writer that cannot wait, and the one this board has is the
    # fault path: z_fatal_error's banner under arch_irq_lock() and the
    # Xtensa register dump, both before LOG_PANIC. Turned off (10bf7b4),
    # a buffer full of LVGL's pool warnings refused the crash dump line by
    # line. It must stay at Zephyr's default, dropping the OLDEST.
    assert _conf("LOG_MODE_OVERFLOW") in (None, "y")


def test_the_log_thread_wakes_on_the_first_line():
    # A writer blocked on a full buffer waits for the log thread. At Zephyr's
    # default of ten it could be asleep on its 1 s timer with the buffer full
    # of fewer, longer lines.
    assert _conf("LOG_PROCESS_TRIGGER_THRESHOLD") == "1"


def test_every_protocol_line_is_one_printk():
    # One printk is one log message, written out whole and in order. A line
    # assembled from several printk calls could be split by another thread's
    # message landing between them.
    # emit() in proto.c hands every line to link_emit() (spec 5.3), so the
    # printk lives there now: emit() must print nothing itself and
    # link_emit()'s USB path must be exactly one.
    proto = (FW / "src" / "proto.c").read_text(encoding="utf-8")
    m = re.search(r"static void emit\(const char \*json\)\n\{(.*?)\n\}",
                  proto, re.S)
    assert m
    assert "printk(" not in m.group(1)
    assert "link_emit(json);" in m.group(1)
    link = (FW / "src" / "link.c").read_text(encoding="utf-8")
    m = re.search(r"void link_emit\(const char \*line\)\n\{(.*?)\n\}",
                  link, re.S)
    assert m
    body = m.group(1)
    assert body.count("printk(") == 1
    assert 'printk("%s\\n", line);' in body


# --- the panel's wiring (issue #23, review round 4) --------------------------
#
# Not a log setting, but the same kind of guarantee: nothing fails to build
# when it is lost. The panel's decisions live in firmware/src/panel.h, where
# tests/backlight/host_test.c and tests/cfg_store/host_test.c drive them with
# recorders; main.c, backlight.c and proto.c can only be compiled by the
# Zephyr build, so no host test reaches them. Without these checks one of
# them could stop calling panel.h -- or carry its own copy of a decision --
# and every host test would still pass against code the board never runs.

def _code(name):
    """firmware/src/<name> with its comments removed, so a measurement
    quoted in a comment is not mistaken for a decision in the code."""
    text = (FW / "src" / name).read_text(encoding="utf-8")
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    return re.sub(r"//[^\n]*", " ", text)


def _body(code, signature):
    m = re.search(re.escape(signature) + r"\n\{(.*?)\n\}", code, re.S)
    assert m, signature
    return m.group(1)


_PANEL_DECISION = re.compile(r"\b0[xX](?:[aA]0|28)\b")


def test_the_backlight_drives_panel_h_pulse():
    code = _code("backlight.c")
    apply = _body(code, "static void apply(void)")
    # The shown percent (quiet hours may dim it) on the stored panel's scale.
    assert "pwm_set_pulse_dt(&bl, panel_bl_pulse(bl.period, shown, panel))" \
        in apply
    # The panel at boot is the record's...
    assert "cfg_get_panel()" in _body(code, "void backlight_init(void)")
    # ...and a live change sets it, so a failed MADCTL write (which never
    # reaches backlight_reapply) cannot rescale the light.
    assert "apply();" in _body(code, "void backlight_reapply(uint8_t now)")
    assert apply.count("cfg_get_panel(") == 0
    # No copy of the sum or the scale of its own.
    for own in ("panel_bl_scale_pct", "10000", "uint64_t"):
        assert own not in code, own
    # (the 60 of minutes-per-hour in quiet hours' clock sum is not the scale)
    assert not re.search(r"\b60\b", code.replace("hh * 60", ""))


def test_the_backlight_panel_is_the_records_before_the_first_apply():
    code = _code("backlight.c")
    init = _body(code, "void backlight_init(void)")
    # `panel` is assigned from the record, and before apply() runs: the
    # other order drives the first duty for a stale panel.
    m = re.search(r"\bpanel\s*=\s*cfg_get_panel\(\);", init)
    assert m, init
    assert m.end() < init.index("apply();"), init
    # backlight_init must run after cfg_init, or cfg_get_panel() is still
    # the default and an alternate board boots at the standard 60% scale.
    main = _code("main.c")
    assert main.count("backlight_init();") == 1
    assert main.index("cfg_init();") < main.index("backlight_init();")


def test_main_boot_and_live_change_are_panel_h():
    code = _code("main.c")
    boot = _body(code, "static void panel_fix_madctl(void)")
    assert "panel_boot(cfg_get_panel(), &panel_hw," in boot
    live = _body(code, "int panel_apply(uint8_t panel)")
    assert re.fullmatch(r"\s*return panel_live_apply\(panel, &panel_hw\);\s*",
                        live), live
    # The ops are the real hardware.
    for op in (".write_madctl = panel_write_madctl",
               ".backlight_reapply = backlight_reapply",
               ".redraw = panel_redraw"):
        assert op in code, op
    assert "lv_obj_invalidate(lv_scr_act());" in _body(
        code, "static void panel_redraw(void)")
    assert "mipi_dbi_command_write(panel_dbi, &panel_dbi_cfg, 0x36, &madctl" \
        in _body(code, "static int panel_write_madctl(uint8_t madctl)")
    # The boot fix runs once, after the record is read and before the
    # screen is unblanked.
    order = re.search(r"cfg_init\(\);\s*panel_fix_madctl\(\);\s*"
                      r"display_blanking_off\(display_dev\);", code)
    assert order
    assert code.count("panel_fix_madctl();") == 1
    # No decision of its own: no MADCTL value, no table lookup, no panel
    # comparison.
    assert not _PANEL_DECISION.search(code)
    assert "panel_madctl(" not in code
    assert not re.search(r"cfg_get_panel\(\)\s*[!=]=", code)


def test_the_panel_message_is_panel_h_over_the_record_and_the_glass():
    code = _code("proto.c")
    start = code.index('strcmp(type, "panel") == 0')
    end = code.index("} else if (strcmp(type,", start)
    branch = code[start:end]
    assert ".store = cfg_set_panel," in branch
    assert ".apply = panel_apply," in branch
    assert "panel_handle_msg(" in branch
    assert "&panel_msg_hw, &rc)" in branch
    # The cable-only check comes before anything is handled.
    assert branch.index("src != PROTO_SRC_USB") < branch.index(
        "panel_handle_msg(")
    # One constant line per outcome, the new one included.
    for case, line in (
            ("PANEL_MSG_SET_STANDARD", "[cfg] panel set to standard\\n"),
            ("PANEL_MSG_SET_ALTERNATE", "[cfg] panel set to alternate\\n"),
            ("PANEL_MSG_STORE_FAILED",
             "[cfg] could not store the panel (%d)\\n"),
            ("PANEL_MSG_UNKNOWN", "[cfg] unknown panel; ignored\\n")):
        i = branch.index("case " + case + ":")
        assert line in branch[i:branch.index("break;", i)], case
    i = branch.index("case PANEL_MSG_NOT_SHOWN:")
    tail = branch[i:branch.index("break;", i)]
    assert '"[cfg] panel saved; restart the board to show it"' in tail
    # No decision of its own: it does not name the panels, store or apply
    # directly, or carry a MADCTL value.
    for own in ("cfg_set_panel(", "panel_apply(", "CFG_PANEL_",
                "strcmp(pv"):
        assert own not in branch, own
    assert not _PANEL_DECISION.search(branch)


def test_backlight_and_proto_name_no_madctl_value():
    for name in ("backlight.c", "proto.c"):
        assert not _PANEL_DECISION.search(_code(name)), name


def test_lvgl_still_reports_a_pool_running_dry():
    # "No memory" (lv_draw_buf.c) and "Allocating layer buffer failed"
    # (lv_draw.c) are LV_LOG_WARN in LVGL 9. Turning LVGL down to errors to
    # quieten the log would hide the warning that comes before a fault.
    conf = (FW / "prj.conf").read_text(encoding="utf-8")
    assert _conf("LV_USE_LOG") == "y"
    for quieter in ("LV_LOG_LEVEL_ERROR", "LV_LOG_LEVEL_USER",
                    "LV_LOG_LEVEL_NONE"):
        assert f"CONFIG_{quieter}=y" not in conf
    lvl = _conf("LV_Z_LOG_LEVEL")
    assert lvl is None or int(lvl) >= 2
