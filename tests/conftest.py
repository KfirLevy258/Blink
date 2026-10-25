"""Setup every test in this suite gets, whether it asks for it or not.

Every fixture below is autouse, and each is here rather than in a test file
because the failure mode is a test file that FORGETS them. That is not a
hypothetical in either case:

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
"""
import ipaddress
import os
import socket
import urllib.request

import pytest


@pytest.fixture(autouse=True)
def _sandboxed_home(tmp_path, monkeypatch):
    """tmp_path is ~, on every platform.

    os.path.expanduser("~") reads HOME on POSIX and USERPROFILE on Windows.
    Setting one without the other is the bug described above, so this is the
    only place either of them is set.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    # Claude Desktop's files on Windows live under %APPDATA%, which is its
    # own variable and was never redirected: on a Windows 11 VM, 2026-09-30,
    # `blink status` inside the suite read a real plan-usage-history.json
    # from the profile and a test expecting "no usage data" failed. The same
    # layout as a real profile, so code joining paths under it sees nothing
    # unusual.
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "AppData" / "Local"))


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
