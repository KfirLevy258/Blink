"""Registering Blink's shim with Codex, and never damaging its config.

Nothing here may touch a real ~/.codex or a real ~/.blink. tests/conftest.py
redirects HOME and USERPROFILE at every test, which covers the marker file;
CODEX_HOME is an environment variable that fixture knows nothing about, so it
is cleared below -- on a machine that sets it, codex_home() would otherwise
resolve to the owner's live install.
"""
import json
import os

import pytest

from pc import install_codex_hooks as ich
from pc.install_statusline import SettingsUnreadable

SHIM = "/home/k/.blink/blink-hook.sh"


@pytest.fixture(autouse=True)
def _no_real_codex_home(monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)


def _events(data):
    return data["hooks"] if ich._EVENTS_KEY else data


def _read(p):
    return _events(json.loads(p.read_text(encoding="utf-8")))


def _write(p, events):
    payload = {"hooks": events} if ich._EVENTS_KEY else dict(events)
    p.write_text(json.dumps(payload), encoding="utf-8")


def test_the_command_carries_the_codex_argument():
    """Without it the shim writes Codex sessions into ~/.blink/state and the
    board reports them as Claude ones."""
    cmd = ich.hook_command(SHIM, "PreToolUse")
    assert cmd.endswith("PreToolUse codex")


def test_install_writes_one_group_per_event(tmp_path):
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)

    events = _read(p)
    assert set(events) == {ev for ev, _ in ich.HOOK_EVENTS}
    group = events["PreToolUse"][0]
    assert group["matcher"] == "*"
    assert group["hooks"] == [{
        "type": "command",
        "command": "sh /home/k/.blink/blink-hook.sh PreToolUse codex"}]
    assert "matcher" not in events["Stop"][0], \
        "events that take no matcher must not be given one"


def test_the_events_are_wrapped_the_way_codex_demands(tmp_path):
    """The struct behind this file carries deny_unknown_fields, so a bare
    {"PreToolUse": [...]} at the top level is not ignored -- it is rejected,
    and every hook in the file with it."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)

    data = json.loads(p.read_text(encoding="utf-8"))
    assert set(data) == {"hooks"}
    assert isinstance(data["hooks"], dict)


def test_the_waiting_event_and_all_of_its_clears_are_registered():
    """A waiting state with no way out is worse than no waiting state. If
    PermissionRequest is registered, everything that can follow it must be
    too -- otherwise the amber pip never goes back."""
    events = {ev for ev, _ in ich.HOOK_EVENTS}
    assert "PermissionRequest" in events
    for clearing in ("PostToolUse", "Stop", "Interrupt", "UserPromptSubmit",
                     "SessionEnd"):
        assert clearing in events, f"{clearing} cannot clear a wait it never sees"


def test_install_is_idempotent(tmp_path):
    """A reinstall must not stack a second copy that then fires twice per
    tool call forever."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    msg = ich.install(str(p), SHIM)

    events = _read(p)
    assert len(events["PreToolUse"]) == 1
    assert len(events["PreToolUse"][0]["hooks"]) == 1
    assert "already installed" in msg


