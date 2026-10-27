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
and the system directories: pkill, pgrep, lsof, ps, launchctl, west, sleep,
esptool, and the python3 that `up` launches, which only records that it was
launched. The real launchctl lives in /bin, on that PATH, so its stand-in is
installed for every test, not only the ones about the installed daemon. The
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


# The machine's process table, port and launchd, as files under tmp_path the
# stand-ins read: `holder` is the pid on the port (absent: the port is free),
# `cmd.<pid>` is what ps prints for it, and `agent` means com.blink.bridge is
# loaded, holding the pid launchd reports (empty: loaded, not running).
#
# bootout unloads the agent and frees the port if the agent held it (unless
# `sticky`); `linger` keeps launchd answering print for that many more calls
# after a bootout, as the real one does while it tears the job down, and a
# bootstrap in that window fails and is noted in `too-early`.
# `bootstrap-fails` makes that many bootstraps fail. bootstrap loads the job
# idle; kickstart runs it. With KICK_MARKER set, kickstart marks that it has
# started, waits, and notes `kick-done` only if it was not killed meanwhile.
_LAUNCHCTL = r'''
case "$1" in
print)
  if [ -f "$SYS/leaving" ]; then
    n=$(cat "$SYS/leaving")
    if [ "$n" -gt 0 ]; then echo $((n - 1)) >"$SYS/leaving"; echo "	state = not running"; exit 0; fi
  fi
  [ -f "$SYS/agent" ] || exit 113
  p=$(cat "$SYS/agent")
  if [ -n "$p" ]; then echo "	state = running"; echo "	pid = $p"
  else echo "	state = not running"; fi
  exit 0 ;;
bootout)
  [ -f "$SYS/agent" ] || exit 3
  if [ ! -f "$SYS/sticky" ] && [ "$(cat "$SYS/holder" 2>/dev/null)" = "$(cat "$SYS/agent")" ]; then
    rm -f "$SYS/holder"
  fi
  rm -f "$SYS/agent"
  [ -f "$SYS/linger" ] && cp "$SYS/linger" "$SYS/leaving"
  exit 0 ;;
bootstrap)
  if [ -f "$SYS/leaving" ] && [ "$(cat "$SYS/leaving")" -gt 0 ]; then
    : >"$SYS/too-early"; exit 5
  fi
  if [ -f "$SYS/bootstrap-fails" ]; then
    n=$(cat "$SYS/bootstrap-fails")
    if [ "$n" -gt 0 ]; then echo $((n - 1)) >"$SYS/bootstrap-fails"; exit 5; fi
  fi
  : >"$SYS/agent"; exit 0 ;;
kickstart)
  if [ -n "${KICK_MARKER:-}" ]; then
    : >"$KICK_MARKER"; /bin/sleep 3; : >"$SYS/kick-done"
  fi
  [ -f "$SYS/agent" ] || exit 113
  echo 9999 >"$SYS/agent"; exit 0 ;;
esac
exit 0'''

_LSOF = r'''
[ -f "$SYS/holder" ] || exit 1
if [ "$1" = -t ]; then cat "$SYS/holder"; else
  echo "COMMAND PID"; echo "x $(cat "$SYS/holder")"; fi
exit 0'''

# ps -o command= -p PID
_PS = r'''
cat "$SYS/cmd.$4" 2>/dev/null || exit 1'''


