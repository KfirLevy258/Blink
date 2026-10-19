"""tools/blink-opencode.js, run under node, and read back by claude_state.

Two things only a real run can show, so both are skipped where there is no
node rather than faked:

  - tests/ci/check_opencode_plugin.sh passes. It is the plugin's own
    behaviour -- every event, every hostile id, the silence -- and running it
    from here means it runs wherever the suite does, not only in the CI job
    that calls it by name.

  - The Python reader agrees with the JavaScript writer. The plugin exists to
    write files claude_state already understands; a slot it reads as unknown
    is a session missing from the panel, and nothing on either side would say
    so. tests/ci/opencode_plugin_driver.mjs `fixture` builds one desk with a
    session in every state and HOLDS ITS PROCESS OPEN, because the pid in
    every slot is that process's: read while it lives, the pid check has to
    keep every session; read after it exits, the plugin's exit handler has to
    have ended them.
"""
import os
import shutil
import subprocess
import time
import sys

import pytest

from pc.providers import base
from pc.providers import claude_state

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DRIVER = os.path.join(ROOT, "tests", "ci", "opencode_plugin_driver.mjs")
CHECK = os.path.join(ROOT, "tests", "ci", "check_opencode_plugin.sh")

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(autouse=True)
def _pid_trust_is_per_test():
    # Trust is process-wide (see claude_state.settle_pid_trust); a test that
    # suspends it would otherwise disarm the checks in every later test.
    claude_state.reset_pid_liveness()
    yield
    claude_state.reset_pid_liveness()


@needs_node
@pytest.mark.skipif(shutil.which("sh") is None, reason="no POSIX sh")
def test_check_opencode_plugin_sh_passes():
    r = subprocess.run(["sh", CHECK], capture_output=True, text=True,
                       timeout=120)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "all checks passed" in r.stdout


@needs_node
def test_driver_check_mode_passes_silently(tmp_path):
    # The same scenarios without sh, so Windows runs them too.
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    r = subprocess.run([NODE, DRIVER, "check"], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""


@needs_node
def test_python_reader_agrees_with_the_plugin(tmp_path):
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    state_dir = os.path.join(str(tmp_path), ".blink", "state-opencode")
    proc = subprocess.Popen([NODE, DRIVER, "fixture"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, env=env)
    try:
        line = proc.stdout.readline()
        assert line == "ready\n", line + proc.stderr.read()

        provider = claude_state.ClaudeStateProvider(path=state_dir,
                                                    sweep=False)
        now = time.time()
        states, agents = provider.session_states(now)
        got = {sid: st for sid, (st, _name) in states.items()}
        assert got == {
            "ses_run": base.STATE_RUNNING,
            "ses_wait": base.STATE_WAITING,
            "ses_idle": base.STATE_IDLE,
            # The error is followed by two idles, as captured from OpenCode,
            # and still reads as failed.
            "ses_fail": base.STATE_FAILED,
            "ses_abort": base.STATE_IDLE,
            "ses_long": base.STATE_FAILED,
            "ses_par": base.STATE_RUNNING,
        }
        # One subagent still running under ses_par; the other went idle.
        assert agents == 1
        assert all(name == "MyProj" for _st, name in states.values())
        assert provider.ended_sessions(now) == {"ses_gone"}
        # No subagent was counted as a session of its own.
        assert not any(s.startswith("ses_kid") for s in states)
        if os.name == "posix":
            # Every pid named a live process, so liveness stayed armed.
            assert claude_state.pid_liveness_trusted()

        frames = provider.poll(now)
        assert len(frames) == 1
        f = frames[0]
        assert (f.n_run, f.n_wait, f.n_idle, f.n_stuck, f.n_agents) == \
            (2, 1, 2, 2, 1)
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)
    assert proc.returncode == 0, proc.stderr.read()

    # The process exiting ended every session it held: the panel loses them
    # through the tombstone, not by waiting out the pid check or the hour.
    states, agents = provider.session_states(time.time())
    assert states == {} and agents == 0
    assert provider.ended_sessions(time.time()) == {
        "ses_run", "ses_wait", "ses_idle", "ses_fail", "ses_abort",
        "ses_long", "ses_par", "ses_gone"}
    leftovers = sorted(n for n in os.listdir(state_dir)
                       if not n.endswith(".ended"))
    assert leftovers == []
