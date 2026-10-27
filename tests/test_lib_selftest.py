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
    (phrase,) = re.findall(r"grep -aqF '([^']+)'", LIB.read_text(encoding="utf-8"))
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