def test_install_repoints_a_moved_shim(tmp_path):
    """What `blink update` does every time it moves the binary. Without this
    the old entries are orphaned: invisible to uninstall, still invoking a
    script that is not there, and a third install appends a duplicate."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), "/old/blink-hook.sh")
    msg = ich.install(str(p), "/new/blink-hook.sh")

    events = _read(p)
    assert len(events["PreToolUse"]) == 1
    assert events["PreToolUse"][0]["hooks"][0]["command"] == \
        "sh /new/blink-hook.sh PreToolUse codex"
    assert "repointed" in msg


def test_install_never_touches_someone_elses_hook(tmp_path):
    p = tmp_path / "hooks.json"
    theirs = {"matcher": "*", "hooks": [
        {"type": "command", "command": "/usr/local/bin/audit.sh"}]}
    _write(p, {"PreToolUse": [theirs]})

    ich.install(str(p), SHIM)

    events = _read(p)
    commands = [h["command"] for g in events["PreToolUse"] for h in g["hooks"]]
    assert "/usr/local/bin/audit.sh" in commands
    assert "sh /home/k/.blink/blink-hook.sh PreToolUse codex" in commands


def test_install_keeps_the_rest_of_the_file(tmp_path):
    """Codex allows a top-level `description`, and other keys may arrive in a
    later version. Rebuilding the document instead of merging into it would
    delete every one of them."""
    p = tmp_path / "hooks.json"
    p.write_text(json.dumps({"description": "mine", "hooks": {}}),
                 encoding="utf-8")

    ich.install(str(p), SHIM)

    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["description"] == "mine"


def test_install_leaves_a_shared_groups_matcher_alone(tmp_path):
    """Our hook and someone else's in one group: correcting the matcher would
    change when THEIR hook fires, which is not ours to decide."""
    p = tmp_path / "hooks.json"
    shared = {"matcher": "Bash", "hooks": [
        {"type": "command", "command": "/usr/local/bin/audit.sh"},
        {"type": "command",
         "command": "sh /home/k/.blink/blink-hook.sh PreToolUse codex"}]}
    _write(p, {"PreToolUse": [shared]})

    ich.install(str(p), SHIM)

    events = _read(p)
    assert len(events["PreToolUse"]) == 1
    assert events["PreToolUse"][0]["matcher"] == "Bash"


def test_install_steps_over_a_group_it_cannot_read(tmp_path):
    """A stray string where a matcher group belongs is one person's typo, not
    a reason to refuse the file -- and never a reason to crash on .get()."""
    p = tmp_path / "hooks.json"
    _write(p, {"PreToolUse": ["not a group"]})

    ich.install(str(p), SHIM)

    events = _read(p)
    assert "not a group" in events["PreToolUse"]
    commands = [h["command"] for g in events["PreToolUse"]
                if isinstance(g, dict) for h in g["hooks"]]
    assert commands == ["sh /home/k/.blink/blink-hook.sh PreToolUse codex"]


def test_install_refuses_a_file_it_cannot_parse(tmp_path):
    """The judgement call, stated: refuse and change nothing.

    A hooks file that does not parse is usually a file someone is halfway
    through editing, and it belongs to another vendor's tool. Repairing it
    means writing our idea of it over theirs. install_statusline has refused
    on this exact ground since it was written; this follows it.
    """
    p = tmp_path / "hooks.json"
    before = '{"hooks": {"PreToolUse": [oops'
    p.write_text(before, encoding="utf-8")

    with pytest.raises(SettingsUnreadable):
        ich.install(str(p), SHIM)
    assert p.read_text(encoding="utf-8") == before, \
        "an unparseable file must come out byte-identical"


def test_install_refuses_a_hooks_key_that_is_not_an_object(tmp_path):
    p = tmp_path / "hooks.json"
    payload = ({"hooks": []} if ich._EVENTS_KEY else {"PreToolUse": "nope"})
    p.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(SettingsUnreadable):
        ich.install(str(p), SHIM)


def test_install_refuses_an_event_that_is_not_a_list(tmp_path):
    """Refusal on the tenth event must be as clean as on the first: the file
    is written once, at the end, so nothing has reached disk yet."""
    p = tmp_path / "hooks.json"
    _write(p, {"SubagentStop": {"not": "a list"}})
    before = p.read_text(encoding="utf-8")

    with pytest.raises(SettingsUnreadable):
        ich.install(str(p), SHIM)
    assert p.read_text(encoding="utf-8") == before


def test_install_refuses_a_file_it_cannot_open(tmp_path):
    """A hooks.json that is a directory, or owned by someone else, is not a
    parse failure -- and unconverted it escapes `blink install` as a traceback
    about a file the user never asked us to touch."""
    p = tmp_path / "hooks.json"
    p.mkdir()

    with pytest.raises(SettingsUnreadable):
        ich.install(str(p), SHIM)


def test_the_marker_records_what_was_written(tmp_path):
    """Uninstall matches on the marker rather than on the command text, so a
    customer hook that merely mentions our filename is never deleted."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    recorded = ich._read_marker()
    assert "sh /home/k/.blink/blink-hook.sh Stop codex" in recorded


def test_a_marker_that_is_not_utf8_is_no_marker(tmp_path):
    """UnicodeDecodeError is a ValueError, not an OSError. A marker truncated
    by a power cut must read as absent, not take the install down with it."""
    os.makedirs(os.path.dirname(ich._marker_path()), exist_ok=True)
    with open(ich._marker_path(), "wb") as f:
        f.write(b"sh /home/k/.blink/blink-hook.sh Stop \xff\xfe codex\n")

    assert ich._read_marker() == set()
    p = tmp_path / "hooks.json"
    assert "installed" in ich.install(str(p), SHIM)


def test_an_unwritable_marker_does_not_fail_a_written_file(tmp_path,
                                                           monkeypatch):
    """The hooks file is written first. Reporting failure afterwards, over the
    marker, would send someone looking in the wrong file for a change that did
    land."""
    def boom(_commands):
        raise OSError("read-only home")

    monkeypatch.setattr(ich, "_write_marker", boom)
    p = tmp_path / "hooks.json"

    assert "installed" in ich.install(str(p), SHIM)
    assert "PermissionRequest" in _read(p)


def test_codex_home_honours_the_environment(monkeypatch, tmp_path):
    """Codex itself honours CODEX_HOME, and codex_cli.sessions_root already
    does. Writing to ~/.codex on a machine that redirects it would register a
    hook nothing ever reads."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "elsewhere"))
    assert ich.codex_home() == str(tmp_path / "elsewhere")
    assert ich.hooks_file().startswith(str(tmp_path / "elsewhere"))


def test_the_hooks_file_sits_directly_in_codex_home(monkeypatch, tmp_path):
    """Verified in both directions on 0.150.0: $CODEX_HOME/hooks.json fires,
    $CODEX_HOME/hooks/hooks.json is silently ignored. `hooks/hooks.json` is
    the plugin loader's path and it is in the binary, which is exactly how
    this got written down wrong the first time."""
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert ich.hooks_file() == str(tmp_path / "hooks.json")
