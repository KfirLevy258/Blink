"""tools/lib_launchd.sh: burn.sh and dev.sh wait for the installed daemon
as long as the daemon's own stop grace, and never kick an instance that is
still leaving.

The grace is pc/cli.py's STOP_GRACE_S, written into the plist as
ExitTimeOut, and the "say what it is waiting for" point is its
SLOW_STOP_NOTE_S. They were 5 and 10 s in the scripts while the daemon held a
SIGTERM for up to 180 s inside an OTA write, so a flash started mid-write
refused with the port still busy and its restore ran `kickstart -k` on the
instance that was still writing (review of develop/fork-picks, 2026-10-28,
S3). The numbers live in two languages; this is what holds them together.

dev.sh's behaviour around the wait is driven end to end by
tests/test_dev_sh.py. burn.sh needs a board, a build and a signing key to
reach that far, so its half is pinned here on the text.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "tools" / "lib_launchd.sh"

from pc import cli


def _lib_value(name):
    m = re.search(rf'^{name}=(?:"\$\{{{name}:-)?(\d+)',
                  LIB.read_text(encoding="utf-8"), re.M)
    assert m, name
    return int(m.group(1))


def test_the_grace_is_the_daemons():
    assert _lib_value("BLINK_STOP_GRACE_S") == cli.STOP_GRACE_S
    assert _lib_value("BLINK_SLOW_STOP_NOTE_S") == cli.SLOW_STOP_NOTE_S


def _code(path):
    return [l for l in path.read_text(encoding="utf-8").splitlines()
            if not l.lstrip().startswith("#")]


@pytest.mark.parametrize("script", ["burn.sh", "dev.sh"])
def test_no_restore_kills_a_running_instance(script):
    code = _code(ROOT / "tools" / script)
    kicks = [l for l in code if re.search(r"\blaunchctl kickstart\b", l)]
    assert kicks, script
    assert not [l for l in kicks if "-k" in l.split()], script
    assert any("lib_launchd.sh" in l for l in code), script


def test_burn_waits_under_its_trap_and_before_the_probe():
    code = _code(ROOT / "tools" / "burn.sh")
    trap = next(i for i, l in enumerate(code)
                if l.startswith("trap restore_daemon EXIT"))
    bootout = next(i for i, l in enumerate(code)
                   if 'launchctl bootout "$AGENT"' in l)
    wait = next(i for i, l in enumerate(code)
                if "blink_wait_agent_gone" in l and i > bootout)
    probe = next(i for i, l in enumerate(code) if l.startswith("efuse_probe"))
    assert trap < bootout < wait < probe
    # And the restore waits too, before its bootstrap.
    body = code[next(i for i, l in enumerate(code)
                     if l.startswith("restore_daemon()")):trap]
    w = next(i for i, l in enumerate(body) if "blink_wait_agent_gone" in l)
    b = next(i for i, l in enumerate(body) if "launchctl bootstrap" in l)
    assert w < b


needs_sh = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None, reason="needs sh")


def _run(tmp_path, polls, grace="180"):
    """blink_wait_agent_gone against a launchctl that answers `print` for
    `polls` calls, with a sleep that returns at once."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    count = tmp_path / "left"
    count.write_text(f"{polls}\n", encoding="utf-8")
    (bin_dir / "launchctl").write_text(
        "#!/bin/sh\n"
        f'n=$(cat "{count}")\n'
        '[ "$n" -gt 0 ] || exit 113\n'
        f'echo $((n - 1)) >"{count}"\nexit 0\n', encoding="utf-8")
    (bin_dir / "sleep").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "BLINK_STOP_GRACE_S": grace}
    return subprocess.run(
        ["sh", "-c", f'. "{LIB}"; blink_wait_agent_gone gui/1/x'],
        env=env, capture_output=True, text=True, timeout=120)


@needs_sh
def test_an_ordinary_stop_is_quiet(tmp_path):
    r = _run(tmp_path, polls=3)
    assert r.returncode == 0
    assert r.stderr == ""


@needs_sh
def test_a_slow_stop_says_what_it_waits_for_once(tmp_path):
    # 4 s of polls, then a minute more: still inside the grace.
    r = _run(tmp_path, polls=4 * 64)
    assert r.returncode == 0
    assert r.stderr.count("finish a firmware write or update") == 1
    assert "up to 3 minutes" in r.stderr


@needs_sh
def test_a_job_that_outlives_the_grace_is_reported(tmp_path):
    # The grace and its margin are (1 + 10) s here: 44 polls, then give up.
    r = _run(tmp_path, polls=10 ** 6, grace="1")
    assert r.returncode == 1
    left = int((tmp_path / "left").read_text())
    assert 10 ** 6 - left == 45
