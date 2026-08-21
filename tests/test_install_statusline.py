import json

from pc import install_statusline as ins


def _settings(tmp_path, obj):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps(obj))
    return str(p)


def test_install_into_empty_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh /opt/clauge/clauge-statusline.sh"
    assert got["statusLine"]["type"] == "command"


def test_install_preserves_existing_command_in_chain(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-statusline.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = (tmp_path / ".clauge" / "statusline-chain").read_text()
    assert chain.strip() == "sh ~/my-statusline.sh"


def test_install_is_idempotent(tmp_path, monkeypatch):
    """Installing twice must not chain the shim to itself (infinite loop)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    chain = tmp_path / ".clauge" / "statusline-chain"
    assert "clauge-statusline.sh" not in (chain.read_text() if chain.exists() else "")


def test_uninstall_restores_previous_command(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _settings(tmp_path, {
        "statusLine": {"type": "command", "command": "sh ~/my-statusline.sh"}
    })
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    ins.uninstall(path)
    got = json.loads(open(path).read())
    assert got["statusLine"]["command"] == "sh ~/my-statusline.sh"


def test_uninstall_with_no_previous_removes_key(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    path = _settings(tmp_path, {})
    ins.install(path, "/opt/clauge/clauge-statusline.sh")
    ins.uninstall(path)
    assert "statusLine" not in json.loads(open(path).read())


def test_other_settings_keys_are_untouched(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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
    monkeypatch.setenv("HOME", str(tmp_path))
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
