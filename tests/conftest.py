"""Setup every test in this suite gets, whether it asks for it or not.

Both fixtures below are autouse, and both are here rather than in a test file
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
    # OpenCode's directories follow XDG, which outranks HOME: a developer
    # with XDG_DATA_HOME set would otherwise have the suite read their real
    # opencode auth.json -- which holds an API key the provider SENDS to
    # Z.ai (pc/providers/opencode.py). Unset rather than pointed into
    # tmp_path, so the default ~/.local/share under the redirected HOME is
    # what every test sees unless it sets one on purpose.
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)


@pytest.fixture(autouse=True)
def _no_real_login_service(monkeypatch):
    """Nothing in a unit test may register or remove a real login service."""
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")


# The hosts pc/providers/opencode.py sends a customer's GLM plan key to. The
# suffix, so any subdomain of either is covered as well.
_QUOTA_HOSTS = ("z.ai", "bigmodel.cn")


@pytest.fixture(autouse=True)
def _no_real_quota_request(monkeypatch):
    """No test may reach Z.ai or BigModel, whatever it forgot to stub.

    `blink status` now makes one real request on a desk where OpenCode holds
    a plan key, and the daemon's provider makes one a minute. Every test that
    exercises either is meant to replace fetch_once or pass a fetcher -- and
    the failure mode this guards is the test that does not: with a key
    written into the sandbox it would send that key to a real server, and a
    developer whose XDG_DATA_HOME leaked past _sandboxed_home would send
    THEIR key. Failing loudly here, at urllib's one door, catches both.

    pytest.fail raises a BaseException, on purpose: fetch_once catches every
    Exception and turns it into "unreachable", which would let the attempt
    pass silently as an ordinary network failure. Local test servers
    (test_opencode's http_get tests) are on 127.0.0.1 and go through.
    """
    import urllib.parse
    import urllib.request

    real_open = urllib.request.OpenerDirector.open

    def guarded(self, fullurl, *args, **kwargs):
        url = fullurl if isinstance(fullurl, str) else fullurl.get_full_url()
        host = (urllib.parse.urlsplit(url).hostname or "").lower()
        if any(host == h or host.endswith("." + h) for h in _QUOTA_HOSTS):
            pytest.fail(f"a test tried a real request to {host}; stub"
                        " opencode.fetch_once or pass a fetcher")
        return real_open(self, fullurl, *args, **kwargs)

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", guarded)
