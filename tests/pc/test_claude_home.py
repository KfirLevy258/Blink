"""One record of where Claude Code's files are (pc/claude_home).

Until 2026-11-04 every module worked out Claude Code's folders for itself:
cli.settings_path() was ~/.claude/settings.json whatever CLAUDE_CONFIG_DIR
said, while burn_reader and claude_plan honoured the variable as the daemon's
environment had it -- which, under launchd or systemd, is never. On a desk
that exports CLAUDE_CONFIG_DIR in .zshrc, install wrote the status line where
Claude Code never looks (docs/open-bugs/ultra-burn-followups.md). The record
fixes the folder at `blink install`, and every reader asks it.

The WSL kind is built by hand here: nothing writes one yet. Its paths are
Windows UNC strings, which on macOS are only strings, and nothing here opens
one.
"""
import json
import os

import pytest

from pc import (burn_reader, claude_home, claude_plan, cli, install_hooks,
                install_statusline, statusline_source, user_config)
from pc.providers import claude_state

GUID = "{5C3D2B1A-0000-4000-8000-00000000ABCD}"
UNC = "\\\\?\\UNC\\wsl.localhost\\Ubuntu"


def _wsl(config_dir_set=False):
    cfg = (UNC + "\\srv\\cc" if config_dir_set
           else UNC + "\\home\\ana\\.claude")
    return claude_home.wsl(distro_guid=GUID, distro_name="Ubuntu",
                           linux_user="ana", linux_home="/home/ana",
                           config_dir=cfg, config_dir_set=config_dir_set)


def _j(tmp_path, *parts):
    return os.path.join(str(tmp_path), *parts)


def test_native_paths_are_unchanged(tmp_path):
    """Every path the record now answers, spelled out for a plain HOME, so
    that threading the record through changed none of them."""
    h = claude_home.native()
    assert h.kind == "native"
    assert h.config_dir == _j(tmp_path, ".claude")
    assert h.config_dir_set is False
    assert h.transcripts == _j(tmp_path, ".claude", "projects")
    assert h.drop_dir == _j(tmp_path, ".blink")
    assert h.state_dir == _j(tmp_path, ".blink", "state")
    assert h.marker_dir == _j(tmp_path, ".blink")
    assert (h.distro_guid, h.distro_name, h.linux_user, h.linux_home) == (
        None, None, None, None)
    assert claude_home.current() == h

    assert cli.settings_path() == _j(tmp_path, ".claude", "settings.json")
    assert cli.shim_path() == _j(tmp_path, ".blink", "blink-statusline.sh")
    assert cli.hook_shim_path() == _j(tmp_path, ".blink", "blink-hook.sh")
    assert statusline_source.payload_path() == _j(tmp_path, ".blink",
                                                  "statusline.json")
    assert burn_reader.transcripts_root() == _j(tmp_path, ".claude",
                                                "projects")
    assert claude_plan.config_path() == _j(tmp_path, ".claude.json")
    assert install_statusline._marker_path() == _j(
        tmp_path, ".blink", "statusline-installed-command")
    assert install_statusline._chain_path() == _j(
        tmp_path, ".blink", "statusline-chain")
    assert install_hooks._marker_path() == _j(
        tmp_path, ".blink", "hooks-installed-commands")
    assert claude_state.ClaudeStateProvider(sweep=False).path() == _j(
        tmp_path, ".blink", "state")


