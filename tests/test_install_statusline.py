import json
import os
import stat

from pc import install_statusline as ins


def _home(monkeypatch, tmp_path):
    """Point the module's ~ at tmp_path, on every platform.

    os.path.expanduser("~") reads HOME on POSIX but USERPROFILE on Windows, so
    setting HOME alone left twelve of these tests writing into the real user
    profile while asserting against tmp_path. They failed for a reason that
    had nothing to do with the chain-file behaviour they exist to pin down --
    a false signal, and the kind that is worth removing whether or not Windows
    is ever a supported target.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))


def _settings(tmp_path, obj):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(obj))
    return str(p)


def test_install_into_empty_settings(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh /opt/clauge/clauge-statusline.sh"
    assert got["statusLine"]["type"] == "command"


def test_install_preserves_existing_command_in_chain(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-statusline.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text()
    assert chain.strip() == "sh ~/my-statusline.sh"


def test_install_is_idempotent(tmp_path, monkeypatch):
    """Installing twice must not chain the shim to itself (infinite loop)."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = tmp_path / ".clauge" / "statusline-chain"
    assert "clauge-statusline.sh" not in (chain.read_text() if chain.exists() else "")


def test_uninstall_restores_previous_command(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-statusline.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh ~/my-statusline.sh"


def test_uninstall_with_no_previous_removes_key(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    ins.uninstall(path)
    assert "statusLine" not in json.loads(open(path).read())


def test_other_settings_keys_are_untouched(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {"model": "opus", "enabledPlugins": {"x": True}})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    got = json.loads(open(path).read())
    assert got["model"] == "opus"
    assert got["enabledPlugins"] == {"x": True}


def test_preexisting_command_containing_marker_substring_is_preserved(tmp_path, monkeypatch):
    """A customer's own command can legitimately contain the shim's filename
    as a substring (e.g. a wrapper script named after it). That must never
    be mistaken for "already ours" -- it has to round-trip through
    uninstall() exactly, the same as any other previous command."""
    _home(monkeypatch, tmp_path)
    weird = "sh ~/scripts/wrap-clauge-statusline.sh-backup.sh"
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": weird}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text().strip()
    assert chain == weird
    ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == weird


def test_reinstall_at_different_shim_path_preserves_original_chain(tmp_path, monkeypatch):
    """Reinstalling with a different shim_path must still recognize the
    currently-installed command as ours (not a foreign statusline), or it
    would chain our own old command over the customer's real one."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-statusline.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    ins.install(path, "/usr/local/bin/clauge-statusline.sh")
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/my-statusline.sh"
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh /usr/local/bin/clauge-statusline.sh"
    ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh ~/my-statusline.sh"


def test_install_after_manual_edit_chains_the_edited_command(tmp_path, monkeypatch):
    """If the customer hand-edits settings.json to point statusLine at
    something else -- bypassing uninstall() entirely -- a later install()
    must treat that as a real command to preserve, not silently drop it
    because a stale marker still matches something."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")

    data = json.loads(open(path).read())
    data["statusLine"]["command"] = "sh ~/manually-restored.sh"
    open(path, "w").write(json.dumps(data))

    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/manually-restored.sh"


def test_install_uninstall_preserves_hand_formatting(tmp_path, monkeypatch):
    """Only the statusLine key should change; the customer's own formatting
    (indent width, trailing newline) must survive an install/uninstall
    round trip untouched."""
    _home(monkeypatch, tmp_path)
    original = (
        "{\n"
        '    "model": "opus",\n'
        '    "statusLine": {\n'
        '        "type": "command",\n'
        '        "command": "sh ~/my-statusline.sh"\n'
        "    }\n"
        "}"
    )
    path = tmp_path / "settings.json"
    path.write_text(original)
    ins.install(str(path), "/opt/clauge/clauge-statusline.sh")
    ins.uninstall(str(path))
    assert path.read_text() == original


def test_marker_lost_reinstall_at_same_path_does_not_self_chain(tmp_path, monkeypatch):
    """~/.clauge also holds transient scratch data (statusline.json), so a
    user or cleanup script clearing it -- losing the marker while
    settings.json is untouched -- is plausible, not exotic. A same-path
    reinstall after that must still be recognized as ours via the
    stateless check (it needs no marker to have survived), or we'd chain
    our own command into the chain file and the shim would invoke itself
    forever on every render."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    (tmp_path / ".clauge" / "statusline-installed-command").unlink()

    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = tmp_path / ".clauge" / "statusline-chain"
    assert not chain.exists() or "clauge-statusline.sh" not in chain.read_text()

    ins.uninstall(path)
    assert "statusLine" not in json.loads(open(path).read())


def test_marker_lost_reinstall_at_different_path_chains_rather_than_drops(tmp_path, monkeypatch):
    """When BOTH recognition mechanisms are unavailable at once -- the
    marker is gone AND this call also targets a different shim_path --
    there is no way to prove the current command is ours. The safe default
    is to treat it as foreign and chain it: worst case is a stale
    self-referencing chain entry (a hang, visible and recoverable by
    clearing the chain file or uninstalling), not a silently discarded
    customer command (invisible and unrecoverable). This documents that
    known, accepted residual gap rather than claiming it's closed."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    (tmp_path / ".clauge" / "statusline-installed-command").unlink()

    ins.install(path, "/usr/local/bin/clauge-statusline.sh")
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text().strip()
    assert chain == "sh /opt/clauge/clauge-statusline.sh"


def test_stale_marker_never_matches_a_customers_unrelated_command(tmp_path, monkeypatch):
    """A marker naming a command that is no longer in settings.json (left
    behind by a manual edit) must never cause a genuinely different,
    customer-authored command to be treated as ours and dropped -- it is
    only ever compared for exact equality, and the customer's real command
    here is not equal to it."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    marker = (tmp_path / ".clauge" / "statusline-installed-command").read_text().strip()
    assert marker == "sh /opt/clauge/clauge-statusline.sh"

    # Customer overwrites statusLine by hand; the marker is left stale,
    # naming a command no longer present anywhere in settings.json.
    data = json.loads(open(path).read())
    data["statusLine"]["command"] = "sh ~/customers-own-script.sh"
    open(path, "w").write(json.dumps(data))

    ins.install(path, "/usr/local/bin/clauge-statusline.sh")
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/customers-own-script.sh"


# --- uninstall() symmetry: never touch a statusLine it doesn't recognize ---


def test_uninstall_does_not_clobber_a_command_switched_to_after_install(tmp_path, monkeypatch):
    """Reproduction (a): install, then the customer points statusLine at a
    brand NEW command of their own (bypassing uninstall entirely -- a direct
    edit of settings.json). uninstall() must leave that new command alone,
    not overwrite it with whatever is sitting in the (stale, unrelated)
    chain file."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/original.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")

    data = json.loads(open(path).read())
    data["statusLine"]["command"] = "sh ~/brand-new-command.sh"
    open(path, "w").write(json.dumps(data))

    msg = ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh ~/brand-new-command.sh"
    assert "leaving it alone" in msg.lower() or "isn't clauge" in msg.lower()


