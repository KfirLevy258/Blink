"""The login service is refused when HOME is not the account's own.

The launchd label, the systemd unit name and the task name are global, while
every file the service code writes is scoped to HOME. So `HOME=/tmp/x blink
install` -- a developer trying a build in a scratch folder, a reviewer's
import (2026-10-30, see tests/test_import_is_inert.py) -- writes into the
scratch folder and then boots out the REAL agent and bootstraps one that
points into the scratch folder. BLINK_SKIP_SERVICE=1 is the answer for a
caller who thinks of it; nobody had to think of it in the incident.

So the service code also asks the account database whose home this is, and
skips when HOME names another one, unless BLINK_ALLOW_FOREIGN_HOME=1 says the
caller means it (tests/ci/check_install.sh does, on a runner).
"""
import os

import pytest

from pc import cli, service_ctl

SKIPPED = "skipped (HOME is not this account's home; set" \
          " BLINK_ALLOW_FOREIGN_HOME=1 to use it anyway)"


class _Tripwire(BaseException):
    """A BaseException, as in test_service_ctl: the code under test catches
    Exception, and a tripwire it swallows proves nothing."""


@pytest.fixture
def armed(tmp_path, monkeypatch):
    """The service code switched on (BLINK_SKIP_SERVICE off), the account's
    home somewhere other than the sandbox's HOME, and every subprocess call a
    failure. Returns the account's home."""
    account = tmp_path / "account"
    account.mkdir()
    monkeypatch.delenv("BLINK_SKIP_SERVICE", raising=False)
    monkeypatch.delenv("BLINK_ALLOW_FOREIGN_HOME", raising=False)
    monkeypatch.setattr(cli, "_account_home", lambda: str(account))
    monkeypatch.setattr(cli.sys, "platform", "darwin")

    def boom(*a, **k):
        raise _Tripwire("the service manager was called: %r" % (a,))
    monkeypatch.setattr(cli.subprocess, "run", boom)
    monkeypatch.setattr(cli.subprocess, "Popen", boom)
    return account


def test_a_foreign_home_skips_every_service_entry_point(armed):
    assert cli._skip_service() is True
    for fn in ("_install_service", "restart_service", "_remove_service"):
        assert getattr(cli, fn)() == SKIPPED, fn
    assert cli.halt_service() is None
    assert cli.refresh_service_file() is None


def test_the_fleet_helpers_skip_too(armed):
    for verb in (service_ctl.stop_service, service_ctl.start_service):
        out = verb()
        assert out.skipped and not out.ok and out.detail == SKIPPED


def test_a_skipped_restart_is_not_a_service_left_down(armed):
    """blink update prints the backend's answer and then asks whether the
    daemon is down. Skipping is not the daemon failing to return."""
    assert cli.restart_left_it_down(cli.restart_service()) is False


def test_skipping_says_which_variable_to_set(armed):
    note = cli._install_service()
    assert "HOME" in note and "BLINK_ALLOW_FOREIGN_HOME=1" in note


def test_the_explicit_variable_lets_it_through(armed, monkeypatch):
    monkeypatch.setenv("BLINK_ALLOW_FOREIGN_HOME", "1")
    assert cli._skip_service() is False


def test_the_account_own_home_is_not_foreign(armed, monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(armed))
    assert cli._skip_service() is False


def test_a_symlinked_home_is_the_same_home(armed, monkeypatch, tmp_path):
    """A customer whose HOME is a symlink to the account's directory (a
    relocated home, /home -> /export/home) must not be refused."""
    link = tmp_path / "link"
    link.symlink_to(armed)
    monkeypatch.setenv("HOME", str(link))
    assert cli._skip_service() is False


def test_an_account_the_database_does_not_know_is_let_through(
        armed, monkeypatch):
    """An arbitrary uid in a container has no passwd entry. There is no real
    home to compare with, and refusing would break an install that works."""
    monkeypatch.setattr(cli, "_account_home", lambda: None)
    assert cli._skip_service() is False


def test_the_env_variable_still_gives_its_own_note(armed, monkeypatch):
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")
    assert cli._install_service() == "skipped (BLINK_SKIP_SERVICE=1)"


def test_account_home_is_the_password_databases_answer():
    """Not $HOME, which is the thing being checked."""
    if os.name == "nt":
        assert cli._account_home() is None
        return
    import pwd
    assert cli._account_home() == pwd.getpwuid(os.getuid()).pw_dir