def test_native_without_a_record_follows_the_environment(tmp_path,
                                                         monkeypatch):
    """No install has recorded anything (a desk installed by an earlier
    release): CLAUDE_CONFIG_DIR as this process sees it, as before."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", _j(tmp_path, "work"))
    h = claude_home.native()
    assert (h.config_dir, h.config_dir_set) == (_j(tmp_path, "work"), True)
    assert h.transcripts == _j(tmp_path, "work", "projects")
    assert claude_plan.config_path() == _j(tmp_path, "work", ".claude.json")
    assert cli.settings_path() == _j(tmp_path, "work", "settings.json")


def test_native_config_dir_is_the_recorded_one(tmp_path, monkeypatch,
                                               capsys):
    """Install from a shell that exports CLAUDE_CONFIG_DIR, then read as
    the login service does -- without the variable. The service follows
    the recorded folder: settings, transcripts, config, and the drift
    check."""
    work = _j(tmp_path, "work")
    os.makedirs(work)
    with open(os.path.join(work, "settings.json"), "w") as f:
        json.dump({"model": "opus"}, f)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", work)
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert user_config.load(cli.blink_home())["claude_home"] == {
        "kind": "native", "config_dir": work, "config_dir_set": True}
    assert user_config.path(cli.blink_home()) in out

    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    h = claude_home.native()
    assert (h.config_dir, h.config_dir_set) == (work, True)
    assert claude_home.current() == h
    assert cli.settings_path() == os.path.join(work, "settings.json")
    assert burn_reader.transcripts_root() == os.path.join(work, "projects")
    assert claude_plan.config_path() == os.path.join(work, ".claude.json")

    with open(os.path.join(work, "settings.json")) as f:
        doc = json.load(f)
    assert doc["statusLine"]["command"] == (
        install_statusline.statusline_command(cli.shim_path()))
    assert "hooks" in doc and doc["model"] == "opus"
    # Where Claude Code does not look with the variable set: untouched.
    assert not os.path.exists(_j(tmp_path, ".claude", "settings.json"))

    # The daemon's watchdog, built as claude_usage_bridge builds it, checks
    # the recorded folder.
    del doc["statusLine"]
    with open(os.path.join(work, "settings.json"), "w") as f:
        json.dump(doc, f)
    msg = cli.drift_watchdog().tick()
    assert msg and "restored" in msg
    with open(os.path.join(work, "settings.json")) as f:
        assert "statusLine" in json.load(f)


def test_a_moved_config_dir_takes_blink_out_of_the_old_settings(
        tmp_path, monkeypatch):
    """Installed once into ~/.claude, then again with CLAUDE_CONFIG_DIR:
    the old file gets its own status line back and loses our hooks, and
    uninstall leaves nothing of ours in either."""
    legacy = _j(tmp_path, ".claude", "settings.json")
    os.makedirs(os.path.dirname(legacy))
    mine = {"type": "command", "command": "sh ~/my-bar.sh"}
    with open(legacy, "w") as f:
        json.dump({"statusLine": mine, "model": "opus"}, f)
    assert cli.main(["install"]) == 0

    work = _j(tmp_path, "work")
    os.makedirs(work)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", work)
    assert cli.main(["install"]) == 0
    with open(legacy) as f:
        old = json.load(f)
    assert old == {"statusLine": mine, "model": "opus"}
    with open(os.path.join(work, "settings.json")) as f:
        new = json.load(f)
    assert new["statusLine"]["command"] == (
        install_statusline.statusline_command(cli.shim_path()))

    assert cli.main(["uninstall"]) == 0
    with open(os.path.join(work, "settings.json")) as f:
        assert json.load(f) == {}
    with open(legacy) as f:
        assert json.load(f) == {"statusLine": mine, "model": "opus"}


def test_uninstall_cleans_the_old_settings_entry(tmp_path, monkeypatch):
    """An entry left in ~/.claude/settings.json after the folder moved (a
    hand-copied one, or a migration that could not run) is removed too."""
    work = _j(tmp_path, "work")
    os.makedirs(work)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", work)
    assert cli.main(["install"]) == 0
    legacy = _j(tmp_path, ".claude", "settings.json")
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    shim = cli.shim_path()
    with open(legacy, "w") as f:
        json.dump({"statusLine": {
            "type": "command",
            "command": install_statusline.statusline_command(shim)},
            "hooks": {"Stop": [{"hooks": [{
                "type": "command",
                "command": install_hooks.hook_command(cli.hook_shim_path(),
                                                      "Stop")}]}]},
            "model": "opus"}, f)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert cli.main(["uninstall"]) == 0
    with open(legacy) as f:
        assert json.load(f) == {"model": "opus"}
    with open(os.path.join(work, "settings.json")) as f:
        assert json.load(f) == {}


def test_wsl_home_paths(tmp_path):
    h = _wsl()
    assert h.kind == "wsl"
    assert cli.settings_path(h) == UNC + "\\home\\ana\\.claude\\settings.json"
    assert h.transcripts == UNC + "\\home\\ana\\.claude\\projects"
    assert burn_reader.transcripts_root(h) == h.transcripts
    assert h.drop_dir == _j(tmp_path, ".blink",
                            "wsl-5c3d2b1a-0000-4000-8000-00000000abcd")
    assert h.state_dir == os.path.join(h.drop_dir, "state")
    assert h.marker_dir == h.drop_dir
    assert statusline_source.payload_path(h) == os.path.join(
        h.drop_dir, "statusline.json")
    assert claude_state.ClaudeStateProvider(
        path=h.state_dir, sweep=False).path() == h.state_dir
    # The shims live inside the distribution.
    assert cli.shim_path(h) == UNC + "\\home\\ana\\.blink\\blink-statusline.sh"
    assert cli.hook_shim_path(h) == UNC + "\\home\\ana\\.blink\\blink-hook.sh"
    # .claude.json sits in the Linux home when CLAUDE_CONFIG_DIR did not
    # choose the folder, and in the folder when it did.
    assert claude_plan.config_path(h) == UNC + "\\home\\ana\\.claude.json"
    assert claude_plan.config_path(_wsl(True)) == UNC + "\\srv\\cc\\.claude.json"


def test_a_wsl_record_round_trips(tmp_path):
    h = _wsl()
    claude_home.save(h)
    rec = user_config.load(cli.blink_home())["claude_home"]
    assert rec == {"kind": "wsl", "config_dir": h.config_dir,
                   "config_dir_set": False, "distro_guid": GUID,
                   "distro_name": "Ubuntu", "linux_user": "ana",
                   "linux_home": "/home/ana"}
    assert claude_home.current() == h
    # The native home is still the native one, not the distribution's.
    assert claude_home.native().config_dir == _j(tmp_path, ".claude")


def test_wsl_and_native_markers_are_separate(tmp_path):
    native, wsl = claude_home.native(), _wsl()
    assert (install_statusline._marker_path(native)
            != install_statusline._marker_path(wsl))
    assert install_hooks._marker_path(native) != install_hooks._marker_path(wsl)
    assert (install_statusline._chain_path(native)
            != install_statusline._chain_path(wsl))

    install_statusline._write_marker("sh x", home=wsl)
    install_hooks._write_marker({"sh y Stop"}, home=wsl)
    assert install_statusline._read_marker(native) == ""
    assert install_hooks._read_marker(native) == set()
    assert install_statusline._read_marker(wsl) == "sh x"
    assert install_hooks._read_marker(wsl) == {"sh y Stop"}

    # A WSL install's marker does not wake the native drift check.
    settings = _j(tmp_path, "settings.json")
    with open(settings, "w") as f:
        json.dump({}, f)
    assert install_statusline.drift_check(settings, cli.shim_path()) is None
    with open(settings) as f:
        assert json.load(f) == {}


def test_unreadable_user_config_keeps_the_home(tmp_path):
    work = _j(tmp_path, "work")
    claude_home.save(claude_home.from_environ({"CLAUDE_CONFIG_DIR": work}))
    assert claude_home.current().config_dir == work
    with open(user_config.path(cli.blink_home()), "w") as f:
        f.write("{half an edit")
    assert claude_home.current().config_dir == work
    assert claude_home.native().config_dir == work
    assert cli.settings_path() == os.path.join(work, "settings.json")


@pytest.mark.parametrize("rec", [
    "a string", {"kind": "native"}, {"kind": "native", "config_dir": 5},
    {"kind": "wsl", "config_dir": "x", "config_dir_set": False},
    {"kind": "other", "config_dir": "x", "config_dir_set": False},
])
def test_a_malformed_record_is_no_record(tmp_path, rec):
    user_config.put(cli.blink_home(), "claude_home", rec)
    assert claude_home.current() == claude_home.from_environ({})


@pytest.mark.parametrize("second", [None, "other"], ids=["to-home", "to-Y"])
def test_a_moved_record_takes_blink_out_of_the_recorded_folder(
        tmp_path, monkeypatch, second):
    """Recorded X, then installed again without the variable (or with Y):
    X's settings.json gets its own status line back and loses our hooks,
    and uninstall leaves nothing of Blink's in either file (review of
    b3abec4: only ~/.claude was ever cleaned, so X kept hooks pointing at
    shims uninstall deleted, and its status line was lost to the chain)."""
    x = _j(tmp_path, "x")
    os.makedirs(x)
    mine = {"type": "command", "command": "sh ~/x-bar.sh"}
    with open(os.path.join(x, "settings.json"), "w") as f:
        json.dump({"statusLine": mine}, f)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", x)
    assert cli.main(["install"]) == 0

    if second:
        y = _j(tmp_path, second)
        os.makedirs(y)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", y)
        new = os.path.join(y, "settings.json")
    else:
        monkeypatch.delenv("CLAUDE_CONFIG_DIR")
        new = _j(tmp_path, ".claude", "settings.json")
    assert cli.main(["install"]) == 0
    with open(os.path.join(x, "settings.json")) as f:
        assert json.load(f) == {"statusLine": mine}
    with open(new) as f:
        assert f.read().count("blink-statusline.sh") == 1

    assert cli.main(["uninstall"]) == 0
    for p in (os.path.join(x, "settings.json"), new):
        with open(p) as f:
            assert "blink" not in f.read()


def test_an_unparseable_config_json_is_not_said_to_be_changed(tmp_path,
                                                              capsys):
    os.makedirs(cli.blink_home())
    with open(user_config.path(cli.blink_home()), "w") as f:
        f.write("{half an edit")
    cli._announce()
    out = capsys.readouterr().out
    assert f"Changes    {user_config.path(cli.blink_home())}" not in out
    assert "does not parse" in out


def _legacy_desk(tmp_path, monkeypatch):
    """An earlier release's install in ~/.claude, and a shell whose
    CLAUDE_CONFIG_DIR names another folder Y with a status line of its
    own: (legacy settings text, Y's settings path, Y's text)."""
    from pc import install_hooks, install_statusline
    legacy = claude_home.legacy_settings_path()
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    with open(legacy, "w") as f:
        json.dump({"statusLine": {"type": "command",
                                  "command": "legacy-bar"}}, f)
    os.makedirs(cli.blink_home(), exist_ok=True)
    install_statusline.install(legacy, cli.shim_path())
    install_hooks.install(legacy, cli.hook_shim_path())
    y = _j(tmp_path, "work-claude")
    os.makedirs(y)
    ys = os.path.join(y, "settings.json")
    with open(ys, "w") as f:
        json.dump({"statusLine": {"type": "command", "command": "y-bar"}}, f)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", y)
    with open(legacy) as f, open(ys) as g:
        return f.read(), ys, g.read()


def test_install_stops_when_the_folder_it_needs_to_record_cannot_be(
        tmp_path, monkeypatch, capsys):
    """U23: with no record, the service guards ~/.claude, and only a
    record moves it to Y. A config.json that does not parse cannot take
    one, and install used to go on: it moved Blink out of ~/.claude into
    Y, exited 0, and the service's first watchdog tick reinstalled into
    ~/.claude over the chain file, losing Y's own status line. It stops
    before anything is written now, and says why."""
    legacy_text, ys, y_text = _legacy_desk(tmp_path, monkeypatch)
    config = user_config.path(cli.blink_home())
    with open(config, "w") as f:
        f.write('{"status_page": tru')
    assert cli.main(["install"]) == 1
    out = capsys.readouterr().out
    assert "Blink setup stopped." in out and config in out
    assert "Nothing was changed." in out
    with open(claude_home.legacy_settings_path()) as f:
        assert f.read() == legacy_text
    with open(ys) as f:
        assert f.read() == y_text


def test_an_unparseable_config_json_does_not_stop_a_default_install(
        tmp_path, capsys):
    """With no CLAUDE_CONFIG_DIR the folder is the one an unrecorded
    service reads anyway, so the record costs nothing and install goes on,
    as the disclosure's "Leaves" paragraph says."""
    os.makedirs(cli.blink_home())
    with open(user_config.path(cli.blink_home()), "w") as f:
        f.write("{half an edit")
    assert cli.main(["install"]) == 0
    with open(claude_home.legacy_settings_path()) as f:
        assert "blink-statusline.sh" in f.read()


