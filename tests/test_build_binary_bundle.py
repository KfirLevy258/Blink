"""tools/build_binary.sh bundles the settings page's two vendored fonts.

pc/fonts goes into the frozen app next to theme_builder.html. Git Bash does
not convert "/d/a/.../pc/fonts:pc/fonts" for PyInstaller, so on Windows it is
cygpath's native form and a ";" -- the same treatment THEME_DATA has, for
the same reason (CI run 41093831896).
"""
import os
import re
import stat
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "tools", "build_binary.sh")


def _case_block():
    text = open(SCRIPT, encoding="utf-8").read()
    m = re.search(r'case "\$\(uname -s\)" in\nMINGW\* \| MSYS\* \| CYGWIN\*\)\n'
                  r'\tTHEME_DATA=.*?\nesac\n', text, re.S)
    assert m, "the THEME_DATA case block moved"
    return m.group(0)


def _stub(d, name, body):
    p = os.path.join(d, name)
    with open(p, "w") as f:
        f.write("#!/bin/sh\n" + body + "\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IXUSR)


def _fonts_data(tmp_path, uname, with_cygpath):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _stub(str(bindir), "uname", f"echo {uname}")
    if with_cygpath:
        _stub(str(bindir), "cygpath", r"printf '%s\n' 'D:\a\pc\fonts'")
    script = f'ROOT=/r\n{_case_block()}printf "%s" "$FONTS_DATA"\n'
    env = {"PATH": f"{bindir}:/usr/bin:/bin"}
    return subprocess.run(["/bin/sh", "-c", script], capture_output=True,
                          text=True, env=env, check=True).stdout


# Runs the case block under /bin/sh with stub commands on a ':' PATH, which a
# Windows Python launched from PowerShell has neither of; the block itself is
# exercised on Windows by every CI build, under Git Bash.
@pytest.mark.skipif(os.name == "nt", reason="needs /bin/sh")
def test_fonts_data_argument_per_platform(tmp_path):
    (tmp_path / "w").mkdir()
    assert _fonts_data(tmp_path / "w", "MINGW64_NT", True) == \
        r"D:\a\pc\fonts;pc/fonts"
    (tmp_path / "m").mkdir()
    assert _fonts_data(tmp_path / "m", "Darwin", False) == \
        "/r/pc/fonts:pc/fonts"


def test_fonts_are_bundled():
    text = open(SCRIPT, encoding="utf-8").read()
    assert '--add-data "$FONTS_DATA"' in text
    fonts = os.path.join(ROOT, "pc", "fonts")
    names = sorted(os.listdir(fonts))
    assert names == ["Montserrat-Medium-latin.woff2",
                     "Montserrat-SemiBold-latin.woff2", "OFL.txt"]
    # The build checks each font landed, as it does the page.
    for n in names[:2]:
        assert n in text
