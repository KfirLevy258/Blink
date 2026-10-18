"""The setup the customer runs.

This replaces tests/test_install_sh.py. install.sh needed Python on the
customer's machine, built a virtualenv and reached PyPI at install time; the
binary needs none of that, so the shell script and its tests are gone.

Tested against the module rather than the built binary: the logic is the same
either way, and freezing an 11 MB executable per assertion would buy nothing.
The binary itself is exercised end to end by tests/ci/check_install.sh, which
CI runs on a real macOS and Linux runner.
"""
import json
import os
import sys
import time

import pytest

from pc import cli, install_statusline


@pytest.fixture(autouse=True)
def _sandbox(tmp_path, monkeypatch):
    """The one thing these tests need that the whole suite does not.

    The redirected HOME and the login-service guard both live in
    tests/conftest.py now -- see the note there for what went wrong when they
    were a per-file concern.

    CODEX_HOME is cleared here for the same class of reason: install now edits
    a file under it, and Codex honours the variable, so a developer who has it
    set in their shell would have had these tests write a hooks.json into
    their real Codex install. HOME alone does not sandbox a path that an
    environment variable can override.
    """
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude").mkdir()


def _settings(tmp_path, obj=None):
    p = tmp_path / ".claude" / "settings.json"
    if obj is not None:
        p.write_text(json.dumps(obj))
    return p


def _read(tmp_path):
    return json.loads(_settings(tmp_path).read_text())


def test_install_points_the_statusline_at_the_shim(tmp_path, capsys):
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    shim = tmp_path / ".blink" / "blink-statusline.sh"
    assert shim.exists() and os.access(shim, os.X_OK)
    assert (_read(tmp_path)["statusLine"]["command"]
            == cli.install_statusline.statusline_command(str(shim)))


def test_bare_invocation_installs(tmp_path):
    """Someone who downloads a file and runs it means "set this up"."""
    _settings(tmp_path, {})
    assert cli.main([]) == 0
    assert (tmp_path / ".blink" / "blink-statusline.sh").exists()


