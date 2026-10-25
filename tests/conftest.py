"""Setup every test in this suite gets, whether it asks for it or not.

Every fixture below but move_home is autouse, and each is here rather than
in a test file because the failure mode is a test file that FORGETS them.
That is not a hypothetical in either case:

  - HOME was redirected but USERPROFILE was not, so on Windows twelve tests
    wrote into the real user profile while asserting against tmp_path. They
    failed for a reason with nothing to do with what they were pinning down.

  - A test ran the login-service code for real under a temporary HOME. The
    launchd label and the systemd unit name are global constants while every
    other path is scoped to HOME, so it booted out the agent of the person
    logged in. The board on the desk went to HOST LOST 35 seconds later.

A test that needs the real thing can still opt out with monkeypatch.delenv,
which is the right way round: the safe behaviour is the default and the
dangerous one has to be asked for.

move_home is not autouse: it is for the tests that move home after import.
"""
import ipaddress
import os
import pathlib
import socket
import urllib.request

import pytest

# Pointed under the sandbox's home for every test.
SET_UNDER_HOME = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
# Removed for every test: each names a home of its own and outranks HOME for
# the code that reads it.
CLEARED = ("CODEX_HOME",)


def _point_home_at(monkeypatch, home):
    """Every variable that names a home directory, pointed under `home`.

    os.path.expanduser("~") reads HOME on POSIX and USERPROFILE on Windows.
    Setting one without the other is the first bug described above, so this
    is the only place either of them is set -- the sandbox below and the
    move_home fixture both come through here.
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    # Claude Desktop's files on Windows live under %APPDATA%, which is its
    # own variable and was never redirected: on a Windows 11 VM, 2026-09-30,
    # `blink status` inside the suite read a real plan-usage-history.json
    # from the profile and a test expecting "no usage data" failed. The same
    # layout as a real profile, so code joining paths under it sees nothing
    # unusual.
    monkeypatch.setenv("APPDATA", str(home / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(home / "AppData" / "Local"))


def sandbox_environment(monkeypatch, home):
    """What the autouse sandbox does to the environment, as a function.

    A function so tests/test_sandbox.py can export CODEX_HOME itself and
    then check this clears it -- a test that relied on the developer's shell
    having it set could never fail in CI.
    """
    _point_home_at(monkeypatch, pathlib.Path(home))
    # CODEX_HOME outranks HOME in pc/providers/codex_cli.sessions_root and
    # pc/install_codex_hooks, so a developer who exports it had every test
    # that polls Codex reading their real rollouts, and every hooks test
    # one forgotten delenv away from editing their real hooks.json. A few
    # test files cleared it for themselves; that is the forgetting this file
    # exists to make impossible. Cleared rather than set, so it falls back to
    # ~/.codex, which is under tmp_path like everything else.
    for var in CLEARED:
        monkeypatch.delenv(var, raising=False)


@pytest.fixture(autouse=True)
def _sandboxed_home(tmp_path, monkeypatch):
    """tmp_path is ~, on every platform."""
    sandbox_environment(monkeypatch, tmp_path)


@pytest.fixture
def move_home(monkeypatch):
    """Call with a directory to make it ~ from then on, inside one test.

    For the tests that pin call-time path resolution: they have to move home
    AFTER import, and moving it by hand meant re-deriving which variables
    count as home in each test file.
    """
    return lambda home: _point_home_at(monkeypatch, pathlib.Path(home))


@pytest.fixture(autouse=True)
def _no_real_login_service(monkeypatch):
    """Nothing in a unit test may register or remove a real login service."""
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "allow_network: this test may open a socket to another"
                   " host (none does today)")


def _loopback(address):
    """Is a connect() address local? AF_UNIX paths and loopback IPs are."""
    if not isinstance(address, tuple):
        return True                     # AF_UNIX, or something not IP at all
    host = address[0]
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return host == "localhost"


@pytest.fixture(autouse=True)
def _no_real_network(request, monkeypatch):
    """No test may reach another machine.

    The daemon has two things that make requests on a timer -- the daily
    update check against github.com, and the status-page watch
    (pc/status_page.py) -- and their tests inject a fetch. One left out by
    mistake is a test that silently fetches
    status.claude.com from every CI run and passes or fails on whatever the
    vendor is saying that minute. Refused at the name lookup and at
    connect(), which every HTTP client reaches, so the failure names the
    address instead of timing out. Loopback stays open: the theme builder
    will serve on 127.0.0.1.

    Raised as a BaseException, not an AssertionError. The watch's reader
    catches Exception on purpose -- a status light must never take the daemon
    down -- and would have turned the refusal into an ordinary "unreadable
    page", so the very test this guard exists for would have passed.
    """
    if request.node.get_closest_marker("allow_network"):
        return
    # No proxy either. Loopback is allowed, and a proxy on 127.0.0.1 -- from
    # HTTPS_PROXY in a developer's shell, or macOS's system proxy settings,
    # which urllib reads without any variable being set -- would carry a
    # request straight through the guard to the internet.
    for k in list(os.environ):
        if k.lower().endswith("_proxy"):
            monkeypatch.delenv(k)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def refuse(what):
        raise RealNetworkRefused(
            f"a test tried to reach {what!r}; inject an opener, or mark it"
            " @pytest.mark.allow_network")

    def guarded(real):
        def connect(self, address):
            if not _loopback(address):
                refuse(address)
            return real(self, address)
        return connect

    def getaddrinfo(host, *a, **k):
        if host is not None and not _loopback((host,)):
            refuse(host)
        return real_getaddrinfo(host, *a, **k)

    monkeypatch.setattr(socket.socket, "connect", guarded(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex",
                        guarded(real_connect_ex))
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)


class RealNetworkRefused(BaseException):
    """See _no_real_network."""