def test_install_stops_when_the_record_will_not_save(tmp_path, monkeypatch,
                                                     capsys):
    """U23's other half: a save that fails at the write (an OSError) is
    not foreseen by the parse check, and stops the install there, before
    Blink is moved out of the folder the service still reads."""
    legacy_text, ys, y_text = _legacy_desk(tmp_path, monkeypatch)

    def fails(home):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(claude_home, "save", fails)
    assert cli.main(["install"]) == 1
    out = capsys.readouterr().out
    assert "Blink setup stopped." in out
    assert "Done." not in out
    with open(claude_home.legacy_settings_path()) as f:
        assert f.read() == legacy_text
    with open(ys) as f:
        assert f.read() == y_text


def test_uninstall_takes_old_hook_spellings_out_of_the_legacy_file(
        tmp_path, monkeypatch):
    """U37: no record, CLAUDE_CONFIG_DIR naming Y, and ~/.claude holding
    hooks an early release wrote, in a spelling only the marker knows
    (Windows 1.0.0 to 1.0.2). Step 3 took Y's hooks out and removed the
    marker, and only then was ~/.claude looked at -- by then nothing said
    those ten were ours, and they stayed, running a deleted script."""
    from pc import install_hooks
    legacy = claude_home.legacy_settings_path()
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    old = {ev: f"sh /old/blink-hook.sh {ev}"
           for ev, _ in install_hooks.HOOK_EVENTS}
    with open(legacy, "w") as f:
        json.dump({"hooks": {ev: [{"hooks": [{"type": "command",
                                              "command": c}]}]
                             for ev, c in old.items()}}, f)
    marker = install_hooks._marker_path()
    os.makedirs(os.path.dirname(marker), exist_ok=True)
    with open(marker, "w", encoding="utf-8") as f:
        f.write("\n".join(old.values()) + "\n")
    y = _j(tmp_path, "work-claude")
    os.makedirs(y)
    with open(os.path.join(y, "settings.json"), "w") as f:
        f.write("{}")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", y)
    cli.main(["uninstall"])
    with open(legacy) as f:
        assert "blink-hook.sh" not in f.read()


