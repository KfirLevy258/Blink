"""End-to-end tests for install.sh, the one-command customer setup.

The script is the only piece of this product a customer ever runs by hand, and
it edits a file they own without asking -- so its behaviour is worth pinning
down rather than trusting to a manual run on one machine.

Every test runs it under a temporary HOME with the two skip hooks set, so
nothing here builds a virtualenv, reaches the network, or registers a real
login service on the machine running the tests.
"""
import json
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install.sh")


def _run(action, home, expect_ok=True):
    env = dict(os.environ)
    env.update({
        "HOME": str(home),
        "CLAUGE_SKIP_DEPS": "1",
        "CLAUGE_SKIP_SERVICE": "1",
    })
    proc = subprocess.run(
        ["sh", SCRIPT, action], cwd=ROOT, env=env,
        capture_output=True, text=True)
    if expect_ok:
        assert proc.returncode == 0, proc.stdout + proc.stderr
    return proc


def _settings(home):
    return home / ".claude" / "settings.json"


def _write_settings(home, obj):
    path = _settings(home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj))
    return path


def test_install_sets_statusline_to_the_installed_copy(tmp_path):
    _write_settings(tmp_path, {})
    _run("install", tmp_path)
    got = json.loads(_settings(tmp_path).read_text())
    shim = tmp_path / ".clauge" / "clauge-statusline.sh"
    assert shim.exists()
    assert got["statusLine"]["command"] == f"sh {shim}"


def test_installed_shim_is_a_copy_not_a_pointer_into_the_repo(tmp_path):
    """A customer who moves or deletes the folder must not lose their bar."""
    _write_settings(tmp_path, {})
    _run("install", tmp_path)
    command = json.loads(_settings(tmp_path).read_text())["statusLine"]["command"]
    assert ROOT not in command
    installed = (tmp_path / ".clauge" / "clauge-statusline.sh").read_text()
    assert installed == open(os.path.join(ROOT, "tools", "clauge-statusline.sh")).read()


def test_install_preserves_an_existing_statusline(tmp_path):
    _write_settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-bar.sh"}})
    _run("install", tmp_path)
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/my-bar.sh"


def test_install_discloses_every_change_before_making_it(tmp_path):
    """The script asks nothing, so the disclosure is the only safeguard.

    It has to name the file it edits, the key it touches, and the way back --
    and it has to say so before the first write, not after.
    """
    _write_settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-bar.sh"}})
    out = _run("install", tmp_path).stdout
    disclosure, _, rest = out.partition("[1/4]")
    assert str(_settings(tmp_path)) in disclosure
    assert "statusLine.command" in disclosure
    assert "sh ~/my-bar.sh" in disclosure          # their command, read live
    assert "uninstall" in disclosure
    assert rest, "the disclosure must come before the work, not after"


def test_install_is_idempotent(tmp_path):
    _write_settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-bar.sh"}})
    _run("install", tmp_path)
    _run("install", tmp_path)
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/my-bar.sh", "second run chained the shim to itself"


def test_uninstall_restores_the_original_statusline(tmp_path):
    _write_settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-bar.sh"}})
    _run("install", tmp_path)
    _run("uninstall", tmp_path)
    got = json.loads(_settings(tmp_path).read_text())
    assert got["statusLine"]["command"] == "sh ~/my-bar.sh"
    assert not (tmp_path / ".clauge" / "clauge-statusline.sh").exists()


def test_uninstall_keeps_the_ota_signing_key(tmp_path):
    """~/.clauge is shared with the signing key, which cannot be regenerated.

    Every board already flashed with its public half would stop accepting
    updates, so uninstall removes the three files it created -- never the
    directory.
    """
    key = tmp_path / ".clauge" / "ota_signing_key_p256.pem"
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_text("PRIVATE KEY")
    _write_settings(tmp_path, {})
    _run("install", tmp_path)
    _run("uninstall", tmp_path)
    assert key.read_text() == "PRIVATE KEY"


def test_uninstall_leaves_a_foreign_statusline_alone(tmp_path):
    """Someone who never installed Clauge must not lose their bar to it."""
    _write_settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-bar.sh"}})
    _run("uninstall", tmp_path)
    got = json.loads(_settings(tmp_path).read_text())
    assert got["statusLine"]["command"] == "sh ~/my-bar.sh"


