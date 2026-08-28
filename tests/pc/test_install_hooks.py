"""Hooks go in alongside whatever is already there, and come out cleanly."""
import json

import pytest

from pc import install_hooks as ih
from pc.install_statusline import SettingsUnreadable

SHIM = "/opt/blink/blink-hook.sh"


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


def test_notification_matches_only_the_waiting_kinds(settings):
    """Unmatched, Notification also fires for idle_prompt -- sixty seconds
    after every reply -- and turned every finished turn amber a minute
    later. Only the kinds that mean 'waiting on a person' are wanted."""
    ih.install(settings, SHIM)
    hooks = _read(settings)["hooks"]
    assert hooks["Notification"][0]["matcher"] == ih.NOTIFICATION_MATCHER
    assert "idle_prompt" not in ih.NOTIFICATION_MATCHER


def test_reinstall_corrects_an_unmatched_notification_entry(settings):
    """An install from before the matcher existed left the entry catching
    every notification type; a reinstall must fix it, not stack a second."""
    ih.install(settings, SHIM)
    data = _read(settings)
    data["hooks"]["Notification"][0].pop("matcher")
    with open(settings, "w") as f:
        json.dump(data, f)
    out = ih.install(settings, SHIM)
    hooks = _read(settings)["hooks"]
    assert len(hooks["Notification"]) == 1
    assert hooks["Notification"][0]["matcher"] == ih.NOTIFICATION_MATCHER
    assert "repointed" in out


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
    assert "No Blink state hooks" in ih.uninstall(settings, SHIM)


def test_uninstall_works_from_the_marker_after_the_shim_moved(settings):
    """An update moves the binary; uninstall must still recognise its own."""
    ih.install(settings, SHIM)
    assert "removed" in ih.uninstall(settings, "/somewhere/else/blink-hook.sh")
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


# --- a changed shim path must repoint, not orphan ---------------------------
#
# The old behaviour: entries matched via the OLD marker, so every event was
# skipped and nothing was added -- and then the marker was overwritten with the
# NEW commands. All ten entries became invisible to uninstall, left invoking a
# script that no longer exists, and a third install appended duplicates.


def test_a_moved_shim_repoints_every_hook(tmp_path):
    p = str(tmp_path / "settings.json")
    old_shim = str(tmp_path / "old" / "blink-hook.sh")
    new_shim = str(tmp_path / "new" / "blink-hook.sh")

    ih.install(p, old_shim)
    first = json.loads(open(p).read())["hooks"]
    n = len(ih.HOOK_EVENTS)

    msg = ih.install(p, new_shim)
    data = json.loads(open(p).read())["hooks"]

    cmds = [h["command"]
            for groups in data.values() for g in groups
            for h in g.get("hooks", [])]
    assert len(cmds) == n, "no duplicates, no losses"
    # Exactly the commands a fresh install at the new path would write --
    # every event once, at the new path, with its event argument. The check
    # this replaces tested `"sh" in c`, which every command satisfied.
    assert sorted(cmds) == sorted(ih.hook_command(new_shim, ev)
                                  for ev, _ in ih.HOOK_EVENTS)
    assert not any(old_shim.replace("\\", "/") in c for c in cmds)
    assert "repointed" in msg
    assert len(first) == len(data)


def test_uninstall_then_removes_all_of_them(tmp_path):
    """The point of repointing: uninstall matches expected u marker, so an
    entry left at the old path could never be removed again."""
    p = str(tmp_path / "settings.json")
    ih.install(p, str(tmp_path / "old" / "h.sh"))
    ih.install(p, str(tmp_path / "new" / "h.sh"))
    ih.uninstall(p, str(tmp_path / "new" / "h.sh"))
    left = json.loads(open(p).read()).get("hooks") or {}
    remaining = [h for groups in left.values() for g in groups
                 for h in g.get("hooks", [])]
    assert remaining == [], remaining


def test_a_plain_reinstall_still_says_nothing_changed(tmp_path):
    p = str(tmp_path / "settings.json")
    shim = str(tmp_path / "h.sh")
    ih.install(p, shim)
    assert "already installed" in ih.install(p, shim)