def _rig(tmp_path, west_build_rc=0, esptool=True, efuse_bits="0000000",
         west_flash_rc=0):
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
                etools / "espefuse.real")
    (etools / "espefuse.real").chmod(0o755)
    calls = tmp_path / "calls.log"
    calls.write_text("", encoding="utf-8")
    # The committed stand-in, behind one that also notes the probe in
    # calls.log, so a test can see what ran before it.
    _stub(etools, "espefuse.py",
          f'echo "espefuse $*" >>"{calls}"\nexec "{etools}/espefuse.real" "$@"')

    sys_dir = tmp_path / "sys"
    sys_dir.mkdir()
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = f'echo "$(basename "$0") $*" >>"{calls}"'
    _stub(bin_dir, "pkill", log + "\nexit 1")
    # The system's real launchctl is in /bin, on this PATH: the stand-in must
    # always be first, even in the tests where nothing holds the port.
    _stub(bin_dir, "launchctl", log + _LAUNCHCTL)
    _stub(bin_dir, "lsof", _LSOF)
    _stub(bin_dir, "ps", _PS)
    # down waits on pgrep for its daemon to exit; the real one would look at
    # this machine's processes.
    _stub(bin_dir, "pgrep", "exit 1")
    # Instant, except for `up`'s own `sleep 2` when a test asks for a window
    # to interrupt: it then marks that it has started and really waits.
    _stub(bin_dir, "sleep",
          'if [ "$1" = 2 ] && [ -n "${UP_SLEEP_MARKER:-}" ]; then\n'
          '  : >"$UP_SLEEP_MARKER"; /bin/sleep 3\nfi\nexit 0')
    _stub(bin_dir, "python3", log + "\nexit 0")
    _stub(bin_dir, "west", log + f'\n[ "$1" = build ] && exit {west_build_rc}'
                                 f'\n[ "$1" = flash ] && exit {west_flash_rc}'
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
        "SYS": str(sys_dir),
    })
    return repo / "tools" / "dev.sh", env, calls


def _hold_port(tmp_path, pid, cmd, agent=False, sticky=False):
    """Put pid `pid`, running `cmd`, on the port; `agent` makes it the pid
    launchd reports for com.blink.bridge; `sticky` keeps the port held even
    after a bootout."""
    sys_dir = tmp_path / "sys"
    (sys_dir / "holder").write_text(f"{pid}\n", encoding="utf-8")
    (sys_dir / f"cmd.{pid}").write_text(cmd + "\n", encoding="utf-8")
    if agent:
        (sys_dir / "agent").write_text(f"{pid}\n", encoding="utf-8")
    if sticky:
        (sys_dir / "sticky").write_text("", encoding="utf-8")


def _hold_with_installed(tmp_path, **kw):
    _hold_port(tmp_path, 4242, f"{tmp_path}/.blink/bin/blink run",
               agent=True, **kw)


def _flash(tmp_path, hold=None, expect_up=True, **kw):
    script, env, calls = _rig(tmp_path, **kw)
    if hold:
        hold(tmp_path)
    r = subprocess.run(["bash", str(script), "flash"], env=env,
                       capture_output=True, text=True, timeout=60)
    if not expect_up:
        # The installed daemon's restore is synchronous; nothing is launched
        # in the background. Still give a stray launch time to show up.
        time.sleep(0.5)
        lines = calls.read_text(encoding="utf-8").splitlines()
        ups = [l for l in lines
               if l.startswith("python3 ") and "claude_usage_bridge.py" in l]
        return r, ups, [l for l in lines if l.startswith("west build")]

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


# ---------------------------------------------------------------------------
# The installed daemon. `blink install` registers a launchd agent that holds
# the port, and `down` only knows this script's own daemon -- so the eFuse
# probe found the port busy and refused a board that was fine (found by
# destaben in their fork). flash now steps the agent aside and puts it back,
# once, however the run ends; anything else on the port is refused.

def _launchctl(calls):
    return [l for l in calls.read_text(encoding="utf-8").splitlines()
            if l.startswith("launchctl ")]


def _verbs(calls, verb):
    return [l for l in _launchctl(calls) if l.split()[1] == verb]


def _assert_put_back_once(tmp_path, out):
    calls = tmp_path / "calls.log"
    assert len(_verbs(calls, "bootout")) == 1
    (boot,) = _verbs(calls, "bootstrap")
    assert boot.endswith(
        f"{tmp_path}/Library/LaunchAgents/com.blink.bridge.plist")
    (kick,) = _verbs(calls, "kickstart")
    assert kick.split()[2] == "-k"
    # bootstrap registers, kickstart starts: in that order.
    lines = _launchctl(calls)
    assert lines.index(boot) < lines.index(kick)
    assert out.count("installed daemon put back") == 1


