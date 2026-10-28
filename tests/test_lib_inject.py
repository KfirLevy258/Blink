"""A touch-inject firmware never leaves the bench.

CONFIG_BLINK_TOUCH_INJECT builds an image that takes touches from the cable
(firmware/src/touch_inject.h): anything on the computer could then operate the
panel, the update row included. tools/lib_selftest.sh's blink_refuse_inject is
the check, beside the selftest one and built the same way -- the .config for
what was asked, the image for what ships, and "could not look" counted as a
refusal. release.sh and flash_encrypted.sh run it outright; burn.sh runs it
unless --bench names the bench build. burn.sh is run end to end against an
inject build in tests/ci/check_factory.sh; the other two are checked for where
the call sits, as test_lib_selftest.py does.

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

PLAIN_CONFIG = ("CONFIG_BLINK_PANEL_SCALE=60\n"
                "# CONFIG_BLINK_FATAL_SELFTEST is not set\n"
                "# CONFIG_BLINK_TOUCH_INJECT is not set\n")
INJECT_CONFIG = "CONFIG_INPUT_XPT2046=y\nCONFIG_BLINK_TOUCH_INJECT=y\n"
# touch_inject.c's boot line as it sits in .rodata, between non-text bytes.
INJECT_LINE = (b"[touch] INJECT BUILD: the PC can drive this panel"
               b" -- bench only, never ship this\n")


def _check(tmp_path, config, image):
    cfg = tmp_path / ".config"
    img = tmp_path / "zephyr.signed.bin"
    if config is not None:
        cfg.write_text(config, encoding="utf-8")
    if image is not None:
        img.write_bytes(image)
    return subprocess.run(
        ["sh", "-c", '. "$0"; blink_refuse_inject "$1" "$2"',
         str(LIB), str(cfg), str(img)],
        capture_output=True, text=True, timeout=30)


def _image(*middle):
    return b"\x00\xffMCUB" + b"".join(middle) + bytes(range(256)) * 4


def test_a_plain_build_passes(tmp_path):
    r = _check(tmp_path, PLAIN_CONFIG, _image(b"[touch] trace on\n"))
    assert r.returncode == 0, r.stderr
    assert r.stderr == ""


def test_the_config_symbol_refuses(tmp_path):
    r = _check(tmp_path, INJECT_CONFIG, _image())
    assert r.returncode == 1
    assert "CONFIG_BLINK_TOUCH_INJECT=y" in r.stderr
    assert "pristine" in r.stderr


def test_the_boot_line_in_the_image_refuses(tmp_path):
    """A stale .config does not get an inject image through."""
    r = _check(tmp_path, PLAIN_CONFIG, _image(INJECT_LINE))
    assert r.returncode == 1
    assert "inject boot line" in r.stderr


def test_a_missing_config_refuses(tmp_path):
    r = _check(tmp_path, None, _image())
    assert r.returncode == 1
    assert "cannot tell" in r.stderr


def test_a_missing_image_refuses(tmp_path):
    r = _check(tmp_path, PLAIN_CONFIG, None)
    assert r.returncode == 1


def test_the_selftest_check_still_passes_an_inject_build(tmp_path):
    """Two checks, two answers: an inject build is not a selftest build, so
    burn.sh --bench must not also need --selftest to flash it."""
    cfg = tmp_path / ".config"
    img = tmp_path / "zephyr.signed.bin"
    cfg.write_text(INJECT_CONFIG, encoding="utf-8")
    img.write_bytes(_image(INJECT_LINE))
    r = subprocess.run(
        ["sh", "-c", '. "$0"; blink_refuse_selftest "$1" "$2"',
         str(LIB), str(cfg), str(img)],
        capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr


def test_the_phrase_it_looks_for_is_the_one_touch_inject_c_prints():
    """Reworded in touch_inject.c, the image check would pass every inject
    image. Read from the lib's grep, found inside a printk literal (adjacent
    literals joined), not in a comment."""
    text = LIB.read_text(encoding="utf-8")
    # To the end of the function: the lib holds more refusals after it.
    start = text.index("blink_refuse_inject()")
    body = text[start:text.index("\n}\n", start)]
    (phrase,) = re.findall(r"grep -aqF '([^']+)'", body)
    src = (ROOT / "firmware" / "src" / "touch_inject.c").read_text(
        encoding="utf-8")
    code = re.sub(r"/\*.*?\*/|//[^\n]*", "", src, flags=re.S)
    calls = re.findall(r"printk\(((?:\s*\"(?:[^\"\\]|\\.)*\")+)", code)
    literals = ["".join(re.findall(r'"((?:[^"\\]|\\.)*)"', c)) for c in calls]
    assert any(phrase in lit for lit in literals), (phrase, literals)


def test_the_option_is_off_unless_asked_for():
    """Default n, never in a .conf a build reads by default."""
    kconfig = (ROOT / "firmware" / "Kconfig").read_text(encoding="utf-8")
    block = kconfig[kconfig.index("config BLINK_TOUCH_INJECT"):]
    block = block[:block.index("\nconfig ") if "\nconfig " in block[1:]
                  else len(block)]
    assert "default y" not in block
    assert "never ship" in block.lower()
    for conf in (ROOT / "firmware").glob("*.conf"):
        assert "BLINK_TOUCH_INJECT" not in conf.read_text(encoding="utf-8"), conf


def test_release_sh_checks_before_publishing():
    text = (ROOT / "tools" / "release.sh").read_text(encoding="utf-8")
    lines = text.splitlines()

    def first(pattern):
        return next(i for i, l in enumerate(lines) if re.match(pattern, l))

    build = first(r"\s*(\(\s*cd [^&]*&&\s*)?west build\b")
    guard = first(r"\s*blink_refuse_inject\b")
    publish = first(r"\s*gh release (create|edit|upload)\b")
    assert build < guard < publish
    assert re.search(r'blink_refuse_inject "\$CFG" "\$BIN"', lines[guard])


def test_flash_encrypted_sh_refuses_before_it_signs():
    text = (ROOT / "tools" / "flash_encrypted.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    guard = next(i for i, l in enumerate(lines)
                 if re.match(r"\s*blink_refuse_inject\b", l))
    sign = next(i for i, l in enumerate(lines)
                if "sign_confirmed.py" in l and not l.lstrip().startswith("#"))
    # The first real call, not the recovery hint an earlier error echoes.
    flash = next(i for i, l in enumerate(lines) if "write_flash" in l
                 and not l.lstrip().startswith(("#", "echo")))
    assert guard < sign and guard < flash
    # Outright: no flag on this path lets an inject image through.
    assert not re.search(r"^\s*--bench\)", text, re.M)


def test_burn_sh_checks_before_it_signs_or_flashes():
    text = (ROOT / "tools" / "burn.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    guard = next(i for i, l in enumerate(lines)
                 if re.search(r"\bblink_refuse_inject\b", l)
                 and not l.lstrip().startswith("#"))
    sign = next(i for i, l in enumerate(lines)
                if "sign_confirmed.py" in l and not l.lstrip().startswith("#"))
    flash = next(i for i, l in enumerate(lines) if "write_flash" in l
                 and not l.lstrip().startswith("#"))
    assert guard < sign and guard < flash
    assert "--bench" in text
    assert "never box this unit" in text


def _source_files(fw, pattern):
    """Files under firmware/ matching `pattern`, outside any build directory.

    A Zephyr build tree is not source: it holds generated .conf files and,
    in every build dir, a DIRECTORY named Kconfig (build-44/Kconfig), which
    rglob("Kconfig*") returns and read_text() raises IsADirectoryError on.
    The suite was red on any desk that had built the firmware -- this one,
    with build, build-44, build-sb and build-wifi -- and green only on CI's
    fresh checkout, so the count in CLAUDE.md could not be reproduced on the
    machine it was written on. Both filters, because either alone misses a
    case: a build dir named some other way still has no Kconfig file, and a
    non-file outside one would still crash the read."""
    for path in sorted(fw.rglob(pattern)):
        if any(p.startswith("build") for p in path.relative_to(fw).parts):
            continue
        if not path.is_file():
            continue
        yield path


def test_no_conf_or_kconfig_anywhere_turns_it_on():
    """Zephyr also reads firmware/boards/*.conf and sysbuild reads
    firmware/sysbuild/*.conf; a `select` or `imply` elsewhere would turn it
    on too. The .config gate would still catch any of these at release, so
    this is about failing early (review, NICE-3)."""
    fw = ROOT / "firmware"
    for conf in _source_files(fw, "*.conf"):
        assert "BLINK_TOUCH_INJECT" not in conf.read_text(encoding="utf-8"), conf
    for k in _source_files(fw, "Kconfig*"):
        text = k.read_text(encoding="utf-8")
        assert not re.search(r"^\s*(select|imply)\s+BLINK_TOUCH_INJECT\b",
                             text, re.M), k
    block = (fw / "Kconfig").read_text(encoding="utf-8")
    block = block[block.index("config BLINK_TOUCH_INJECT"):]
    end = block.find("\nconfig ", 1)
    block = block if end < 0 else block[:end]
    assert not re.search(r"^\s*default\s+y\b", block, re.M)


def test_the_kconfig_flash_lines_name_an_edition():
    """burn.sh exits 2 without --edition, so a help line without one is a
    command that cannot run (review, SHOULD-2)."""
    text = (ROOT / "firmware" / "Kconfig").read_text(encoding="utf-8")
    lines = [l for l in text.splitlines() if "tools/burn.sh" in l]
    assert len(lines) >= 2
    for l in lines:
        assert "--edition " in l, l


def test_burn_sh_bench_does_not_call_an_uninspected_build_an_inject_one():
    """With --bench, a missing .config used to fall into the "flashing a
    TOUCH INJECT build" branch (review, NICE-5). The .config must exist
    before either permission branch is taken."""
    text = (ROOT / "tools" / "burn.sh").read_text(encoding="utf-8")
    need = text.index('[ -f "$BUILD/firmware/zephyr/.config" ]')
    assert need < text.index('if [ "$SELFTEST" = 0 ]')
    assert need < text.index('if [ "$BENCH" = 0 ]')


def test_dev_sh_flash_warns_about_a_bench_image():
    text = (ROOT / "tools" / "dev.sh").read_text(encoding="utf-8")
    assert "blink_refuse_inject" in text
    assert "blink_refuse_selftest" in text
