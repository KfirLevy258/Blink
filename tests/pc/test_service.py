"""The login-service backends: what each platform would actually run.

None of this had a test before. The five sys.platform ladders these replaced
were reachable only by installing for real on the platform in question, so
every defect in them -- and there were three on Windows alone -- was found by
a CI runner rather than here. A backend object can be built on any machine and
asked what argv it would hand to launchctl, schtasks or systemctl, which is
most of what there is to get wrong.

This does not replace tests/ci/check_install.sh. That one proves the commands
work; this one proves they are the commands we meant.
"""
import os
import subprocess

import pytest

from pc import cli


class _Runs:
    """Stands in for subprocess.run, recording argv and replaying exit codes."""

    def __init__(self, codes=None, stderr="", running=True):
        self.calls = []
        self._codes = list(codes or [])
        self._stderr = stderr
        # What `launchctl print` reports. install() now believes launchd
        # rather than its own bootstrap exit code, so this is the switch
        # between a service that started and one that only registered.
        self._running = running

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code = self._codes.pop(0) if self._codes else 0
        out = ""
        if len(argv) > 1 and argv[1] == "print":
            out = ("state = running\n" if self._running
                   else "active count = 0\n\tstate = not running\n")
        return subprocess.CompletedProcess(argv, code, stdout=out,
                                           stderr=self._stderr)

    def ran(self, *words):
        """True if some call contains all of `words` in order."""
        for call in self.calls:
            joined = " ".join(call)
            at = -1
            for w in words:
                at = joined.find(w, at + 1)
                if at < 0:
                    break
            else:
                return True
        return False


@pytest.fixture
def home(tmp_path, monkeypatch):
    """HOME is redirected by tests/conftest.py; this adds the stubs."""
    # Never let a stray real call out of a unit test.
    monkeypatch.setattr(cli.subprocess, "run", _Runs())
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    # Deterministic, and present on every platform this suite runs on.
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    return tmp_path


def _runs(monkeypatch, codes=None, stderr="", running=True):
    r = _Runs(codes, stderr, running)
    monkeypatch.setattr(cli.subprocess, "run", r)
    return r


def _platform(monkeypatch, name):
    monkeypatch.setattr(cli.sys, "platform", name)


# ------------------------------------------------------------------ choice --

@pytest.mark.parametrize("platform,cls", [
    ("darwin", cli._LaunchdBackend),
    ("win32", cli._SchtasksBackend),
    ("linux", cli._SystemdBackend),
    ("linux2", cli._SystemdBackend),
])
def test_backend_per_platform(monkeypatch, platform, cls):
    _platform(monkeypatch, platform)
    assert isinstance(cli.backend(), cls)


def test_unknown_platform_gets_the_base_backend(monkeypatch):
    """Not an error. It is a machine we cannot supervise, and it is told so."""
    _platform(monkeypatch, "freebsd14")
    b = cli.backend()
    assert type(b) is cli._Backend
    assert b.creates() is None
    assert "freebsd14" in b.install()
    assert "run yourself" in b.install() or "run it yourself" in b.install()
    assert b.remove() == "nothing to remove"
    assert "freebsd14" in b.status()


# ----------------------------------------------------------------- launchd --

def test_launchd_install_writes_the_plist_and_bootstraps(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch)

    assert cli.backend().install() == "running (launchd)"

    plist = open(cli.plist_path()).read()
    assert "<key>Label</key><string>com.blink.bridge</string>" in plist
    assert "<key>KeepAlive</key><true/>" in plist
    assert cli.log_path() in plist
    # bootout must come first, or bootstrap fails on "already loaded".
    assert r.calls[0][:2] == ["launchctl", "bootout"]
    assert r.calls[1][:2] == ["launchctl", "bootstrap"]
    assert r.calls[1][2] == "gui/501"
    # And STARTED, not merely registered. bootstrap accepts the job; it does
    # not run it, and on a bootout/bootstrap cycle it routinely does not.
    assert r.ran("launchctl", "kickstart", "gui/501/com.blink.bridge")


def test_launchd_install_retries_bootstrap(home, monkeypatch):
    """bootout is asynchronous, so the first bootstrap after it can lose.

    Without the retry a reinstall left no service running at all, silently.
    """
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch, codes=[0, 1, 1, 0])

    assert cli.backend().install() == "running (launchd)"
    bootstraps = [c for c in r.calls if c[1] == "bootstrap"]
    assert len(bootstraps) == 3


def test_launchd_install_gives_up_with_the_command_to_run(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, codes=[0, 1, 1, 1, 1])

    msg = cli.backend().install()
    assert msg.startswith("installed, but could not be started:")
    # The message has to be a command someone can paste.
    assert "launchctl bootstrap gui/501" in msg
    assert cli.plist_path() in msg


