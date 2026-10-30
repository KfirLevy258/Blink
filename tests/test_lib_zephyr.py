"""tools/lib_zephyr.sh, run in the shells a developer really types into.

What these pin, and where it came from (both first turned up in gapietro's
fork, 2026-10-12/13; the zsh one again on this repo's own Mac on
2026-10-25):

  - zsh, the default macOS shell, treats a glob that matches nothing as an
    error and drops the whole command line. With only one of the workspace
    candidates present, the documented
    `. tools/lib_zephyr.sh && blink_zephyr_activate` printed
    "no matches found: ~/zephyr-v*" and did nothing else.
  - A workspace venv with no esptool fails the firmware configure, and the
    fix Zephyr suggests installs a bitstring espefuse cannot import. So the
    hint printed here installs both, held to pc/requirements.txt's pins.

blink_zephyr_activate must NOT require esptool. dev.sh `up` activates the
workspace only to restart the daemon after `down` stopped it, and an
activate that failed over a build tool left the desk on HOST LOST for want
of something the daemon never uses. The esptool check is
blink_zephyr_build_ready, called immediately before a build and nowhere else.

Each test runs the real library in a real shell, against a fake workspace
under tmp_path. HOME is set for every child on top of conftest.py's
redirect, so the developer's own ~/zephyrproject cannot be the one found,
and no shell reads its startup files (Sandbox.run says how).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
LIBRARY = REPO / "tools" / "lib_zephyr.sh"
REQUIREMENTS = REPO / "pc" / "requirements.txt"


def _shell_param(name):
    path = shutil.which(name)
    return pytest.param(path, id=name, marks=pytest.mark.skipif(
        path is None, reason=f"no {name} on this machine"))


ZSH = _shell_param("zsh")
BASH = _shell_param("bash")
EVERY_SHELL = [ZSH, BASH, _shell_param("sh"), _shell_param("dash")]

# Variables that would leak the caller's environment into the child: a
# workspace override, a stale Zephyr, the repo root, or a startup file. A
# developer's ~/.zshenv with nullglob set would mask the exact zsh failure
# this file is about; BASH_ENV and ENV are the same hole for bash and sh.
_SCRUB = ("BLINK_ZEPHYR", "ZEPHYR_BASE", "ROOT", "ZSH_VERSION", "ZDOTDIR",
          "BASH_ENV", "ENV")

# The scripts only call shell builtins until esptool is looked up, so a PATH
# of one directory decides whether esptool "exists". Not on Windows: Git
# Bash cannot be trusted to exec a chmod'ed #!/bin/sh stand-in.
needs_exec = pytest.mark.skipif(os.name == "nt",
                                reason="runs a #!/bin/sh stand-in")

CHECK_BUILD = ('blink_zephyr_activate && blink_zephyr_build_ready;'
               ' echo "status=$?"')


class Sandbox:
    """A fake HOME holding (optionally) a workspace, and a way to run the
    library in a given shell against it."""

    def __init__(self, shell, home):
        self.shell = shell
        self.home = home

    def add_workspace(self):
        """Just enough of a west workspace for blink_zephyr_ws to accept."""
        ws = self.home / "zephyrproject"
        for rel in (".venv/bin/activate", "zephyr/zephyr-env.sh"):
            f = ws / rel
            f.parent.mkdir(parents=True, exist_ok=True)
            f.write_text("", encoding="utf-8")
        return ws

    def path_with(self, esptool):
        """A one-directory PATH, with or without a stand-in esptool."""
        d = self.home / "bin"
        d.mkdir(exist_ok=True)
        if esptool:
            stub = d / "esptool"
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)
        return str(d)

    def subdir(self, name):
        d = self.home / name
        d.mkdir()
        return str(d)

    def run(self, script, path=None, in_repo=True, cwd=None, setup=""):
        env = {k: v for k, v in os.environ.items() if k not in _SCRUB}
        env["HOME"] = str(self.home)
        if in_repo:
            env["ROOT"] = str(REPO)
        if path is not None:
            env["PATH"] = path
        cmd = [self.shell]
        if os.path.basename(self.shell).startswith("zsh"):
            cmd.append("-f")    # skip /etc/zshenv and ~/.zshenv
        # `setup` is the caller's own shell state, in place before sourcing.
        # The library in forward slashes: on Windows str(LIBRARY) is
        # D:\a\...\lib_zephyr.sh, a name with no "/" in it, and POSIX `.`
        # looks such a name up on PATH instead of opening it. bash falls
        # back to opening it anyway; Git for Windows' dash does not, and
        # said "lib_zephyr.sh: not found" (CI run 41093831896). D:/a/...
        # has a slash, so every shell opens it, and the MSYS runtime
        # accepts that spelling of a drive path.
        cmd += ["-c", f'{setup}\n. "{LIBRARY.as_posix()}"; {script}']
        return subprocess.run(cmd, env=env, cwd=cwd, capture_output=True,
                              text=True, timeout=30)


@pytest.fixture
def box(request, tmp_path):
    return Sandbox(request.param, tmp_path)


def _with_shells(shells):
    return pytest.mark.parametrize("box", shells, indirect=True)


# The shell prints the path it expects next to the one it found, so the
# comparison is in the shell's own spelling: Git Bash rewrites HOME's
# C:\Users\... as /c/Users/..., which no path built in Python would match
# (gapietro's fork ran into it on its Windows runner, 8bc2c8e).
LOCATE = ('blink_zephyr_ws; echo "status=$?";'
          ' printf "expect=%s\\n" "$HOME/zephyrproject"')


def _located(result, then=()):
    """LOCATE's three lines, and exactly `then` after them: a stray line
    (a second path, a warning on stdout) fails the test instead of being
    skipped over."""
    lines = result.stdout.splitlines()
    assert lines[3:] == list(then), lines
    found, status, expect = lines[:3]
    assert expect.startswith("expect=")
    return found, status, expect[len("expect="):]


def _pip_hints(stderr):
    return [line.strip() for line in stderr.splitlines()
            if "pip install" in line]


def _is_pinned_hint(hint):
    """An interpreter from the workspace venv, installing both packages
    with pc/requirements.txt as the constraints file that pins them."""
    pins = REQUIREMENTS.read_text(encoding="utf-8")
    assert re.search(r"^esptool==", pins, re.M)
    assert re.search(r"^bitstring==", pins, re.M)
    python, _, rest = hint.partition(" -m pip ")
    return (python.endswith(".venv/bin/python")
            and rest == f"install -c {REQUIREMENTS} esptool bitstring")


@_with_shells(EVERY_SHELL)
def test_the_one_existing_candidate_is_found(box):
    """~/zephyrproject alone, so every other candidate glob is empty: the
    zsh abort, and the sh/bash behaviour that has to stay as it was."""
    box.add_workspace()
    r = box.run(LOCATE)
    assert "no matches found" not in r.stderr
    found, status, expect = _located(r)
    assert (status, found) == ("status=0", expect)


@_with_shells(EVERY_SHELL)
def test_a_missing_workspace_returns_1_instead_of_aborting(box):
    r = box.run('blink_zephyr_ws; echo "status=$?"')
    assert "no matches found" not in r.stderr
    assert r.stdout.strip() == "status=1"


@_with_shells([ZSH])
def test_zsh_nullglob_is_back_off_after_the_search(box):
    """The search turns nullglob on for itself and puts it back."""
    box.add_workspace()
    r = box.run("blink_zephyr_ws >/dev/null;"
                " if [[ -o nullglob ]]; then echo on; else echo off; fi")
    assert r.stdout.strip() == "off"


@_with_shells([BASH])
def test_bash_failglob_neither_breaks_the_search_nor_is_lost(box):
    """failglob makes bash abort on an empty glob the way zsh does. The
    search suspends it while it runs and restores it after."""
    box.add_workspace()
    r = box.run(LOCATE + "; shopt -q failglob && echo failglob-kept",
                setup="shopt -s failglob")
    assert "no match" not in r.stderr
    found, status, expect = _located(r, then=["failglob-kept"])
    assert (status, found) == ("status=0", expect)


@needs_exec
@_with_shells(EVERY_SHELL)
def test_activating_without_esptool_succeeds_quietly(box):
    """dev.sh `up` restarts the daemon via activate; failing it for a
    missing build tool is what left a desk on HOST LOST."""
    box.add_workspace()
    r = box.run('blink_zephyr_activate; echo "status=$?"',
                path=box.path_with(esptool=False))
    assert (r.stdout.strip(), r.stderr) == ("status=0", ""), r.stderr


@needs_exec
@_with_shells(EVERY_SHELL)
def test_build_ready_refuses_without_esptool_and_prints_a_pinned_fix(box):
    box.add_workspace()
    r = box.run(CHECK_BUILD, path=box.path_with(esptool=False))
    assert r.stdout.strip() == "status=1"
    assert "no esptool on PATH" in r.stderr
    # Exactly one line to paste, and a pinned one: plain `pip install
    # esptool` pulls in bitstring 5.0.0 again.
    hints = _pip_hints(r.stderr)
    assert len(hints) == 1 and _is_pinned_hint(hints[0]), hints


@needs_exec
@_with_shells(EVERY_SHELL)
def test_build_ready_is_silent_when_esptool_is_there(box):
    box.add_workspace()
    r = box.run(CHECK_BUILD, path=box.path_with(esptool=True))
    assert (r.stdout.strip(), r.stderr) == ("status=0", ""), r.stderr


@needs_exec
@_with_shells(EVERY_SHELL)
def test_away_from_the_repo_the_fix_is_pinned_or_written_as_prose(box):
    """No ROOT, and the shell started in an unrelated directory. bash and
    zsh can still name the library's own path, so the repo is found and one
    pinned line comes out. Plain sh cannot; it has to describe the fix in
    words, never print a pip line without the pins -- pasted into bash that
    installed bitstring 5.0.0, and zsh passed pip a stray `#`."""
    box.add_workspace()
    r = box.run(CHECK_BUILD, path=box.path_with(esptool=False),
                in_repo=False, cwd=box.subdir("unrelated"))
    assert r.stdout.strip() == "status=1"
    hints = _pip_hints(r.stderr)
    assert all(_is_pinned_hint(h) for h in hints), hints
    if os.path.basename(box.shell).startswith(("bash", "zsh")):
        assert len(hints) == 1, r.stderr
    else:
        assert "pc/requirements.txt" in r.stderr


@needs_exec
@_with_shells([ZSH])
def test_output_from_a_zsh_chpwd_hook_stays_out_of_the_fix(box):
    """Interactive zsh runs chpwd after every cd, and a hook that lists the
    directory (this repo's Mac has one) used to end up inside the capture
    that locates the repo, so no pinned line came out. -f keeps the real
    startup files away, so the hook is declared here."""
    box.add_workspace()
    r = box.run(CHECK_BUILD, path=box.path_with(esptool=False),
                in_repo=False, cwd=box.subdir("unrelated"),
                setup="chpwd() { echo CHPWD-OUTPUT; echo more; }")
    assert r.stdout.strip() == "status=1"
    assert "CHPWD-OUTPUT" not in r.stderr
    hints = _pip_hints(r.stderr)
    assert len(hints) == 1 and _is_pinned_hint(hints[0]), hints


# The first line that RUNS `west build`: plain, behind `if !`, or inside a
# `( cd ... &&` subshell. Comments that mention it do not start that way.
_BUILD_COMMAND = re.compile(
    r"\s*(if\s+!\s*)?(\(\s*cd [^&]*&&\s*)?west build\b")


@pytest.mark.parametrize("script", ["release.sh", "burn.sh", "dev.sh"])
def test_build_ready_runs_before_the_first_build(script):
    """Checked in the text, not by running: running these builds firmware,
    and dev.sh stops the daemon on the way."""
    lines = (REPO / "tools" / script).read_text(encoding="utf-8").splitlines()
    first = next(n for n, line in enumerate(lines)
                 if _BUILD_COMMAND.match(line))
    # Called, at the start of a line; a comment that names it is not a call.
    called = [line for line in lines[:first]
              if re.match(r"\s*blink_zephyr_build_ready\b", line)]
    assert called, f"{script}: no build check before line {first + 1}"


def test_dev_sh_activate_leaves_the_build_check_to_flash():
    """`dev.sh up` relaunches through activate(), which must not need
    esptool."""
    text = (REPO / "tools" / "dev.sh").read_text(encoding="utf-8")
    m = re.search(r"^activate\(\) \{\n(.*?)^\}", text, re.M | re.S)
    assert m is not None
    assert "blink_zephyr_build_ready" not in m.group(1)
