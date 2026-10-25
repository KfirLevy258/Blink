"""tools/dev.sh flash brings the daemon back however it ends, exactly once.

`flash` stops the daemon to free the port, and used to start it again only on
its last line, so an eFuse refusal, a failed build check, a failed west build
or a failed flash all left the desk on HOST LOST. It now sets an EXIT trap
right after `down`, which is also the only thing that starts the daemon in
that path -- so a good flash must start it once, not twice.

Hermetic by construction, because the real script kills processes by name and
by a pid file in /tmp. It is COPIED into tmp_path with its two libraries, the
pid file and log are rewritten to paths under tmp_path (and the copy is
checked to name nothing in /tmp), and every command that could touch a real
process, port or build is a stand-in on a PATH that holds only the stand-ins
and the system directories: pkill, lsof, west, sleep, esptool, and the
python3 that `up` launches, which only records that it was launched. The
eFuse probe reads the committed stand-in espefuse.py (tests/ci/fakes).
"""
import os
import shutil
import signal
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="needs bash, and #!/bin/sh stand-ins it can exec")

REAL_PIDFILE = 'PIDFILE="/tmp/claude-usage-bridge.pid"'
REAL_LOG = 'LOG="/tmp/claude-usage-bridge.log"'


def _stub(bin_dir, name, body):
    p = bin_dir / name
    p.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    p.chmod(0o755)


def _rig(tmp_path, west_build_rc=0, esptool=True, efuse_bits="0000000"):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "firmware").mkdir()
    for lib in ("lib_zephyr.sh", "lib_efuse.sh"):
        shutil.copy(ROOT / "tools" / lib, repo / "tools" / lib)
    text = (ROOT / "tools" / "dev.sh").read_text(encoding="utf-8")
    assert text.count(REAL_PIDFILE) == 1 and text.count(REAL_LOG) == 1
    text = text.replace(REAL_PIDFILE, f'PIDFILE="{tmp_path}/bridge.pid"')
    text = text.replace(REAL_LOG, f'LOG="{tmp_path}/bridge.log"')
    assert "/tmp/" not in text.replace(str(tmp_path), "")
    (repo / "tools" / "dev.sh").write_text(text, encoding="utf-8")

    ws = tmp_path / "ws"
    (ws / ".venv" / "bin").mkdir(parents=True)
    (ws / ".venv" / "bin" / "activate").write_text("", encoding="utf-8")
    (ws / "zephyr").mkdir()
    (ws / "zephyr" / "zephyr-env.sh").write_text("", encoding="utf-8")

    etools = tmp_path / "etools"
    etools.mkdir()
    shutil.copy(ROOT / "tests" / "ci" / "fakes" / "bin" / "espefuse.py",
                etools / "espefuse.py")
    (etools / "espefuse.py").chmod(0o755)

    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = f'echo "$(basename "$0") $*" >>"{calls}"'
    _stub(bin_dir, "pkill", log + "\nexit 1")
    _stub(bin_dir, "lsof", "exit 1")
    # Instant, except for `up`'s own `sleep 2` when a test asks for a window
    # to interrupt: it then marks that it has started and really waits.
    _stub(bin_dir, "sleep",
          'if [ "$1" = 2 ] && [ -n "${UP_SLEEP_MARKER:-}" ]; then\n'
          '  : >"$UP_SLEEP_MARKER"; /bin/sleep 3\nfi\nexit 0')
    _stub(bin_dir, "python3", log + "\nexit 0")
    _stub(bin_dir, "west", log + f'\n[ "$1" = build ] && exit {west_build_rc}'
                                 "\nexit 0")
    if esptool:
        _stub(bin_dir, "esptool", "exit 0")

    env = {k: v for k, v in os.environ.items()
           if k not in ("BASH_ENV", "ENV", "BLINK_SKIP_EFUSE_CHECK",
                        "ZEPHYR_BASE")}
    env.update({
        "HOME": str(tmp_path),
        "PATH": f"{bin_dir}:/usr/bin:/bin",
        "PORT": str(tmp_path / "no-such-port"),
        "BLINK_ZEPHYR": str(ws),
        "BLINK_ETOOLS": str(etools),
        "FAKE_TOOL_LOG": str(tmp_path / "tools.log"),
        "FAKE_EFUSE_BITS": efuse_bits,
    })
    return repo / "tools" / "dev.sh", env, calls