def test_launchd_install_will_not_claim_running_when_it_is_not(home, monkeypatch):
    """The defect this exists for, measured on the owner's machine 2026-09-04.

    Every launchctl command returned zero, the installer printed
    "running (launchd)" as its last word, and the service was not running:

        active count = 0
        state = not running
        last exit code = (never exited)

    The board went quiet and dozed while the install said it was fine. So the
    claim must come from launchd's own answer, not from a bootstrap that
    merely succeeded -- registration is not health, which the comment above
    _service_command() already had to learn once.
    """
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, running=False)

    msg = cli.backend().install()
    assert "running (launchd)" != msg
    assert "not running" in msg
    # And it has to be a command someone can paste, like its sibling above.
    assert "launchctl kickstart -k gui/501/com.blink.bridge" in msg


def test_launchd_remove_boots_out_and_deletes_the_plist(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    os.makedirs(os.path.dirname(cli.plist_path()), exist_ok=True)
    open(cli.plist_path(), "w").write("x")
    r = _runs(monkeypatch)

    assert cli.backend().remove() == "removed"
    assert r.ran("launchctl", "bootout", "gui/501/com.blink.bridge")
    assert not os.path.exists(cli.plist_path())


def test_launchd_status(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, codes=[0])
    assert cli.backend().status() == "registered with launchd"
    _runs(monkeypatch, codes=[1])
    assert cli.backend().status() == "not installed"


def test_launchd_restart_asks_launchd_rather_than_trusting_kickstart(home, monkeypatch):
    """A kickstart that exits zero is a request accepted, not a live process.

    The same defect install() was measured making on 2026-09-04, in the same
    file: every launchctl command returned zero and nothing was running. So
    "restarted" has to come from launchd's own answer.
    """
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch)

    assert cli.backend().restart() == "restarted"
    assert r.calls[0][:2] == ["launchctl", "kickstart"]
    assert r.ran("launchctl", "print", "gui/501/com.blink.bridge")


def test_launchd_restart_will_not_claim_restarted_when_it_is_not(home, monkeypatch):
    """kickstart exits 0, launchd says not running: say so, and where to look.

    This is the `blink update` path -- the binary was just replaced. If the
    new one cannot start, launchd respawns it and it dies again, and the log
    is the only thing that explains why.
    """
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, running=False)

    msg = cli.backend().restart()
    assert msg != "restarted"
    assert "not running" in msg
    assert cli.log_path() in msg


def test_launchd_restart_reports_a_kickstart_that_failed(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    r = _runs(monkeypatch, codes=[1])

    assert cli.backend().restart() == "could not restart it"
    # And it did not go on to ask launchd: there is nothing to confirm when
    # the request itself was refused.
    assert not r.ran("launchctl", "print")


# ---------------------------------------------------------------- schtasks --

def test_schtasks_install_registers_at_logon_then_starts_it(home, monkeypatch):
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)

    assert cli.backend().install() == "running (Scheduled Task)"
    create = r.calls[0]
    assert create[:3] == ["schtasks", "/create", "/f"]
    # /sc onlogon needs no admin rights; a Windows service would.
    assert "onlogon" in create
    assert cli.TASK_NAME in create
    # The task runs a hidden-window launcher, not the program: a console
    # program started by a task gets a console window on the desktop.
    # The path is quoted inside /tr: Windows splits an unquoted one on the
    # space in "Program Files" and on any space in the user's name.
    assert f'wscript.exe //B //Nologo "{cli.launcher_path()}"' in create
    vbs = open(cli.launcher_path(), encoding="ascii").read()
    assert vbs.isascii()                       # the code page never matters
    assert "%USERPROFILE%" in vbs              # the profile, not a literal path
    assert "run --log" in vbs                  # hidden means it logs itself
    assert ", 0, False" in vbs                 # hidden window, do not wait
    # onlogon does not start it now, so it is started explicitly -- after
    # the previous install's daemon is ended, or it keeps the port.
    assert r.ran("schtasks", "/end")
    assert r.calls[-1][:2] == ["schtasks", "/run"]


def test_schtasks_install_surfaces_why_it_failed(home, monkeypatch):
    _platform(monkeypatch, "win32")
    _runs(monkeypatch, codes=[1], stderr="ERROR: Access is denied.\n")

    msg = cli.backend().install()
    assert msg.startswith("could not register a Scheduled Task:")
    assert "Access is denied." in msg


def test_schtasks_remove_ends_deletes_and_kills(home, monkeypatch):
    """/delete alone leaves the daemon running -- and holding the serial port."""
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    killed = []
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: killed.append("pid"))
    monkeypatch.setattr(cli, "_kill_by_path", lambda: killed.append("path"))

    assert cli.backend().remove() == "removed"
    assert r.ran("schtasks", "/end")
    assert r.ran("schtasks", "/delete", "/f")
    assert killed == ["pid", "path"]