def test_the_installed_daemon_is_stepped_aside_and_put_back(tmp_path):
    r, ups, builds = _flash(tmp_path, hold=_hold_with_installed,
                            expect_up=False)
    assert r.returncode == 0, r.stderr
    assert len(builds) == 1
    assert "stopping the installed daemon" in r.stdout
    _assert_put_back_once(tmp_path, r.stdout)
    # It was stopped before the eFuse probe needed the port.
    lines = (tmp_path / "calls.log").read_text(encoding="utf-8").splitlines()
    first_probe = next(i for i, l in enumerate(lines)
                       if l.startswith("espefuse "))
    bootout = next(i for i, l in enumerate(lines)
                   if l.startswith("launchctl bootout"))
    assert bootout < first_probe
    # The port had one daemon on it, and gets one back: not the dev daemon
    # as well, which would open the same port beside it.
    assert ups == []


def test_a_failed_build_puts_the_installed_daemon_back(tmp_path):
    r, ups, builds = _flash(tmp_path, hold=_hold_with_installed,
                            expect_up=False, west_build_rc=1)
    assert r.returncode != 0
    assert len(builds) == 1
    _assert_put_back_once(tmp_path, r.stdout)
    assert ups == []


def test_a_failed_flash_puts_the_installed_daemon_back(tmp_path):
    r, ups, builds = _flash(tmp_path, hold=_hold_with_installed,
                            expect_up=False, west_flash_rc=1)
    assert r.returncode != 0
    _assert_put_back_once(tmp_path, r.stdout)
    assert ups == []


def test_a_fused_refusal_puts_the_installed_daemon_back(tmp_path):
    r, ups, builds = _flash(tmp_path, hold=_hold_with_installed,
                            expect_up=False, efuse_bits="0000001")
    assert r.returncode == 1
    assert "FUSED" in r.stderr
    assert builds == []
    _assert_put_back_once(tmp_path, r.stdout)
    assert ups == []


def test_a_port_that_never_clears_is_refused_and_put_back(tmp_path):
    r, ups, builds = _flash(
        tmp_path, hold=lambda t: _hold_with_installed(t, sticky=True),
        expect_up=False)
    assert r.returncode == 1
    assert "still held 10 s after stopping the installed daemon" in r.stderr
    assert builds == []
    assert not any(l.startswith("espefuse ") for l in
                   (tmp_path / "calls.log").read_text(
                       encoding="utf-8").splitlines())
    _assert_put_back_once(tmp_path, r.stdout)


def test_a_blink_outside_launchd_is_refused_and_not_bootstrapped(tmp_path):
    """~/.blink/bin/blink run on the port, with no agent loaded: started by
    hand, or left over. Not stopped, and -- since launchd never had it --
    no bootstrap afterwards either, which would hand this machine a login
    service it was not running."""
    r, ups, builds = _flash(
        tmp_path,
        hold=lambda t: _hold_port(t, 4242, f"{t}/.blink/bin/blink run"))
    assert r.returncode == 1
    port = tmp_path / "no-such-port"
    assert (f"FATAL: a blink outside launchd holds {port} (pid 4242); "
            "stop it first.") in r.stderr
    assert builds == []
    calls = tmp_path / "calls.log"
    for verb in ("bootout", "bootstrap", "kickstart"):
        assert _verbs(calls, verb) == []
    assert "installed daemon put back" not in r.stdout


def test_a_loaded_agent_with_the_port_closed_is_still_stepped_aside(tmp_path):
    """The daemon closes the port while the board is unplugged or silent and
    reopens it on its next cycle. Loaded is what counts, not holding."""
    def hold(t):
        (t / "sys" / "agent").write_text("4242\n", encoding="utf-8")
    r, ups, builds = _flash(tmp_path, hold=hold, expect_up=False)
    assert r.returncode == 0, r.stderr
    assert len(builds) == 1
    _assert_put_back_once(tmp_path, r.stdout)
    assert ups == []


