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


# Settled by prj.conf alone. An overlay (trace.conf, pilot.conf, any later
# one) that set one of these would quietly change it for that image only.
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
    src = (FW / "src" / "proto.c").read_text(encoding="utf-8")
    m = re.search(r"static void emit\(const char \*json\)\n\{(.*?)\n\}",
                  src, re.S)
    assert m
    body = m.group(1)
    assert body.count("printk(") == 1
    assert 'printk("%s\\n", json);' in body


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