def test_an_existing_statusline_is_kept_and_still_runs(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    chain = (tmp_path / ".blink" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/my-bar.sh"


def test_unrelated_settings_survive(tmp_path):
    _settings(tmp_path, {"model": "opus", "env": {"FOO": "bar"}})
    cli.main(["install"])
    got = _read(tmp_path)
    assert got["model"] == "opus" and got["env"] == {"FOO": "bar"}


def test_install_discloses_before_it_writes(tmp_path, capsys):
    """It asks nothing, so the disclosure is the only safeguard."""
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    out = capsys.readouterr().out
    # Split on the step marker by pattern, not the literal "[1/3]": the step
    # count changes whenever a step is added, and partition() on a separator
    # that is no longer there returns the WHOLE output as the disclosure --
    # so every assertion below passed vacuously while the real check was gone.
    import re
    m = re.search(r"^\[1/\d\]", out, re.M)
    assert m, "install printed no step markers at all"
    disclosure, rest = out[:m.start()], out[m.start():]
    assert str(_settings(tmp_path)) in disclosure
    assert "statusLine.command" in disclosure
    assert "sh ~/my-bar.sh" in disclosure
    assert "uninstall" in disclosure
    assert rest, "the disclosure must come before the work"


def test_the_disclosure_names_every_file_install_writes(tmp_path, capsys):
    """It asks nothing, so a disclosure that is merely MOSTLY right is worse
    than none -- it is the thing people rely on instead of reading the diff.

    This pins the failure that actually happened: the hooks key started being
    written while the disclosure still said 'statusLine.command, and nothing
    else in the file'.
    """
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    import re
    out = capsys.readouterr().out
    disclosure = out[:re.search(r"^\[1/\d\]", out, re.M).start()]

    assert "hooks" in disclosure, "disclosure omits the hooks key it writes"
    assert str(cli.hook_shim_path()) in disclosure, \
        "disclosure omits the hook shim it creates"
    # The hook shim records session and agent ids. The disclosure is the only
    # safeguard on an install that asks nothing, so it has to say so.
    assert "session and" in disclosure and "agent ids" in disclosure, \
        "disclosure does not mention the ids the hook shim records"
    assert "state" in disclosure, "disclosure omits the state directory"
    assert str(cli.shim_path()) in disclosure
    assert str(_settings(tmp_path)) in disclosure

    # And everything it named is a thing that now exists or was changed.
    settings = json.loads(_settings(tmp_path).read_text())
    assert "hooks" in settings
    assert os.path.exists(cli.hook_shim_path())


def test_install_is_idempotent(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    cli.main(["install"])
    chain = (tmp_path / ".blink" / "statusline-chain").read_text().strip()
    assert chain == "sh ~/my-bar.sh", "second run chained the shim to itself"


def test_uninstall_restores_their_command(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["install"])
    cli.main(["uninstall"])
    assert _read(tmp_path)["statusLine"]["command"] == "sh ~/my-bar.sh"
    assert not (tmp_path / ".blink" / "blink-statusline.sh").exists()


def test_uninstall_keeps_the_ota_signing_key(tmp_path):
    """~/.blink is shared with a key that cannot be regenerated."""
    key = tmp_path / ".blink" / "ota_signing_key_p256.pem"
    key.parent.mkdir(exist_ok=True)
    key.write_text("PRIVATE KEY")
    _settings(tmp_path, {})
    cli.main(["install"])
    cli.main(["uninstall"])
    assert key.read_text() == "PRIVATE KEY"


def test_uninstall_leaves_a_foreign_statusline_alone(tmp_path):
    _settings(tmp_path, {"statusLine": {"type": "command",
                                        "command": "sh ~/my-bar.sh"}})
    cli.main(["uninstall"])
    assert _read(tmp_path)["statusLine"]["command"] == "sh ~/my-bar.sh"


def test_install_on_a_machine_where_claude_never_wrote_settings(tmp_path):
    """~/.claude/settings.json only exists once a setting has been changed."""
    _settings(tmp_path).unlink(missing_ok=True)
    assert cli.main(["install"]) == 0
    assert "blink-statusline.sh" in _read(tmp_path)["statusLine"]["command"]


def test_status_runs_before_and_after_install(tmp_path, capsys):
    assert cli.main(["status"]) == 0
    assert "none yet" in capsys.readouterr().out
    _settings(tmp_path, {})
    cli.main(["install"])
    (tmp_path / ".blink" / "statusline.json").write_text("{}")
    assert cli.main(["status"]) == 0
    assert "fresh" in capsys.readouterr().out


def test_the_shim_it_writes_is_the_one_in_the_tree(tmp_path):
    """One source of truth, so the shipped shim cannot drift from the repo."""
    _settings(tmp_path, {})
    cli.main(["install"])
    here = os.path.dirname(os.path.dirname(os.path.abspath(cli.__file__)))
    src = open(os.path.join(here, "tools", "blink-statusline.sh")).read()
    assert (tmp_path / ".blink" / "blink-statusline.sh").read_text() == src


def test_too_old_claude_warns_rather_than_refusing(tmp_path, capsys, monkeypatch):
    """Everything installed stays correct, so it works the moment they update."""
    monkeypatch.setattr(cli, "claude_version", lambda: ("2.1.0 (Claude Code)", (2, 1, 0)))
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "needs 2.1.100 or newer" in out
    assert _read(tmp_path)["statusLine"]["command"]  # installed anyway


def test_run_does_not_hand_the_subcommand_name_to_the_daemon(monkeypatch):
    """`blink run` must not leave "run" in the daemon's own argv.

    It did: claude_usage_bridge.main() parsed sys.argv itself, saw the
    subcommand name, rejected it and exited. The login service restarted it
    every ten seconds and the board never received anything -- 866 KB of
    identical errors before anyone looked at the log.
    """
    seen = {}

    class FakeBridge:
        @staticmethod
        def main(argv=None):
            seen["argv"] = argv

    monkeypatch.setitem(__import__("sys").modules, "claude_usage_bridge", FakeBridge)
    assert cli.main(["run"]) == 0
    assert seen["argv"] is not None, "argv must be passed, not left to sys.argv"
    assert "run" not in seen["argv"]


def test_run_forwards_an_explicit_port(monkeypatch):
    seen = {}

    class FakeBridge:
        @staticmethod
        def main(argv=None):
            seen["argv"] = argv

    monkeypatch.setitem(__import__("sys").modules, "claude_usage_bridge", FakeBridge)
    cli.main(["run", "--port", "/dev/cu.usbserial-1"])
    assert "--port" in seen["argv"] and "/dev/cu.usbserial-1" in seen["argv"]


def test_uninstall_says_so_when_the_binary_will_not_go(tmp_path, monkeypatch,
                                                       capsys):
    """An uninstall that cannot finish must not report that it did.

    On Windows the daemon holds its own .exe open and `schtasks /end` returns
    before the process has exited, so the delete lost the race -- and
    rmtree(ignore_errors=True) swallowed it, leaving a 12 MB binary behind
    under a printed "removed". Six CI scenarios went red the moment the daemon
    stopped crashing on startup and lived long enough to hold the file.
    """
    cli.main(["install"])
    assert os.path.exists(cli.bin_dir())

    monkeypatch.setattr(cli.shutil, "rmtree", lambda *a, **k: None)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)   # no real waiting

    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out
    if sys.platform == "win32":
        # Windows hands the delete to a detached cmd that outlives this
        # process (_schedule_windows_cleanup), so the uninstall IS going to
        # finish and says so; exiting 1 there would be the false report.
        assert rc == 0
        assert "will be gone" in out
        return
    assert rc == 1, "a failed uninstall must not exit 0"
    assert "could not be removed" in out
    assert cli.bin_dir() in out, "it has to say which path to delete by hand"


def test_uninstall_reports_removed_when_it_worked(tmp_path, capsys):
    cli.main(["install"])
    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not be removed" not in out
    assert not os.path.exists(cli.bin_dir())


def test_install_refuses_to_touch_a_settings_file_that_will_not_parse(
        tmp_path, capsys):
    """Treating unparseable JSON as {} would write a fresh settings.json over
    whatever the customer was halfway through editing."""
    claude = tmp_path / ".claude"
    claude.mkdir(exist_ok=True)
    broken = '{"statusLine": {"command": "sh /their/bar.sh",}}'   # trailing comma
    (claude / "settings.json").write_text(broken)

    rc = cli.main(["install"])
    out = capsys.readouterr().out

    assert rc == 1
    assert "not valid JSON" in out
    assert "Nothing was changed" in out
    assert (claude / "settings.json").read_text() == broken
    assert not os.path.exists(cli.bin_dir()), "it installed anyway"


def test_uninstall_finishes_even_when_settings_will_not_parse(tmp_path, capsys):
    """The login service is removed before this step, so stopping here would
    leave the machine half-undone."""
    cli.main(["install"])
    (tmp_path / ".claude" / "settings.json").write_text("{ not json")

    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out

    assert "Left alone" in out
    assert not os.path.exists(cli.bin_dir()), "the binary survived"
    assert rc == 0


def test_status_reports_the_hooks_not_only_the_status_line(tmp_path, capsys):
    """Install writes two things into settings.json, so status must report
    two. Someone whose activity pip never lights needs a way to see whether
    the hooks are there; 'Status line installed' answers a different
    question."""
    _settings(tmp_path, {})
    cli.main(["install"])
    capsys.readouterr()
    cli.main(["status"])
    # Derived, not hardcoded: the event list grows, and a pinned "6/6" turns
    # every addition into a spurious failure in a test about something else.
    from pc import install_hooks
    n = len(install_hooks.HOOK_EVENTS)
    assert f"hooks installed ({n}/{n} events" in capsys.readouterr().out


def test_status_notices_hooks_that_went_missing(tmp_path, capsys):
    """Counted from settings.json, not from the shim existing on disk: the
    file being there proves an install ran once, not that Claude Code is
    still configured to call it."""
    _settings(tmp_path, {})
    cli.main(["install"])
    from pc import install_hooks
    n = len(install_hooks.HOOK_EVENTS)
    data = json.loads(_settings(tmp_path).read_text())
    del data["hooks"]["PreToolUse"]
    del data["hooks"]["Stop"]
    _settings(tmp_path).write_text(json.dumps(data))
    capsys.readouterr()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert f"PARTIAL -- {n - 2}/{n}" in out
    assert "install` to restore them" in out


def test_status_does_not_call_a_missing_hook_shim_out_of_date(tmp_path, capsys):
    """Absent and stale are different faults and want different sentences.

    The hooks in settings.json still point at the shim path, so Claude Code
    runs a file that is not there on every event and the pip never lights at
    all. "Out of date" sends the reader hunting for a version mismatch in a
    file they will not find.
    """
    _settings(tmp_path, {})
    cli.main(["install"])
    os.remove(cli.hook_shim_path())
    capsys.readouterr()
    cli.main(["status"])

    out = capsys.readouterr().out
    assert "the activity hook shim is missing" in out
    assert "out of date" not in out
    # What went wrong AND what to do about it: the daemon rewrites a missing
    # shim exactly as it rewrites a stale one, so the promise still holds.
    assert "the hooks run nothing --" in out


def test_status_still_says_out_of_date_for_a_shim_that_is_merely_stale(
        tmp_path, capsys):
    """The other half of the same branch: a file that exists and is wrong."""
    _settings(tmp_path, {})
    cli.main(["install"])
    with open(cli.hook_shim_path(), "w") as f:
        f.write("#!/bin/sh\n# an older install left this here\n")
    capsys.readouterr()
    cli.main(["status"])

    out = capsys.readouterr().out
    assert "the activity hook shim is out of date" in out
    assert "missing" not in out


def test_live_sessions_counts_real_sessions_not_zero(tmp_path):
    """_live_sessions() unpacks ClaudeStateProvider.scan()'s return tuple by
    hand. scan() gained a third value (names, for the session-name hint) and
    the bare `except Exception: return 0` around this call means a stale
    2-value unpack does not crash -- it just silently reports zero live
    sessions forever, on every machine, whether or not hooks are installed.
    Nothing else in this suite calls _live_sessions(), so this is the only
    test that would have caught that regression."""
    state_dir = tmp_path / ".blink" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "s1.state").write_text(
        json.dumps({"event": "PreToolUse", "t": time.time()}))
    (state_dir / "s2.state").write_text(
        json.dumps({"event": "Notification", "t": time.time()}))
    assert cli._live_sessions() == 2


def test_status_says_so_when_no_hooks_are_installed(tmp_path, capsys):
    _settings(tmp_path, {"statusLine": {"type": "command", "command": "x"}})
    cli.main(["status"])
    assert "hooks not installed" in capsys.readouterr().out


# --- Claude Code absent ----------------------------------------------------
#
# "Absent" is no `claude` on PATH. The status line installed by step 2 needs
# the command-line Claude Code to render; the hooks from step 3 also run in
# Claude Desktop's Code tab. These tests are about the install note and
# `blink status` saying which half this machine gets -- and they ask WHICH
# list an item is in, not whether a word appears anywhere. Substring tests
# kept passing with the activity light moved back under Missing (review,
# 2026-09-30).


def _no_cli(monkeypatch, desktop=True, codex=False, desktop_installed=None):
    """A machine with no command-line Claude Code. claude_on_path is patched
    too: claude_version fails the same way for a broken install that IS on
    PATH, and a developer's own `claude` would otherwise leak in."""
    monkeypatch.setattr(cli, "claude_version", lambda: (None, None))
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    monkeypatch.setattr(cli, "desktop_app_present", lambda: desktop)
    monkeypatch.setattr(cli, "codex_present", lambda: codex)
    installed = desktop if desktop_installed is None else desktop_installed
    monkeypatch.setattr(cli, "desktop_app_installed", lambda: installed)


def _listed(out, label):
    """The items printed under `label` ("Shown"/"Missing") by _note_rows."""
    items, current = [], None
    for line in out.splitlines():
        m = line[7:].split(None, 1) if line.startswith(" " * 7) else []
        if (len(m) == 2 and m[0] in ("Shown", "Missing")
                and not line.startswith(" " * 8)):
            current = m[0]
            if current == label:
                items.append(m[1].strip())
        elif current and line.startswith(" " * 18) and line.strip():
            if current == label:
                items[-1] += " " + line.strip()
        elif current and line.startswith(" " * 16) and line.strip():
            if current == label:
                items.append(line.strip())
        else:
            current = None
    return items


def _has(items, phrase):
    return any(phrase in item for item in items)


def _learned_weekly_anchor(tmp_path):
    now = time.time()
    d = tmp_path / ".blink"
    d.mkdir(exist_ok=True)
    (d / "weekly-anchor.json").write_text(json.dumps(
        {"resets_at": now + 3 * 86400, "observed_at": now - 86400}))


def test_desktop_only_note_sorts_what_it_shows_from_what_it_lacks(
        tmp_path, capsys, monkeypatch):
    """The real desktop-only desk, hooks written by this install, no weekly
    anchor yet. Measured 2026-09-26: the Code tab's bundled Claude Code reads
    ~/.claude/settings.json and the hooks fired on a real session, so the
    light belongs under Shown; no weekly reset is known, so that belongs
    under Missing, with the reason."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    shown, missing = _listed(out, "Shown"), _listed(out, "Missing")
    assert "Claude Desktop alone" in out
    assert _has(shown, "both usage percentages")
    assert _has(shown, "five-hour countdown while a window is running")
    assert _has(shown, "activity light")
    assert not _has(missing, "activity light")
    assert _has(missing, "weekly countdown")
    assert not _has(shown, "weekly countdown")
    assert _has(missing, "up-to-the-minute weekly percentage")
    assert not _has(missing, "live figures")
    # The reason, not just the fact -- otherwise it reads as unimplemented.
    assert "does not record when the week resets" in out
    # Server-controlled intervals are never quoted (desktop_staleness_note).
    assert "5 to 15 minutes" not in out


def test_desktop_note_does_not_promise_a_light_the_hooks_step_skipped(
        tmp_path, capsys, monkeypatch):
    """A settings.json whose "hooks" is not an object makes step 3 skip. The
    fixed note promised the light anyway; the hooks stay [] and the pip can
    never come on."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {"hooks": []})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert _has(_listed(out, "Missing"), "activity light")
    assert not _has(_listed(out, "Shown"), "activity light")


def test_desktop_note_shows_a_weekly_countdown_already_learned(
        tmp_path, capsys, monkeypatch):
    """Reinstalling a desk that already learned the weekly boundary: the
    panel is counting it down, so the note must not file it under Missing."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    _learned_weekly_anchor(tmp_path)
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert _has(_listed(out, "Shown"), "weekly countdown")
    assert not _has(_listed(out, "Missing"), "weekly countdown")
    assert "does not record when the week resets" not in out


def test_desktop_and_codex_is_not_called_desktop_alone(
        tmp_path, capsys, monkeypatch):
    _no_cli(monkeypatch, desktop=True, codex=True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Claude Desktop alone" not in out
    assert "Claude Desktop and Codex" in out
    assert _has(_listed(out, "Shown"), "Codex")


def test_codex_only_note_does_not_deny_the_activity_light(
        tmp_path, capsys, monkeypatch):
    """Codex sessions drive the light through their own hooks since a88d7d6;
    the note still said there was none until 2026-09-30."""
    _no_cli(monkeypatch, desktop=False, codex=True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    # "runs on Codex", not `"alone" in out`: the install disclosure says
    # "readable by you alone", so that check could never fail (review,
    # 2026-10-19).
    assert "runs on Codex" in out
    assert "no activity light" not in out
    assert "activity light" in out
    # Its light comes from the session log; it does not wait on step 4.
    assert "approved" not in out


def test_a_claude_on_path_that_does_not_answer_is_not_called_absent(
        tmp_path, capsys, monkeypatch):
    """claude_version() returns (None, None) for a timeout or a broken npm
    shim too. Telling that person Claude Code is not installed sends them to
    reinstall something that is there."""
    _no_cli(monkeypatch)
    monkeypatch.setattr(cli, "claude_on_path", lambda: True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "not installed" not in out.split("[5/")[-1]
    assert "did not" in out and "answer" in out
    cli.main(["status"])
    st = capsys.readouterr().out
    assert "Claude Code not found" not in st
    assert "did not answer" in st


def test_status_on_desktop_alone_agrees_with_its_own_activity_row(
        tmp_path, capsys, monkeypatch):
    """Before install: no hooks. Status used to say "activity light from the
    Code tab" three lines above "hooks not installed -- the busy/idle pip
    will stay dark"."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "Claude Desktop alone" in out
    assert "hooks not installed" in out
    assert "activity light from the Code tab" not in out
    assert "no activity light: Claude hooks not working" in out
    assert "no countdowns" not in out
    # Not "open Claude Code once so it renders its status line": the Code
    # tab never renders one, so that advice could never clear.
    assert "from Claude Desktop's own files" in out
    assert "open Claude Code once" not in out


def test_status_after_install_credits_the_code_tab(tmp_path, capsys,
                                                   monkeypatch):
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    capsys.readouterr()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "activity light from the Code tab" in out
    assert "hooks installed" in out
    assert "no weekly countdown" in out


def test_desktop_installed_but_never_used_is_not_called_absent(
        tmp_path, capsys, monkeypatch):
    """Measured on a Windows 11 VM, 2026-09-30: Claude Desktop installed and
    not yet signed in has its data folder but no plan-usage-history.json, so
    install said "With none of them installed the panel will connect and
    then sit blank" about a machine with Claude Desktop on it. Signing in
    wrote the file within a minute and the panel needed nothing redone."""
    _no_cli(monkeypatch, desktop=False, desktop_installed=True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "none of them installed" not in out
    assert "Claude Desktop is installed" in out
    assert "sign in" in out
    cli.main(["status"])
    st = capsys.readouterr().out
    assert "no Claude Desktop cache" not in st
    assert "Claude Desktop is installed" in st
    assert "open Claude Code once" not in st


def test_no_claude_code_still_installs_everything(tmp_path, capsys, monkeypatch):
    """A note, not a refusal, and not a failed step. The edits are correct and
    start working by themselves the day Claude Code arrives -- exactly the
    rule the too-old warning already follows."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    settings = _read(tmp_path)
    assert settings["statusLine"]["command"]
    assert settings.get("hooks")


def test_neither_source_is_stated_more_firmly(tmp_path, capsys, monkeypatch):
    """No Claude Code and no desktop cache is not a reduced panel, it is an
    empty one, and the copy must not soften that into 'works with less'."""
    _no_cli(monkeypatch, desktop=False)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Nothing on this machine reports usage yet" in out
    assert "sit blank" in out
    assert "Claude Desktop alone" not in out


# --- found by review, 2026-10-19 ---------------------------------------------


def test_a_scalar_hook_event_does_not_crash_install_or_status(
        tmp_path, capsys, monkeypatch):
    """{"PreToolUse": true}: step 3 skips it, install printed "Done.", and then
    a TypeError escaped main() from the note. Status crashed on its Claude
    Code row. One level further down, a scalar "hooks" inside a group."""
    _no_cli(monkeypatch)
    for hooks in ({"PreToolUse": True},
                  {"Stop": [{"hooks": 5}], "PreToolUse": [7]}):
        _settings(tmp_path, {"hooks": hooks})
        assert cli.main(["install"]) == 0
        assert cli.main(["status"]) == 0
        capsys.readouterr()


def test_a_skipped_hooks_step_is_not_answered_with_run_install_again(
        tmp_path, capsys, monkeypatch):
    """Step 3 refuses a hooks section that is not an object, and refuses it
    again on every re-run -- so "run install again" at the end of that very
    install was a loop."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {"hooks": []})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    light = [i for i in _listed(out, "Missing") if "activity light" in i]
    assert light and "run install again" not in light[0]
    assert "Activity hooks step" in light[0]


def test_duplicated_hooks_count_once_per_event(tmp_path, capsys, monkeypatch):
    """A second Blink Stop group, from a hand edit or a sync tool, counted 11
    of 10: not equal, so install said "run install again" on every run with
    every event wired."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    data = _read(tmp_path)
    data["hooks"]["Stop"].append(data["hooks"]["Stop"][0])
    _settings(tmp_path, data)
    capsys.readouterr()
    hooks = cli.claude_hooks()
    assert hooks.wired == hooks.total and hooks.light
    cli.main(["status"])
    out = capsys.readouterr().out
    assert f"hooks installed ({hooks.total}/{hooks.total} events" in out
    assert "activity light from the Code tab" in out


def test_disable_all_hooks_means_no_light(tmp_path, capsys, monkeypatch):
    """Claude Code's own off switch, honoured by the Code tab's bundled copy
    too. With every Blink hook present, install promised the light and
    status said "activity light from the Code tab"; the pip never lit."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {"disableAllHooks": True})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert not _has(_listed(out, "Shown"), "activity light")
    assert _has(_listed(out, "Missing"), "disableAllHooks")
    cli.main(["status"])
    st = capsys.readouterr().out
    assert "activity light from the Code tab" not in st
    assert "disableAllHooks" in st


def test_an_unreadable_settings_file_is_unknown_not_missing(
        tmp_path, capsys, monkeypatch):
    """Status printed "no activity light: hooks missing" three rows above
    "Activity unknown -- settings.json does not parse"."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    (tmp_path / ".claude" / "settings.json").write_text('{"hooks": {},}')
    capsys.readouterr()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "Activity    unknown -- settings.json does not parse" in out
    assert "hooks missing" not in out
    assert "activity light from the Code tab" not in out


def test_a_missing_hook_shim_is_not_credited_with_the_light(
        tmp_path, capsys, monkeypatch):
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    os.remove(cli.hook_shim_path())
    capsys.readouterr()
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "activity light from the Code tab" not in out
    assert "shim is missing" in out


def test_desktop_and_codex_keep_the_codex_light_without_claude_hooks(
        tmp_path, capsys, monkeypatch):
    """Codex sessions light the pip from their own session log. A desk with
    both, and Claude hooks that could not be written, was told it had no
    light while its wire line said "state":"running"."""
    _no_cli(monkeypatch, desktop=True, codex=True)
    _settings(tmp_path, {"hooks": []})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert _has(_listed(out, "Shown"), "activity light, for Codex sessions")
    assert _has(_listed(out, "Missing"),
                "activity light for Desktop's Code tab")
    cli.main(["status"])
    st = capsys.readouterr().out
    assert "activity light from Codex only" in st
    assert "no activity light" not in st


def test_codex_with_an_unsigned_desktop_is_told_to_sign_in(
        tmp_path, capsys, monkeypatch):
    """The Codex branch ran first, so this desk heard "the Claude page stays
    empty until you add Claude Code" with Claude Desktop already on it."""
    _no_cli(monkeypatch, desktop=False, codex=True, desktop_installed=True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "sign in" in out
    assert "until you add Claude Code" not in out
    cli.main(["status"])
    st = capsys.readouterr().out
    assert "sign in to Claude Desktop" in st


def test_the_weekly_dial_is_not_promised_a_rate(tmp_path, capsys,
                                                monkeypatch):
    """The daemon sends only the five-hour burn, and the firmware draws no
    rate on the weekly gauge (usage_view.c): the slot shows "--"."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "shows a rate" not in out
    assert "no\n     countdown." in out or "has no countdown" in out
    # And the closing line offers the countdown it lacks.
    assert "and its countdown" in out


def test_a_learned_weekly_countdown_is_not_offered_again(
        tmp_path, capsys, monkeypatch):
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    _learned_weekly_anchor(tmp_path)
    assert cli.main(["install"]) == 0
    assert "and its countdown" not in capsys.readouterr().out


def test_weekly_countdown_known_asks_what_the_daemon_publishes(
        tmp_path, monkeypatch):
    """A refuted anchor stays loadable for weeks while the provider -- and so
    the wire -- withholds it. Asking the file said "known"."""
    from pc.providers import weekly_anchor as provider
    _learned_weekly_anchor(tmp_path)
    assert cli.weekly_countdown_known()
    monkeypatch.setattr(provider.WeeklyAnchorProvider, "poll",
                        lambda self, now: [])
    assert not cli.weekly_countdown_known()


def test_weekly_countdown_known_counts_what_the_daemon_will_seed(
        tmp_path, monkeypatch):
    """No anchor yet, but a Cowork session records the reset: the daemon
    install just started seeds it on its first poll. "Missing" was printed
    seconds before the panel showed it."""
    from pc import cowork_audit, desktop_idb
    assert not cli.weekly_countdown_known()
    monkeypatch.setattr(cowork_audit, "seven_day_reset",
                        lambda: (time.time() + 86400, time.time()))
    assert cli.weekly_countdown_known()
    monkeypatch.setattr(cowork_audit, "seven_day_reset", lambda: None)
    monkeypatch.setattr(desktop_idb, "seven_day_reset",
                        lambda: (time.time() + 86400, time.time()))
    assert cli.weekly_countdown_known()


def test_status_on_a_cli_and_desktop_desk_still_asks_for_the_status_line(
        tmp_path, capsys, monkeypatch):
    """The commonest desk: a working Claude Code and Claude Desktop, before
    the first status line. It was told "no Claude Code status line on this
    machine" right under "Claude Code 2.1.300"."""
    monkeypatch.setattr(cli, "claude_version",
                        lambda: ("2.1.300 (Claude Code)", (2, 1, 300)))
    monkeypatch.setattr(cli, "claude_on_path", lambda: True)
    monkeypatch.setattr(cli, "desktop_app_present", lambda: True)
    monkeypatch.setattr(cli, "codex_present", lambda: False)
    _settings(tmp_path, {})
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "open Claude Code once" in out
    assert "no Claude Code status line on this machine" not in out


def test_status_on_desktop_alone_ignores_a_leftover_status_line(
        tmp_path, capsys, monkeypatch):
    """A statusline.json from a Claude Code since removed: "open Claude Code
    to refresh it" directly under "running on Claude Desktop alone"."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    blink = tmp_path / ".blink"
    blink.mkdir(exist_ok=True)
    payload = blink / "statusline.json"
    payload.write_text("{}")
    old = time.time() - 86400
    os.utime(payload, (old, old))
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "open Claude Code to refresh it" not in out
    assert "from Claude Desktop's own files" in out


def test_a_silent_claude_is_asked_once_and_desktop_still_described(
        tmp_path, capsys, monkeypatch):
    """A `claude` that hangs cost two 30 s timeouts after "Done." (measured
    60.0 s), and the note dropped the Desktop explanation for a sentence
    claiming the panel needs --version to answer. Nothing runs it but this
    note."""
    calls = []
    _no_cli(monkeypatch)
    monkeypatch.setattr(cli, "claude_version",
                        lambda: calls.append(1) or (None, None))
    monkeypatch.setattr(cli, "claude_on_path", lambda: True)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert len(calls) == 1
    assert "panel needs it" not in out
    assert "Claude Desktop" in out.split("Done.")[-1]
    # Sentence case: it opened with a lower-case `claude`.
    note = [l.strip() for l in out.splitlines() if "`claude --version`" in l]
    assert note and note[0].startswith("!  There is")


def test_desktop_app_installed_wants_desktops_own_files(tmp_path):
    """Not the data folder alone: MCP setup tools create it to write
    claude_desktop_config.json on machines that never ran Desktop. Local
    Storage only the app's own browser engine writes."""
    from pc import desktop_local_storage
    from pc.providers import claude_desktop
    folder = os.path.dirname(claude_desktop.cache_path())
    os.makedirs(folder)
    with open(os.path.join(folder, "claude_desktop_config.json"), "w") as f:
        f.write("{}")
    assert not cli.desktop_app_installed()
    os.makedirs(desktop_local_storage.store_path())
    assert cli.desktop_app_installed()


def test_claude_on_path_finds_one_that_is_there(tmp_path, monkeypatch):
    """Unstubbed: every install/status test patches it."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    monkeypatch.setenv("PATH", str(bindir))
    assert not cli.claude_on_path()
    exe = bindir / ("claude.exe" if sys.platform == "win32" else "claude")
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    assert cli.claude_on_path()


def test_a_working_claude_code_says_none_of_it(tmp_path, capsys, monkeypatch):
    """The common machine pays nothing for this."""
    monkeypatch.setattr(cli, "claude_version",
                        lambda: ("2.1.245 (Claude Code)", (2, 1, 245)))
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Claude Desktop alone" not in out
    assert "Nothing on this machine reports usage" not in out


def test_the_disclosure_counts_the_hooks_it_actually_writes(tmp_path, capsys,
                                                            monkeypatch):
    """It said six while writing ten. The number drifted when three events were
    added and nothing failed -- and this text is the only consent the customer
    is asked for before we edit a file they own."""
    monkeypatch.setattr(cli, "claude_version",
                        lambda: ("2.1.245 (Claude Code)", (2, 1, 245)))
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    n = len(cli.install_hooks.HOOK_EVENTS)
    assert f"for each of {n} Claude Code" in out
    written = _read(tmp_path).get("hooks") or {}
    assert len(written) == n, "disclosure and reality must agree"


def test_a_settings_file_that_is_not_an_object_is_refused_not_crashed(tmp_path):
    """Valid JSON, wrong shape. Five call sites did data.get(...) on it; in the
    daemon that AttributeError killed main() and KeepAlive restarted it every
    ten seconds forever."""
    for doc in ("[]", "null", '"a string"', "42"):
        (tmp_path / ".claude").mkdir(exist_ok=True)
        (tmp_path / ".claude" / "settings.json").write_text(doc)
        with pytest.raises(install_statusline.SettingsUnreadable):
            install_statusline._load(str(tmp_path / ".claude" / "settings.json"))


def test_a_statusline_that_is_not_a_dict_reads_as_absent():
    """`{"statusLine": "my-bar"}` -- the `or {}` idiom looked safe and was not:
    only None and {} take that branch."""
    for bad in ("my-bar", [], 7, None):
        assert install_statusline._current_command({"statusLine": bad}) == ""
    assert install_statusline._current_command({"statusLine": {"command": 9}}) == ""
    assert install_statusline._current_command(
        {"statusLine": {"command": "x"}}) == "x"


# --- Codex hooks -----------------------------------------------------------
#
# Install now edits a second vendor's configuration. These tests never let it
# reach a real one: codex_present and install_codex_hooks.install are both
# replaced, HOME is tmp_path (conftest) and CODEX_HOME is cleared (_sandbox).

# The column the labelled block in _announce_codex_hooks wraps into. A line
# indented this far continues the sentence above it and is not a new one, so
# the sentence-case rule below does not apply to it.
_CODEX_WRAP = " " * 11


def test_install_says_codex_will_ask_before_it_writes(monkeypatch, capsys):
    """Disclosure before the write, not after -- and specifically the trust
    prompt, because an unexplained dialog from a tool the user did not think
    they were configuring is a support incident, not a feature."""
    monkeypatch.setattr(cli, "codex_present", lambda: True)
    written = []
    monkeypatch.setattr(
        cli.install_codex_hooks, "install",
        lambda p, s: written.append(p) or "installed (10 events).")

    cli._announce_codex_hooks()
    out_before = capsys.readouterr().out
    cli._install_codex_hooks()

    assert written, "the test must actually reach the installer"
    assert "trust" in out_before.lower(), \
        "the disclosure does not mention the prompt Codex will show"
    assert "Codex" in out_before
    for line in out_before.splitlines():
        if not line.strip() or line.startswith(_CODEX_WRAP):
            continue
        assert line.strip()[0].isupper(), f"copy is sentence case: {line!r}"


def test_install_skips_the_codex_step_when_codex_is_absent(monkeypatch):
    """Writing a hooks file for a tool that is not installed would leave a
    stranger's configuration on the machine."""
    monkeypatch.setattr(cli, "codex_present", lambda: False)
    called = []
    monkeypatch.setattr(cli.install_codex_hooks, "install",
                        lambda p, s: called.append(p))
    msg = cli._install_codex_hooks()
    assert called == []
    assert "no Codex" in msg


def test_install_does_not_fail_when_the_codex_config_is_unreadable(monkeypatch):
    """The status line is the product; the activity light is a nicety. A hooks
    file we cannot safely edit costs a pip, not an install."""
    monkeypatch.setattr(cli, "codex_present", lambda: True)

    def boom(_p, _s):
        raise cli.install_statusline.SettingsUnreadable("not valid JSON")

    monkeypatch.setattr(cli.install_codex_hooks, "install", boom)
    msg = cli._install_codex_hooks()
    assert msg.startswith("skipped")
    assert "not valid JSON" in msg


def test_install_creates_the_codex_state_directory_private(monkeypatch):
    """The slots name the projects someone has open. The default umask would
    leave them readable by every account on the machine."""
    import stat
    monkeypatch.setattr(cli, "codex_present", lambda: True)
    monkeypatch.setattr(cli.install_codex_hooks, "install",
                        lambda p, s: "installed (10 events).")
    cli._install_codex_hooks()
    d = os.path.join(cli.blink_home(), "state-codex")
    assert os.path.isdir(d)
    if os.name != "nt":
        assert stat.S_IMODE(os.stat(d).st_mode) == 0o700


def test_uninstall_removes_the_codex_slots_too(tmp_path):
    """A left-behind slot directory is not litter, it is a lie: the daemon
    would go on counting sessions from a tool it no longer hooks."""
    for sub in ("state", "state-codex"):
        d = os.path.join(cli.blink_home(), sub)
        os.makedirs(os.path.join(d, "sess-1"), exist_ok=True)
        with open(os.path.join(d, "sess-1.state"), "w") as f:
            f.write("{}")
        with open(os.path.join(d, "sess-1", "agent-1"), "w") as f:
            f.write("")

    cli._rm_state_dir()

    for sub in ("state", "state-codex"):
        d = os.path.join(cli.blink_home(), sub)
        assert not os.path.exists(os.path.join(d, "sess-1.state"))
        assert not os.path.exists(os.path.join(d, "sess-1"))


def test_uninstall_keeps_going_when_the_codex_file_is_unreadable(capsys):
    """The login service is already gone by this point in cmd_uninstall, so
    stopping here would leave the machine half-undone.

    Against a real unparseable file, not a stub that raises. This used to
    monkeypatch install_codex_hooks.uninstall into raising SettingsUnreadable
    so that cmd_uninstall's handler had something to catch -- but uninstall()
    catches its own at every point that can raise one and returns a sentence
    instead, so the exception being caught could not occur and the handler has
    gone. What is left to prove is the property that actually matters: the
    step reports and returns, and the file it could not parse is untouched.
    """
    hooks = cli.install_codex_hooks.hooks_file()
    os.makedirs(os.path.dirname(hooks), exist_ok=True)
    with open(hooks, "w") as f:
        f.write("{ not json")

    cli._uninstall_codex_hooks()

    assert "left it alone" in capsys.readouterr().out
    with open(hooks) as f:
        assert f.read() == "{ not json", "it rewrote a file it cannot parse"


def test_uninstall_says_it_left_the_codex_trust_record_alone(tmp_path, capsys,
                                                             monkeypatch):
    """The ruling this carries is only half a decision if the user cannot see
    it. Removing the trust record would make a headless reinstall silently
    dead -- Codex skips a distrusted hook with no output at all -- so it stays,
    and uninstall says so, names the file, and says how to undo it."""
    codex = tmp_path / ".codex"
    codex.mkdir()
    config = codex / "config.toml"
    config.write_text('[hooks.state."k"]\ntrusted_hash = "sha256:0"\n')
    monkeypatch.setattr(cli.install_codex_hooks, "uninstall",
                        lambda p, s=None: "Codex state hooks removed (10).")

    cli._uninstall_codex_hooks()

    out = capsys.readouterr().out
    assert "trust" in out.lower(), "uninstall says nothing about the trust record"
    assert str(config) in out, "it does not name the file it left behind"
    assert "hooks.state" in out, "it does not say how to undo it"
    # And the record really is still there. This is the assertion the ruling
    # turns on: a single added os.remove() in _uninstall_codex_hooks would be
    # invisible to every other check in this file.
    assert "trusted_hash" in config.read_text()


def test_uninstall_says_nothing_about_a_codex_config_that_is_not_there(
        monkeypatch, capsys):
    """The majority of machines have no Codex. A paragraph about a config file
    that does not exist is noise about a record that was never written."""
    monkeypatch.setattr(cli.install_codex_hooks, "uninstall",
                        lambda p, s=None: "No Codex state hooks to remove.")
    cli._uninstall_codex_hooks()
    assert "trust" not in capsys.readouterr().out.lower()


def test_status_reports_codex_hooks_not_installed(monkeypatch):
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker", lambda: set())
    lines = cli._codex_hook_status()
    assert any("not installed" in ln for ln in lines)


def test_status_reports_a_registered_hook_that_has_never_fired(monkeypatch):
    """Which is exactly what declining the trust prompt looks like, and the
    single most likely support call this feature will generate: under
    `codex exec` a distrusted hook is skipped with no prompt and no output at
    all, so there is nothing else anywhere to see."""
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})
    monkeypatch.setattr(cli.codex_state, "scan",
                        lambda now, path=None, sweep=True: ({}, 0))
    lines = cli._codex_hook_status()
    assert any("trust" in ln.lower() for ln in lines)


def test_status_reports_live_codex_sessions(monkeypatch):
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})
    monkeypatch.setattr(cli.codex_state, "scan",
                        lambda now, path=None, sweep=True: (
                            {"a": "running", "b": "waiting"}, 0))
    lines = cli._codex_hook_status()
    assert any("2" in ln for ln in lines)


