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
from pc.providers import opencode

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
DRIVER = os.path.join(ROOT, "tests", "ci", "opencode_plugin_driver.mjs")
CHECK = os.path.join(ROOT, "tests", "ci", "check_opencode_plugin.sh")

NODE = shutil.which("node")
needs_node = pytest.mark.skipif(NODE is None, reason="node is not installed")


@pytest.fixture(autouse=True)
def _pid_trust_is_per_test():
    # Trust is process-wide (see claude_state.settle_pid_trust); a test that
    # suspends it would otherwise disarm the checks in every later test. And
    # OpenCode's own memory of pids seen alive is process-wide too.
    claude_state.reset_pid_liveness()
    opencode.reset_pid_memory()
    yield
    claude_state.reset_pid_liveness()
    opencode.reset_pid_memory()


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


def _reader_agrees(tmp_path, mode):
    """The fixture desk, read while the driver lives and after it exits.

    `fixture` builds it from OpenCode 1.x events and `fixture2` from 2.0's,
    with the same session ids in the same states, so one expectation holds
    both writers to what claude_state reads."""
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    state_dir = os.path.join(str(tmp_path), ".blink", "state-opencode")
    proc = subprocess.Popen([NODE, DRIVER, mode], stdin=subprocess.PIPE,
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
            # 1.x: the error is followed by two idles, as captured from
            # OpenCode, and still reads as failed. 2.0: execution.failed.
            "ses_fail": base.STATE_FAILED,
            # 1.x MessageAbortedError; 2.0 interrupted with reason "user".
            # Either way the turn is the person's again, not a failure.
            "ses_abort": base.STATE_IDLE,
            # A retry an hour away: 1.x status.next, 2.0 retry.scheduled's at.
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


@needs_node
def test_python_reader_agrees_with_the_plugin(tmp_path):
    _reader_agrees(tmp_path, "fixture")


@needs_node
def test_python_reader_agrees_with_the_plugin_under_opencode_2(tmp_path):
    """The same desk through the default export's setup, as 2.0 loads it.

    The 2.0 slots end differently -- there is no idle after a failure to
    latch against, an abort is an interrupt with a reason, and the exit
    that tombstones them comes from the one process-wide 2.0 ender rather
    than a per-instance one -- so agreeing on 1.x proves nothing here."""
    _reader_agrees(tmp_path, "fixture2")


def _run_instances(tmp_path, *extra):
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    r = subprocess.run([NODE, DRIVER, "instances", *extra],
                       capture_output=True, text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""
    return set(os.listdir(os.path.join(str(tmp_path), ".blink",
                                       "state-opencode")))


@needs_node
def test_a_newer_instance_for_a_directory_replaces_the_older_at_exit(
        tmp_path):
    """Found in review, 2026-10-19: every factory call kept its exit handler,
    and with it its sessions map, for the life of a `serve` -- and at exit
    the stale instances ended sessions a live one owned. One handler per
    directory now."""
    left = _run_instances(tmp_path)
    assert "ses_live.ended" in left and "ses_live.state" not in left
    assert "ses_other.ended" in left and "ses_other.state" not in left
    # The replaced instance ran nothing at exit.
    assert "ses_stale.state" in left and "ses_stale.ended" not in left


@needs_node
def test_the_exit_handlers_are_bounded(tmp_path):
    left = _run_instances(tmp_path, "many")
    assert "ses_d0.state" in left and "ses_d0.ended" not in left
    for i in range(1, 65):
        assert f"ses_d{i}.ended" in left, i
        assert f"ses_d{i}.state" not in left, i
    # Older than ses_d0, so evicted before it.
    assert "ses_live.state" in left and "ses_other.state" in left


@needs_node
def test_instances_with_no_directory_each_keep_their_exit_handler(tmp_path):
    """Third review, 2026-10-19: with no input.directory every instance was
    keyed "" and each evicted the one before, so at exit only the newest
    ended its sessions; the others held "your turn" until the pid check, or
    for the hour if no poll had seen the process alive."""
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path))
    r = subprocess.run([NODE, DRIVER, "nodir"], capture_output=True,
                       text=True, env=env, timeout=120)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""
    left = set(os.listdir(os.path.join(str(tmp_path), ".blink",
                                       "state-opencode")))
    for sid in ("ses_nodir1", "ses_nodir2"):
        assert f"{sid}.ended" in left, left
        assert f"{sid}.state" not in left, left


@needs_node
def test_a_finished_opencode_2_session_ends_after_idling(tmp_path):
    """The owner's desk, 2026-10-20: ten sessions on the panel while three
    were open. A finished 2.0 session now ends after IDLE_END_MS of nothing,
    shortened here to 300 ms; the driver's idle2 mode holds the cases."""
    home = tmp_path / "home"
    home.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               BLINK_OPENCODE_IDLE_END_MS="300")
    r = subprocess.run([NODE, DRIVER, "idle2"], capture_output=True,
                       text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout == "" and r.stderr == ""
