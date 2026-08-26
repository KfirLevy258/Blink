"""Hooks go in alongside whatever is already there, and come out cleanly."""
import json

import pytest

from pc import install_hooks as ih
from pc.install_statusline import SettingsUnreadable

SHIM = "/opt/clauge/clauge-hook.sh"


@pytest.fixture
def settings(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus"}))
    return str(p)


def _read(settings):
    return json.loads(open(settings).read())


def _commands(settings, event):
    hooks = _read(settings).get("hooks", {}).get(event, [])
    return [h.get("command") for g in hooks for h in (g.get("hooks") or [])]


def test_installs_one_hook_per_lifecycle_event(settings):
    ih.install(settings, SHIM)
    for event, _ in ih.HOOK_EVENTS:
        assert ih.hook_command(SHIM, event) in _commands(settings, event)


def test_the_event_name_is_passed_as_an_argument(settings):
    assert ih.hook_command(SHIM, "PreToolUse").endswith(" PreToolUse")


def test_tool_events_get_a_matcher_and_others_do_not(settings):
    ih.install(settings, SHIM)
    hooks = _read(settings)["hooks"]
    assert hooks["PreToolUse"][0]["matcher"] == "*"
    assert "matcher" not in hooks["Stop"][0]


def test_installing_twice_does_not_stack_a_second_copy(settings):
    """A duplicate would fire twice on every tool call, forever."""
    ih.install(settings, SHIM)
    msg = ih.install(settings, SHIM)
    assert "already installed" in msg
    assert len(_commands(settings, "PreToolUse")) == 1


def test_an_existing_user_hook_is_left_completely_alone(settings):
    data = _read(settings)
    data["hooks"] = {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-audit"}]}]}
    open(settings, "w").write(json.dumps(data))

    ih.install(settings, SHIM)
    cmds = _commands(settings, "PreToolUse")
    assert "my-audit" in cmds
    assert ih.hook_command(SHIM, "PreToolUse") in cmds


def test_uninstall_removes_ours_and_only_ours(settings):
    data = _read(settings)
    data["hooks"] = {"PreToolUse": [
        {"matcher": "Bash", "hooks": [{"type": "command", "command": "my-audit"}]}]}
    open(settings, "w").write(json.dumps(data))

    ih.install(settings, SHIM)
    ih.uninstall(settings, SHIM)
    assert _commands(settings, "PreToolUse") == ["my-audit"]


def test_uninstall_leaves_no_empty_scaffolding(settings):
    """settings.json goes back to the shape it had, not to a shell of
    empty groups and dangling matchers."""
    before = _read(settings)
    ih.install(settings, SHIM)
    ih.uninstall(settings, SHIM)
    assert _read(settings) == before


def test_uninstall_on_a_clean_file_is_a_no_op(settings):
    assert "No Clauge state hooks" in ih.uninstall(settings, SHIM)


def test_uninstall_works_from_the_marker_after_the_shim_moved(settings):
    """An update moves the binary; uninstall must still recognise its own."""
    ih.install(settings, SHIM)
    assert "removed" in ih.uninstall(settings, "/somewhere/else/clauge-hook.sh")
    assert _commands(settings, "PreToolUse") == []


def test_a_hooks_key_that_is_not_an_object_is_refused_not_overwritten(settings):
    data = _read(settings)
    data["hooks"] = "surprise"
    open(settings, "w").write(json.dumps(data))
    with pytest.raises(SettingsUnreadable):
        ih.install(settings, SHIM)
    assert _read(settings)["hooks"] == "surprise"


def test_an_event_list_that_is_not_a_list_is_refused(settings):
    data = _read(settings)
    data["hooks"] = {"Stop": {"not": "a list"}}
    open(settings, "w").write(json.dumps(data))
    with pytest.raises(SettingsUnreadable):
        ih.install(settings, SHIM)


def test_unrelated_keys_are_never_rewritten(settings):
    ih.install(settings, SHIM)
    assert _read(settings)["model"] == "opus"
