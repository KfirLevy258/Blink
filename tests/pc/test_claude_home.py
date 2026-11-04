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
