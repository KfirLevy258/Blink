"""The board's "[mem] lvgl ..." line, and what of it reaches bridge.log.

The LVGL pool is read on the board (firmware/src/mem_stats.c) and printed to
the console, where the next feature to need that pool gets sized from it. The
daemon's console echo once copied the whole line into ~/.blink/bridge.log; it
now copies only "[mem]" (pc/logbook.console_line), because the figures are
values off the serial buffer. Two ways for the
number to silently stop arriving are pinned here, because neither shows up as
a build error:

  - CONFIG_SYS_HEAP_RUNTIME_STATS off. The source is then not compiled and the
    calls are empty stubs. gapietro's fork found the other half of this on
    2026-10-14: with the option off, lvgl_heap_stats() writes nothing, and
    their first reading printed whatever the stack held.
  - CONFIG_SYS_HEAP_ARRAY_SIZE at its default 0. The LVGL heap is then not
    findable and every line says "pool not found".

And what the echo makes of it is checked by running a line rendered from the
firmware's own format string through it: the tag, and none of the figures.
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


def test_the_line_reaches_the_log_as_its_tag_only():
    """It used to reach bridge.log whole. Its figures are values off the
    serial buffer, and since 2026-10-30 the echo keeps a tag and a fixed set
    of messages and nothing else (pc/logbook.CONSOLE_MESSAGES), so a
    customer's log says a report happened and the numbers are read from the
    board's console on a bench."""
    fmt = _printk_format().replace("\\n", "\n")
    line = fmt.replace("%s", "settings", 1).replace("%u", "31337")
    out = logbook.ConsoleEcho().feed(line.encode())
    assert out == ["[mem]"]


def test_the_lvgl_pool_is_32_kib_and_the_bench_knows_it():
    # 30720 left 2320 B on the board's deepest measured popup path, and a
    # notice over the install confirm was estimated at under 1.3 KB (full
    # review #2, M1). The 2 KB it had given up went to a networking stack
    # that no longer exists.
    assert int(_conf("LV_Z_MEM_POOL_SIZE")) == 32768
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "board_touch", ROOT / "tools" / "board_touch.py")
    bt = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bt)
    # The heap's own metadata: 10 chunks of 8 B plus the 4 B footer and
    # the 4 B lost to rounding the end down, the same at both sizes
    # (twelve free-list buckets either way).
    assert bt.POOL_USABLE == 32768 - 88


def test_no_overlay_resizes_the_lvgl_pool():
    # trace.conf cut it to 27648, under what the board is measured to use
    # opening Theme, so a trace build rebooted mid-trace (fp/pool review,
    # S1). Every image runs prj.conf's pool.
    overlays = [c for c in FW.glob("*.conf") if c.name != "prj.conf"]
    assert any(c.name == "trace.conf" for c in overlays)
    for conf in overlays:
        assert not re.search(r"^CONFIG_LV_Z_MEM_POOL_SIZE=",
                             conf.read_text(), re.M), conf.name


def test_no_setting_for_the_removed_network_stack_is_left():
    # Each made every build warn that its dependencies are off, which hides
    # the next warning that matters.
    conf = (FW / "prj.conf").read_text()
    for name in ("NET_SOCKETS_SERVICE_STACK_SIZE", "ZVFS_OPEN_MAX"):
        assert not re.search(rf"^CONFIG_{name}=", conf, re.M), name
