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

    def __init__(self, codes=None, stderr=""):
        self.calls = []
        self._codes = list(codes or [])
        self._stderr = stderr

    def __call__(self, argv, **kw):
        self.calls.append(list(argv))
        code = self._codes.pop(0) if self._codes else 0
        return subprocess.CompletedProcess(argv, code, stdout="",
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


def _runs(monkeypatch, codes=None, stderr=""):
    r = _Runs(codes, stderr)
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
    assert "<key>Label</key><string>com.clauge.bridge</string>" in plist
    assert "<key>KeepAlive</key><true/>" in plist
    assert cli.log_path() in plist
    # bootout must come first, or bootstrap fails on "already loaded".
    assert r.calls[0][:2] == ["launchctl", "bootout"]
    assert r.calls[1][:2] == ["launchctl", "bootstrap"]
    assert r.calls[1][2] == "gui/501"


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


def test_launchd_remove_boots_out_and_deletes_the_plist(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    os.makedirs(os.path.dirname(cli.plist_path()), exist_ok=True)
    open(cli.plist_path(), "w").write("x")
    r = _runs(monkeypatch)

    assert cli.backend().remove() == "removed"
    assert r.ran("launchctl", "bootout", "gui/501/com.clauge.bridge")
    assert not os.path.exists(cli.plist_path())


def test_launchd_status(home, monkeypatch):
    _platform(monkeypatch, "darwin")
    _runs(monkeypatch, codes=[0])
    assert cli.backend().status() == "registered with launchd"
    _runs(monkeypatch, codes=[1])
    assert cli.backend().status() == "not installed"


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
    # The path is quoted inside /tr: Windows splits an unquoted one on the
    # space in "Program Files" and on any space in the user's name.
    assert f'"{cli.installed_bin()}" run' in create
    # onlogon does not start it now, so it is started explicitly.
    assert r.calls[1][:2] == ["schtasks", "/run"]


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
    """`clauge install` prints this before it does anything."""
    _platform(monkeypatch, platform)
    assert needle in cli.backend().creates()


# ------------------------------------------------------------------- skips --

@pytest.mark.parametrize("fn", ["_install_service", "restart_service",
                                "_remove_service"])
def test_skip_service_short_circuits_every_entry_point(home, monkeypatch, fn):
    """The tests' own guard. If one of these forgot it, a unit test under a
    temporary HOME would boot out the agent of whoever is logged in."""
    _platform(monkeypatch, "darwin")
    monkeypatch.setenv("CLAUGE_SKIP_SERVICE", "1")
    r = _runs(monkeypatch)

    assert getattr(cli, fn)() == "skipped (CLAUGE_SKIP_SERVICE=1)"
    assert r.calls == []