def test_one_unc_builder():
    """pc/wsl and pc/claude_home spell a path into a distribution the same
    way, because they are the same function: two builders had drifted on a
    trailing slash (final branch review, finding 10)."""
    from pc import claude_home, wsl
    assert wsl.unc is claude_home.unc
    assert claude_home.unc("U", "/home/a/") == \
        "\\\\?\\UNC\\wsl.localhost\\U\\home\\a"
    assert claude_home.unc("U", "/") == "\\\\?\\UNC\\wsl.localhost\\U"
    assert claude_home.unc("U", "/x", long_paths=False) == "\\\\wsl$\\U\\x"


def test_a_desk_with_no_record_guards_the_legacy_settings(tmp_path,
                                                          monkeypatch):
    """Finding 12: a desk installed by an earlier release has no record,
    and its install wrote ~/.claude/settings.json whatever
    CLAUDE_CONFIG_DIR said. The daemon's watchdog guards that file, not
    the one the service's own CLAUDE_CONFIG_DIR would name (the Windows
    task does carry the account's environment); only a record moves it."""
    from pc import cli, install_statusline
    elsewhere = str(tmp_path / "service-env")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", elsewhere)
    assert claude_home.recorded() is None
    dog = cli.drift_watchdog(claude_home.current())
    assert dog._settings == claude_home.legacy_settings_path()
    assert dog._home.kind == claude_home.NATIVE

    # A record does move it.
    claude_home.save(claude_home.from_environ())
    dog = cli.drift_watchdog(claude_home.current())
    assert dog._settings == os.path.join(elsewhere, "settings.json")


