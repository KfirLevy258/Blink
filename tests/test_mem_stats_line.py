"""The board's "[mem] lvgl ..." line, and the path it takes to bridge.log.

The LVGL pool is read on the board (firmware/src/mem_stats.c) and printed to
the console; the daemon's console echo is what puts it in ~/.blink/bridge.log,
where the next feature to need that pool gets sized from it. Two ways for the
number to silently stop arriving are pinned here, because neither shows up as
a build error:

  - CONFIG_SYS_HEAP_RUNTIME_STATS off. The source is then not compiled and the
    calls are empty stubs. gapietro's fork found the other half of this on
    2026-10-14: with the option off, lvgl_heap_stats() writes nothing, and
    their first reading printed whatever the stack held.
  - CONFIG_SYS_HEAP_ARRAY_SIZE at its default 0. The LVGL heap is then not
    findable and every line says "pool not found".

And the line must stay one the echo keeps: it drops anything starting with
'{' as protocol JSON (pc/logbook.py), so the format is checked by running a
line rendered from the firmware's own format string through it.
"""
import re
from pathlib import Path

from pc import logbook

ROOT = Path(__file__).resolve().parent.parent
FW = ROOT / "firmware"


def _conf(name):
    m = re.search(rf"^CONFIG_{name}=(\S+)$",
                  (FW / "prj.conf").read_text(), re.M)
    return m.group(1) if m else None


def _printk_format():
    """The report's format string, with C's adjacent literals joined."""
    src = (FW / "src" / "mem_stats.c").read_text()
    m = re.search(r'printk\(("\[mem\] lvgl %s: used=.*?)\s*,\s*where',
                  src, re.S)
    assert m, "the [mem] report's printk is gone from mem_stats.c"
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))


def test_the_release_build_keeps_the_heap_counters_on():
    assert _conf("SYS_HEAP_RUNTIME_STATS") == "y"


def test_the_lvgl_heap_can_be_found():
    """Three heaps in the release build: the kernel's, libc's, and LVGL's."""
    assert int(_conf("SYS_HEAP_ARRAY_SIZE") or 0) >= 3


def test_the_line_names_every_field_the_measurement_needs():
    fmt = _printk_format()
    for key in ("used=", "free=", "largest=", "peak=", "boot_peak=",
                "pool="):
        assert key in fmt, key
    assert fmt.endswith("\\n")


def test_the_line_reaches_the_log_whole():
    fmt = _printk_format().replace("\\n", "\n")
    line = fmt.replace("%s", "settings", 1).replace("%u", "1")
    out = logbook.ConsoleEcho().feed(line.encode())
    assert out == [line.rstrip("\n")]
    assert out[0].startswith("[mem] lvgl settings: used=1 ")