def test_install_leaves_other_settings_untouched(tmp_path):
    _write_settings(tmp_path, {"model": "opus", "env": {"FOO": "bar"}})
    _run("install", tmp_path)
    got = json.loads(_settings(tmp_path).read_text())
    assert got["model"] == "opus"
    assert got["env"] == {"FOO": "bar"}


def test_status_reports_a_missing_payload_without_failing(tmp_path):
    _write_settings(tmp_path, {})
    _run("install", tmp_path)
    out = _run("status", tmp_path).stdout
    assert "none yet" in out


def test_status_reports_a_fresh_payload(tmp_path):
    _write_settings(tmp_path, {})
    _run("install", tmp_path)
    (tmp_path / ".clauge" / "statusline.json").write_text("{}")
    out = _run("status", tmp_path).stdout
    assert "fresh" in out


def test_unknown_action_fails_loudly(tmp_path):
    proc = _run("frobnicate", tmp_path, expect_ok=False)
    assert proc.returncode != 0
    assert "usage" in proc.stderr


def test_install_on_a_machine_with_no_claude_directory(tmp_path):
    """Claude Code writes ~/.claude/settings.json only once a setting changes.

    On a machine that never has, the directory itself is absent -- and the
    settings write is a temp file placed *next to* the target, so an absent
    parent failed the install after it had already copied the shim.
    """
    assert not (tmp_path / ".claude").exists()
    _run("install", tmp_path)
    got = json.loads(_settings(tmp_path).read_text())
    assert got["statusLine"]["command"].endswith("clauge-statusline.sh")


def test_install_fails_loudly_when_the_setting_cannot_be_written(tmp_path):
    """A failed step must stop the run, not print a traceback and 'Done.'

    Piping the installer's output through sed handed the pipeline sed's exit
    status, so the script sailed past a crashed install and reported success.
    """
    (tmp_path / ".claude").write_text("not a directory")
    proc = _run("install", tmp_path, expect_ok=False)
    assert proc.returncode != 0
    assert "Done." not in proc.stdout
    assert "Could not change the Claude Code setting" in proc.stderr


def test_disclosure_names_the_undo_that_actually_undoes_everything(tmp_path):
    """The nested module's own hint undoes one key; the script undoes the lot.

    A customer following the narrower command would leave the login service
    and the shim copy in place, believing they had removed Clauge.
    """
    _write_settings(tmp_path, {})
    out = _run("install", tmp_path).stdout
    assert f"{SCRIPT} uninstall" in out
    assert "python3 -m pc.install_statusline uninstall" not in out


def test_disclosure_does_not_claim_to_chain_its_own_shim(tmp_path):
    """A reinstall at a new path must not describe itself as chaining.

    The old command is Clauge's own shim, so install() recognises it and does
    NOT record it -- the customer's real command is already in the chain file.
    Saying "your command is recorded and still runs" of a command that was
    deliberately dropped is the one inaccuracy a disclosure cannot afford.
    """
    _write_settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-bar.sh"}})
    _run("install", tmp_path)                       # chains the real bar

    # Simulate the shim moving: point settings at an old Clauge path that the
    # marker still recognises, exactly as this machine's real state did.
    old = str(tmp_path / "elsewhere" / "clauge-statusline.sh")
    settings = json.loads(_settings(tmp_path).read_text())
    settings["statusLine"]["command"] = f"sh {old}"
    _settings(tmp_path).write_text(json.dumps(settings))
    (tmp_path / ".clauge" / "statusline-installed-command").write_text(f"sh {old}\n")

    out = _run("install", tmp_path).stdout
    disclosure = out.partition("Clauge statusline installed")[0]
    assert "Clauge's own shim" in disclosure
    assert "records the" not in disclosure
    assert "sh ~/my-bar.sh" in disclosure, "should show what actually still runs"
    # And the real bar is still there afterwards.
    assert (tmp_path / ".clauge" / "statusline-chain").read_text().strip() == "sh ~/my-bar.sh"


def test_uninstall_honours_the_service_skip_hook(tmp_path):
    """The launchd label and systemd unit name are global; HOME is not.

    Without this, an uninstall run under a throwaway HOME still boots out the
    real agent of whoever is logged in -- which is exactly what happened: a CI
    scenario run killed the live bridge and the board on the desk went to
    HOST LOST 35 seconds later.
    """
    _write_settings(tmp_path, {})
    _run("install", tmp_path)
    out = _run("uninstall", tmp_path).stdout
    assert "Background service ... skipped" in out