def test_uninstall_never_installed_leaves_unrelated_command_untouched(tmp_path, monkeypatch):
    """Reproduction (b): uninstall() is run against a settings.json holding
    someone else's command, and Clauge never installed here (no marker, no
    chain file -- e.g. ~/.clauge was wiped, or this machine never ran
    install()). Must not delete or replace that command."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/someone-elses-script.sh"}
    })
    ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh ~/someone-elses-script.sh"


def test_uninstall_after_clauge_directory_wiped_leaves_statusline_alone(tmp_path, monkeypatch):
    """~/.clauge (marker + chain) gone entirely, but settings.json still
    names the Clauge shim as statusLine (a real, currently-active install).
    With no way left to prove what to restore, uninstall() must do nothing
    rather than guess -- pop()'ing the key here would delete a live,
    correctly-configured statusline with no way to recover it."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")

    import shutil
    shutil.rmtree(tmp_path / ".clauge")

    msg = ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh /opt/clauge/clauge-statusline.sh"
    assert "leaving it alone" in msg.lower() or "isn't clauge" in msg.lower()


def test_uninstall_with_shim_path_recognizes_ours_even_without_marker(tmp_path, monkeypatch):
    """The CLI always knows its own shim_path. When passed, uninstall() can
    recognize the current command as ours statelessly (matches what
    install() would write for that path today), the same way install()
    already does -- so losing just the marker (not all of ~/.clauge) still
    allows a clean uninstall."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/original.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    (tmp_path / ".clauge" / "statusline-installed-command").unlink()

    ins.uninstall(path, "/opt/clauge/clauge-statusline.sh")
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh ~/original.sh"


# --- _save() preserves the original file's permission bits ---


def test_install_and_uninstall_preserve_0600_permissions(tmp_path, monkeypatch):
    """settings.json can legitimately hold env.ANTHROPIC_API_KEY or
    apiKeyHelper; a customer who locked it down to 0600 must get 0600 back,
    not the process umask's default (typically 0644)."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/original.sh"}
    })
    os.chmod(path, 0o600)

    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600

    ins.uninstall(path)
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


