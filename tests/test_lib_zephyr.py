"""tools/lib_zephyr.sh, sourced from the shells people actually type in.

Two faults, both found by gapietro in their fork on 2026-10-12/13:

  - zsh, the macOS login shell, aborts a whole command on a glob that matches
    nothing. A Mac with just one of the four workspace candidates got
    "no matches found: ~/zephyr-v*" from the documented
    `. tools/lib_zephyr.sh && blink_zephyr_activate`, and nothing else. It
    happened on this repo's own Mac on 2026-10-25 too.
  - A workspace venv without esptool fails the firmware configure, and
    Zephyr's own suggested fix installs a bitstring that espefuse cannot
    import -- so the fix printed here installs both at the pinned versions.

The esptool check is deliberately NOT part of blink_zephyr_activate. dev.sh
`up` activates the workspace to relaunch the daemon, after `down` has killed
it; an activate that failed over a build tool left the desk on HOST LOST for
want of something the daemon never runs. So activate must succeed without
esptool, and blink_zephyr_build_ready is called only right before a build.

Every test runs the library in a real shell against a fake workspace in
tmp_path. HOME is set explicitly for each child on top of the redirect in
tests/conftest.py, so the real ~/zephyrproject can never be the one found,
and the shells start without their startup files (see _run).
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "tools" / "lib_zephyr.sh"


def _shell(name):
    found = shutil.which(name)
    return pytest.param(found, id=name, marks=pytest.mark.skipif(
        found is None, reason=f"{name} is not installed here"))


ZSH = _shell("zsh")
POSIX_SHELLS = [_shell("sh"), _shell("bash"), _shell("dash")]
ALL_SHELLS = [ZSH] + POSIX_SHELLS


def _workspace(home):
    """The least that blink_zephyr_ws() takes for a west workspace."""
    ws = home / "zephyrproject"
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")
    (ws / "zephyr").mkdir()
    (ws / "zephyr" / "zephyr-env.sh").write_text("", encoding="utf-8")
    return ws


def _run(shell, script, home, path=None, root=True, cwd=None, pre=""):
    env = dict(os.environ)
    # Anything that would make a shell read a startup file. A developer's
    # .zshenv that sets nullglob would otherwise hide the very zsh fault
    # these tests exist for; BASH_ENV and ENV do the same for bash and sh.
    for leak in ("BLINK_ZEPHYR", "ZEPHYR_BASE", "ROOT", "ZSH_VERSION",
                 "ZDOTDIR", "BASH_ENV", "ENV"):
        env.pop(leak, None)
    env["HOME"] = str(home)
    if root:
        env["ROOT"] = str(ROOT)
    if path is not None:
        env["PATH"] = path
    argv = [shell]
    if os.path.basename(shell).startswith("zsh"):
        argv.append("-f")   # no /etc/zshenv, no ~/.zshenv
    # `pre` runs before the library is sourced: a caller's own settings.
    argv += ["-c", f'{pre}\n. "{LIB}"; {script}']
    return subprocess.run(argv, env=env, cwd=cwd, capture_output=True,
                          text=True, timeout=30)


# The expected path is printed by the same shell rather than built here: Git
# Bash on Windows turns HOME's C:\Users\... into /c/Users/..., so a path made
# in Python can never equal what the shell prints there (gapietro's fork hit
# this on its Windows runner, 8bc2c8e). The shell's own spelling of
# "$HOME/zephyrproject" is right on every platform.
FIND = 'blink_zephyr_ws; echo "exit=$?"; printf "want=%s\\n" "$HOME/zephyrproject"'


@pytest.mark.parametrize("shell", ALL_SHELLS)
def test_the_workspace_is_found_when_the_other_candidates_are_absent(
        shell, tmp_path):
    """Only ~/zephyrproject exists, so the three other candidates match
    nothing: the zsh case that aborted, and the sh/bash case that must not
    change."""
    _workspace(tmp_path)
    r = _run(shell, FIND, tmp_path)
    assert "no matches found" not in r.stderr
    got, status, want = r.stdout.splitlines()
    assert status == "exit=0"
    assert want.startswith("want=") and got == want[len("want="):]


@pytest.mark.parametrize("shell", ALL_SHELLS)
def test_no_workspace_is_a_failure_status_not_an_abort(shell, tmp_path):
    r = _run(shell, 'blink_zephyr_ws; echo "exit=$?"', tmp_path)
    assert "no matches found" not in r.stderr
    assert r.stdout.strip() == "exit=1"


@pytest.mark.parametrize("shell", [ZSH])
def test_zsh_callers_keep_their_own_glob_setting(shell, tmp_path):
    """nullglob is set only for the function's own duration."""
    _workspace(tmp_path)
    r = _run(shell,
             "blink_zephyr_ws >/dev/null; "
             "[[ -o nullglob ]] && echo nullglob=on || echo nullglob=off",
             tmp_path)
    assert r.stdout.strip() == "nullglob=off"


