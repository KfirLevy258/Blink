"""Importing a module of ours does nothing: no install, no service call.

On 2026-10-30, 22:25-22:27 IST, a reviewer checking that every module still
imported on Python 3.9 ran `import blink_main` with HOME set to a scratch
folder. blink_main.py ended in a bare `raise SystemExit(main())` with no
`if __name__ == "__main__":` in front of it, and a bare `blink` is
`blink install` -- so the import installed. HOME was scratch, but launchctl
acts on the gui/<uid> domain whatever HOME says: it booted out the owner's
real com.blink.bridge job and bootstrapped a stray daemon, which ran for a
minute against the real ~/.blink and the real board.

tests/test_sandbox.py could not see it: it asks whether anything at import
resolves HOME, and had a written exemption for blink_main.py's last line.

This runs the import in a child, as the reviewer did, and watches for the two
things it did: a call to launchctl / systemctl / schtasks, and files under
the home it was given. The child's PATH holds nothing but stubs that record
their arguments -- not even /usr/bin, so a real launchctl is out of reach
even if the guard under test were gone. The stubs are checked to shadow
before anything is imported.
"""
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.skipif(
    sys.platform == "win32",
    reason="the stubs are sh scripts; Windows has no launchctl to protect")

SERVICE_TOOLS = ("launchctl", "systemctl", "schtasks")

# What the child does: import each module on its own and say what came of it.
# SystemExit is caught per module -- an import that ENDS the process is the
# bug (blink_main exited 0 after installing), and one such module must not
# hide the verdict on the rest.
_CHILD = r"""
import importlib, sys
# A bare argv, as in the reviewer's import: a bare `blink` is `blink install`.
names, sys.argv = sys.argv[1:], sys.argv[:1]
for name in names:
    try:
        importlib.import_module(name)
    except SystemExit as e:
        print("EXIT", name, e.code)
    except BaseException as e:
        print("ERR", name, type(e).__name__)
"""


def _stub_path(tmp_path):
    """A directory holding only recording stubs, and the log they write."""
    bindir = tmp_path / "stubbin"
    bindir.mkdir()
    log = tmp_path / "service-calls.log"
    for tool in SERVICE_TOOLS:
        stub = bindir / tool
        # `print` says "not loaded" (113, ESRCH), the answer for a job that
        # is not there. Answering 0 with no output left an install polling
        # for a bootout to finish until the test timed out.
        stub.write_text('#!/bin/sh\necho "%s $*" >> "%s"\n'
                        '[ "$1" = print ] && exit 113\nexit 0\n'
                        % (tool, log))
        stub.chmod(0o755)
    return bindir, log


def _exits(run):
    return [ln for ln in run.stdout.splitlines() if ln.startswith("EXIT ")]


def assert_stubs_shadow(bindir, env):
    """Every service tool resolves to its stub in `env`, or the test stops.

    One `command -v` per tool: dash, which is /bin/sh on Debian and Ubuntu,
    answers only for the first operand of `command -v a b c`, so the three at
    once passed on macOS and failed on the Linux runners (CI run
    41192997263). `command -v` is a builtin, so it needs no PATH of its own;
    the script is the same on every POSIX shell."""
    for tool in SERVICE_TOOLS:
        seen = subprocess.run(
            ["/bin/sh", "-c", 'command -v "$1"', "sh", tool],
            env=env, capture_output=True, text=True, timeout=30)
        assert seen.stdout.strip() == str(bindir / tool), (
            "the stub for %s does not shadow the real tool; refusing to"
            " import: %r" % (tool, seen.stdout))


def _modules():
    mods = ["blink_main", "claude_usage_bridge"]
    for p in sorted((ROOT / "pc").rglob("*.py")):
        rel = p.relative_to(ROOT).with_suffix("")
        parts = rel.parts[:-1] if rel.name == "__init__" else rel.parts
        mods.append(".".join(parts))
    return mods