# --- shim_path containing a space is quoted, not split ---


def test_install_quotes_shim_path_containing_a_space(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    spaced = "/Users/kfir/Application Support/clauge/clauge-statusline.sh"
    ins.install(path, spaced)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh '" + spaced + "'"


def test_install_twice_with_spaced_shim_path_does_not_self_chain(tmp_path, monkeypatch):
    """Quoting must not break the same-path reinstall recognition that
    keeps the shim from being chained to itself."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    spaced = "/Users/kfir/Application Support/clauge/clauge-statusline.sh"
    ins.install(path, spaced)
    ins.install(path, spaced)
    chain = tmp_path / ".clauge" / "statusline-chain"
    assert not chain.exists() or "clauge-statusline.sh" not in chain.read_text()


# --- install() clears a stale/ghost chain file when there's nothing to chain ---


def test_install_with_no_previous_statusline_clears_a_ghost_chain_file(tmp_path, monkeypatch):
    """A chain file can outlive its relevance (the statusLine key it was
    meant to protect got removed by hand, or by some other flow). If the
    next install() has no current statusLine to chain, any pre-existing
    chain content is a ghost from an unrelated era and must be cleared --
    otherwise a later uninstall() would "restore" it as if it were the
    customer's real previous statusline."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {})
    clauge_dir = tmp_path / ".clauge"
    clauge_dir.mkdir(parents=True)
    (clauge_dir / "statusline-chain").write_text("sh ~/ghost-command.sh\n")

    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = clauge_dir / "statusline-chain"
    assert not chain.exists() or chain.read_text().strip() == ""

    ins.uninstall(path)
    got = json.loads(open(path).read())
    assert "statusLine" not in got


def test_install_with_marker_but_no_statusline_preserves_a_live_chain(tmp_path, monkeypatch):
    """Regression: 'statusLine is currently absent' is NOT proof the chain
    file is a ghost -- a marker surviving from an earlier install means the
    chain may still hold a real, still-live original that a later
    uninstall() needs to restore.

    Sequence: install over a real customer command (chains it); the
    statusLine key then gets cleared by some means other than uninstall()
    (hand edit, settings migration, a merge); install() runs again at the
    same shim path. The chain file must survive that reinstall, and the
    following uninstall() must restore the original customer command --
    not report 'Removed the Clauge statusline' with nothing to restore."""
    _home(monkeypatch, tmp_path)
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/original.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain_path = tmp_path / ".clauge" / "statusline-chain"
    assert chain_path.read_text().strip() == "sh ~/original.sh"

    data = json.loads(open(path).read())
    del data["statusLine"]
    open(path, "w").write(json.dumps(data))

    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    assert chain_path.exists(), "chain file must survive a reinstall over a cleared statusLine"
    assert chain_path.read_text().strip() == "sh ~/original.sh"

    ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh ~/original.sh"
