"""BLINK's own `claude -p /usage` run must leave no trace through the shims.

The daemon passes --settings disableAllHooks and BLINK_REFRESH=1. The setting
is the first lock; this is the second, in case a future Claude Code ignores
it. A refresh run's payload would otherwise be saved as a real render, or
count as a session, and the panel would show the daemon's own question.
"""
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))
SH = shutil.which("sh")
pytestmark = pytest.mark.skipif(SH is None, reason="needs a POSIX sh")

PAYLOAD = (b'{"session_id":"abc123","cwd":"/x","rate_limits":{},'
           b'"padding":"' + b"x" * 300000 + b'"}')

SHIMS = [
    ("blink-statusline.sh", []),
    ("blink-hook.sh", ["PostToolUse"]),
    ("blink-hook.sh", ["PostToolUse", "codex"]),
]


def _run(home, name, args, refresh):
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
           "HOME": str(home)}
    if refresh:
        env["BLINK_REFRESH"] = "1"
    return subprocess.run(
        [SH, os.path.join(ROOT, "tools", name)] + args,
        input=PAYLOAD, env=env, capture_output=True, timeout=30)


def _files(home):
    return sorted(os.path.join(d, f) for d, _, fs in os.walk(home)
                  for f in fs)


@pytest.mark.parametrize("name,args", SHIMS)
def test_a_refresh_run_writes_nothing_and_prints_nothing(tmp_path, name, args):
    r = _run(tmp_path, name, args, True)
    assert r.returncode == 0
    assert r.stdout == b"" and r.stderr == b""
    assert _files(tmp_path) == []
    assert not (tmp_path / ".blink").exists()


@pytest.mark.parametrize("name,args", SHIMS)
def test_an_ordinary_run_still_writes(tmp_path, name, args):
    r = _run(tmp_path, name, args, False)
    assert r.returncode == 0
    assert _files(tmp_path) != []