def _import_in_a_child(tmp_path, modules):
    bindir, log = _stub_path(tmp_path)
    home = tmp_path / "scratch-home"
    home.mkdir()
    env = {
        "PATH": str(bindir),
        "HOME": str(home), "USERPROFILE": str(home),
        "APPDATA": str(home / "AppData" / "Roaming"),
        "LOCALAPPDATA": str(home / "AppData" / "Local"),
        "PYTHONDONTWRITEBYTECODE": "1",
        # So a regression is caught by what it did to the scratch home and
        # the stubs, not by a pip install reaching the network from here.
        "BLINK_SKIP_DEPS": "1",
        # Deliberately NOT BLINK_SKIP_SERVICE: that variable is what made
        # every test blind to this.
    }
    # The stubs must be what a bare name resolves to, or this test is the
    # incident. `command -v` is a builtin, so it needs no PATH of its own.
    assert_stubs_shadow(bindir, env)
    try:
        run = subprocess.run(
            [sys.executable, "-s", "-c", _CHILD, *modules], cwd=ROOT,
            env=env, capture_output=True, text=True, timeout=60)
    except subprocess.TimeoutExpired:
        # An import that never returns is an install waiting on its service
        # (the first run of this test without the guard did exactly that).
        calls = log.read_text().splitlines() if log.exists() else []
        pytest.fail("the import did not return in 60 s; it was running"
                    " something. %d service calls so far, of kinds %r; home:"
                    " %r" % (len(calls), sorted(set(calls)),
                             sorted(p.name for p in home.iterdir())))
    calls = log.read_text().splitlines() if log.exists() else []
    left = sorted(p.name for p in home.iterdir())
    return run, calls, left


def test_importing_the_entry_points_installs_nothing(tmp_path):
    run, calls, left = _import_in_a_child(
        tmp_path, ["blink_main", "claude_usage_bridge"])
    assert _exits(run) == [], "an import ended the process: %r" % _exits(run)
    assert calls == [], ("an import called the service manager: %r"
                         % sorted(set(calls)))
    assert left == [], "an import wrote into the home: %r" % left


def test_importing_any_module_of_ours_does_nothing(tmp_path):
    """Every module under pc/ as well: whichever grows a bare main() or a
    start-up call next is caught the day it is written, not by the next
    reviewer's import."""
    run, calls, left = _import_in_a_child(tmp_path, _modules())
    assert _exits(run) == [], "an import ended the process: %r" % _exits(run)
    assert calls == [], ("an import called the service manager: %r"
                         % sorted(set(calls)))
    assert left == [], "an import wrote into the home: %r" % left


def test_the_entry_point_still_runs_as_a_program(tmp_path):
    """The guard must not turn the launcher into a no-op: PyInstaller runs
    the script as __main__, and so does `python blink_main.py`. `--version`
    reaches main() and touches nothing."""
    bindir, _log = _stub_path(tmp_path)
    env = dict(os.environ, PATH=str(bindir), HOME=str(tmp_path),
               PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run(
        [sys.executable, "-s", str(ROOT / "blink_main.py"), "--version"],
        env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.startswith("blink "), out.stdout


def test_the_dash_m_branch_still_runs_a_module(tmp_path):
    """`blink -m <module>` is how the frozen daemon runs esptool and the
    eFuse probe. Run here against a stdlib module that prints."""
    bindir, _log = _stub_path(tmp_path)
    env = dict(os.environ, PATH=str(bindir), HOME=str(tmp_path),
               PYTHONDONTWRITEBYTECODE="1")
    out = subprocess.run(
        [sys.executable, "-s", str(ROOT / "blink_main.py"), "-m",
         "this"], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert "Zen of Python" in out.stdout


def test_the_shadow_check_refuses_a_stub_that_is_missing(tmp_path):
    """The check is only worth its place if it can fail: a PATH with a stub
    for two of the three tools, or none, is refused, on every shell."""
    bindir, _log = _stub_path(tmp_path)
    (bindir / "systemctl").unlink()
    with pytest.raises(AssertionError, match="systemctl"):
        assert_stubs_shadow(bindir, {"PATH": str(bindir)})
    with pytest.raises(AssertionError, match="launchctl"):
        assert_stubs_shadow(bindir, {"PATH": str(tmp_path)})
