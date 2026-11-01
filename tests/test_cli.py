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
import shlex
import subprocess
import sys
import time

import pytest

from pc import cli, install_statusline
from tests.support import desktop_cache as _dc
from tests.support.home import inside
from tests.support.process import as_checkout, as_frozen


@pytest.fixture(autouse=True)
def _sandbox(tmp_path):
    """The one thing these tests need that the whole suite does not.

    The redirected HOME, the cleared CODEX_HOME and the login-service guard
    all live in tests/conftest.py now -- see the note there for what went
    wrong when they were a per-file concern. CODEX_HOME was cleared here
    first: install edits a file under it, so a developer with it set in their
    shell had these tests one forgotten line from writing a hooks.json into
    their real Codex install.
    """
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


def test_status_and_uninstall_ask_where_the_payload_is(tmp_path, capsys,
                                                       monkeypatch):
    """One spelling of the payload path in Python, statusline_source's. These
    two built their own from blink_home(), the same file today and a second
    place to forget the day it moves."""
    from pc import statusline_source
    moved = tmp_path / "elsewhere" / "payload.json"
    moved.parent.mkdir()
    monkeypatch.setattr(statusline_source, "payload_path", lambda: str(moved))
    _settings(tmp_path, {})
    cli.main(["install"])
    moved.write_text("{}")
    (moved.parent / "payload.json.123.tmp").write_text("{")
    capsys.readouterr()

    cli.main(["status"])
    assert "Usage data  fresh" in capsys.readouterr().out

    cli.main(["uninstall"])
    assert not moved.exists()
    assert not (moved.parent / "payload.json.123.tmp").exists()


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
    # Made by hand: a checkout install no longer creates ~/.blink/bin, and
    # this test is about a frozen install's binary refusing to go.
    os.makedirs(cli.bin_dir(), exist_ok=True)

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
    # By hand: a checkout install no longer makes ~/.blink/bin, and without
    # it this would pass with uninstall's removal stubbed out.
    os.makedirs(cli.bin_dir(), exist_ok=True)
    rc = cli.main(["uninstall"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "could not be removed" not in out
    assert not os.path.exists(cli.bin_dir())


def test_uninstall_forgets_whether_the_account_has_a_window(tmp_path):
    """Owner's decision after the /code-review max of 2026-11-01: a
    reinstall starts fresh. Kept, an old "no window" verdict -- a misread
    one included -- put the next install straight into the burn view before
    any evidence had been seen. The verdict, the temp names theme._write_json
    writes it through, and the fixed `.tmp` an earlier writer used all go.
    Paths are asked of the code and checked against the caller's home
    (tests/support/home.py) before anything is written."""
    from pc import session_verdict
    _settings(tmp_path, {})
    cli.main(["install"])
    _verdict(tmp_path, False, time.time() - 60)
    path = inside(tmp_path, session_verdict.verdict_path())
    leftovers = [path + ".tmp", path + ".1234.0.blink-tmp",
                 path + install_statusline._TEMP_SUFFIX]
    for p in leftovers:
        with open(p, "w", encoding="utf-8") as f:
            f.write("{")
    assert set(leftovers) <= set(
        install_statusline.temp_siblings(path) + [path + ".tmp"])

    cli.main(["uninstall"])

    for p in [path] + leftovers:
        assert not os.path.exists(p), p
    assert session_verdict.SessionVerdict().current() is None


def test_install_refuses_to_touch_a_settings_file_that_will_not_parse(
        tmp_path, capsys, monkeypatch):
    """Treating unparseable JSON as {} would write a fresh settings.json over
    whatever the customer was halfway through editing."""
    # Frozen, so an install that went ahead would make ~/.blink/bin, which
    # a checkout install no longer does.
    as_frozen(monkeypatch)
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
    # By hand: a checkout install no longer makes ~/.blink/bin, and without
    # it this would pass with uninstall's removal stubbed out.
    os.makedirs(cli.bin_dir(), exist_ok=True)
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


def _desktop_reporting():
    """desktop_reading() for a cache with a fresh sample in it, made by the
    provider's own parser from a generated document."""
    from pc.providers import claude_desktop
    now = time.time()
    frame = claude_desktop.ClaudeDesktopProvider(path="unused").parse_cache_file(
        _dc.doc([_dc.sample(int(now * 1000))]), now)
    return claude_desktop.VERDICT_FRAME, frame


def _no_cli(monkeypatch, desktop=True, codex=False, desktop_installed=None):
    """A machine with no command-line Claude Code. claude_on_path is patched
    too: claude_version fails the same way for a broken install that IS on
    PATH, and a developer's own `claude` would otherwise leak in."""
    monkeypatch.setattr(cli, "claude_version", lambda: (None, None))
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    monkeypatch.setattr(cli, "desktop_reading",
                        lambda: _desktop_reporting() if desktop else None)
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
    monkeypatch.setattr(cli, "desktop_reading", _desktop_reporting)
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


def test_a_fresh_status_line_wins_even_with_no_claude_on_path(
        tmp_path, capsys, monkeypatch):
    """Something is rendering the status line -- a Claude Code outside PATH,
    an IDE's copy -- so it is the live source. The leftover-payload fix hid
    it behind "from Claude Desktop's own files", and CI's all-sources
    scenario failed on its first run (2026-10-19)."""
    _no_cli(monkeypatch)
    _settings(tmp_path, {})
    blink = tmp_path / ".blink"
    blink.mkdir(exist_ok=True)
    (blink / "statusline.json").write_text("{}")
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "Usage data  fresh" in out
    assert "from Claude Desktop's own files" not in out


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
# replaced, HOME is tmp_path and CODEX_HOME is cleared (both tests/conftest.py).

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

def _board_lines_for(fw, **kw):
    from pc.cli import board_lines
    return "\n".join(board_lines({"port": "/dev/x", "board_id": "abc",
                                  "fw": fw},
                                 [("/dev/x", "CH340")], (), "blink", **kw))


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


# The Desktop row of `blink status`, driven by _source_lines() alone: the rest
# of status runs `claude --version` (a 30 s timeout on a desk where it hangs)
# and lists the real serial ports, and an assertion about stderr has to be
# about this row, not about everything else on the machine (review,
# 2026-10-25). The shapes are tests/support/desktop_cache.py's, shared with
# the provider's own tests.

def _desktop_cache(home, content):
    """Write Claude Desktop's usage cache where status looks for it. The path
    is resolved at call time, so under pytest it lands in conftest's sandbox
    HOME (and APPDATA on Windows). `home` is the caller's and the path must
    lie inside it (tests/support/home.py): from a script with the real HOME
    this would overwrite the desk's own Claude Desktop cache."""
    from pc.providers import claude_desktop
    path = inside(home, claude_desktop.cache_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(_dc.as_bytes(content))
    return path


def _source_rows(capsys):
    """(every line _source_lines printed, everything on stderr)."""
    cli._source_lines()
    cap = capsys.readouterr()
    return cap.out.splitlines(), cap.err


def _desktop_status(capsys):
    """(the Desktop row with its continuation lines, everything on stderr)."""
    lines, err = _source_rows(capsys)
    starts = [i for i, l in enumerate(lines) if l.startswith("Desktop     ")]
    assert starts, "status printed no Desktop line"
    i = starts[0]
    row = [lines[i]]
    for l in lines[i + 1:]:
        if not l.startswith("            "):
            break
        row.append(l)
    return "\n".join(row), err


_DESKTOP_REPORTABLE = {**_dc.NOT_UNFILLED, **_dc.GARBLED}


@pytest.mark.parametrize("content", list(_dc.UNFILLED.values()),
                         ids=list(_dc.UNFILLED))
def test_status_calls_an_empty_desktop_cache_not_yet_filled(
        tmp_path, capsys, content):
    """The app before its first reading, not a format change. Asking for a
    bug report there sent people off to report a fault that was not there."""
    _desktop_cache(tmp_path, content)
    row, err = _desktop_status(capsys)
    assert "did not parse" not in row
    assert "format may have changed" not in row
    assert "holds no usage figures yet" in row
    # poll()'s once-per-path daemon complaint used to land right above this
    # row and contradict it.
    assert err == ""


@pytest.mark.parametrize("content", list(_DESKTOP_REPORTABLE.values()),
                         ids=list(_DESKTOP_REPORTABLE))
def test_status_still_asks_for_a_report_when_the_desktop_cache_is_not_ours(
        tmp_path, capsys, content):
    _desktop_cache(tmp_path, content)
    row, err = _desktop_status(capsys)
    assert "did not parse" in row
    assert "please report it" in row
    assert "holds no usage figures" not in row
    assert err == ""


@pytest.mark.parametrize("content", list(_dc.GARBLED.values()),
                         ids=list(_dc.GARBLED))
def test_a_cache_that_is_not_text_does_not_end_status_at_desktop(
        tmp_path, capsys, content):
    """UnicodeDecodeError went straight out of the Desktop row, and every row
    after it was lost to a traceback (review, 2026-10-25)."""
    _desktop_cache(tmp_path, content)
    lines, _err = _source_rows(capsys)
    assert any(l.startswith("Desktop     usage cache present but did not"
                            " parse") for l in lines)
    assert any(l.startswith("Codex       ") for l in lines), \
        "the rows after Desktop went missing"


@pytest.mark.skipif(sys.platform == "win32",
                    reason="chmod 000 does not stop the owner reading on"
                           " Windows")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a mode-000 file anyway")
def test_an_unreadable_cache_is_not_called_a_format_change(tmp_path, capsys):
    path = _desktop_cache(tmp_path, _dc.UNFILLED["version 2 with no samples"])
    os.chmod(path, 0)
    try:
        row, err = _desktop_status(capsys)
    finally:
        os.chmod(path, 0o600)
    assert "usage cache present but could not be read" in row
    assert "format may have changed" not in row
    assert "please report" not in row
    assert err == ""


@pytest.mark.parametrize("content", list(_dc.UNFILLED.values()),
                         ids=list(_dc.UNFILLED))
def test_status_wire_says_nothing_about_a_cache_not_filled_yet(
        tmp_path, capsys, content):
    """`status --wire` polls the providers the way the daemon does, and
    poll()'s "did not parse" complaint landed under a Desktop row that had
    just called the same file not filled yet (review, 2026-10-25)."""
    _desktop_cache(tmp_path, content)
    cli._wire_line()
    assert "did not parse" not in capsys.readouterr().err


# One Desktop state per desk, from one read of the cache: absent, installed
# but not signed in, signed in with no reading yet, or reporting. The empty
# file used to count as "written usage", then as "not signed in", and the
# second told a signed-in person to sign in (review, 2026-10-25).

def _desk_with_cache(home, monkeypatch, content, codex=False,
                     local_storage=False):
    """read_desk() over a real cache file, with the command-line side
    stubbed: no `claude` is run."""
    monkeypatch.setattr(cli, "claude_version", lambda: (None, None))
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    monkeypatch.setattr(cli, "codex_present", lambda: codex)
    monkeypatch.setattr(cli, "desktop_app_installed", lambda: local_storage)
    if content is not None:
        _desktop_cache(home, content)
    return cli.read_desk()


_WAITING = "hasn't written a reading yet"


@pytest.mark.parametrize("content", list(_dc.UNFILLED.values()),
                         ids=list(_dc.UNFILLED))
def test_a_cache_not_filled_yet_is_a_desk_waiting_for_its_reading(
        tmp_path, monkeypatch, content):
    desk = _desk_with_cache(tmp_path, monkeypatch, content)
    assert desk.desktop is False            # nothing on the Claude page yet
    assert desk.desktop_waiting is True
    assert desk.desktop_installed is True   # the app's own file proves it
    assert desk.unsigned_desktop is False   # and it is signed in


@pytest.mark.parametrize("content", [
    json.dumps({"version": 2, "samples": [_dc.sample(_dc.T_MS)]}),
    *_dc.NOT_UNFILLED.values(), *_dc.GARBLED.values()],
    ids=["a reading", *_dc.NOT_UNFILLED, *_dc.GARBLED])
def test_any_other_cache_still_counts_as_desktop_usage(
        tmp_path, monkeypatch, content):
    """A file in a layout we cannot read is still the app having written
    usage; the status row is where that gets reported, not this light."""
    desk = _desk_with_cache(tmp_path, monkeypatch, content)
    assert desk.desktop is True
    assert desk.desktop_waiting is False


def test_no_cache_and_local_storage_is_not_signed_in(tmp_path, monkeypatch):
    desk = _desk_with_cache(tmp_path, monkeypatch, None, local_storage=True)
    assert desk.unsigned_desktop is True
    assert desk.desktop_waiting is False


@pytest.mark.parametrize("codex", [False, True], ids=["alone", "with Codex"])
def test_a_waiting_desktop_is_never_told_to_sign_in(
        tmp_path, monkeypatch, capsys, codex):
    desk = _desk_with_cache(tmp_path, monkeypatch,
                            _dc.UNFILLED["an empty object"],
                            codex=codex, local_storage=True)
    cli._note_if_no_claude_code(desk)
    monkeypatch.setattr(cli, "read_desk", lambda version=None: desk)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "sign in" not in out
    assert _WAITING in out


def _status_rows(monkeypatch, capsys, desk):
    monkeypatch.setattr(cli, "read_desk", lambda version=None: desk)
    cli.main(["status"])
    return capsys.readouterr().out.splitlines()


def _row(lines, start):
    rows = [l for l in lines if l.startswith(start)]
    assert len(rows) == 1, (start, lines)
    return rows[0]


@pytest.mark.parametrize("content", [
    *_dc.NOT_UNFILLED.values(), *_dc.GARBLED.values()],
    ids=[*_dc.NOT_UNFILLED, *_dc.GARBLED])
def test_rows_that_credit_desktop_admit_its_cache_did_not_parse(
        tmp_path, monkeypatch, capsys, content):
    """"Running on Claude Desktop alone" and "Usage data from Claude
    Desktop's own files" above a Desktop row saying the file did not parse:
    three rows, two of them wrong (review, 2026-10-25)."""
    desk = _desk_with_cache(tmp_path, monkeypatch, content)
    lines = _status_rows(monkeypatch, capsys, desk)
    caveat = "(but its usage cache did not parse; see the Desktop row)"
    assert _row(lines, "Claude Code not found -- running on").endswith(caveat)
    assert _row(lines, "Usage data  ").endswith(caveat)


@pytest.mark.skipif(sys.platform == "win32",
                    reason="chmod 000 does not stop the owner reading on"
                           " Windows")
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a mode-000 file anyway")
def test_rows_that_credit_desktop_admit_its_cache_could_not_be_read(
        tmp_path, monkeypatch, capsys):
    path = _desktop_cache(tmp_path, _dc.UNFILLED["an empty object"])
    os.chmod(path, 0)
    try:
        desk = _desk_with_cache(tmp_path, monkeypatch, None)
    finally:
        os.chmod(path, 0o600)
    lines = _status_rows(monkeypatch, capsys, desk)
    caveat = "(but its usage cache could not be read; see the Desktop row)"
    assert _row(lines, "Claude Code not found -- running on").endswith(caveat)
    assert _row(lines, "Usage data  ").endswith(caveat)


def test_a_reading_desktop_gets_no_caveat(tmp_path, monkeypatch, capsys):
    desk = _desk_with_cache(tmp_path, monkeypatch, _dc.doc([_dc.sample(
        int(time.time() * 1000))]))
    lines = _status_rows(monkeypatch, capsys, desk)
    assert "see the Desktop row" not in "\n".join(lines)


def test_the_waiting_usage_row_says_yet_once(tmp_path, monkeypatch, capsys):
    desk = _desk_with_cache(tmp_path, monkeypatch,
                            _dc.UNFILLED["an empty object"])
    row = _row(_status_rows(monkeypatch, capsys, desk), "Usage data  ")
    assert row.count("yet") == 1


def test_an_installed_desktop_with_no_cache_is_named_as_such(
        tmp_path, monkeypatch, capsys):
    """Its Local Storage proves the app ran here, so "not installed, or
    never signed in" offered a possibility the desk had already ruled out."""
    desk = _desk_with_cache(tmp_path, monkeypatch, None, local_storage=True)
    cli._source_lines(desk)
    out = capsys.readouterr().out
    assert ("Desktop     no usage cache (Claude Desktop installed but never"
            " signed in)") in out
    assert "Claude Desktop not installed" not in out


def test_no_desktop_at_all_keeps_both_possibilities(
        tmp_path, monkeypatch, capsys):
    desk = _desk_with_cache(tmp_path, monkeypatch, None, local_storage=False)
    cli._source_lines(desk)
    assert ("no usage cache (Claude Desktop not installed, or never signed"
            " in)") in capsys.readouterr().out


def test_status_reads_the_desktop_cache_once(tmp_path, monkeypatch, capsys):
    """One read decides every Desktop line: the app rewrites the file every
    few minutes, and two reads can disagree with each other."""
    from pc.providers import claude_desktop
    reads = []
    real = claude_desktop.ClaudeDesktopProvider.verdict
    monkeypatch.setattr(claude_desktop.ClaudeDesktopProvider, "verdict",
                        lambda self, now: reads.append(1) or real(self, now))
    _desk_with_cache(tmp_path, monkeypatch, _dc.UNFILLED["an empty object"])
    reads.clear()
    cli.main(["status"])
    assert reads == [1]


# What a person is told to type, as distinct from where the frozen binary
# lives. From a source checkout `blink install` copies nothing into
# ~/.blink/bin, so every "run X" that named installed_bin() there named a file
# that does not exist -- including the undo command (gapietro's bench
# Mac, 2026-10-12).

def _split_like_the_shell(line):
    """`line` split the way this platform's shell hands it to a program:
    shlex for a POSIX shell, and on Windows the system's own
    CommandLineToArgvW, which follows the C runtime rules cmd.exe relies on.
    The real splitter, so a quoting test cannot agree with itself. A leading
    `& ` is PowerShell's call operator, which the shell consumes, so it is
    not part of the argv a program sees."""
    if sys.platform != "win32":
        return shlex.split(line)
    if line.startswith("& "):
        line = line[2:]
    import ctypes
    from ctypes import wintypes
    f = ctypes.windll.shell32.CommandLineToArgvW
    f.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(ctypes.c_int)]
    f.restype = ctypes.POINTER(wintypes.LPWSTR)
    n = ctypes.c_int()
    argv = f(line, ctypes.byref(n))
    try:
        return [argv[i] for i in range(n.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _home_with_a_space(tmp_path, monkeypatch):
    """A profile like C:\\Users\\John Smith. Only cli's own idea of home moves:
    conftest's sandbox stays where it is, and the new folder is inside it."""
    home = tmp_path / "John Smith"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude" / "settings.json").write_text("{}")
    monkeypatch.setattr(cli, "_home", lambda: str(home))
    return home


def test_from_a_checkout_the_printed_command_is_python_and_blink_main(
        monkeypatch):
    """Run on whatever platform the runner is: sys.platform picks the
    quoting, so faking darwin on Windows would quote C:\\ paths as POSIX
    ones and test nothing real. Checked by splitting the line back with the
    platform's own splitter, not by looking for the paths inside it: a checkout under a
    folder with an apostrophe in its name is printed with that quote
    escaped, and a substring check failed there (review, 2026-10-25)."""
    as_checkout(monkeypatch)
    cmd = cli.command_to_type()
    assert _split_like_the_shell(cmd) == cli._checkout_argv()
    assert cli._checkout_argv()[1] == os.path.join(cli._checkout_dir(),
                                                 "blink_main.py")
    assert os.path.exists(cli._checkout_argv()[1])
    assert cli.installed_bin() not in _split_like_the_shell(cmd)


def test_a_checkout_command_survives_a_space_in_the_interpreter_path(
        monkeypatch):
    """The quoting is the point: an interpreter under a folder with a space in
    its name must still come back as one argument when the person pastes the
    line into their shell. Checked with the platform's own splitter."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(sys, "executable",
                        os.path.join(os.path.dirname(sys.executable),
                                     "with space", "python"))
    assert _split_like_the_shell(cli.command_to_type()) == cli._checkout_argv()


def test_a_frozen_command_survives_a_space_in_the_profile(tmp_path,
                                                         monkeypatch):
    """`C:\\Users\\John Smith\\.blink\\bin\\blink.exe uninstall`, printed bare,
    is `'C:\\Users\\John' is not recognized` once pasted (review, 2026-10-25)."""
    as_frozen(monkeypatch)
    _home_with_a_space(tmp_path, monkeypatch)
    assert " " in cli.installed_bin()
    assert _split_like_the_shell(cli.command_to_type() + " uninstall") == [
        cli.installed_bin(), "uninstall"]


@pytest.mark.parametrize("word,quoted", [
    (r"C:\Users\kfir\.blink\bin\blink.exe", False),
    (r"C:\Users\John Smith\.blink\bin\blink.exe", True),
    (r"C:\Users\R&D\.blink\bin\blink.exe", True),
    # Not quoted either: ^ is cmd's escape but an ordinary character to
    # PowerShell, the shell Windows Terminal opens, and a path that worked
    # there bare would have been newly quoted (review, 2026-10-25).
    (r"C:\Users\a^b\.blink\bin\blink.exe", False),
    # Not quoted: cmd expands %VAR% inside quotes as well, so quoting buys
    # nothing, and PowerShell would then need `& ` in front (review,
    # 2026-10-25). A path with a % and no space prints as it always did.
    (r"C:\Users\50%\.blink\bin\blink.exe", False),
    (r"C:\Program Files (x86)\Python\python.exe", True),
    (r"C:\Users\x(1)\.blink\bin\blink.exe", True),
])
def test_windows_quoting_covers_what_cmd_treats_specially(word, quoted):
    """Pure string work, so it runs on every runner. list2cmdline alone
    quotes only on whitespace, and cmd.exe splits `R&D` into two commands."""
    assert cli._win_quote(word) == (f'"{word}"' if quoted else word)


@pytest.mark.parametrize("word,quoted", [
    # An apostrophe ends a PowerShell single-quoted string and starts one
    # bare, so it is quoted -- in double quotes, where it is ordinary.
    (r"C:\Users\O'Brien\.blink\bin\blink.exe",
     "\"" + r"C:\Users\O'Brien\.blink\bin\blink.exe" + "\""),
    # $ and the backtick are live inside PowerShell's double quotes ($name
    # expands, ` escapes), so those words take the single-quoted form, with
    # any apostrophe doubled (review, 2026-10-25).
    (r"C:\Users\$name\.blink\bin\blink.exe",
     r"'C:\Users\$name\.blink\bin\blink.exe'"),
    (r"C:\Users\a`b\.blink\bin\blink.exe",
     r"'C:\Users\a`b\.blink\bin\blink.exe'"),
    (r"C:\Users\O'$x\.blink\bin\blink.exe",
     r"'C:\Users\O''$x\.blink\bin\blink.exe'"),
])
def test_windows_quoting_keeps_powershell_from_reading_the_path(word,
                                                                quoted):
    assert cli._win_quote(word) == quoted


def test_a_windows_profile_with_a_dollar_gets_a_line_powershell_runs():
    assert cli._win_line([r"C:\Users\$name\.blink\bin\blink.exe",
                          "uninstall"]) == (
        r"& 'C:\Users\$name\.blink\bin\blink.exe' uninstall")
    assert cli._win_line([r"C:\Users\O'Brien\.blink\bin\blink.exe",
                          "uninstall"]) == (
        "& \"" + r"C:\Users\O'Brien\.blink\bin\blink.exe" + "\" uninstall")


@pytest.mark.parametrize("argv,line", [
    ([r"C:\Users\kfir\.blink\bin\blink.exe", "uninstall"],
     r"C:\Users\kfir\.blink\bin\blink.exe uninstall"),
    ([r"C:\Users\John Smith\.blink\bin\blink.exe", "uninstall"],
     r'& "C:\Users\John Smith\.blink\bin\blink.exe" uninstall'),
    # Only a quoted PROGRAM needs the call operator; a quoted argument
    # after a bare one is an ordinary PowerShell argument.
    ([r"C:\Python\python.exe", r"C:\my repo\blink_main.py", "status"],
     r'C:\Python\python.exe "C:\my repo\blink_main.py" status'),
])
def test_a_quoted_windows_program_gets_powershells_call_operator(argv,
                                                                 line):
    """Windows Terminal opens PowerShell, which reads a line starting with a
    quoted string as a string to print, not a program to run: the spaced
    profile's undo line printed its own path back and did nothing (review,
    2026-10-25). A line that needs no quoting is unchanged, and works in both
    shells."""
    assert cli._win_line(argv) == line


def test_installing_from_a_checkout_never_suggests_deleting_it(
        tmp_path, capsys, monkeypatch):
    """Nothing is copied out of a checkout, so the frozen build's "you can
    delete the folder you downloaded" pointed at the very folder the new
    install depends on."""
    as_checkout(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "delete the folder" not in out
    assert "can be deleted" not in out
    assert f"Creates    {cli.installed_bin()}" not in out
    assert cli._checkout_dir() in out
    assert "Leave" in out and "in place" in out
    assert f"{cli.command_to_type()} uninstall" in out
    assert f"{cli.command_to_type()} status" in out
    assert f"{cli.installed_bin()} uninstall" not in out
    assert f"{cli.installed_bin()} status" not in out


def test_a_frozen_install_says_exactly_what_it_always_said(tmp_path, capsys,
                                                           monkeypatch):
    """Customers run the frozen binary, and the commands it prints are the
    ones in the README and in every support answer: not one character of them
    may move. Pinned for a profile path that needs no quoting, which is the
    shape of every install that has ever worked; the case with a space is
    the one below."""
    as_frozen(monkeypatch)
    if cli.command_to_type() != cli.installed_bin():
        # The precondition: pytest's tmp path needs no quoting here. A
        # runner whose temp dir has a space or a non-ASCII name is the case
        # the tests below cover; this one pins the shape every install
        # that worked before has.
        pytest.skip("this runner's temp path needs quoting")
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    lines = capsys.readouterr().out.splitlines()
    b = cli.installed_bin()
    disclosure = [
        f"  Creates    {b}",
        "             a copy of this program and its support files, so the",
        "             folder you downloaded can be deleted when this finishes.",
    ]
    i = lines.index(disclosure[0])
    assert lines[i:i + 3] == disclosure
    assert f"  To undo all of it:  {b} uninstall" in lines
    j = lines.index(f"  Check:   {b} status")
    assert lines[j:j + 4] == [
        f"  Check:   {b} status",
        f"  Undo:    {b} uninstall",
        "",
        "  You can delete the folder you downloaded.",
    ]
    assert not any(cli._checkout_dir() in l for l in lines)


# Which backends really run _service_command(): per backend, in
# tests/pc/test_service.py. The Windows task runs the launcher script, which
# names ~\.blink\bin\blink.exe outright, and a Linux machine without systemd
# runs nothing; telling either that "the service runs this folder's
# blink_main.py" was false (review, 2026-10-25).

def test_nothing_runs_the_checkout_once_frozen(monkeypatch):
    as_frozen(monkeypatch)
    assert cli._service_runs_checkout() is False


def test_a_checkout_install_promises_the_service_only_where_it_runs_there(
        tmp_path, capsys, monkeypatch):
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_service_runs_checkout", lambda: False)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "The login service starts" not in out
    assert "the service starts" not in out
    # Still a checkout install, and still told not to delete the folder: the
    # commands it printed run from there.
    assert cli._checkout_dir() in out
    assert "delete the folder" not in out
    assert f"Leave {cli._checkout_dir()} in place" in out


@pytest.mark.parametrize("result,runs", [
    ("running (launchd)", True),
    ("running (systemd)", True),
    ("installed, but could not be started: launchctl bootstrap gui/501 x",
     False),
    ("no systemd here; run it yourself: python blink_main.py run", False),
    ("skipped (BLINK_SKIP_SERVICE=1)", False),
])
def test_a_checkout_install_says_the_service_runs_only_once_it_does(
        tmp_path, capsys, monkeypatch, result, runs):
    """The closing line follows what the service step returned, not what the
    platform would normally do: WSL with no user manager, a failed bootstrap
    and BLINK_SKIP_SERVICE all leave nothing running from the folder, and
    were told it did (review, 2026-10-25)."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_service_runs_checkout", lambda: True)
    monkeypatch.setattr(cli, "_install_service", lambda *a: result)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    lead = f"  Leave {cli._checkout_dir()} in place: "
    closing = [l[len(lead):] for l in out.splitlines() if l.startswith(lead)]
    assert closing == (["the service starts from there."] if runs
                       else ["the commands above start from there."])


def test_the_checkout_disclosure_names_the_service_where_it_will_run(
        capsys, monkeypatch):
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_service_runs_checkout", lambda: True)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    cli._announce()
    assert "The login service starts blink_main.py from" in \
        capsys.readouterr().out


def test_the_checkout_disclosure_promises_no_service_that_will_not_run_it(
        capsys, monkeypatch):
    """_skip_service is False here on purpose: conftest's BLINK_SKIP_SERVICE
    alone would hide the backend half of the gate, and a gate reduced to
    `not _skip_service()` would pass every other test (review,
    2026-10-25). Only _announce runs, which registers nothing."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_service_runs_checkout", lambda: False)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    cli._announce()
    out = capsys.readouterr().out
    assert "The login service starts" not in out
    assert "Every command printed below starts from this" in out


def test_the_checkout_disclosure_promises_no_service_it_will_skip(
        capsys, monkeypatch):
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_service_runs_checkout", lambda: True)
    cli._announce()                      # conftest's BLINK_SKIP_SERVICE=1
    out = capsys.readouterr().out
    assert "The login service starts" not in out
    assert "Every command printed below starts from this" in out


def test_a_windows_checkout_disclosure_says_no_task_is_registered(
        capsys, monkeypatch):
    """Said before anything is written, as install() itself returns it. The
    Windows backend is used directly: sys.platform stays real, so the
    command is quoted for this runner."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "backend", lambda: cli._SchtasksBackend())
    cli._announce()
    out = capsys.readouterr().out
    want = ("  A checkout on Windows gets no login service; start it by"
            f" hand with: {cli.command_to_type()} run")
    # The command ends the line: a trailing period was pasted with it, and
    # `run.` is not a subcommand (review, 2026-10-25).
    assert want in out.splitlines()
    assert "Scheduled Task" not in out


# `blink update` downloads the frozen build into ~/.blink/bin and restarts
# the service. Whether that does anything depends on what the SERVICE runs,
# not on which program typed the command: fleet desks run the checkout's CLI
# beside a frozen service, and refusing there (an earlier fix of this
# branch) took `update` away from exactly the desks that use it. Refused
# only where the download would change nothing anyone can see (review,
# 2026-10-25).

def _feed(monkeypatch):
    asked = []
    monkeypatch.setattr(cli.update, "fetch_signed_manifest",
                        lambda: asked.append(1))     # None: no answer
    return asked


@pytest.mark.parametrize("process,service,refused", [
    ("checkout", "frozen", False),     # the fleet desk
    ("frozen", "frozen", False),       # a customer
    ("frozen", None, False),           # a downloaded binary, never installed
    ("checkout", "checkout", True),    # a checkout install
    ("checkout", None, True),          # a bare checkout
    ("frozen", "checkout", True),      # the service runs a checkout anyway
])
def test_update_follows_what_the_service_runs(capsys, monkeypatch, process,
                                              service, refused):
    asked = _feed(monkeypatch)
    prog = {"frozen": [cli.installed_bin()], "checkout": cli._checkout_argv(),
            None: None}[service]
    monkeypatch.setattr(cli, "service_program", lambda: prog)
    (as_frozen if process == "frozen" else as_checkout)(monkeypatch)
    assert cli.main(["update"]) == 1
    out = capsys.readouterr().out
    assert ("Running from a checkout: update with git." in out) is refused
    assert asked == ([] if refused else [1])


def test_an_update_left_down_names_the_service_program(capsys, monkeypatch):
    """"Run `<checkout> install` to finish" would re-register the frozen
    service against the checkout."""
    as_checkout(monkeypatch)
    frozen = [cli.installed_bin()]
    monkeypatch.setattr(cli, "service_program", lambda: frozen)
    monkeypatch.setattr(cli.update, "fetch_signed_manifest", lambda: {})
    monkeypatch.setattr(cli.update, "available", lambda _m: ("9.9.9", {}))
    monkeypatch.setattr(cli.update, "platform_key", lambda: "k")
    monkeypatch.setattr(cli.update, "download", lambda *_a: b"")
    monkeypatch.setattr(cli.update, "apply", lambda *_a: (True, "applied"))
    monkeypatch.setattr(cli, "halt_service", lambda: None)
    monkeypatch.setattr(cli, "_refresh_shims", lambda: None)
    monkeypatch.setattr(cli, "restart_service", lambda: "could not restart it")
    assert cli.main(["update"]) == 1
    out = capsys.readouterr().out
    assert f"Run `{cli._quote_line(frozen)} install` to finish." in out


def test_a_board_ahead_of_a_checkout_is_not_sent_to_update():
    from pc.version import RELEASE_VERSION
    a, b, c = RELEASE_VERSION.split(".")
    out = _board_lines_for(f"{a}.{b}.{int(c) + 1}", can_update=False)
    assert "they ship together" in out
    assert " update" not in out.replace("they ship together", "")
    assert "git" in out


@pytest.mark.parametrize("process,service,can", [
    ("checkout", "frozen", True),
    ("checkout", None, False),
    ("frozen", None, True),
])
def test_status_tells_board_lines_whether_update_can_run(monkeypatch,
                                                         process, service,
                                                         can):
    seen = {}

    def _capture(*a, **kw):
        seen.update(kw)
        return []
    monkeypatch.setattr(cli, "board_lines", _capture)
    import claude_usage_bridge
    monkeypatch.setattr(claude_usage_bridge, "describe_ports", lambda: [])
    monkeypatch.setattr(claude_usage_bridge, "remembered_board",
                        lambda _h: {})
    monkeypatch.setattr(cli, "service_program",
                        lambda: [cli.installed_bin()] if service else None)
    (as_frozen if process == "frozen" else as_checkout)(monkeypatch)
    cli._board_lines()
    assert seen.get("can_update") is can


def test_a_frozen_install_under_a_spaced_profile_prints_pasteable_commands(
        tmp_path, capsys, monkeypatch):
    as_frozen(monkeypatch)
    _home_with_a_space(tmp_path, monkeypatch)
    assert cli.main(["install"]) == 0
    lines = capsys.readouterr().out.splitlines()
    b = cli.installed_bin()
    undo = [l for l in lines if l.startswith("  Undo:    ")]
    check = [l for l in lines if l.startswith("  Check:   ")]
    assert len(undo) == 1 and len(check) == 1
    assert _split_like_the_shell(undo[0][len("  Undo:    "):]) == [
        b, "uninstall"]
    assert _split_like_the_shell(check[0][len("  Check:   "):]) == [
        b, "status"]


def test_the_frozen_disclosure_under_a_spaced_profile_pastes_back(
        tmp_path, capsys, monkeypatch):
    """The disclosure's undo line, which tests/ci/check_install.sh greps for
    in its spaced-home scenario. Quoting it broke that grep on all three
    runners (review, 2026-10-25); the grep now also takes the binary's name
    followed by the closing quote, which is what this prints."""
    as_frozen(monkeypatch)
    _home_with_a_space(tmp_path, monkeypatch)
    cli._announce()
    lines = capsys.readouterr().out.splitlines()
    undo = [l for l in lines if l.startswith("  To undo all of it:  ")]
    assert len(undo) == 1
    cmd = undo[0][len("  To undo all of it:  "):]
    assert _split_like_the_shell(cmd) == [cli.installed_bin(), "uninstall"]
    name = os.path.basename(cli.installed_bin())
    assert any(f"{name}{q} uninstall" in cmd for q in ("'", '"'))


def test_a_checkout_under_a_folder_with_an_apostrophe_pastes_back(
        tmp_path, monkeypatch):
    """shlex escapes the quote, so the printed line holds neither path as a
    substring; it still has to split back into the same argv."""
    as_checkout(monkeypatch)
    root = str(tmp_path / "kfir's checkout")
    monkeypatch.setattr(cli, "_checkout_dir", lambda: root)
    assert _split_like_the_shell(cli.command_to_type()) == cli._checkout_argv()
    assert cli._checkout_argv()[1] == os.path.join(root, "blink_main.py")


def test_a_checkout_install_leaves_no_empty_program_folder(tmp_path,
                                                           monkeypatch):
    """Nothing is copied from a checkout, and the disclosure does not mention
    ~/.blink/bin, so install must not create it either."""
    as_checkout(monkeypatch)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    assert not os.path.exists(cli.bin_dir())
    assert cli.main(["uninstall"]) == 0


# A hint that repairs a service which already exists must name the program
# that service runs, not the one printing the hint. Fleet desks run the
# checkout's CLI next to a live frozen install; told to run the checkout's
# `install`, they would silently swap the frozen service for a checkout one
# (review, 2026-10-25). The service files are written here by the backends'
# own writers; no launchctl, systemctl or schtasks is run.

def _write_frozen_service_files(home, monkeypatch):
    """The plist, the unit and the launcher a frozen install writes, and its
    binary, then back to being a checkout process -- the fleet desk. Each
    lands inside the caller's `home` or not at all (tests/support/home.py):
    from a script with the real HOME they would replace the desk's own
    service files and truncate its installed program."""
    as_frozen(monkeypatch)
    frozen_argv = cli._service_command()
    _write_plist(home, frozen_argv)
    unit = inside(home, cli.unit_path())
    os.makedirs(os.path.dirname(unit), exist_ok=True)
    with open(unit, "w", encoding="utf-8") as f:
        f.write(cli._unit_text(frozen_argv))
    launcher = inside(home, cli.launcher_path())
    os.makedirs(os.path.dirname(launcher), exist_ok=True)
    with open(launcher, "w", encoding="utf-8") as f:
        f.write(cli.LAUNCHER_VBS)
    program = inside(home, cli.installed_bin())
    os.makedirs(os.path.dirname(program), exist_ok=True)
    open(program, "w").close()
    as_checkout(monkeypatch)
    return frozen_argv[:-1]


def _write_plist(home, argv):
    """The plist launchd would be handed for `argv`, by the backend's own
    writer, so the reader is tested against what install really writes.
    Inside the caller's `home` only: this is the desk's real LaunchAgent
    otherwise."""
    plist = inside(home, cli.plist_path())
    os.makedirs(os.path.dirname(plist), exist_ok=True)
    with open(plist, "w", encoding="utf-8") as f:
        f.write(cli._plist_text(argv))


@pytest.mark.parametrize("platform", ["darwin", "linux", "win32"])
def test_the_registered_service_names_its_own_program(
        tmp_path, monkeypatch, platform):
    """Only the service-file reader runs under the forced platform: it returns
    a list, so no quoting is decided here."""
    want = _write_frozen_service_files(tmp_path, monkeypatch)
    monkeypatch.setattr(sys, "platform", platform)
    if platform == "win32":
        # The launcher names blink.exe outright, whatever wrote it; that
        # binary, which the helper on this runner's platform may not have
        # made, is what has to exist.
        open(cli.installed_bin(), "w").close()
        want = [cli.installed_bin()]
    assert cli.service_program() == want


def test_the_registered_service_survives_a_space_in_the_profile(
        tmp_path, monkeypatch):
    _home_with_a_space(tmp_path, monkeypatch)
    want = _write_frozen_service_files(tmp_path, monkeypatch)
    assert " " in want[0]
    for platform in ("darwin", "linux"):
        monkeypatch.setattr(sys, "platform", platform)
        assert cli.service_program() == want


def test_repair_hints_name_the_service_program(monkeypatch):
    as_checkout(monkeypatch)
    frozen = os.path.join(cli.bin_dir(), "blink")
    monkeypatch.setattr(cli, "service_program", lambda: [frozen])
    assert cli._repair_command() == frozen
    assert f"run `{frozen} install` to fix it" in cli._shim_repair_line(
        "x", bridge_running=False)
    assert cli.command_to_type() not in cli._shim_repair_line("x", False)


def test_repair_hints_fall_back_to_this_program_with_no_service(monkeypatch):
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "service_program", lambda: None)
    assert cli._repair_command() == cli.command_to_type()


def _skip_without_a_service_file_format():
    if cli.service_program() is None:
        pytest.skip(f"no service file format on {sys.platform}")


def test_the_codex_hint_names_the_frozen_service_from_a_checkout(
        tmp_path, monkeypatch):
    """End to end on this platform's own service file, through a real hint."""
    want = _write_frozen_service_files(tmp_path, monkeypatch)
    _skip_without_a_service_file_format()
    monkeypatch.setattr(cli.install_codex_hooks, "_read_marker", lambda: None)
    line = cli._codex_hook_status()[0]
    assert f"run `{cli._quote_line(want)} install`" in line
    assert cli.command_to_type() not in line


def test_the_partial_hooks_hint_names_the_frozen_service_from_a_checkout(
        tmp_path, capsys, monkeypatch):
    _settings(tmp_path, {})
    cli.main(["install"])
    data = json.loads(_settings(tmp_path).read_text())
    del data["hooks"]["Stop"]
    _settings(tmp_path).write_text(json.dumps(data))
    want = _write_frozen_service_files(tmp_path, monkeypatch)
    _skip_without_a_service_file_format()
    capsys.readouterr()
    cli.main(["status"])
    partial = [l for l in capsys.readouterr().out.splitlines()
               if l.startswith("Activity    PARTIAL")]
    assert partial and f"run `{cli._quote_line(want)} install`" in partial[0]
    assert cli.command_to_type() not in partial[0]


def test_launchd_not_loaded_names_the_frozen_service_from_a_checkout(
        tmp_path, monkeypatch):
    want = _write_frozen_service_files(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "backend", lambda: cli._LaunchdBackend())
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    monkeypatch.setattr(
        cli.subprocess, "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 113, "", ""))
    line = cli._LaunchdBackend().status()
    assert line == (f"installed but not loaded -- run "
                    f"`{cli._quote_line(want)} install` to start it")


def _verdict(home, has_window, at, kind="statusline_sustained"):
    """The verdict the daemon leaves in ~/.blink (pc/session_verdict),
    inside the caller's `home` or not at all (tests/support/home.py)."""
    from pc import session_verdict
    session_verdict.save(inside(home, session_verdict.verdict_path()),
                         session_verdict.evidence(has_window, kind, at))


def test_status_says_when_the_account_has_no_five_hour_window(
        tmp_path, capsys):
    """The mark rests on evidence that can be misread -- a vendor rename of
    five_hour, a Desktop history whose fh key went for another reason -- so
    this row says it and what proved it. It is how support tells a real
    Ultra account from a misread one (review, 2026-10-31). The row reads
    the verdict the running daemon persisted, and a fresh `blink status`
    writes nothing."""
    from pc import session_verdict
    from tests.pc.test_claude_cli import payload
    _settings(tmp_path, {})
    cli.main(["install"])
    status = inside(tmp_path, statusline_source_path())
    with open(status, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload(five_hour=None, seven_day=2.0)))
    _verdict(tmp_path, False, time.time() - 60)
    before = open(session_verdict.verdict_path(), "rb").read()
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    assert ("Session     no five-hour window (Claude Code replied for over"
            " five hours without one)") in capsys.readouterr().out

    with open(status, "w", encoding="utf-8") as f:
        # Rendered after the proof, so it lifts it, whenever its window
        # opened: here three hours before (claude_cli._frame).
        f.write(json.dumps(payload(five_hour=27.0,
                                   resets_at=time.time() + 2 * 3600)))
    assert cli.main(["status"]) == 0
    assert "Session     " not in capsys.readouterr().out
    assert open(session_verdict.verdict_path(), "rb").read() == before


def test_status_names_the_ultra_fast_path(tmp_path, capsys):
    """A verdict the Ultra fast path saved (pc/claude_plan; owner's
    request, 2026-11-01) is said as such, not as five hours of replies."""
    from tests.pc.test_claude_cli import payload
    _settings(tmp_path, {})
    cli.main(["install"])
    with open(inside(tmp_path, statusline_source_path()), "w",
              encoding="utf-8") as f:
        f.write(json.dumps(payload(five_hour=None, seven_day=2.0)))
    _verdict(tmp_path, False, time.time() - 60, kind="statusline_ultra")
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    assert ("Session     no five-hour window (Claude Code reports an Ultra"
            " plan, and its replies come back without one)"
            ) in capsys.readouterr().out


def test_status_on_a_marked_desk_with_no_reading_yet_still_prints_the_row(
        tmp_path, capsys):
    """A Claude Code-only Ultra desk, its verdict saved, in a session that
    has not had its first reply: the status line carries no rate_limits.
    The daemon remembers its last reading and is marked and sending the
    burn line; a fresh `blink status` has nothing to merge, and the row
    went missing with the page (/code-review max, 2026-11-01). With no
    reading at all there is no figure the row could contradict, so it
    says what the saved verdict says, for `--wire` too."""
    from pc import session_verdict
    _settings(tmp_path, {})
    cli.main(["install"])
    with open(inside(tmp_path, statusline_source_path()), "w",
              encoding="utf-8") as f:
        f.write(json.dumps({"session_id": "s1",
                            "cost": {"total_cost_usd": 0.0}}))
    _verdict(tmp_path, False, time.time() - 60)
    before = open(session_verdict.verdict_path(), "rb").read()
    capsys.readouterr()

    row = ("Session     no five-hour window (Claude Code replied for over"
           " five hours without one)")
    assert cli.main(["status"]) == 0
    assert row in capsys.readouterr().out
    assert cli.main(["status", "--wire"]) == 0
    assert capsys.readouterr().out.count(row) == 1
    assert open(session_verdict.verdict_path(), "rb").read() == before

    # A window on disk, or no verdict at all, and the row stays away.
    _verdict(tmp_path, True, time.time() - 30, kind="statusline_window")
    assert cli.main(["status"]) == 0
    assert "Session     " not in capsys.readouterr().out
    os.remove(session_verdict.verdict_path())
    assert cli.main(["status"]) == 0
    assert "Session     " not in capsys.readouterr().out


def statusline_source_path():
    from pc import statusline_source
    return statusline_source.payload_path()


def test_status_on_a_pro_desk_after_a_reset_says_nothing(tmp_path, capsys):
    """Finding 5. `blink status` is a fresh process, and it read a Pro or
    Max status line rendered after a reset -- weekly-only, byte for byte an
    Ultra one -- as no window, while the daemon beside it was drawing the
    session. Quiet proves nothing now, and the daemon's verdict, a window,
    is on disk."""
    from tests.pc.test_claude_cli import payload
    _settings(tmp_path, {})
    cli.main(["install"])
    with open(inside(tmp_path, statusline_source_path()), "w",
              encoding="utf-8") as f:
        f.write(json.dumps(payload(five_hour=None, seven_day=30.0)))
    capsys.readouterr()

    for verdict in (None, (True, time.time() - 3 * 3600)):
        if verdict is not None:
            _verdict(tmp_path, *verdict, kind="statusline_window")
        assert cli.main(["status"]) == 0
        assert "Session     " not in capsys.readouterr().out, verdict
        assert cli.main(["status", "--wire"]) == 0
        out = capsys.readouterr().out
        assert "Session     " not in out, verdict


def test_status_on_a_desktop_only_ultra_desk_prints_the_row(tmp_path,
                                                           capsys):
    """Claude Desktop's Code tab never runs the status line, so a desk with
    Desktop alone has its proof in Desktop's history: weekly usage risen
    between two samples one poll apart, neither with an fh key (owner's
    decision and the re-review, 2026-11-01). A single such sample is not
    proof, and nor is a rise from a sample that had fh."""
    now_ms = int(time.time() * 1000)
    rows = []
    for t_ms, sd in ((now_ms - 1_200_000, 11), (now_ms - 300_000, 12)):
        s = _dc.sample(t_ms, sd=sd)
        del s["u"]["fh"]
        rows.append(s)

    _desktop_cache(tmp_path, _dc.doc(rows[1:]))
    assert cli.main(["status"]) == 0
    assert "Session     " not in capsys.readouterr().out

    _desktop_cache(tmp_path, _dc.doc(rows))
    assert cli.main(["status"]) == 0
    assert ("Session     no five-hour window (Claude Desktop's weekly usage"
            " rose with no five-hour figure before or after)"
            ) in capsys.readouterr().out

    had_fh = _dc.sample(now_ms - 1_200_000, fh=3, sd=11)
    _desktop_cache(tmp_path, _dc.doc([had_fh, rows[1]]))
    assert cli.main(["status"]) == 0
    assert "Session     " not in capsys.readouterr().out

    pro = _dc.sample(now_ms - 300_000, fh=4, sd=12)
    _desktop_cache(tmp_path, _dc.doc([pro]))
    assert cli.main(["status"]) == 0
    assert "Session     " not in capsys.readouterr().out


def _ultra_desk_with_an_open_desktop_window(home, monkeypatch):
    """A weekly-only status line and an hour-old "no window" verdict beside
    a Claude Desktop Local Storage record, written since, whose window is
    still open: proof newer than the verdict that the account has a window.
    The record is generated and handed over where the LevelDB reader would
    return it.

    `blink install` and the status line land inside the caller's `home` or
    not at all (tests/support/home.py): from a script with the real HOME,
    install would rewrite the desk's own Claude Code settings and the file
    write would replace its live statusline.json. Both paths are asked of
    the code, and checked, before anything is written."""
    from pc import desktop_local_storage, statusline_source
    from tests.pc.test_claude_cli import payload
    status = inside(home, statusline_source.payload_path())
    inside(home, cli.settings_path())
    _settings(home, {})
    cli.main(["install"])
    with open(status, "w", encoding="utf-8") as f:
        f.write(json.dumps(payload(five_hour=None, seven_day=2.0)))
    now = time.time()
    # An hour-old verdict that the account has no window, which the record
    # below, written since, overrules (pc/session_verdict).
    _verdict(home, False, now - 3600)
    record = {"resetsAt": now + 4 * 3600, "utilization": 0.08,
              "observedAt": now - 300}
    monkeypatch.setattr(desktop_local_storage, "newest_record",
                        lambda path: dict(record))


def test_the_ultra_desk_helper_writes_nothing_outside_the_callers_home(
        tmp_path, monkeypatch):
    """Handed a home that is not the one the code resolves -- a script
    importing it with the real HOME -- it stops before `blink install`
    runs, and before the status line is written."""
    elsewhere = tmp_path / "not-home"
    elsewhere.mkdir()
    ran = []
    monkeypatch.setattr(cli, "main", lambda argv: ran.append(argv))

    with pytest.raises(AssertionError, match="outside the caller's home"):
        _ultra_desk_with_an_open_desktop_window(elsewhere, monkeypatch)

    assert ran == []
    assert not (tmp_path / ".blink" / "statusline.json").exists()
    assert list(elsewhere.iterdir()) == []


def test_the_session_row_follows_the_merge_not_one_provider(
        tmp_path, capsys, monkeypatch):
    """The row used to come from a provider of its own, which knew nothing
    of a Local Storage window that lifts the rule in the merge: it printed
    "no five-hour window" for a desk whose daemon was drawing 8% (review,
    2026-10-31)."""
    _ultra_desk_with_an_open_desktop_window(tmp_path, monkeypatch)
    capsys.readouterr()

    assert cli.main(["status"]) == 0
    assert "Session     " not in capsys.readouterr().out


def test_status_wire_prints_the_row_and_the_line_from_one_poll(
        tmp_path, capsys, monkeypatch):
    _ultra_desk_with_an_open_desktop_window(tmp_path, monkeypatch)
    capsys.readouterr()

    assert cli.main(["status", "--wire"]) == 0
    out = capsys.readouterr().out
    wire = json.loads(out.strip().splitlines()[-1])
    assert wire["session_pct"] == 8.0 and wire["src"] == "desktop_ls"
    assert "Session     " not in out

    from pc import desktop_local_storage
    monkeypatch.setattr(desktop_local_storage, "newest_record",
                        lambda path: None)        # the window closes
    assert cli.main(["status", "--wire"]) == 0
    out = capsys.readouterr().out
    wire = json.loads(out.strip().splitlines()[-1])
    assert wire["session_pct"] == -1 and wire["src"] == "cli"
    assert out.count("Session     no five-hour window") == 1


def test_the_disclosure_names_every_usage_source_the_daemon_reads(capsys):
    """The install note said the figures come from "Claude Code, Claude
    Desktop's usage cache, or the Codex session log", and left out Desktop's
    Local Storage, which pc/desktop_local_storage.py reads for the five-hour
    figure and its countdown (review follow-up, 2026-11-01). Install asks
    nothing, so this sentence is the disclosure; it has to be the whole
    list."""
    cli._announce()
    said = " ".join(l.strip() for l in capsys.readouterr().out.splitlines())
    assert ("The usage figures come from Claude Code, Claude Desktop's usage"
            " cache and Local Storage, or the Codex session log") in said


# The burn view's folder in the install disclosure. It printed
# burn_reader.transcripts_root() as the installing shell resolves it, which
# honours CLAUDE_CONFIG_DIR -- but the launchd and systemd services carry
# only PYTHONUNBUFFERED, and the Windows task gets the account's registry
# environment, not this window's. So on a desk that exports the variable in
# .zshrc the note named a folder the daemon never reads (2026-11-01
# re-review). It names the service's folder now, and says so when the shell
# would have named another.

SHELL_ONLY = "  CLAUDE_CONFIG_DIR is set in this shell, but the background"


def _burn_folder(out):
    lines = out.splitlines()
    i = lines.index("  output-token count of each reply from Claude Code's"
                    " transcripts:")
    return lines[i + 1].strip(), lines[i + 2:]


def _caveat(after):
    """The CLAUDE_CONFIG_DIR caveat's lines among `after`: the lines from
    the plan paragraph's last up to the Codex paragraph. It is said once,
    after both the transcripts' folder and the plan file (the review of
    PR #6, 2026-11-01: said twice, a test of the first was satisfied by
    the second, and deleting the first print failed nothing)."""
    end = after.index("  break of over an hour.")
    out = []
    for l in after[end + 1:]:
        if l.startswith("  If Codex") or not l.startswith("  "):
            break
        out.append(l)
    return out


def _with_a_service(monkeypatch, service_sees=None, codex_sees=None):
    # Patched on the class backend() actually returns, not on _Backend: the
    # launchd and schtasks backends override creates(), and schtasks also
    # overrides service_environ() to read the registry, so a patch on the
    # base class was silently shadowed on Windows (windows VM run,
    # 2026-11-01: the folder came out as the shell's). started_from_shell
    # and shell_until_logon too, or a Linux runner with no systemctl, or
    # Windows, would answer for itself.
    kind = type(cli.backend())
    sees = {"CLAUDE_CONFIG_DIR": service_sees, "CODEX_HOME": codex_sees}
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(kind, "creates", lambda self: "a service")
    monkeypatch.setattr(kind, "service_environ",
                        lambda self, name: sees[name])
    monkeypatch.setattr(kind, "started_from_shell", lambda self: False)
    monkeypatch.setattr(kind, "shell_until_logon", lambda self: False)


def test_the_burn_folder_is_the_one_the_service_reads(tmp_path, capsys,
                                                      monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    folder, after = _burn_folder(out)
    assert folder == str(tmp_path / ".claude" / "projects")
    caveat = _caveat(after)
    assert caveat and caveat[0].startswith(SHELL_ONLY), after
    assert ("so it reads the folder and the file above."
            in " ".join(l.strip() for l in caveat))
    assert out.count(SHELL_ONLY) == 1


def test_without_claude_config_dir_nothing_more_is_said(tmp_path, capsys,
                                                         monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / ".claude" / "projects")
    assert "CLAUDE_CONFIG_DIR" not in out


def test_a_service_that_sees_claude_config_dir_reads_it(tmp_path, capsys,
                                                         monkeypatch):
    """Windows, where it is a user environment variable (setx): the task
    starts with it, so the folder is the shell's and nothing more is said."""
    as_checkout(monkeypatch)
    work = str(tmp_path / "work-config")
    _with_a_service(monkeypatch, service_sees=work)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", work)
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == os.path.join(work, "projects")
    assert "CLAUDE_CONFIG_DIR" not in out


def test_with_no_service_the_folder_is_this_shells(tmp_path, capsys,
                                                   monkeypatch):
    """No service is registered (conftest's BLINK_SKIP_SERVICE, a Windows
    checkout): the daemon is started by hand from a shell, which does see
    the variable."""
    as_checkout(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / "work-config" / "projects")
    assert SHELL_ONLY not in out


def test_launchd_and_systemd_hand_the_service_no_claude_config_dir(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work-codex"))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        assert cli._LaunchdBackend().service_environ(name) is None
        assert cli._SystemdBackend().service_environ(name) is None


def test_a_service_that_sees_another_value_is_not_said_to_miss_it(
        tmp_path, capsys, monkeypatch):
    """A `setx` to a new folder, typed in a window that still has the old
    one: the service sees the variable, just not this window's value, and
    "does not see it" was false (2026-11-01 third review)."""
    as_checkout(monkeypatch)
    new = str(tmp_path / "new")
    _with_a_service(monkeypatch, service_sees=new)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "old"))
    cli._announce()
    folder, after = _burn_folder(capsys.readouterr().out)
    said = " ".join(l.strip() for l in after)
    assert folder == os.path.join(new, "projects")
    assert ("The background service sees a different CLAUDE_CONFIG_DIR"
            " from this shell's, so it reads the folder and the file"
            " above.") in said
    assert "does not see it" not in said


def test_a_service_that_sees_what_this_shell_does_not_says_so(
        tmp_path, capsys, monkeypatch):
    as_checkout(monkeypatch)
    work = str(tmp_path / "work-config")
    _with_a_service(monkeypatch, service_sees=work)
    cli._announce()
    folder, after = _burn_folder(capsys.readouterr().out)
    said = " ".join(l.strip() for l in after)
    assert folder == os.path.join(work, "projects")
    assert ("CLAUDE_CONFIG_DIR is set for the background service but not in"
            " this shell, so it reads the folder and the file above.") in said


def _plan_file(out):
    lines = out.splitlines()
    i = lines.index("  one, it reads Claude Code's config to learn the plan:")
    return lines[i + 1].strip(), lines[i + 2:]


def test_the_install_note_says_what_is_taken_from_claude_json(
        tmp_path, capsys, monkeypatch):
    """pc/claude_plan parses all of Claude Code's config, which also holds
    the account's email address and name and can hold MCP servers' tokens,
    to look at two fields. The note says so: "nothing else from the file"
    and "reads no token" were not true of a whole-file parse (the review
    of PR #6, 2026-11-01). Five and a quarter hours is CHAIN_SPAN_S."""
    as_checkout(monkeypatch)
    cli._announce()
    out = capsys.readouterr().out
    folder, after = _plan_file(out)
    assert folder == str(tmp_path / ".claude.json")
    said = " ".join(l.strip() for l in out.splitlines())
    assert ("The whole file is parsed in memory. It can hold your MCP"
            " servers' tokens and, on Linux and Windows, an API key; only the"
            " plan name and rate-limit tier in it are looked at, only whether"
            " they say Ultra is kept, and nothing from it is logged, printed"
            " or saved.") in said
    assert ("On the Ultra plan one such reply switches on the burn view;"
            " other plans need five and a quarter hours of them, longer"
            " after a break of over an hour.") in said
    from pc.providers import claude_cli
    assert claude_cli.CHAIN_SPAN_S == 5 * 3600 + 15 * 60
    assert claude_cli.CHAIN_MAX_GAP_S == 3600
    assert "reads no credential" not in said
    assert "It keeps no credential and no token" in said


def test_the_plan_file_is_the_one_the_service_reads(tmp_path, capsys,
                                                    monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    folder, after = _plan_file(out)
    assert folder == str(tmp_path / ".claude.json")
    said = " ".join(l.strip() for l in _caveat(after))
    assert ("CLAUDE_CONFIG_DIR is set in this shell, but the background"
            " service does not see it, so it reads the folder and the file"
            " above.") in said
    assert out.count(SHELL_ONLY) == 1


def test_with_no_service_the_plan_file_follows_claude_config_dir(
        tmp_path, capsys, monkeypatch):
    as_checkout(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    folder, _ = _plan_file(capsys.readouterr().out)
    assert folder == str(tmp_path / "work-config" / ".claude.json")


# The same for CODEX_HOME, which codex_cli honours and a login service does
# not carry either: the note printed codex_cli.sessions_root() as this shell
# resolves it (2026-11-01 third review).

def _codex_folder(out):
    lines = out.splitlines()
    i = lines.index("  output tokens:")
    return lines[i + 1].strip(), " ".join(l.strip() for l in lines[i + 2:])


def test_the_codex_folder_is_the_one_the_service_reads(tmp_path, capsys,
                                                       monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work-codex"))
    cli._announce()
    folder, said = _codex_folder(capsys.readouterr().out)
    assert folder == str(tmp_path / ".codex" / "sessions")
    assert ("CODEX_HOME is set in this shell, but the background service"
            " does not see it, so every Codex figure, the rate limits"
            " included, comes from the folder above.") in said


def test_without_codex_home_nothing_more_is_said(tmp_path, capsys,
                                                  monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    cli._announce()
    out = capsys.readouterr().out
    assert _codex_folder(out)[0] == str(tmp_path / ".codex" / "sessions")
    assert "CODEX_HOME" not in out


def test_a_service_that_sees_codex_home_reads_it(tmp_path, capsys,
                                                 monkeypatch):
    as_checkout(monkeypatch)
    work = str(tmp_path / "work-codex")
    _with_a_service(monkeypatch, codex_sees=work)
    monkeypatch.setenv("CODEX_HOME", work)
    cli._announce()
    out = capsys.readouterr().out
    assert _codex_folder(out)[0] == os.path.join(work, "sessions")
    assert "CODEX_HOME" not in out


@pytest.mark.parametrize("codex_home", [None, "work-codex"])
def test_the_disclosures_codex_folder_is_codex_clis(tmp_path, monkeypatch,
                                                    codex_home):
    """_codex_sessions_root repeats codex_cli.sessions_root's two lines
    with the variable as an argument; this keeps them the same."""
    from pc.providers import codex_cli
    value = str(tmp_path / codex_home) if codex_home else None
    if value:
        monkeypatch.setenv("CODEX_HOME", value)
    assert cli._codex_sessions_root(value) == codex_cli.sessions_root()


# Who actually starts the daemon. A truthy creates() was read as "a service
# with its own environment runs it", which is false on two desks (2026-11-01
# third review).

def test_linux_without_systemctl_names_this_shells_folders(tmp_path, capsys,
                                                           monkeypatch):
    """install writes the unit and says "no systemd here; run it yourself",
    and a daemon started by hand reads this shell's folders."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SystemdBackend())
    monkeypatch.setattr(cli._SystemdBackend, "_has_systemctl",
                        staticmethod(lambda: False))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work-codex"))
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / "work-config" / "projects")
    assert _codex_folder(out)[0] == str(tmp_path / "work-codex" / "sessions")
    assert "background service" not in out


def test_linux_with_systemd_names_the_units_folders(tmp_path, capsys,
                                                    monkeypatch):
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SystemdBackend())
    monkeypatch.setattr(cli._SystemdBackend, "_has_systemctl",
                        staticmethod(lambda: True))
    monkeypatch.setattr(cli._SystemdBackend, "_booted",
                        staticmethod(lambda: True))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    folder, after = _burn_folder(out)
    assert folder == str(tmp_path / ".claude" / "projects")
    caveat = _caveat(after)
    assert caveat and caveat[0].startswith(SHELL_ONLY), after
    assert out.count(SHELL_ONLY) == 1
    assert not any("Run key" in l for l in after)


def test_a_windows_checkout_names_this_shells_folders(tmp_path, capsys,
                                                      monkeypatch):
    """A checkout on Windows registers nothing and is told to start the
    daemon by hand."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SchtasksBackend())
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg({}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / "work-config" / "projects")
    assert "background service" not in out


def test_the_windows_run_key_start_is_said_to_keep_this_window(
        tmp_path, capsys, monkeypatch):
    """When schtasks refuses the task, install starts the launcher itself
    (_start_launcher's Popen has no env=), so until the next logon the
    daemon has this window's variable, not the registry's. Install cannot
    know beforehand which way Windows goes, so it says both."""
    as_frozen(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SchtasksBackend())
    reg = str(tmp_path / "reg")
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg(
        {("HKCU", "CLAUDE_CONFIG_DIR"): (reg, 1)}))
    window = str(tmp_path / "window")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", window)
    cli._announce()
    folder, after = _burn_folder(capsys.readouterr().out)
    said = " ".join(l.strip() for l in after)
    assert folder == os.path.join(reg, "projects")
    assert ("If Windows refuses the task and the Run key is used instead,"
            " the bridge started now keeps this window's CLAUDE_CONFIG_DIR"
            " until you next log in, and reads this folder and this file"
            " until then: " + os.path.join(window, "projects") + " "
            + os.path.join(window, ".claude.json")) in said
    assert said.count("Run key") == 1


@pytest.mark.parametrize("make, frozen, systemctl", [
    (cli._Backend, False, None),
    (cli._LaunchdBackend, False, None),
    (cli._SchtasksBackend, False, None),
    (cli._SchtasksBackend, True, None),
    (cli._SystemdBackend, False, False),
    (cli._SystemdBackend, False, True),
])
def test_a_backend_that_creates_nothing_is_started_from_a_shell(
        monkeypatch, make, frozen, systemctl):
    """_service_folder asks only started_from_shell(): it used to ask
    `not creates()` as well, which every backend already answers through
    started_from_shell(). Pinned, so a backend that breaks that is seen."""
    (as_frozen if frozen else as_checkout)(monkeypatch)
    if systemctl is not None:
        monkeypatch.setattr(cli._SystemdBackend, "_has_systemctl",
                            staticmethod(lambda: systemctl))
        monkeypatch.setattr(cli._SystemdBackend, "_booted",
                            staticmethod(lambda: True))
    b = make()
    assert b.creates() or b.started_from_shell()


def test_a_window_without_the_variable_is_not_said_to_keep_it(
        tmp_path, capsys, monkeypatch):
    """`setx CODEX_HOME D:\\codex`, then install from a window opened before
    it. The Run-key paragraph said the bridge "keeps this window's
    CODEX_HOME" right under a line saying this window has none (fourth
    review, 2026-11-01). The folder was right: _start_launcher inherits a
    window without the variable, so the bridge reads the default."""
    as_frozen(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SchtasksBackend())
    reg = str(tmp_path / "reg-codex")
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg(
        {("HKCU", "CODEX_HOME"): (reg, 1)}))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    cli._announce()
    folder, said = _codex_folder(capsys.readouterr().out)
    assert folder == os.path.join(reg, "sessions")
    assert "CODEX_HOME is set for the background service but not in this" \
        " shell" in said
    assert "keeps this window's" not in said
    assert ("If Windows refuses the task and the Run key is used instead,"
            " this window has no CODEX_HOME, so until you next log in the"
            " bridge started now reads this folder: "
            + str(tmp_path / ".codex" / "sessions")) in said


class _FakeWinreg:
    """Just enough of winreg for the environment keys."""
    HKEY_CURRENT_USER, HKEY_LOCAL_MACHINE = "HKCU", "HKLM"
    REG_SZ, REG_EXPAND_SZ = 1, 2

    def __init__(self, values):
        self.values = values            # {(hive, name): (value, type)}

    def OpenKey(self, hive, path):
        assert path in ("Environment", cli.SYSTEM_ENV_KEY)
        fake = self

        class _Key:
            def __enter__(self):
                return hive

            def __exit__(self, *a):
                return False
        if not any(h == hive for h, _ in fake.values):
            raise FileNotFoundError(2, "no key")
        return _Key()

    def QueryValueEx(self, hive, name):
        try:
            return self.values[(hive, name)]
        except KeyError:
            raise FileNotFoundError(2, "no value") from None

    @staticmethod
    def ExpandEnvironmentStrings(s):
        return s.replace("%USERPROFILE%", r"C:\Users\k")


@pytest.mark.parametrize("values, want", [
    ({}, None),
    ({("HKLM", "CLAUDE_CONFIG_DIR"): (r"D:\cc", 1)}, r"D:\cc"),
    ({("HKCU", "CLAUDE_CONFIG_DIR"): (r"E:\mine", 1),
      ("HKLM", "CLAUDE_CONFIG_DIR"): (r"D:\cc", 1)}, r"E:\mine"),
    ({("HKCU", "CLAUDE_CONFIG_DIR"): (r"%USERPROFILE%\cc", 2)},
     r"C:\Users\k\cc"),
    ({("HKCU", "CLAUDE_CONFIG_DIR"): ("", 1)}, None),
])
def test_the_windows_task_sees_the_accounts_environment(monkeypatch, values,
                                                        want):
    """Task Scheduler (and the Run key's Explorer) start the daemon with the
    account's environment from the registry -- the user's variable over the
    machine's -- and never with the installing window's."""
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg(values))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", r"F:\only-in-this-window")
    assert cli._SchtasksBackend().service_environ("CLAUDE_CONFIG_DIR") == want


def test_the_windows_task_sees_codex_home_the_same_way(monkeypatch):
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg(
        {("HKCU", "CODEX_HOME"): (r"%USERPROFILE%\cx", 2),
         ("HKLM", "CODEX_HOME"): (r"D:\cx", 1)}))
    monkeypatch.setenv("CODEX_HOME", r"F:\only-in-this-window")
    assert cli._SchtasksBackend().service_environ("CODEX_HOME") == \
        r"C:\Users\k\cx"


@pytest.mark.parametrize("value, kind", [
    (7, 4),                         # REG_DWORD reads back an int
    ([r"D:\a", r"D:\b"], 7),        # REG_MULTI_SZ, a list
    (b"D:\\x", 3),                  # REG_BINARY, bytes
])
def test_a_registry_value_that_is_not_a_string_is_unset(
        tmp_path, capsys, monkeypatch, value, kind):
    """It crashed install in expanduser() (2026-11-01 third review). Read as
    unset at that hive, so the machine's value, here none, applies."""
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg(
        {("HKCU", "CLAUDE_CONFIG_DIR"): (value, kind),
         ("HKCU", "CODEX_HOME"): (value, kind)}))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        assert cli._SchtasksBackend().service_environ(name) is None
    as_frozen(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SchtasksBackend())
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / ".claude" / "projects")
    assert _codex_folder(out)[0] == str(tmp_path / ".codex" / "sessions")


def test_a_non_string_user_value_leaves_the_machines_in_force(monkeypatch):
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg(
        {("HKCU", "CODEX_HOME"): (1, 4), ("HKLM", "CODEX_HOME"): (r"D:\cx", 1)}))
    assert cli._SchtasksBackend().service_environ("CODEX_HOME") == r"D:\cx"