@pytest.fixture(autouse=True)
def _no_recapture_wait(monkeypatch):
    """`blink uninstall` waits cli.HOOK_RECAPTURE_S between taking the
    hooks out and removing the script they run; not here."""
    from pc import cli as _cli
    monkeypatch.setattr(_cli, "HOOK_RECAPTURE_S", 0)


# ------------------- review of the WSL work, 2026-11-05: A5-6, B-1, A5-5

def _install_into(work):
    """A native install recorded for folder `work`, as `blink install`
    leaves it: the record, the shims, the markers, the status line."""
    home = claude_home._native(work, True)
    claude_home.save(home)
    cli._write_shim(cli.shim_path(home), "blink-statusline.sh")
    cli._write_shim(cli.hook_shim_path(home), "blink-hook.sh")
    os.makedirs(work, exist_ok=True)
    install_statusline.install(cli.settings_path(home), cli.shim_path(home),
                               home=home)
    return home


def _status_line(work):
    try:
        with open(os.path.join(work, "settings.json")) as f:
            return json.load(f).get("statusLine")
    except FileNotFoundError:
        return None


def test_the_daemon_watchdog_follows_a_record_that_moves(tmp_path):
    """A native install moving the record from X to Y left the running
    daemon's watchdog guarding X (it was built once, at start): its next
    tick put Blink back into X and overwrote the shared chain file. The
    daemon's watchdog reads the record at every tick and guards only the
    folder it names now."""
    x, y = _j(tmp_path, "x"), _j(tmp_path, "y")
    hx = _install_into(x)
    dog = cli.daemon_watchdog()
    assert dog.tick() is None                    # guarding X, all well
    # install moves to Y: X's status line goes back to the customer's.
    with open(os.path.join(x, "settings.json"), "w") as f:
        json.dump({"statusLine": {"type": "command", "command": "mine"}}, f)
    _install_into(y)
    del hx
    # Y is the one it now repairs, at once: a new folder's first check is
    # due on the tick that first sees it.
    with open(os.path.join(y, "settings.json"), "w") as f:
        json.dump({}, f)
    msg = dog.tick()
    assert msg and "restored" in msg
    assert "blink-statusline.sh" in _status_line(y)["command"]
    assert _status_line(x) == {"type": "command", "command": "mine"}


