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

  - theme_server.Launcher fell back to the real opener when a test's stub
    opener was falsy, and on 2026-10-30 tabs opened in the owner's Chrome
    while the suite ran. The socket guard cannot see that: a browser is
    started by a process or a file handler, not by a connect() from here.

A test that needs the real thing can still opt out with monkeypatch.delenv,
which is the right way round: the safe behaviour is the default and the
dangerous one has to be asked for.

move_home is not autouse: it is for the tests that move home after import.
"""
import ipaddress
import os
import pathlib
import re
import socket
import subprocess
import urllib.request
import webbrowser

import pytest

# Pointed under the sandbox's home for every test.
SET_UNDER_HOME = ("HOME", "USERPROFILE", "APPDATA", "LOCALAPPDATA")
# Removed for every test. CODEX_HOME names a home of its own and outranks HOME
# for the code that reads it. The BLINK_* ones are each read by the daemon and
# each outranks something a test injects: BLINK_STATUS_DIR is read INSTEAD of
# the injected status-page opener (exported, it failed 31 tests, among them
# the one that proves the suite refuses a real request, found by the review of
# PR #4, 2026-10-31); BLINK_RELEASE_PUBKEY_FILE replaces the shipped release
# key; BLINK_OTA_DIR replaces the release feed; BLINK_TAP and BLINK_SCENARIO
# make the daemon write a transcript or play a script; the rest switch the
# poll interval, the watchdog and auto-update. A test that wants one sets it
# itself, after this. tests/test_sandbox.py fails if the code reads a BLINK_*
# variable that is in neither this list nor SET_BY_FIXTURE.
CLEARED = ("CODEX_HOME", "BLINK_STATUS_DIR", "BLINK_STATUS_POLL_S",
           "BLINK_RELEASE_PUBKEY_FILE", "BLINK_OTA_DIR", "BLINK_TAP",
           "BLINK_SCENARIO", "BLINK_POLL_INTERVAL_S", "BLINK_NO_WATCHDOG",
           "BLINK_NO_AUTO_UPDATE", "BLINK_ALLOW_FOREIGN_HOME")
# Set to a value for every test, by their own fixtures below.
SET_BY_FIXTURE = ("BLINK_SKIP_SERVICE",)


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
    config.addinivalue_line(
        "markers", "allow_browser: this test may start the real browser or"
                   " file opener (none does today)")


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


# What can start the owner's browser from inside a test. `open` and
# `osascript` are what webbrowser runs on macOS, `xdg-open` on Linux; `start`
# and `explorer` are Windows' (os.startfile is the usual way there, and is
# guarded below). The rest are the Linux desktops' own spellings.
_OPENERS = frozenset({
    "open", "osascript", "xdg-open", "gnome-open", "kde-open",
    "sensible-browser", "x-www-browser", "wslview", "start", "explorer"})

# Every refusal, whether or not the code under test let the exception through.
# A BaseException can still be caught by a bare `except:` or die in a worker
# thread, and then the test would pass having opened the browser; the
# autouse fixture fails the test at teardown if this is not empty.
_BROWSER_ATTEMPTS = []


class RealBrowserRefused(BaseException):
    """See _no_real_browser. A BaseException for the reason given on
    RealNetworkRefused: theme_server's opener catches Exception."""


def browser_attempts():
    return list(_BROWSER_ATTEMPTS)


def forget_browser_attempts():
    """For the tests of the guard itself, which provoke it on purpose."""
    del _BROWSER_ATTEMPTS[:]


def fail_on_browser_attempts():
    if _BROWSER_ATTEMPTS:
        what = ", ".join(_BROWSER_ATTEMPTS)
        forget_browser_attempts()
        pytest.fail(f"this test opened a browser ({what}); inject an opener,"
                    " or mark it @pytest.mark.allow_browser", pytrace=False)


def _stem(word):
    """`C:\\Windows\\Explorer.EXE` and `/usr/bin/open` are `explorer`, `open`."""
    name = re.split(r"[\\/]", word)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def browser_launcher(args):
    """The opener a command would start, or None.

    `args` is what subprocess or os.system is handed: a list, or a string
    whose first word is the program (the shell reads it that way, and so
    does a bare Popen). `cmd /c start ...` is a shell built-in, so for cmd
    the first word that is not a /switch is what counts. It looks at the
    program, never at the words after it: `echo open` is not an opener.
    """
    if isinstance(args, os.PathLike):
        args = os.fspath(args)
    if isinstance(args, (str, bytes)):
        words = os.fsdecode(args).split()
    else:
        try:
            words = [os.fsdecode(a) for a in args]
        except TypeError:
            return None
    if not words:
        return None
    prog = _stem(words[0])
    if prog in _OPENERS:
        return prog
    if prog == "cmd":
        rest = [w for w in " ".join(words[1:]).split()
                if not w.startswith("/")]
        if rest and rest[0].lower() in _OPENERS:
            return "cmd /c " + rest[0].lower()
    return None


def _guard(fn):
    fn.blink_browser_guard = True       # tests/test_sandbox.py looks for it
    return fn


class _RefusingBrowser:
    """What webbrowser.get() hands back: the controllers for Chrome, Firefox
    and the rest run their own binaries, which the launcher list above would
    not know by name on a Linux runner that has one installed."""
    name = "refused"

    def __init__(self, refuse):
        self._refuse = refuse

    def open(self, url, new=0, autoraise=True):
        self._refuse("webbrowser.get().open")

    def open_new(self, url):
        self._refuse("webbrowser.get().open_new")

    def open_new_tab(self, url):
        self._refuse("webbrowser.get().open_new_tab")


@pytest.fixture(autouse=True)
def _no_real_browser(request, monkeypatch):
    """No test may open the owner's browser, or any file opener.

    Refused at webbrowser, at os.startfile and os.system, and at every
    subprocess launch of an opener by name (Popen is what subprocess.run,
    check_output and os.popen all come to), so the failure names the
    opener instead of a tab appearing in someone's Chrome. A test that
    injects its own opener replaces these by monkeypatch and is unaffected.

    Raised as a BaseException, and recorded as well: see _BROWSER_ATTEMPTS.
    """
    if request.node.get_closest_marker("allow_browser"):
        yield
        return

    def refuse(what):
        _BROWSER_ATTEMPTS.append(what)
        raise RealBrowserRefused(
            f"a test tried to open a browser through {what}; inject an"
            " opener, or mark it @pytest.mark.allow_browser")

    def refusing(name):
        def opener(*a, **k):
            refuse(f"webbrowser.{name}")
        return _guard(opener)

    for name in ("open", "open_new", "open_new_tab"):
        monkeypatch.setattr(webbrowser, name, refusing(name))
    monkeypatch.setattr(webbrowser, "get",
                        _guard(lambda *a, **k: _RefusingBrowser(refuse)))

    # Only where it exists: code tells Windows by the presence of
    # os.startfile, and a guard that invented one would change the answer.
    if hasattr(os, "startfile"):
        monkeypatch.setattr(
            os, "startfile", _guard(lambda *a, **k: refuse("os.startfile")))

    real_system = os.system

    def system(command):
        name = browser_launcher(command)
        if name:
            refuse(f"os.system {name}")
        return real_system(command)

    monkeypatch.setattr(os, "system", _guard(system))

    real_init = subprocess.Popen.__init__

    def popen_init(self, args, *a, **k):
        name = browser_launcher(args)
        if name:
            refuse(f"subprocess {name}")
        return real_init(self, args, *a, **k)

    monkeypatch.setattr(subprocess.Popen, "__init__", _guard(popen_init))
    yield
    fail_on_browser_attempts()
