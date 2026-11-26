"""A selftest firmware never leaves the bench.

CONFIG_BLINK_FATAL_SELFTEST builds an image that faults on every boot, on
purpose (firmware/Kconfig). Its help said "never ship this" and nothing in
tools/ enforced it (final review N3, 2026-10-27). tools/lib_selftest.sh is
the check; tools/release.sh runs it on what it is about to publish and
tools/burn.sh on what it is about to flash. burn.sh is run end to end against
a selftest build in tests/ci/check_factory.sh; release.sh cannot be run here
without building firmware, so its call is checked for where it sits.

The .config and image are written by this file, never taken from a build.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "tools" / "lib_selftest.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None,
    reason="needs a POSIX sh, as the scripts it guards do")

PLAIN_CONFIG = "CONFIG_BLINK_PANEL_SCALE=60\n# CONFIG_BLINK_FATAL_SELFTEST is not set\n"
SELFTEST_CONFIG = ("CONFIG_BLINK_FATAL_SELFTEST=y\n"
                   "CONFIG_BLINK_FATAL_SELFTEST_AT_S=60\n")
# What the image of a selftest build carries: fatal.c's boot line, between
# bytes that are not text, as it sits in .rodata.
SELFTEST_LINE = b"[fatal] SELFTEST BUILD: will fault at %d s -- never ship this\n"


def _check(tmp_path, config, image):
    cfg = tmp_path / ".config"
    img = tmp_path / "zephyr.signed.bin"
    if config is not None:
        cfg.write_text(config, encoding="utf-8")
    if image is not None:
        img.write_bytes(image)
    return subprocess.run(
        ["sh", "-c", '. "$0"; blink_refuse_selftest "$1" "$2"',
         str(LIB), str(cfg), str(img)],
        capture_output=True, text=True, timeout=30)


def _image(*middle):
    return b"\x00\xffMCUB" + b"".join(middle) + bytes(range(256)) * 4


def test_a_plain_build_passes(tmp_path):
    r = _check(tmp_path, PLAIN_CONFIG, _image(b"[fatal] streak %d\n"))
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""


def test_the_config_symbol_refuses(tmp_path):
    r = _check(tmp_path, SELFTEST_CONFIG, _image())
    assert r.returncode == 1
    assert "CONFIG_BLINK_FATAL_SELFTEST=y" in r.stderr
    # The way out, since the cause is a cached -D value.
    assert "pristine" in r.stderr


def test_the_boot_line_in_the_image_refuses(tmp_path):
    """A .config that disagrees with its image -- a stale or hand-copied
    one -- does not get the image through: the image is what ships."""
    r = _check(tmp_path, PLAIN_CONFIG, _image(SELFTEST_LINE))
    assert r.returncode == 1
    assert "selftest boot line" in r.stderr


def test_a_missing_config_refuses(tmp_path):
    r = _check(tmp_path, None, _image())
    assert r.returncode == 1
    assert "cannot tell" in r.stderr


def test_a_missing_image_refuses(tmp_path):
    r = _check(tmp_path, PLAIN_CONFIG, None)
    assert r.returncode == 1


def test_the_phrase_it_looks_for_is_the_one_fatal_c_prints():
    """The image check greps for a phrase from fatal.c's selftest boot line.
    Reworded there, it would pass every selftest image without a word -- and
    a comment still carrying the phrase must not count, so this reads the
    phrase out of the lib's own grep and finds it inside the string literal
    of a printk, not anywhere in the file."""
    # The selftest function's own grep: the lib also holds the touch-inject
    # check, with a phrase of its own (tests/test_lib_inject.py).
    text = LIB.read_text(encoding="utf-8")
    body = text[text.index("blink_refuse_selftest()"):
                text.index("blink_refuse_inject()")]
    (phrase,) = re.findall(r"grep -aqF '([^']+)'", body)
    src = (ROOT / "firmware" / "src" / "fatal.c").read_text(encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)
    literals = re.findall(r'printk\(\s*"((?:[^"\\]|\\.)*)"', code)
    assert any(phrase in lit for lit in literals), (phrase, literals)


def test_release_sh_checks_what_it_built_before_publishing():
    text = (ROOT / "tools" / "release.sh").read_text(encoding="utf-8")
    lines = text.splitlines()

    def first(pattern):
        return next(i for i, l in enumerate(lines) if re.match(pattern, l))

    build = first(r"\s*(\(\s*cd [^&]*&&\s*)?west build\b")
    guard = first(r"\s*blink_refuse_selftest\b")
    # The first WRITE to a release; release.sh asks `gh release view` about
    # the tag long before it builds anything.
    publish = first(r"\s*gh release (create|edit|upload)\b")
    assert build < guard < publish
    # On the release's own artifact and config, not some other build's.
    assert re.search(r'blink_refuse_selftest "\$CFG" "\$BIN"', lines[guard])
    assert re.search(r"^\s*(source|\.) .*tools/lib_selftest\.sh", text, re.M)


def test_burn_sh_checks_before_it_signs_or_flashes():
    text = (ROOT / "tools" / "burn.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    guard = next(i for i, l in enumerate(lines)
                 if re.search(r"\bblink_refuse_selftest\b", l)
                 and not l.lstrip().startswith("#"))
    sign = next(i for i, l in enumerate(lines)
                if "sign_confirmed.py" in l and not l.lstrip().startswith("#"))
    flash = next(i for i, l in enumerate(lines) if "write_flash" in l
                 and not l.lstrip().startswith("#"))
    assert guard < sign and guard < flash


def test_burn_sh_refuses_test_knobs_and_standalone_net_unless_told():
    """burn.sh signs the image confirmed, so a knob build it flashes hangs
    or reverts with no rescue (PR 9 round 2, finding 1). Both guards sit
    before the sign and the flash, and the only way past is the loud
    BLINK_BENCH_KNOBS=1."""
    text = (ROOT / "tools" / "burn.sh").read_text(encoding="utf-8")
    lines = text.splitlines()

    def at(pat):
        return next(i for i, l in enumerate(lines)
                    if re.search(pat, l) and not l.lstrip().startswith("#"))

    sign = at(r"sign_confirmed\.py")
    for fn in ("blink_refuse_test_knobs", "blink_refuse_standalone_net"):
        i = at(rf"\b{fn}\b")
        assert i < sign and "||" in lines[i] + lines[i + 1]
    gate = at(r'BLINK_BENCH_KNOBS:-0\}" = 1')
    assert gate < at(r"\bblink_refuse_test_knobs\b")
    assert "SKIPPED" in text


# --- the touch trace: release.sh's alone (review, S6) -------------------------

# What every image carries now, whatever its panel: main.c's boot line.
PRODUCTION_LINE = b"[usage] panel: MADCTL=0x%02x\n"
TRACE_LINE = b"\n#TT-BEGIN seq=%u n=%u sat=%u frame=channel240x320\n"


def _check_fn(tmp_path, fn, config, image):
    cfg = tmp_path / ".config"
    img = tmp_path / "zephyr.signed.bin"
    cfg.write_text(config, encoding="utf-8")
    img.write_bytes(image)
    return subprocess.run(
        ["sh", "-c", f'. "$0"; {fn} "$1" "$2"', str(LIB), str(cfg), str(img)],
        capture_output=True, text=True, timeout=30)


def test_a_production_image_passes_the_trace_check(tmp_path):
    r = _check_fn(tmp_path, "blink_refuse_trace", PLAIN_CONFIG,
                  _image(PRODUCTION_LINE))
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""


def test_the_trace_symbol_refuses(tmp_path):
    symbol = "CONFIG_BLINK_TOUCH_TRACE=y"
    r = _check_fn(tmp_path, "blink_refuse_trace", PLAIN_CONFIG + symbol + "\n",
                  _image(PRODUCTION_LINE))
    assert r.returncode == 1
    assert symbol in r.stderr
    assert "pristine" in r.stderr


def test_the_trace_dump_in_the_image_refuses(tmp_path):
    r = _check_fn(tmp_path, "blink_refuse_trace", PLAIN_CONFIG,
                  _image(PRODUCTION_LINE, TRACE_LINE))
    assert r.returncode == 1
    assert "touch trace dump" in r.stderr


def test_a_missing_config_refuses_the_trace_check(tmp_path):
    img = tmp_path / "zephyr.signed.bin"
    img.write_bytes(_image(PRODUCTION_LINE))
    r = subprocess.run(
        ["sh", "-c", '. "$0"; blink_refuse_trace "$1" "$2"', str(LIB),
         str(tmp_path / ".config"), str(img)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert "cannot tell" in r.stderr


def _fn_body(text, fn):
    start = text.index(f"{fn}()")
    end = text.find("\n}\n", start)
    return text[start:end]


def _printk_literals(path):
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "",
                  path.read_text(encoding="utf-8"), flags=re.S)
    return code, re.findall(r'printk\(\s*"((?:[^"\\]|\\.)*)"', code)


def test_the_trace_phrase_is_the_one_touch_trace_c_prints():
    (phrase,) = re.findall(r"grep -aqF '([^']+)'",
                           _fn_body(LIB.read_text(encoding="utf-8"),
                                    "blink_refuse_trace"))
    _, lits = _printk_literals(ROOT / "firmware" / "src" / "touch_trace.c")
    assert any(phrase in lit for lit in lits), (phrase, lits)


def test_release_sh_refuses_trace_before_publishing():
    lines = (ROOT / "tools" / "release.sh").read_text(
        encoding="utf-8").splitlines()

    def first(pattern):
        return next(i for i, l in enumerate(lines) if re.match(pattern, l))

    build = first(r"\s*(\(\s*cd [^&]*&&\s*)?west build\b")
    publish = first(r"\s*gh release (create|edit|upload)\b")
    guard = first(r"\s*blink_refuse_trace\b")
    assert build < guard < publish
    assert re.search(r'blink_refuse_trace "\$CFG" "\$BIN" \|\| exit 1',
                     lines[guard])


# --- the panel is a run-time setting now (issue #23) --------------------------
#
# The two boards whose display module renders mirrored on the standard
# setting used to need a build of their own, and release.sh refused that
# build by its Kconfig symbol and by the absence of main.c's MADCTL boot
# line. Issue #23 made the panel a setting saved on the board (`blink config
# panel standard|alternate`), so every image is the same image and the
# refusal could never fire. These pin that it is gone and stays gone. The
# retired names are built by concatenation so this file does not itself
# carry them for the repo-wide search below to find.

RETIRED = ("BLINK_PANEL_" + "PILOT", "BLINK_BL_" + "SCALE_PCT",
           "pilot" + ".conf")
REFUSE_PANEL = "blink_refuse_" + "pilot"


def test_the_panel_build_refusal_is_gone():
    lib = LIB.read_text(encoding="utf-8")
    assert not re.search(rf"^\s*{REFUSE_PANEL}\s*\(\)", lib, re.M)
    assert REFUSE_PANEL not in lib
    release = (ROOT / "tools" / "release.sh").read_text(encoding="utf-8")
    assert REFUSE_PANEL not in release


def test_release_sh_still_calls_every_other_refusal_in_order():
    """Dropping one refusal must not take a neighbour with it. Each call is
    a real command (not a comment) that ends the script when it fails."""
    lines = (ROOT / "tools" / "release.sh").read_text(
        encoding="utf-8").splitlines()
    order = ["blink_refuse_wifi_mode", "blink_refuse_selftest",
             "blink_refuse_inject", "blink_refuse_trace",
             "blink_refuse_standalone_net", "blink_refuse_test_knobs",
             "blink_refuse_oversize"]
    at = []
    for fn in order:
        (i,) = [i for i, l in enumerate(lines)
                if re.match(rf"\s*{fn}\b", l)]
        assert "|| exit 1" in lines[i], lines[i]
        at.append(i)
    assert at == sorted(at)


def _is_build_dir(part):
    return part.startswith("build") or part.startswith(".venv")


def test_nothing_in_firmware_or_tools_names_the_retired_panel_build():
    """A stale mention is an instruction someone will follow: a maker told
    to build with the old overlay gets a build error at best."""
    hits = []
    for top in ("firmware", "tools"):
        for path in sorted((ROOT / top).rglob("*")):
            rel = path.relative_to(ROOT)
            # Directories only: tools/ has files whose names start "build".
            if any(_is_build_dir(p) for p in rel.parts[1:-1]):
                continue
            if not path.is_file():
                continue
            data = path.read_bytes()
            if b"\0" in data:
                continue  # binary
            text = data.decode("utf-8", errors="replace")
            for needle in RETIRED:
                if needle in text:
                    hits.append((str(rel), needle))
    assert hits == []


def test_main_c_prints_the_madctl_boot_line_exactly_once():
    """The platform's board check reads "[usage] panel: MADCTL=0x.." from
    the boot console to tell the panel correction ran. One printk of it, in
    code rather than a comment, and no second copy to drift from it."""
    code, lits = _printk_literals(ROOT / "firmware" / "src" / "main.c")
    phrase = "panel: MADCTL=0x"
    assert sum(lit.count(phrase) for lit in lits) == 1, lits
    assert code.count(phrase) == 1


# --- the three WiFi-update test knobs (spec S9, Task 5.1) ---------------------

KNOBS = [("NEVER_HEALTHY", b"[test] never healthy\n"),
         ("CONFIRM_FAILS", b"[test] confirm fails\n"),
         ("HANG_AFTER_BOOT", b"[test] hang after boot\n")]


def _knobs(tmp_path, config, image):
    cfg = tmp_path / ".config"
    img = tmp_path / "zephyr.signed.bin"
    if config is not None:
        cfg.write_text(config, encoding="utf-8")
    if image is not None:
        img.write_bytes(image)
    return subprocess.run(
        ["sh", "-c", '. "$0"; blink_refuse_test_knobs "$1" "$2"',
         str(LIB), str(cfg), str(img)],
        capture_output=True, text=True, timeout=30)


def test_a_build_without_knobs_passes(tmp_path):
    r = _knobs(tmp_path, "# CONFIG_BLINK_TEST_NEVER_HEALTHY is not set\n",
               _image(b"[ota] image confirmed\n"))
    assert r.returncode == 0, r.stderr


@pytest.mark.parametrize("knob,line", KNOBS)
def test_each_knob_refuses_by_config_and_by_image(tmp_path, knob, line):
    r = _knobs(tmp_path, f"CONFIG_BLINK_TEST_{knob}=y\n", _image())
    assert r.returncode == 1 and f"CONFIG_BLINK_TEST_" in r.stderr
    r = _knobs(tmp_path, PLAIN_CONFIG, _image(line))
    assert r.returncode == 1 and "[test] boot line" in r.stderr


def test_a_missing_config_or_image_refuses(tmp_path):
    assert _knobs(tmp_path, None, _image()).returncode == 1
    (tmp_path / "other").mkdir()
    assert _knobs(tmp_path / "other", PLAIN_CONFIG, None).returncode == 1


def test_the_knob_lines_are_the_firmwares_own_words():
    text = (ROOT / "firmware" / "src" / "main.c").read_text(encoding="utf-8")
    for _knob, line in KNOBS:
        assert line.decode().strip() in text
