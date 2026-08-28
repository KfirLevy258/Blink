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


@pytest.fixture(autouse=True)
def _no_real_login_service(monkeypatch):
    """Nothing in a unit test may register or remove a real login service."""
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")
