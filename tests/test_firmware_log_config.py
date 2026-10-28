"""The board's protocol lines must never be thrown away by its own log.

Every line the board sends the daemon is a printk, and with CONFIG_LOG_PRINTK
every printk is a message in Zephyr's deferred log, beside LVGL's warnings and
the "[tag]" diagnostics. Zephyr's defaults drop the OLDEST message when that
buffer is full, which on 2026-10-28 cost the bench image 22 replies in one
board run (25912f7) and left a release board able to lose a hello, a
theme_edit or an OTA reply with no error on either side. prj.conf now makes a
thread wait for room instead. None of these settings shows up as a build
error when it is lost, so they are pinned here.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"


def _conf(name):
    m = re.search(rf"^CONFIG_{name}=(\S+)$",
                  (FW / "prj.conf").read_text(encoding="utf-8"), re.M)
    return m.group(1) if m else None


def test_a_thread_waits_for_room_rather_than_lose_a_line():
    assert _conf("LOG_BLOCK_IN_THREAD") == "y"
    # Finite: -1 can deadlock the log thread on its own message. 1000 ms is
    # twenty times the drain of the widest (512 B) line at 115200 baud.
    t = int(_conf("LOG_BLOCK_IN_THREAD_TIMEOUT_MS"))
    assert 100 <= t <= 5000


def test_a_full_buffer_never_evicts_a_queued_line():
    # With OVERFLOW on, a message that cannot wait (interrupt context) would
    # still push the oldest queued line -- possibly a reply -- out.
    assert _conf("LOG_MODE_OVERFLOW") == "n"


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