def test_schtasks_restart_kills_the_detached_successor(home, monkeypatch):
    """A daemon that replaced itself is not the task's child any more.

    /run would otherwise add a second daemon competing for the same port.
    """
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    killed = []
    monkeypatch.setattr(cli, "_kill_recorded_daemon", lambda: killed.append("pid"))

    assert cli.backend().restart() == "restarted"
    assert killed == ["pid"]
    assert r.calls[-1][:2] == ["schtasks", "/run"]


# ----------------------------------------------------------------- systemd --

def test_systemd_install_writes_the_unit_and_enables_it(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    r = _runs(monkeypatch)

    assert cli.backend().install() == "running (systemd)"
    unit = open(cli.unit_path()).read()
    assert "Restart=always" in unit
    assert "WantedBy=default.target" in unit
    assert r.ran("systemctl", "--user", "daemon-reload")
    assert r.ran("systemctl", "--user", "enable", "--now")


def test_systemd_remove_disables_and_reloads(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    os.makedirs(os.path.dirname(cli.unit_path()), exist_ok=True)
    open(cli.unit_path(), "w").write("x")
    r = _runs(monkeypatch)

    assert cli.backend().remove() == "removed"
    assert r.ran("systemctl", "--user", "disable", "--now")
    assert not os.path.exists(cli.unit_path())
    # daemon-reload after the unit file is gone, not before.
    assert r.calls[-1][:3] == ["systemctl", "--user", "daemon-reload"]


def test_systemd_status(home, monkeypatch):
    _platform(monkeypatch, "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    _runs(monkeypatch, codes=[0])
    assert cli.backend().status() == "running"
    _runs(monkeypatch, codes=[1])
    assert cli.backend().status() == "not running"


class TestLinuxWithoutSystemd:
    """A machine with no systemd is not a broken machine.

    Every method has to have a real answer for it, and they have to agree
    with each other. They did not: remove() knew exactly what was going on
    and status() said "unknown on linux".
    """

    @pytest.fixture(autouse=True)
    def _no_systemctl(self, monkeypatch):
        _platform(monkeypatch, "linux")
        monkeypatch.setattr(cli.shutil, "which", lambda _n: None)

    def test_install_still_writes_the_unit(self, home, monkeypatch):
        """So a machine that gains systemd later already has the right file."""
        r = _runs(monkeypatch)
        msg = cli.backend().install()
        assert "no systemd here" in msg
        assert cli.installed_bin() in msg          # what to run by hand
        assert os.path.exists(cli.unit_path())
        assert r.calls == []                       # nothing was invoked

    def test_remove_does_not_claim_to_have_stopped_anything(self, home, monkeypatch):
        os.makedirs(os.path.dirname(cli.unit_path()), exist_ok=True)
        open(cli.unit_path(), "w").write("x")
        _runs(monkeypatch)
        msg = cli.backend().remove()
        assert msg == "no systemd here; stop it yourself if you started it"
        assert not os.path.exists(cli.unit_path())

    def test_status_says_what_remove_says(self, home, monkeypatch):
        _runs(monkeypatch)
        assert "no systemd here" in cli.backend().status()

    def test_restart_falls_back(self, home, monkeypatch):
        _runs(monkeypatch)
        assert cli.backend().restart() == (
            "not running under a supervisor; restart it yourself")


# ---------------------------------------------------------------- disclosure --

@pytest.mark.parametrize("platform,needle", [
    ("darwin", "LaunchAgents"),
    ("linux", "systemd"),
    ("win32", "Scheduled Task"),
])
def test_creates_names_the_thing_install_will_make(home, monkeypatch,
                                                   platform, needle):
    """`blink install` prints this before it does anything."""
    _platform(monkeypatch, platform)
    assert needle in cli.backend().creates()


# ------------------------------------------------------------------- skips --

@pytest.mark.parametrize("fn", ["_install_service", "restart_service",
                                "_remove_service"])
def test_skip_service_short_circuits_every_entry_point(home, monkeypatch, fn):
    """The tests' own guard. If one of these forgot it, a unit test under a
    temporary HOME would boot out the agent of whoever is logged in."""
    _platform(monkeypatch, "darwin")
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")
    r = _runs(monkeypatch)

    assert getattr(cli, fn)() == "skipped (BLINK_SKIP_SERVICE=1)"
    assert r.calls == []


def test_kill_recorded_daemon_reads_the_legacy_pid_beside_the_old_program(home, monkeypatch):
    """Before 1.1.0 the daemon kept its pid beside its executable, and the
    1.1.0 install rotates that directory to bin.old. The first upgrade on a
    real PC left the 1.0.4 daemon alive on the serial port because only the
    new location, ~/.blink/bridge.pid, was read (2026-08-29)."""
    _platform(monkeypatch, "win32")
    r = _runs(monkeypatch)
    os.makedirs(cli.bin_dir() + ".old")
    with open(os.path.join(cli.bin_dir() + ".old", "bridge.pid"), "w") as f:
        f.write("4242")
    cli._kill_recorded_daemon()
    assert r.ran("taskkill", "/f", "/t", "/pid", "4242")
