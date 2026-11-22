"""The Linux binary is built where its glibc floor is, and checked for it.

1.5.0's Linux build could not open a serial port on Ubuntu 24.04: the release
workflow built on `ubuntu-latest`, which had moved to 26.04, and the bundled
termios module asked for GLIBC_2.42 (issue #11, 2026-11-19). The build ran
fine on the runner that made it, so every step passed.

Two things are pinned here: tools/check_glibc_floor.py, which reads a build
for the glibc versions it needs, and the two workflows, which have to name
their Linux runner and run that check.

The ELF files are made here, a magic number and a string table's worth of
version names. A real one would have to come off somebody's machine.
"""
import importlib.util
import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
WORKFLOWS = ROOT / ".github" / "workflows"


def _load():
    spec = importlib.util.spec_from_file_location(
        "check_glibc_floor", ROOT / "tools" / "check_glibc_floor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


floor = _load()


def _elf(path, *versions):
    path.parent.mkdir(parents=True, exist_ok=True)
    names = b"".join(b"GLIBC_" + v.encode() + b"\0" for v in versions)
    path.write_bytes(b"\x7fELF\x02\x01\x01\0" + b"libc.so.6\0" + names)
    return path


def _build(tmp_path, termios="2.4"):
    """The shape of dist/blink, with 1.5.0's other files' versions."""
    d = tmp_path / "blink"
    _elf(d / "blink", "2.2.5", "2.34")
    _elf(d / "_internal" / "libpython3.11.so.1.0", "2.2.5", "2.38", "2.4")
    _elf(d / "_internal" / "lib-dynload" / "termios.so", "2.2.5", termios)
    (d / "_internal" / "base_library.zip").write_bytes(b"PK\x03\x04 GLIBC_9.9")
    return d


def test_the_newest_version_is_compared_as_numbers(tmp_path):
    # As text, 2.4 sorts after 2.38 and 2.2.5 after 2.17.
    f = _elf(tmp_path / "a.so", "2.4", "2.38", "2.2.5", "2.17")
    assert floor.needs(str(f)) == (2, 38)


def test_a_file_that_is_not_elf_is_not_read_for_versions(tmp_path):
    f = tmp_path / "notes.txt"
    f.write_bytes(b"needs GLIBC_2.99")
    assert floor.needs(str(f)) is None


def test_an_elf_file_naming_no_version_is_not_counted(tmp_path):
    f = tmp_path / "static"
    f.write_bytes(b"\x7fELF GLIBC_PRIVATE")
    assert floor.needs(str(f)) is None


def test_a_build_at_the_floor_passes(tmp_path, capsys):
    d = _build(tmp_path, termios="2.39")
    assert floor.main(["x", str(d)]) == 0
    assert "3 files, none needs more than 2.39" in capsys.readouterr().out


def test_the_150_build_is_refused_and_the_file_is_named(tmp_path, capsys):
    d = _build(tmp_path, termios="2.42")
    assert floor.main(["x", str(d)]) == 1
    err = capsys.readouterr().err
    assert "GLIBC_2.42  " + os.path.join(
        "_internal", "lib-dynload", "termios.so") in err
    # Only the offender: the files that are fine are not listed beside it.
    assert "libpython" not in err


def test_a_folder_with_no_elf_in_it_fails(tmp_path, capsys):
    """A wrong path, or a macOS build: passing would be a check that checks
    nothing and reads as green."""
    (tmp_path / "blink").mkdir()
    (tmp_path / "blink" / "blink.exe").write_bytes(b"MZ GLIBC_2.99")
    assert floor.main(["x", str(tmp_path / "blink")]) == 1
    assert "is this a Linux build" in capsys.readouterr().err


@pytest.mark.skipif(os.name == "nt", reason="symlinks need a privilege there")
def test_a_symlink_is_not_followed(tmp_path):
    """PyInstaller links libpython under two names; one file, one report --
    and a link out of the build is not this build's to answer for."""
    d = _build(tmp_path)
    outside = _elf(tmp_path / "elsewhere.so", "2.99")
    os.symlink(outside, d / "_internal" / "link.so")
    assert floor.too_new(str(d)) == ([], 3)


def test_the_floor_is_ubuntu_2404s():
    assert floor.NEWEST_ALLOWED == (2, 39)


# --- the workflows ---------------------------------------------------------

def _text(name):
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def _code(text):
    """The workflow without its comments, which name ubuntu-latest to say why
    it is not used."""
    return "\n".join(l for l in text.splitlines()
                     if not l.lstrip().startswith("#"))


def _job(text, name):
    m = re.search(rf"^  {re.escape(name)}:\n(.*?)(?=^  \S|\Z)", text,
                  re.S | re.M)
    assert m, f"no job named {name}"
    return m.group(1)


def test_the_release_builds_linux_on_a_named_runner():
    code = _code(_text("release-binaries.yml"))
    assert "{os: ubuntu-24.04, label: linux-x86_64}" in code
    assert "ubuntu-latest" not in code


@pytest.mark.parametrize("workflow, job", [
    ("release-binaries.yml", "build"),
    ("installer.yml", "build"),
])
def test_every_build_is_checked_before_the_binary_is_run(workflow, job):
    body = _code(_job(_text(workflow), job))
    build = body.index("sh tools/build_binary.sh")
    check = body.index("python tools/check_glibc_floor.py dist/blink")
    assert build < check
    # On Linux only: the check fails a build with no ELF file in it.
    step = body[body.rindex("- name:", 0, check):check]
    assert "if: runner.os == 'Linux'" in step
    # Ahead of the first step that executes the binary, which is the step
    # that passes on the build machine whatever glibc it was built against.
    ran = re.search(r"--version|check_install\.sh", body)
    assert ran and check < ran.start()


def test_ci_builds_linux_where_the_release_does():
    """And every job that takes the artifact names the same runner: the
    artifact is called blink-<matrix.os>, so one left on ubuntu-latest would
    wait for a download that was never uploaded."""
    code = _code(_text("installer.yml"))
    assert "ubuntu-24.04" in _job(code, "build")
    users = [name for name in re.findall(r"^  ([\w-]+):\n", code, re.M)
             if "name: blink-${{ matrix.os }}" in _job(code, name)]
    assert len(users) >= 4, users
    for name in users:
        assert "ubuntu-latest" not in _job(code, name), name