def test_status_does_not_blame_the_trust_prompt_for_slots_it_cannot_read(
        monkeypatch):
    """Two different faults with two different repairs. Reporting an
    unreadable directory as 'never written anything' sends someone to Codex's
    trust prompt to fix a permissions problem, and it will not be there."""
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker",
                        lambda: {"sh /x/blink-hook.sh Stop codex"})

    def boom(now, path=None, sweep=True):
        raise OSError("permission denied")

    monkeypatch.setattr(cli.codex_state, "scan", boom)
    lines = cli._codex_hook_status()
    assert any("permission denied" in ln for ln in lines)
    assert not any("trust" in ln.lower() for ln in lines)


def test_status_says_nothing_about_codex_hooks_without_codex(tmp_path, capsys):
    """No Codex log and no registration of ours: telling someone to install a
    hook for a tool they do not have is worse than saying nothing."""
    assert cli.main(["status"]) == 0
    assert "Codex hook" not in capsys.readouterr().out


def test_status_does_not_sweep_the_codex_slots(tmp_path):
    """A diagnostic that deletes what it is diagnosing destroys the evidence
    somebody ran it to see -- and here that is not an abstraction: the Codex
    hook status is read off exactly the slots the sweep collects, so a status
    run could manufacture the 'never written anything' finding that the next
    status run then reports."""
    sessions = tmp_path / ".codex" / "sessions" / "2026" / "09" / "04"
    sessions.mkdir(parents=True)
    (sessions / "rollout-2026-09-04T00-00-00-abc.jsonl").write_text("")
    slots = tmp_path / ".blink" / "state-codex"
    slots.mkdir(parents=True)
    # Older than claude_state.ABANDONED_AFTER_S, so a sweep would collect it.
    slot = slots / "sess-old.state"
    slot.write_text(json.dumps({"event": "Stop", "t": time.time() - 7200}))

    assert cli.main(["status"]) == 0

    assert slot.exists(), "`blink status` swept the slots it was reporting on"


# --- the board running ahead of this app -------------------------------------
#
# The line fires on a comparison against THIS APP, so it also fires when the app
# is already the newest thing published -- every developer's desk from the first
# local build onwards. `blink status` does not touch the network on purpose (it
# has to work on a plane, and it is the first thing anyone runs when nothing
# works), so it cannot tell those apart and must not pretend to.

def _board_lines_for(fw):
    from pc.cli import board_lines
    return "\n".join(board_lines({"port": "/dev/x", "board_id": "abc",
                                  "fw": fw},
                                 [("/dev/x", "CH340")], (), "blink"))


def test_a_board_ahead_names_both_possibilities():
    from pc.version import RELEASE_VERSION
    a, b, c = RELEASE_VERSION.split(".")
    out = _board_lines_for(f"{a}.{b}.{int(c) + 1}")
    assert "they ship together" in out
    assert "blink update" in out
    # The half that was missing: the command can find nothing, and then the
    # board is simply ahead of the feed rather than the app being behind it.
    assert "ahead of the published release" in out


def test_a_matched_pair_says_none_of_it():
    from pc.version import RELEASE_VERSION
    out = _board_lines_for(RELEASE_VERSION)
    assert "they ship together" not in out
    assert "ahead of the published release" not in out
