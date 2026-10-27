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


def spawn(tmp_path, body):
    script = tmp_path / "child.py"
    script.write_text(textwrap.dedent(body), encoding="utf-8")
    env = dict(os.environ, HOME=str(tmp_path), PYTHONPATH=ROOT,
               PYTHONDONTWRITEBYTECODE="1", BLINK_SKIP_SERVICE="1")
    return subprocess.Popen([sys.executable, str(script), str(tmp_path)],
                            env=env, cwd=str(tmp_path),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def wait_for(path, timeout=10):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if os.path.exists(path):
            return True
        time.sleep(0.02)
    return False


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
        assert wait_for(tmp_path / "started")
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
        assert wait_for(tmp_path / "ready")
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
        assert wait_for(tmp_path / "ready")
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
