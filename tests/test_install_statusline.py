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