def test_a_loaded_agent_is_stepped_aside_before_a_stranger_is_refused(
        tmp_path):
    """Agent loaded but idle, a stranger on the port: refused, and the agent
    this run booted out still goes back."""
    def hold(t):
        _hold_port(t, 777, "screen /dev/cu.usbserial-1 115200")
        (t / "sys" / "agent").write_text("4242\n", encoding="utf-8")
    r, ups, builds = _flash(tmp_path, hold=hold, expect_up=False)
    assert r.returncode == 1
    assert "held by pid 777" in r.stderr
    assert builds == []
    _assert_put_back_once(tmp_path, r.stdout)
    assert ups == []


def test_a_dev_daemon_still_exiting_is_named_as_such(tmp_path):
    r, ups, builds = _flash(tmp_path, hold=lambda t: _hold_port(
        t, 888, f"python3 -u claude_usage_bridge.py --port {t}/no-such-port"))
    assert r.returncode == 1
    assert "the dev daemon (pid 888) is still exiting" in r.stderr
    assert "not the Blink daemon" not in r.stderr
    assert builds == []


def test_the_restore_waits_for_launchd_to_forget_the_job(tmp_path):
    """bootout is asynchronous: launchd answers print for a moment after it,
    and a bootstrap in that window fails."""
    def hold(t):
        _hold_with_installed(t)
        (t / "sys" / "linger").write_text("3\n", encoding="utf-8")
    r, ups, builds = _flash(tmp_path, hold=hold, expect_up=False)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "sys" / "too-early").exists()
    _assert_put_back_once(tmp_path, r.stdout)


def test_a_failed_bootstrap_is_retried(tmp_path):
    def hold(t):
        _hold_with_installed(t)
        (t / "sys" / "bootstrap-fails").write_text("2\n", encoding="utf-8")
    r, ups, builds = _flash(tmp_path, hold=hold, expect_up=False)
    assert r.returncode == 0, r.stderr
    calls = tmp_path / "calls.log"
    assert len(_verbs(calls, "bootstrap")) == 3
    assert len(_verbs(calls, "kickstart")) == 1
    assert r.stdout.count("installed daemon put back") == 1


def test_a_restore_that_did_not_take_says_how_to_finish_it(tmp_path):
    """Three failed bootstraps leave nothing running, and "put back" would
    be a lie. The warning carries the exact commands to run by hand."""
    def hold(t):
        _hold_with_installed(t)
        (t / "sys" / "bootstrap-fails").write_text("99\n", encoding="utf-8")
    r, ups, builds = _flash(tmp_path, hold=hold, expect_up=False)
    calls = tmp_path / "calls.log"
    assert len(_verbs(calls, "bootstrap")) == 3
    assert "installed daemon put back" not in r.stdout
    uid = os.getuid()
    plist = f"{tmp_path}/Library/LaunchAgents/com.blink.bridge.plist"
    assert "the installed daemon did not come back" in r.stderr
    assert f"launchctl bootstrap gui/{uid} {plist}" in r.stderr
    assert f"launchctl kickstart -k gui/{uid}/com.blink.bridge" in r.stderr
    assert ups == []


def test_a_checkout_install_is_recognised_by_its_launchd_pid(tmp_path):
    """A checkout install runs python and blink_main.py, not ~/.blink/bin;
    launchd's pid for the agent is what identifies it."""
    def hold(t):
        _hold_port(t, 5150, "/usr/bin/python3 /src/Blink/blink_main.py run",
                   agent=True)
    r, ups, builds = _flash(tmp_path, hold=hold, expect_up=False)
    assert r.returncode == 0, r.stderr
    _assert_put_back_once(tmp_path, r.stdout)