def _flash(tmp_path, **kw):
    script, env, calls = _rig(tmp_path, **kw)
    r = subprocess.run(["bash", str(script), "flash"], env=env,
                       capture_output=True, text=True, timeout=60)

    def ups_now():
        return [l for l in calls.read_text(encoding="utf-8").splitlines()
                if l.startswith("python3 ") and "claude_usage_bridge.py" in l]

    # `up` launches the daemon in the background with nohup, and sleep is a
    # stand-in, so the script can exit before the stand-in daemon has logged.
    # Wait for the first launch, then a little longer so a second one would
    # have had time to show up too.
    deadline = time.monotonic() + 10
    while not ups_now() and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    lines = calls.read_text(encoding="utf-8").splitlines()
    ups = ups_now()
    builds = [l for l in lines if l.startswith("west build")]
    return r, ups, builds


def test_a_good_flash_starts_the_daemon_once(tmp_path):
    r, ups, builds = _flash(tmp_path)
    assert r.returncode == 0, r.stderr
    assert len(builds) == 1
    assert len(ups) == 1


def test_a_failed_build_still_brings_the_daemon_back(tmp_path):
    r, ups, builds = _flash(tmp_path, west_build_rc=1)
    assert r.returncode != 0
    assert len(builds) == 1
    assert len(ups) == 1


def test_a_fused_chip_refusal_brings_the_daemon_back(tmp_path):
    r, ups, builds = _flash(tmp_path, efuse_bits="0000001")
    assert r.returncode == 1
    assert "FUSED" in r.stderr
    assert builds == []
    assert len(ups) == 1


def test_a_missing_esptool_brings_the_daemon_back(tmp_path):
    r, ups, builds = _flash(tmp_path, esptool=False)
    assert r.returncode == 1
    assert "esptool is not on PATH" in r.stderr
    assert builds == []
    assert len(ups) == 1


def _launches(calls):
    return [l for l in calls.read_text(encoding="utf-8").splitlines()
            if l.startswith("python3 ") and "claude_usage_bridge.py" in l]


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_a_signal_during_the_restore_does_not_start_a_second_daemon(
        tmp_path, sig):
    """The flash has finished and the EXIT trap is in `up`, waiting out its
    `sleep 2` -- where a second Ctrl-C used to run `up` again from the INT
    trap, leaving two daemons on one port. The restore must run once."""
    script, env, calls = _rig(tmp_path)
    marker = tmp_path / "in-up-sleep"
    env["UP_SLEEP_MARKER"] = str(marker)
    p = subprocess.Popen(["bash", str(script), "flash"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "up never reached its sleep"
        p.send_signal(sig)
        p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    time.sleep(0.5)   # time for a second background launch to log, if any
    assert len(_launches(calls)) == 1


@pytest.mark.parametrize("sig, status", [(signal.SIGINT, 130),
                                         (signal.SIGTERM, 143)])
def test_an_interrupted_flash_says_so_and_restores_once(tmp_path, sig,
                                                         status):
    """A signal during the flash itself: the handler says what happened,
    brings the daemon back once, and exits with the signal's status."""
    script, env, calls = _rig(tmp_path)
    marker = tmp_path / "in-west-flash"
    # west flash waits, so there is a flash to interrupt.
    _stub(tmp_path / "bin", "west",
          f'echo "west $*" >>"{calls}"\n'
          f'if [ "$1" = flash ]; then : >"{marker}"; /bin/sleep 3; fi\n'
          "exit 0")
    p = subprocess.Popen(["bash", str(script), "flash"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), "west flash never started"
        p.send_signal(sig)
        _, err = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    deadline = time.monotonic() + 10
    while not _launches(calls) and time.monotonic() < deadline:
        time.sleep(0.05)
    time.sleep(0.5)
    assert p.returncode == status
    assert "flash interrupted; re-run dev.sh flash" in err
    assert len(_launches(calls)) == 1
