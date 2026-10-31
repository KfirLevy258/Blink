"""How the daemon ends on SIGTERM, measured on a real child process.

launchd and systemd stop the service with SIGTERM, and so do dev.sh down,
pkill and the fleet agent's terminate(). Python's default is to die without
running atexit, which left a builder preview on the board for its own 15
minutes; the daemon now turns SIGTERM into an ordinary exit. That exit must
never land inside an OTA flash: SystemExit raised inside subprocess.run
kills esptool mid write_flash, and slot 0 has no revert, so the board does
not boot until somebody reflashes it (review, 2026-10-27).

Each test runs a small script in a child Python with HOME in tmp_path. No
serial port, no esptool, no browser.
"""
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

pytestmark = pytest.mark.skipif(sys.platform == "win32",
                                reason="SIGTERM is POSIX; Windows stops the"
                                       " daemon another way")


# Put first in every child, so wait_for can ask a stuck one where it is
# stuck. Without it a child that hung printed nothing at all: the review of
# fp/ci-fixes made the resolver take 30 s and got "(still running);
# stderr:" with an empty stderr after it. SIGUSR1 rather than
# PYTHONFAULTHANDLER and SIGABRT, which also works but aborts the child and
# leaves a macOS crash report behind. Child line numbers shift by two.
_DUMP_ON_USR1 = ("import faulthandler, signal\n"
                 "faulthandler.register(signal.SIGUSR1)\n")


def spawn(tmp_path, body):
    script = tmp_path / "child.py"
    script.write_text(_DUMP_ON_USR1 + textwrap.dedent(body), encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=ROOT,
               PYTHONDONTWRITEBYTECODE="1", BLINK_SKIP_SERVICE="1")
    return subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                            env=env, cwd=str(tmp_path),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for(path, proc, timeout=10):
    """True once the child has made `path`. Otherwise a failure that says
    what the child did: on macos-latest this once came back as a bare
    `assert False` twice running, with nothing to tell a child that died
    from one that was stuck. A child that has exited is reported at once
    rather than waited out, with its stderr; a stuck one is first asked
    for every thread's stack (_DUMP_ON_USR1), so the failure shows the
    frame it was stuck in."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        if proc.poll() is not None:
            break
        time.sleep(0.02)
    # Looked at once more: a child may make the file and exit between the
    # check above and its poll(). None of today's children exit after
    # making theirs, but a future one would fail here for nothing.
    if os.path.exists(path):
        return True
    state = ("still running" if proc.poll() is None
             else f"exited {proc.returncode}")
    if proc.poll() is None:
        proc.send_signal(signal.SIGUSR1)
        time.sleep(0.5)     # faulthandler writes from its own handler
        proc.kill()
    _, err = proc.communicate(timeout=10)
    pytest.fail(f"child made no {os.path.basename(path)!r} ({state});"
                f" stderr:\n{err.decode(errors='replace')}")


def test_sigterm_mid_flash_lets_the_flash_finish_then_exits(tmp_path):
    proc = spawn(tmp_path, """
        import os, sys, time
        import claude_usage_bridge as d
        out = sys.argv[1]
        d.exit_on_sigterm()

        def flasher():
            open(os.path.join(out, "started"), "w").close()
            # esptool's write_flash, in miniature: a child process this
            # one waits on.
            import subprocess
            subprocess.run([sys.executable, "-c", "import time; time.sleep(1.5)"])
            open(os.path.join(out, "flashed"), "w").close()
            return True, ""

        with d.no_exit_while("flashing"):
            ok, _ = flasher()
        open(os.path.join(out, "after"), "w").close()
        time.sleep(30)
    """)
    try:
        assert wait_for(tmp_path / "started", proc)
        proc.send_signal(signal.SIGTERM)
        rc = proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
    err = proc.stderr.read().decode()
    assert (tmp_path / "flashed").exists(), err
    # It exits as soon as the flash returns, not after whatever comes next.
    assert not (tmp_path / "after").exists(), err
    assert rc == 0, err


def test_sigterm_outside_a_flash_exits_at_once(tmp_path):
    proc = spawn(tmp_path, """
        import os, sys, time
        import claude_usage_bridge as d
        d.exit_on_sigterm()
        open(os.path.join(sys.argv[1], "ready"), "w").close()
        time.sleep(30)
    """)
    try:
        assert wait_for(tmp_path / "ready", proc)
        t0 = time.monotonic()
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
        assert time.monotonic() - t0 < 5
    finally:
        if proc.poll() is None:
            proc.kill()


def test_sigterm_runs_atexit_and_the_builder_cancels(tmp_path):
    proc = spawn(tmp_path, """
        import atexit, os, sys, time
        import claude_usage_bridge as d
        from pc import theme_server
        home = os.path.join(sys.argv[1], ".blink")
        lau = theme_server.Launcher(home, opener=lambda url: True)
        assert lau.launch()
        d.exit_on_sigterm()
        atexit.register(lau.close)
        open(os.path.join(sys.argv[1], "ready"), "w").close()
        time.sleep(30)
    """)
    try:
        assert wait_for(tmp_path / "ready", proc)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    preview = tmp_path / ".blink" / "theme_preview.json"
    assert preview.exists(), proc.stderr.read().decode()
    assert '"cancel"' in preview.read_text(encoding="utf-8")


def test_the_flash_is_wrapped_in_the_guard():
    # The guard is only as good as its one call site.
    src = open(os.path.join(ROOT, "claude_usage_bridge.py"),
               encoding="utf-8").read()
    i = src.index("ota_mod.flash(port, blob)")
    assert "no_exit_while(" in src[i - 300:i]


def test_the_flash_result_is_logged_inside_the_guard():
    """no_exit_while's finally calls sys.exit when a stop arrived during the
    block, so anything after the `with` never runs on that path. The result
    line ("ota: flashed X" or "FAILED: ...") sat after it, and a stop that
    waited for the firmware write left bridge.log ending at "stopping once
    the firmware write has finished" with no outcome (found in review,
    2026-10-31). Read as text and syntax only: the daemon module is never
    imported to prove this.
    """
    import ast
    src = open(os.path.join(ROOT, "claude_usage_bridge.py"),
               encoding="utf-8").read()
    guards = [
        w for w in ast.walk(ast.parse(src)) if isinstance(w, ast.With)
        and any(isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "id", "") == "no_exit_while"
                and i.context_expr.args
                and getattr(i.context_expr.args[0], "value", "")
                == "firmware write"
                for i in w.items)]
    assert len(guards) == 1
    printed = [ast.get_source_segment(src, n) for n in ast.walk(guards[0])
               if isinstance(n, ast.Call)
               and getattr(n.func, "id", "") == "print"]
    assert any("ota:" in p and "FAILED" in p for p in printed), printed