@pytest.mark.parametrize("shell", [_shell("bash")])
def test_bash_with_failglob_finds_the_workspace_and_keeps_failglob(
        shell, tmp_path):
    """`shopt -s failglob` makes bash abort on an unmatched glob just as zsh
    does by default. The search turns it off for its own length only."""
    _workspace(tmp_path)
    r = _run(shell, FIND + '; shopt -q failglob && echo failglob=on',
             tmp_path, pre="shopt -s failglob")
    assert "no match" not in r.stderr
    got, status, want, fg = r.stdout.splitlines()
    assert status == "exit=0" and got == want[len("want="):]
    assert fg == "failglob=on"


def _pip_lines(stderr):
    return [l for l in stderr.splitlines() if "pip install" in l]


def _assert_pinned(line, python_suffix=".venv/bin/python"):
    """The line names an interpreter and installs both packages through
    pc/requirements.txt as constraints, which is what pins them."""
    req = ROOT / "pc" / "requirements.txt"
    text = req.read_text(encoding="utf-8")
    assert re.search(r"^esptool==", text, re.M)
    assert re.search(r"^bitstring==", text, re.M)
    hint = line.strip()
    assert hint.endswith(f" -m pip install -c {req} esptool bitstring"), hint
    assert hint.split(" -m pip ")[0].endswith(python_suffix), hint


# PATH is an empty directory, or one holding a stand-in esptool: activate and
# the check use only shell builtins, so whether esptool is "installed" is
# decided entirely by what goes in there. Skipped on Windows, where a
# #!/bin/sh stand-in made executable by chmod is not something Git Bash can
# be relied on to run.
posix_only = pytest.mark.skipif(os.name == "nt",
                                reason="exec of a #!/bin/sh stand-in")


def _bin(tmp_path, esptool):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if esptool:
        fake = bin_dir / "esptool"
        fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake.chmod(0o755)
    return str(bin_dir)


@posix_only
@pytest.mark.parametrize("shell", ALL_SHELLS)
def test_activate_does_not_need_esptool(shell, tmp_path):
    """dev.sh `up` relaunches the daemon through activate. Failing it over a
    build tool left the desk on HOST LOST."""
    _workspace(tmp_path)
    r = _run(shell, 'blink_zephyr_activate; echo "exit=$?"', tmp_path,
             path=_bin(tmp_path, esptool=False))
    assert r.stdout.strip() == "exit=0", r.stderr
    assert r.stderr == ""


@posix_only
@pytest.mark.parametrize("shell", ALL_SHELLS)
def test_the_build_check_stops_without_esptool(shell, tmp_path):
    _workspace(tmp_path)
    r = _run(shell, 'blink_zephyr_activate && blink_zephyr_build_ready; '
                    'echo "exit=$?"', tmp_path,
             path=_bin(tmp_path, esptool=False))
    assert r.stdout.strip() == "exit=1"
    assert "esptool is not on PATH" in r.stderr
    # The line to paste installs what pc/requirements.txt pins -- a bare
    # `pip install esptool` would bring back bitstring 5.0.0.
    (line,) = _pip_lines(r.stderr)
    _assert_pinned(line)