@pytest.mark.skipif(os.name == "nt", reason="sh stubs; no password database")
def test_install_under_a_scratch_home_never_reaches_the_service_manager(
        tmp_path):
    """End to end, as the 2026-10-30 reviewer's import ended up: the real
    entry point, a scratch HOME, BLINK_SKIP_SERVICE unset. The child's PATH
    is the recording stubs and nothing else, checked to shadow first, so a
    refusal that stopped working fails here instead of on a desk."""
    import subprocess
    import sys

    from tests.test_import_is_inert import ROOT, SERVICE_TOOLS, _stub_path

    if cli._account_home() is None:
        pytest.skip("this uid has no passwd entry, so no account home")
    bindir, log = _stub_path(tmp_path)
    home = tmp_path / "scratch-home"
    home.mkdir()
    env = {"PATH": str(bindir), "HOME": str(home),
           "PYTHONDONTWRITEBYTECODE": "1", "BLINK_SKIP_DEPS": "1"}
    seen = subprocess.run(
        ["/bin/sh", "-c", "command -v " + " ".join(SERVICE_TOOLS)],
        env=env, capture_output=True, text=True, timeout=30)
    assert seen.stdout.split() == [str(bindir / t) for t in SERVICE_TOOLS]

    out = subprocess.run(
        [sys.executable, "-s", str(ROOT / "blink_main.py"), "install"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=120)
    # Fails, and says so: "Done. Plug the board in" with exit 0 told a
    # customer with no service that it worked.
    assert out.returncode == 1, out.stdout + out.stderr
    assert not log.exists(), log.read_text()
    assert "HOME is not this account's home" in out.stdout
    assert "NOT installed" in out.stdout and "Done." not in out.stdout
    assert "BLINK_ALLOW_FOREIGN_HOME=1" in out.stdout


def test_ci_install_script_opts_in_only_on_github_actions():
    """tests/ci/check_install.sh must still be refused on a desk: it sets
    the override only on GitHub Actions. `CI` was the first key and many
    developers' shells export it (npm, direnv), which made "run by hand on a
    desk is still refused" untrue (review of fp/safety-guards, S2);
    GITHUB_ACTIONS=true is set by the runner and by nobody's profile."""
    import pathlib
    text = (pathlib.Path(__file__).resolve().parents[2]
            / "tests" / "ci" / "check_install.sh").read_text()
    assert ('if [ "${GITHUB_ACTIONS:-}" = "true" ]; then\n'
            '\tBLINK_ALLOW_FOREIGN_HOME=1') in text
    assert '${CI:-}' not in text



# --- the commands say so, and fail ----------------------------------------
#
# A skip that ends in "Done. Plug the board in -- it picks it up on its own"
# and exit 0 is a customer with no service who was told it worked (review of
# fp/safety-guards, S1). Asked for by BLINK_SKIP_SERVICE=1 the same ending is
# fine -- that is the tests' and CI's own choice -- so only the HOME refusal
# changes the outcome.


def test_the_refusal_is_told_apart_from_the_variable(armed, monkeypatch):
    assert cli._home_refusal() is True
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")
    assert cli._home_refusal() is False         # the caller chose that
    monkeypatch.delenv("BLINK_SKIP_SERVICE")
    monkeypatch.setenv("BLINK_ALLOW_FOREIGN_HOME", "1")
    assert cli._home_refusal() is False


def _says_not_done(text, what):
    assert f"NOT {what}" in text, text
    assert "BLINK_ALLOW_FOREIGN_HOME=1" in text, text
    assert "is not this account's home" in text, text


def test_uninstall_says_the_service_was_not_stopped_and_fails(armed, capsys):
    assert cli.cmd_uninstall(None) == 1
    out = capsys.readouterr().out
    _says_not_done(out, "stopped or removed")
    assert "Nothing of Blink's is left running" not in out


def test_uninstall_under_the_variable_is_as_before(
        armed, monkeypatch, capsys):
    monkeypatch.setenv("BLINK_SKIP_SERVICE", "1")
    assert cli.cmd_uninstall(None) == 0
    out = capsys.readouterr().out
    assert "NOT" not in out and "Done." in out


def test_update_says_the_service_was_not_restarted_and_fails(
        armed, monkeypatch, capsys):
    monkeypatch.setattr(cli, "_update_reaches_the_service", lambda: True)
    monkeypatch.setattr(cli.update, "fetch_signed_manifest", lambda: {})
    monkeypatch.setattr(cli.update, "available",
                        lambda m: ("9.9.9", {"size": 0, "sha256": ""}))
    monkeypatch.setattr(cli.update, "platform_key", lambda: "k")
    monkeypatch.setattr(cli.update, "download", lambda *a, **k: b"")
    monkeypatch.setattr(cli.update, "apply", lambda *a, **k: (True, "ok"))
    monkeypatch.setattr(cli, "_refresh_shims", lambda: None)
    assert cli.cmd_update(None) == 1
    _says_not_done(capsys.readouterr().out, "restarted")