@pytest.mark.parametrize("cmd", [
    "screen /dev/cu.usbserial-1 115200",
    # Ours in name only: a blink somewhere else is not the installed one.
    "/opt/blink/bin/blink run",
])
def test_a_foreign_holder_is_refused_and_nothing_is_flashed(tmp_path, cmd):
    r, ups, builds = _flash(tmp_path, hold=lambda t: _hold_port(t, 777, cmd))
    assert r.returncode == 1
    port = tmp_path / "no-such-port"
    assert (f"FATAL: {port} is held by pid 777 ({cmd}), "
            "which is not the Blink daemon.") in r.stderr
    assert builds == []
    calls = tmp_path / "calls.log"
    assert not [l for l in calls.read_text(encoding="utf-8").splitlines()
                if l.startswith(("west ", "espefuse "))]
    # Not ours, so not stopped -- and nothing to put back.
    assert _verbs(calls, "bootout") == []
    assert _verbs(calls, "bootstrap") == []
    assert _verbs(calls, "kickstart") == []
    assert "installed daemon put back" not in r.stdout
    # The dev daemon `down` stopped still comes back, as on every exit.
    assert len(ups) == 1


def test_no_agent_and_a_free_port_stops_nothing(tmp_path):
    r, ups, builds = _flash(tmp_path)
    assert r.returncode == 0, r.stderr
    # launchd is asked whether the agent is loaded, and that is all.
    calls = tmp_path / "calls.log"
    assert [l.split()[1] for l in _launchctl(calls)] == ["print"]
    assert "installed daemon put back" not in r.stdout
    assert len(ups) == 1


def _interrupt(tmp_path, env, script, marker, sig, group=False):
    """Signal the script once `marker` appears. group=True sends it to the
    whole process group, the way a terminal's Ctrl-C arrives, so the
    children it is waiting on get it too."""
    p = subprocess.Popen(["bash", str(script), "flash"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, start_new_session=group)
    try:
        deadline = time.monotonic() + 20
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert marker.exists(), f"{marker.name} never happened"
        if group:
            os.killpg(p.pid, sig)
        else:
            p.send_signal(sig)
        out, err = p.communicate(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
            p.communicate()
    return p.returncode, out, err


def test_an_interrupted_flash_puts_the_installed_daemon_back_once(tmp_path):
    script, env, calls = _rig(tmp_path)
    _hold_with_installed(tmp_path)
    marker = tmp_path / "in-west-flash"
    _stub(tmp_path / "bin", "west",
          f'echo "west $*" >>"{calls}"\n'
          f'if [ "$1" = flash ]; then : >"{marker}"; /bin/sleep 3; fi\n'
          "exit 0")
    rc, out, err = _interrupt(tmp_path, env, script, marker, signal.SIGINT)
    assert rc == 130
    assert "flash interrupted; re-run dev.sh flash" in err
    _assert_put_back_once(tmp_path, out)
    time.sleep(0.5)
    assert _launches(calls) == []


def test_a_second_ctrl_c_during_the_restore_does_not_restore_twice(tmp_path):
    """The flash is done and the EXIT trap is inside the kickstart when a
    terminal Ctrl-C reaches the whole group. It must neither run the restore
    a second time nor kill the launchctl doing this one: either leaves the
    desk wrong, and the second leaves it on HOST LOST."""
    script, env, calls = _rig(tmp_path)
    _hold_with_installed(tmp_path)
    marker = tmp_path / "in-kickstart"
    env["KICK_MARKER"] = str(marker)
    rc, out, err = _interrupt(tmp_path, env, script, marker, signal.SIGINT,
                              group=True)
    assert (tmp_path / "sys" / "kick-done").exists(), \
        "the Ctrl-C killed the kickstart"
    assert len(_verbs(calls, "bootstrap")) == 1
    assert len(_verbs(calls, "kickstart")) == 1
    assert out.count("installed daemon put back") == 1
    time.sleep(0.5)
    assert _launches(calls) == []
