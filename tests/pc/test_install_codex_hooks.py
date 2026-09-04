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


def test_uninstall_returns_the_file_to_the_shape_it_had(tmp_path):
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    msg = ich.uninstall(str(p), SHIM)

    assert json.loads(p.read_text(encoding="utf-8")) == {}
    assert "removed" in msg
    assert ich._read_marker() == set()


def test_uninstall_keeps_someone_elses_hook(tmp_path):
    """Their handler sits in a group WE created. Dropping the group to tidy
    up our own entry would take their automation with it."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    data = json.loads(p.read_text(encoding="utf-8"))
    _events(data)["PreToolUse"][0]["hooks"].append(
        {"type": "command", "command": "/usr/local/bin/audit.sh"})
    p.write_text(json.dumps(data), encoding="utf-8")

    ich.uninstall(str(p), SHIM)

    events = _read(p)
    assert [h["command"] for g in events["PreToolUse"] for h in g["hooks"]] \
        == ["/usr/local/bin/audit.sh"]


def test_uninstall_keeps_a_sibling_group_of_theirs(tmp_path):
    """Their own matcher group for the same event, alongside ours."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    data = json.loads(p.read_text(encoding="utf-8"))
    _events(data)["PreToolUse"].append(
        {"matcher": "Bash", "hooks": [
            {"type": "command", "command": "/usr/local/bin/audit.sh"}]})
    p.write_text(json.dumps(data), encoding="utf-8")

    ich.uninstall(str(p), SHIM)

    events = _read(p)
    assert len(events["PreToolUse"]) == 1
    assert events["PreToolUse"][0]["matcher"] == "Bash"


def test_uninstall_removes_a_moved_shim_by_its_marker(tmp_path):
    """The entries `blink update` left behind name an old path. The marker is
    the only thing that still proves they are ours."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), "/old/blink-hook.sh")
    ich.uninstall(str(p), "/new/blink-hook.sh")
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_uninstall_needs_no_shim_path(tmp_path):
    """`blink uninstall` may run after the shim file is already gone, so the
    path is optional and the marker carries the whole identification."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    ich.uninstall(str(p))
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_uninstall_without_a_marker_still_removes_by_the_command(tmp_path):
    """~/.blink wiped before `blink uninstall` ran. What we would write now is
    the fallback proof that the entries are ours."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    os.remove(ich._marker_path())

    ich.uninstall(str(p), SHIM)
    assert json.loads(p.read_text(encoding="utf-8")) == {}


def test_uninstall_never_deletes_a_hook_that_merely_mentions_us(tmp_path):
    """Substring matching on the command text would delete this. It is the
    customer's wrapper around our shim, not our entry."""
    p = tmp_path / "hooks.json"
    theirs = "sh /home/k/.blink/blink-hook.sh PreToolUse codex >> /var/log/x"
    _write(p, {"PreToolUse": [
        {"matcher": "*", "hooks": [{"type": "command", "command": theirs}]}]})

    msg = ich.uninstall(str(p), SHIM)

    assert _read(p)["PreToolUse"][0]["hooks"][0]["command"] == theirs
    assert "No Codex state hooks" in msg


def test_uninstall_keeps_the_rest_of_the_file(tmp_path):
    """Codex allows a top-level `description`. Emptying the event map is not
    licence to empty the document."""
    p = tmp_path / "hooks.json"
    p.write_text(json.dumps({"description": "mine"}), encoding="utf-8")
    ich.install(str(p), SHIM)

    ich.uninstall(str(p), SHIM)

    assert json.loads(p.read_text(encoding="utf-8")) == {"description": "mine"}


def test_uninstall_leaves_an_event_of_theirs_alone(tmp_path):
    """An event we register, holding only their hook: the key stays, with
    their group in it, and is not tidied into nothing."""
    p = tmp_path / "hooks.json"
    _write(p, {"Stop": [{"hooks": [
        {"type": "command", "command": "/usr/local/bin/audit.sh"}]}]})

    ich.uninstall(str(p), SHIM)

    events = _read(p)
    assert events["Stop"][0]["hooks"][0]["command"] \
        == "/usr/local/bin/audit.sh"


def test_uninstall_with_nothing_of_ours_rewrites_nothing(tmp_path):
    """Byte-identical, not merely equal: a file we take nothing out of must
    not come back reindented, reordered or renewline'd."""
    p = tmp_path / "hooks.json"
    before = '{\n    "hooks": {"Stop": []},\n        "description": "mine"}'
    p.write_text(before, encoding="utf-8")

    msg = ich.uninstall(str(p), SHIM)

    assert p.read_text(encoding="utf-8") == before
    assert "No Codex state hooks" in msg


def test_uninstall_steps_over_a_group_it_cannot_read(tmp_path):
    """A stray string where a matcher group belongs is one person's typo.
    install() steps over it, so uninstall copies it through untouched --
    stepping over is never licence to delete."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    data = json.loads(p.read_text(encoding="utf-8"))
    _events(data)["PreToolUse"].insert(0, "not a group")
    p.write_text(json.dumps(data), encoding="utf-8")

    ich.uninstall(str(p), SHIM)

    assert _read(p)["PreToolUse"] == ["not a group"]


def test_uninstall_leaves_an_unparseable_file_alone(tmp_path):
    """A destructive operation is the last place to start guessing at a file
    that is probably mid-edit."""
    p = tmp_path / "hooks.json"
    before = "{oops"
    p.write_text(before, encoding="utf-8")
    msg = ich.uninstall(str(p), SHIM)
    assert p.read_text(encoding="utf-8") == before
    assert "left it alone" in msg


def test_uninstall_refuses_a_hooks_key_that_is_not_an_object(tmp_path):
    """The shape install() refuses. Uninstall has to refuse the same one, or
    the pair is not symmetric."""
    p = tmp_path / "hooks.json"
    before = json.dumps({"hooks": []} if ich._EVENTS_KEY
                        else {"PreToolUse": "nope"})
    p.write_text(before, encoding="utf-8")

    msg = ich.uninstall(str(p), SHIM)

    assert p.read_text(encoding="utf-8") == before
    assert "left it alone" in msg


def test_uninstall_refuses_an_event_that_is_not_a_list(tmp_path):
    """And refuses it before removing anything: a bad tenth event must not
    leave the first nine stripped."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    data = json.loads(p.read_text(encoding="utf-8"))
    _events(data)["SubagentStop"] = {"not": "a list"}
    p.write_text(json.dumps(data), encoding="utf-8")
    before = p.read_text(encoding="utf-8")

    msg = ich.uninstall(str(p), SHIM)

    assert p.read_text(encoding="utf-8") == before
    assert "left it alone" in msg