@pytest.mark.parametrize("damage", ["garbage", "unknown-kind"])
def test_an_unusable_record_guards_nothing(tmp_path, damage):
    """config.json exists but does not parse, or names a kind this release
    does not know (a hand edit, a downgrade): the watchdog guarded the
    legacy ~/.claude/settings.json, and its first tick installed Blink
    there and overwrote the chain file with that folder's command. Now it
    guards nothing until the record reads again, and then the recorded
    folder only. Only a desk with no record at all is a pre-record install,
    whose status line is in ~/.claude."""
    work = _j(tmp_path, "work")
    _install_into(work)
    legacy = claude_home.legacy_settings_path()
    os.makedirs(os.path.dirname(legacy))
    with open(legacy, "w") as f:
        json.dump({"statusLine": {"type": "command", "command": "origX"}}, f)
    path = user_config.path(cli.blink_home())
    with open(path) as f:
        good = f.read()
    claude_home._last.clear()                    # a new process
    with open(path, "w") as f:
        if damage == "garbage":
            f.write("{not json")
        else:
            json.dump({"claude_home": {"kind": "later", "config_dir": "q",
                                       "config_dir_set": False}}, f)
    dog = cli.daemon_watchdog()
    assert dog.tick() is None
    with open(legacy) as f:
        assert json.load(f) == {"statusLine": {"type": "command",
                                               "command": "origX"}}
    # Readable again: the recorded folder is guarded.
    with open(path, "w") as f:
        f.write(good)
    with open(os.path.join(work, "settings.json"), "w") as f:
        json.dump({}, f)
    msg = dog.tick()
    assert msg and "restored" in msg
    assert "blink-statusline.sh" in _status_line(work)["command"]
    with open(legacy) as f:
        assert json.load(f)["statusLine"]["command"] == "origX"


def test_record_state_tells_no_record_from_an_unusable_one(tmp_path):
    assert claude_home.record_state() == (claude_home.NO_RECORD, None)
    user_config.put(cli.blink_home(), "status-page", True)
    assert claude_home.record_state() == (claude_home.NO_RECORD, None)
    with open(user_config.path(cli.blink_home()), "w") as f:
        f.write("{")
    # The last good answer, within a process; a new one cannot know.
    assert claude_home.record_state() == (claude_home.NO_RECORD, None)
    claude_home._last.clear()
    assert claude_home.record_state() == (claude_home.UNKNOWN, None)
    home = claude_home.from_environ()
    with open(user_config.path(cli.blink_home()), "w") as f:
        json.dump({"claude_home": claude_home.to_record(home)}, f)
    assert claude_home.record_state() == (claude_home.RECORDED, home)


def _codex_marker(present):
    p = os.path.expanduser("~/.blink/codex-hooks-installed-commands")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    if present:
        with open(p, "w") as f:
            f.write("bash x codex\n")
    elif os.path.exists(p):
        os.remove(p)