@posix_only
@pytest.mark.parametrize("shell", ALL_SHELLS)
def test_the_build_check_passes_a_ready_workspace(shell, tmp_path):
    _workspace(tmp_path)
    r = _run(shell, 'blink_zephyr_activate && blink_zephyr_build_ready; '
                    'echo "exit=$?"', tmp_path,
             path=_bin(tmp_path, esptool=True))
    assert r.stdout.strip() == "exit=0", r.stderr
    assert r.stderr == ""


@posix_only
@pytest.mark.parametrize("shell", ALL_SHELLS)
def test_outside_the_repo_the_hint_is_pinned_or_not_a_command(shell,
                                                               tmp_path):
    """ROOT unset and the shell somewhere else. The library finds the repo
    from its own path where the shell can say what that is (bash, zsh). A
    plain POSIX sh cannot, and must then print prose, never a pip line with
    the versions missing: pasted into bash that installed bitstring 5.0.0,
    and in zsh it handed a stray `#` to pip."""
    _workspace(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = _run(shell, 'blink_zephyr_activate && blink_zephyr_build_ready; '
                    'echo "exit=$?"', tmp_path,
             path=_bin(tmp_path, esptool=False), root=False,
             cwd=str(elsewhere))
    assert r.stdout.strip() == "exit=1"
    for line in _pip_lines(r.stderr):
        _assert_pinned(line)
    name = os.path.basename(shell)
    if name.startswith(("bash", "zsh")):
        assert len(_pip_lines(r.stderr)) == 1, r.stderr
    else:
        assert "pc/requirements.txt" in r.stderr


@posix_only
@pytest.mark.parametrize("shell", [ZSH])
def test_a_chatty_chpwd_hook_does_not_spoil_the_hint(shell, tmp_path):
    """An interactive zsh runs chpwd hooks on every cd, and one that lists
    the directory (as this repo's own Mac's does) used to land in the
    capture that finds the repo, so no pinned line was printed. -f keeps
    the real startup files out; the hook is defined by hand instead."""
    _workspace(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    r = _run(shell, 'blink_zephyr_activate && blink_zephyr_build_ready; '
                    'echo "exit=$?"', tmp_path,
             path=_bin(tmp_path, esptool=False), root=False,
             cwd=str(elsewhere),
             pre="chpwd() { echo NOISE-FROM-CHPWD; echo second line; }")
    assert r.stdout.strip() == "exit=1"
    assert "NOISE-FROM-CHPWD" not in r.stderr
    (line,) = _pip_lines(r.stderr)
    _assert_pinned(line)


@pytest.mark.parametrize("script", ["release.sh", "burn.sh", "dev.sh"])
def test_every_build_is_preceded_by_the_check(script):
    """Read from the scripts rather than run: running them builds firmware,
    and dev.sh's runs stop the daemon. Comments mention `west build` too,
    so this looks for the first build COMMAND, a line that starts with it
    after optional indentation, an `if !`, or a subshell's `( cd ... &&`."""
    text = (ROOT / "tools" / script).read_text(encoding="utf-8")
    lines = text.splitlines()
    build = next(i for i, l in enumerate(lines)
                 if re.match(r"\s*(if\s+!\s*)?(\(\s*cd [^&]*&&\s*)?west build\b", l))
    before = "\n".join(lines[:build])
    # A command, not a mention: a comment naming the function must not count.
    assert re.search(r"^\s*blink_zephyr_build_ready\b", before, re.M)


def test_dev_sh_activate_does_not_run_the_build_check():
    """`dev.sh up` goes through activate(); the check belongs to flash."""
    text = (ROOT / "tools" / "dev.sh").read_text(encoding="utf-8")
    body = re.search(r"^activate\(\) \{\n(.*?)^\}", text, re.M | re.S)
    assert body and "blink_zephyr_build_ready" not in body.group(1)