def test_a_refusal_keeps_the_marker(tmp_path):
    """The marker is the only surviving proof that the entries still sitting
    in that file are ours. Dropping it on a refusal strands them forever."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    p.write_text("{oops", encoding="utf-8")

    ich.uninstall(str(p), SHIM)

    assert "sh /home/k/.blink/blink-hook.sh Stop codex" in ich._read_marker()


def test_uninstall_refuses_a_file_it_cannot_open(tmp_path):
    """A hooks.json that is a directory. Unconverted, the IsADirectoryError
    escapes `blink uninstall` as a traceback about someone else's file."""
    p = tmp_path / "hooks.json"
    p.mkdir()

    msg = ich.uninstall(str(p), SHIM)

    assert "left it alone" in msg


def test_uninstall_with_no_hooks_file_is_not_an_error(tmp_path):
    """`blink uninstall` runs this on every machine, including the many that
    never installed the Codex hook."""
    p = tmp_path / "nope.json"
    msg = ich.uninstall(str(p), SHIM)
    assert "No Codex state hooks" in msg
    assert not p.exists(), "uninstall must not create the file it removes from"


def test_uninstall_leaves_no_temp_file_behind(tmp_path):
    """Every write goes through the sibling-temp-and-rename path, and a
    machine that dies mid-uninstall must leave Codex a whole hooks file."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    ich.uninstall(str(p), SHIM)

    assert sorted(f.name for f in tmp_path.iterdir() if f.is_file()) \
        == ["hooks.json"]


def test_install_after_uninstall_is_a_clean_install(tmp_path):
    """Whatever uninstall leaves behind has to be something install can read
    -- the reinstall path is the one every `blink update` takes."""
    p = tmp_path / "hooks.json"
    ich.install(str(p), SHIM)
    ich.uninstall(str(p), SHIM)
    msg = ich.install(str(p), SHIM)

    assert "installed (10 events)" in msg
    assert len(_read(p)["PreToolUse"]) == 1


# A CODEX_HOME whose directory is readable but not writable is the shape a
# real machine takes when hooks.json was laid down under sudo, when ~/.codex
# lives on a read-only dotfiles mount, or when the volume is full. The
# permission bits are the honest reproduction; a monkeypatched _save would
# prove only that the arm exists, not that it covers the write that actually
# fails (which is os.open on the SIBLING TEMP file, not on hooks.json).
_needs_unprivileged_posix = pytest.mark.skipif(
    os.name == "nt" or os.geteuid() == 0,
    reason="directory permission bits do not deny root, and do not deny"
           " writes at all on Windows")


@_needs_unprivileged_posix
def test_uninstall_reports_a_codex_home_it_cannot_write(tmp_path):
    """uninstall promises never to raise, and `blink uninstall` leans on that
    promise: step [1/5] has already removed the login service by the time this
    runs at [4/5], and step [5/5] never happens if this throws. A machine with
    no service and all its files still in place is exactly the half-undone
    state the promise exists to prevent -- so a Codex hook we cannot remove
    has to come back as a sentence and let the rest of the uninstall run."""
    home = tmp_path / "codex"
    home.mkdir()
    p = home / "hooks.json"
    ich.install(str(p), SHIM)
    before = p.read_text(encoding="utf-8")

    home.chmod(0o500)
    try:
        msg = ich.uninstall(str(p), SHIM)
    finally:
        # Restored even on failure, or pytest cannot clear its own tmp_path.
        home.chmod(0o700)

    assert "left it alone" in msg
    assert p.read_text(encoding="utf-8") == before, \
        "a write that could not happen must not have half-happened"


@_needs_unprivileged_posix
def test_a_write_that_failed_keeps_the_marker(tmp_path):
    """Our entries are still sitting in that file, and the marker is the only
    proof they are ours once the shim path moves. Dropping it because the
    removal was attempted would strand them: the next uninstall would walk
    past its own hooks. Same reasoning as the parse-refusal path."""
    home = tmp_path / "codex"
    home.mkdir()
    p = home / "hooks.json"
    ich.install(str(p), SHIM)

    home.chmod(0o500)
    try:
        ich.uninstall(str(p), SHIM)
    finally:
        home.chmod(0o700)

    assert "sh /home/k/.blink/blink-hook.sh Stop codex" in ich._read_marker()


@_needs_unprivileged_posix
def test_install_refuses_a_codex_home_it_cannot_write(tmp_path):
    """The mirror case. cli._install_codex_hooks catches SettingsUnreadable
    and nothing else, so a raw PermissionError from the write aborts `blink
    install` at [4/5] -- before the background service is ever registered."""
    home = tmp_path / "codex"
    home.mkdir()
    home.chmod(0o500)
    try:
        with pytest.raises(SettingsUnreadable):
            ich.install(str(home / "hooks.json"), SHIM)
    finally:
        home.chmod(0o700)