@pytest.mark.parametrize("fault", ["stale", "deleted"])
def test_a_wsl_desk_still_keeps_the_codex_hook_shim(tmp_path, fault):
    """On a WSL desk the Claude settings are inside the distribution and
    checked by pc/wsl_watch, so the daemon's watchdog did nothing at all.
    But Codex on Windows runs the native ~/.blink/blink-hook.sh, and a
    self-update never rewrites shims: it stayed at the old release, and a
    deleted one stayed deleted. The native check now runs for that shim,
    while Codex's hooks are registered."""
    claude_home.save(_wsl())
    shim = cli.hook_shim_path()
    os.makedirs(os.path.dirname(shim), exist_ok=True)
    if fault == "stale":
        with open(shim, "w") as f:
            f.write("# an older release\n")
    _codex_marker(True)
    dog = cli.daemon_watchdog()
    msg = dog.tick()
    assert msg and "blink-hook.sh" in msg
    assert cli.shim_is_current(shim, "blink-hook.sh")
    # Never the Claude settings or shims on Windows: those are WSL's.
    assert not os.path.exists(_j(tmp_path, ".claude", "settings.json"))
    assert not os.path.exists(cli.shim_path())


def test_a_wsl_desk_without_codex_hooks_writes_no_shim(tmp_path):
    claude_home.save(_wsl())
    _codex_marker(False)
    assert cli.daemon_watchdog().tick() is None
    assert not os.path.exists(cli.hook_shim_path())


class _Service(cli._Backend):
    """A backend that records what install asks of the service, and when
    the record changed relative to it. The rest is _Backend's."""

    def __init__(self, calls):
        self.calls = calls

    def _note(self, what):
        rec = claude_home.recorded()
        self.calls.append((what, None if rec is None else rec.config_dir))

    def stop(self, runner=None):
        self._note("stop")
        return "stopped"

    def start(self, runner=None):
        self._note("start")
        return "started"

    def install(self):
        self._note("install")
        return "running (launchd)"

    def halt(self):
        self._note("halt")

    def restart(self):
        self._note("restart")
        return "restarted"


def _with_service(monkeypatch):
    calls = []
    svc = _Service(calls)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: svc)
    return calls


def test_a_record_move_stops_the_service_first_and_starts_it_after(
        tmp_path, monkeypatch):
    """A5-6/B-2: a native install that moves the record left the running
    daemon up while the folders changed under it; its watchdog, guarding
    the old folder, could reinstall there before the restart (on Linux,
    whose `enable --now` leaves a running daemon running, every time). The
    service is stopped before the record changes, on every platform, and
    the service step starts it again on the new record."""
    x, y = _j(tmp_path, "x"), _j(tmp_path, "y")
    os.makedirs(x)
    os.makedirs(y)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", x)
    calls = _with_service(monkeypatch)
    assert cli.main(["install"]) == 0
    calls.clear()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", y)
    assert cli.main(["install"]) == 0
    names = [c[0] for c in calls]
    assert names.index("stop") < names.index("install")
    assert calls[names.index("stop")] == ("stop", x)
    assert calls[names.index("install")] == ("install", y)


def test_a_reinstall_into_the_same_folder_does_not_stop_the_service(
        tmp_path, monkeypatch):
    calls = _with_service(monkeypatch)
    assert cli.main(["install"]) == 0
    calls.clear()
    assert cli.main(["install"]) == 0
    assert "stop" not in [c[0] for c in calls]


def test_a_record_move_that_fails_starts_the_service_again(tmp_path,
                                                           monkeypatch):
    """Stopped for the move, and the record then could not be saved: the
    service is started again, reading what it read before."""
    x, y = _j(tmp_path, "x"), _j(tmp_path, "y")
    os.makedirs(x)
    os.makedirs(y)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", x)
    calls = _with_service(monkeypatch)
    assert cli.main(["install"]) == 0
    calls.clear()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", y)

    def refuse(home):
        raise OSError("read-only")
    monkeypatch.setattr(claude_home, "save", refuse)
    assert cli.main(["install"]) == 1
    names = [c[0] for c in calls]
    assert names[-2:] == ["stop", "start"] or names[-1] == "start"
    assert "stop" in names and names.index("stop") < names.index("start")
