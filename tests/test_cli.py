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
    monkeypatch.setattr(statusline_source, "payload_path", lambda *a, **k: str(moved))
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


def test_too_old_claude_warns_rather_than_refusing(tmp_path,
                                                   capsys, monkeypatch):
    """Everything installed stays correct, so it works the moment they update."""
    monkeypatch.setattr(cli, "claude_version", lambda *a, **k: ("2.1.0 (Claude Code)", (2, 1, 0)))
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


@pytest.fixture(autouse=True)
def _no_recapture_wait(monkeypatch):
    """Uninstall waits HOOK_RECAPTURE_S between taking the hooks out and
    removing the script they run; the tests that care say so."""
    monkeypatch.setattr(cli, "HOOK_RECAPTURE_S", 0)


def test_uninstall_waits_for_claude_code_to_let_go_of_the_hooks(
        tmp_path, monkeypatch):
    """U29, natively: Claude Code re-reads its hooks 1 to 1.5 s after
    settings.json changes (2.1.316 and .318), and a hook that fires in
    between runs the script uninstall has just deleted -- an exit 2, which
    blocks that call. So the script goes two seconds after the hooks, and
    only when hooks were taken out."""
    _settings(tmp_path, {})
    cli.main(["install"])
    monkeypatch.setattr(cli, "HOOK_RECAPTURE_S", 2.0)
    shim = inside(tmp_path, cli.hook_shim_path())
    waited = []
    monkeypatch.setattr(cli.time, "sleep", lambda s: waited.append(
        (s, os.path.exists(shim), "blink-hook.sh" in json.dumps(
            _read(tmp_path)))))
    cli.main(["uninstall"])
    assert waited == [(2.0, True, False)]
    assert not os.path.exists(shim)
    waited.clear()
    cli.main(["uninstall"])
    assert waited == []


def test_uninstall_removes_the_spend_count(tmp_path):
    """U36: the spend count is install's file -- written only because
    Blink was installed on a seat billed by usage -- and it holds session
    ids and a fingerprint of the account. Left behind, a reinstall in the
    same month picked the old count up again. Its temp names go too: the
    mkstemp `.spend-count.*` names and those theme._write_json writes."""
    from pc import spend_count
    _settings(tmp_path, {})
    cli.main(["install"])
    path = inside(tmp_path, spend_count._default_path())
    leftovers = [os.path.join(os.path.dirname(path), ".spend-count.ab12cd"),
                 path + ".1234.0.blink-tmp"]
    for p in [path] + leftovers:
        with open(p, "w", encoding="utf-8") as f:
            f.write("{}")

    cli.main(["uninstall"])

    for p in [path] + leftovers:
        assert not os.path.exists(p), p


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


def test_uninstall_finishes_even_when_settings_will_not_parse(tmp_path,
                                                              capsys):
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


def test_status_does_not_call_a_missing_hook_shim_out_of_date(tmp_path,
                                                              capsys):
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
    monkeypatch.setattr(cli, "claude_version", lambda *a, **k: (None, None))
    monkeypatch.setattr(cli, "claude_on_path", lambda *a, **k: False)
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
    monkeypatch.setattr(cli, "claude_on_path", lambda *a, **k: True)
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


def test_no_claude_code_still_installs_everything(tmp_path,
                                                  capsys, monkeypatch):
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
                        lambda *a, **k: ("2.1.300 (Claude Code)", (2, 1, 300)))
    monkeypatch.setattr(cli, "claude_on_path", lambda *a, **k: True)
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
                        lambda *a, **k: calls.append(1) or (None, None))
    monkeypatch.setattr(cli, "claude_on_path", lambda *a, **k: True)
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
                        lambda *a, **k: ("2.1.245 (Claude Code)", (2, 1, 245)))
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
                        lambda *a, **k: ("2.1.245 (Claude Code)", (2, 1, 245)))
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
    monkeypatch.setattr(cli, "claude_version", lambda *a, **k: (None, None))
    monkeypatch.setattr(cli, "claude_on_path", lambda *a, **k: False)
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
    monkeypatch.setattr(cli, "read_desk", lambda *a, **k: desk)
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "sign in" not in out
    assert _WAITING in out


def _status_rows(monkeypatch, capsys, desk):
    monkeypatch.setattr(cli, "read_desk", lambda *a, **k: desk)
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
# re-review), and then named the service's folder with a caveat. Since
# 2026-11-04 install records the shell's folder (pc/claude_home) and the
# daemon reads the record, so the folder is the shell's whatever the service
# sees, and the one thing said about the variable is that it was recorded.

RECORDED = "  CLAUDE_CONFIG_DIR is set in this shell, and install records"


def _burn_folder(out):
    lines = out.splitlines()
    i = lines.index("  output-token count of each reply from Claude Code's"
                    " transcripts:")
    return lines[i + 1].strip(), lines[i + 2:]


def _caveat(after):
    """The lines among `after` from the plan paragraph's last up to the
    Codex paragraph: where the CLAUDE_CONFIG_DIR note is said, once, after
    both the transcripts' folder and the plan file (the review of PR #6,
    2026-11-01: said twice, a test of the first was satisfied by the
    second, and deleting the first print failed nothing)."""
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


def _no_service_caveat(out):
    """None of the old lines that explained a folder the service would
    read instead of the shell's."""
    for said in ("does not see it", "sees a different CLAUDE_CONFIG_DIR",
                 "set for the background service but not in",
                 "keeps this window's CLAUDE_CONFIG_DIR"):
        assert said not in out


def test_the_burn_folder_is_the_recorded_one(tmp_path, capsys,
                                             monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    folder, after = _burn_folder(out)
    assert folder == str(tmp_path / "work-config" / "projects")
    caveat = _caveat(after)
    assert caveat and caveat[0] == RECORDED, after
    assert ("so the background service reads the folder and the file above"
            " too." in " ".join(l.strip() for l in caveat))
    assert out.count(RECORDED) == 1
    _no_service_caveat(out)


def test_without_claude_config_dir_nothing_more_is_said(tmp_path, capsys,
                                                         monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / ".claude" / "projects")
    assert "CLAUDE_CONFIG_DIR" not in out


def test_a_service_that_sees_claude_config_dir_reads_the_same_folder(
        tmp_path, capsys, monkeypatch):
    """Windows, where it is a user environment variable (setx): the same
    folder either way, recorded."""
    as_checkout(monkeypatch)
    work = str(tmp_path / "work-config")
    _with_a_service(monkeypatch, service_sees=work)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", work)
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == os.path.join(work, "projects")
    assert out.count(RECORDED) == 1
    _no_service_caveat(out)


def test_with_no_service_the_folder_is_this_shells(tmp_path, capsys,
                                                   monkeypatch):
    """No service is registered (conftest's BLINK_SKIP_SERVICE, a Windows
    checkout): the daemon is started by hand, and reads the record too."""
    as_checkout(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / "work-config" / "projects")
    _no_service_caveat(out)


def test_launchd_and_systemd_hand_the_service_no_claude_config_dir(
        tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work-codex"))
    for name in ("CLAUDE_CONFIG_DIR", "CODEX_HOME"):
        assert cli._LaunchdBackend().service_environ(name) is None
        assert cli._SystemdBackend().service_environ(name) is None


def test_a_service_that_sees_another_value_reads_this_shells(
        tmp_path, capsys, monkeypatch):
    """A `setx` to a new folder, typed in a window that still has the old
    one: install records this window's, and the service reads that."""
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch, service_sees=str(tmp_path / "new"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "old"))
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / "old" / "projects")
    _no_service_caveat(out)


def test_a_service_that_sees_what_this_shell_does_not_reads_this_shells(
        tmp_path, capsys, monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch, service_sees=str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / ".claude" / "projects")
    assert "CLAUDE_CONFIG_DIR" not in out


def _plan_file(out):
    lines = out.splitlines()
    i = lines.index(
        "  It reads Claude Code's config to learn the plan and the seat type:")
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
            " plan name, rate-limit tier, seat type and the account and"
            " organisation ids in it are looked at, plus, on a usage-billed"
            " seat, the spend Claude Code saved at the last /usage. Of the"
            " plan name and tier, only whether they say Ultra is kept, and"
            " nothing from the file is logged or printed.") in said
    assert ("On the Ultra plan one reply with a weekly figure and no"
            " five-hour one switches on the burn view; other plans need five"
            " and a quarter hours of them, longer after a break of over an hour.") in said
    from pc.providers import claude_cli
    assert claude_cli.CHAIN_SPAN_S == 5 * 3600 + 15 * 60
    assert claude_cli.CHAIN_MAX_GAP_S == 3600
    assert "reads no credential" not in said
    assert "It keeps no credential and no token" in said


def test_the_plan_file_is_the_recorded_one(tmp_path, capsys, monkeypatch):
    as_checkout(monkeypatch)
    _with_a_service(monkeypatch)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    cli._announce()
    out = capsys.readouterr().out
    folder, after = _plan_file(out)
    assert folder == str(tmp_path / "work-config" / ".claude.json")
    assert out.count(RECORDED) == 1
    _no_service_caveat(out)


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
    assert "CODEX_HOME is set" not in out
    _no_service_caveat(out)


def test_linux_with_systemd_names_the_units_folders(tmp_path, capsys,
                                                    monkeypatch):
    """The unit is handed no CODEX_HOME, so its folder is ~/.codex and the
    note says why; Claude Code's folder is the recorded one."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SystemdBackend())
    monkeypatch.setattr(cli._SystemdBackend, "_has_systemctl",
                        staticmethod(lambda: True))
    monkeypatch.setattr(cli._SystemdBackend, "_booted",
                        staticmethod(lambda: True))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work-codex"))
    cli._announce()
    out = capsys.readouterr().out
    folder, after = _burn_folder(out)
    assert folder == str(tmp_path / "work-config" / "projects")
    assert out.count(RECORDED) == 1
    _no_service_caveat(out)
    codex, said = _codex_folder(out)
    assert codex == str(tmp_path / ".codex" / "sessions")
    assert "CODEX_HOME is set in this shell, but the background" in said
    assert "Run key" not in out


def test_a_windows_checkout_names_this_shells_folders(tmp_path, capsys,
                                                      monkeypatch):
    """A checkout on Windows registers nothing and is told to start the
    daemon by hand."""
    as_checkout(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SchtasksBackend())
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg({}))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "work-config"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "work-codex"))
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == str(tmp_path / "work-config" / "projects")
    assert _codex_folder(out)[0] == str(tmp_path / "work-codex" / "sessions")
    assert "CODEX_HOME is set" not in out
    _no_service_caveat(out)


def test_the_windows_run_key_start_is_said_to_keep_this_window(
        tmp_path, capsys, monkeypatch):
    """When schtasks refuses the task, install starts the launcher itself
    (_start_launcher's Popen has no env=), so until the next logon the
    daemon has this window's variable, not the registry's. Install cannot
    know beforehand which way Windows goes, so it says both. Said of
    CODEX_HOME only: Claude Code's folder is recorded, and the same either
    way."""
    as_frozen(monkeypatch)
    monkeypatch.setattr(cli, "_skip_service", lambda: False)
    monkeypatch.setattr(cli, "backend", lambda: cli._SchtasksBackend())
    reg = str(tmp_path / "reg")
    monkeypatch.setattr(cli, "_winreg", lambda: _FakeWinreg(
        {("HKCU", "CODEX_HOME"): (reg, 1),
         ("HKCU", "CLAUDE_CONFIG_DIR"): (reg, 1)}))
    window = str(tmp_path / "window")
    monkeypatch.setenv("CODEX_HOME", window)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", window)
    cli._announce()
    out = capsys.readouterr().out
    assert _burn_folder(out)[0] == os.path.join(window, "projects")
    _no_service_caveat(out)
    folder, said = _codex_folder(out)
    assert folder == os.path.join(reg, "sessions")
    assert ("If Windows refuses the task and the Run key is used instead,"
            " the bridge started now keeps this window's CODEX_HOME"
            " until you next log in, and reads this folder until then: "
            + os.path.join(window, "sessions")) in said
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


SPEND_NOTE = (
    "This Claude seat is billed by usage. BLINK reads the seat type, and the"
    " month's spend and limit that Claude Code saved the last time /usage ran"
    " in a terminal. Between /usage runs it adds the cost Claude Code's status"
    " line estimates for each session, and keeps each session's id and"
    " highest estimate, what it counted in the last five hours, the month's"
    " total and a one-way fingerprint of the account in {path}. Spend in"
    " Claude Desktop (chat"
    " and its Code tab), Cowork, claude.ai, claude -p, the Agent SDK or on"
    " other computers appears only after the next /usage, and a session's last"
    " reply can be missed. Between /usage runs the figure is an estimate and"
    " may differ from your bill; run /usage in Claude Code to make it exact."
    " Nothing about it leaves this computer except the figure on the board.")


def _claude_json(tmp_path, seat, used=4321, fetched_ms=1_793_500_000_000):
    d = {"oauthAccount": {"accountUuid": "A-1", "organizationUuid": "O-1"},
         "cachedUsageUtilization": {
             "accountUuid": "A-1", "fetchedAtMs": fetched_ms,
             "utilization": {"extra_usage": {
                 "is_enabled": True, "monthly_limit": 25000,
                 "used_credits": used, "currency": "usd"}}}}
    if seat:
        d["oauthAccount"]["seatTier"] = seat
    (tmp_path / ".claude.json").write_text(json.dumps(d), encoding="utf-8")


def test_install_note_on_a_spend_seat(tmp_path, capsys, monkeypatch):
    from pc import spend_count
    as_checkout(monkeypatch)
    _claude_json(tmp_path, "enterprise_usage_based")
    cli._announce()
    said = " ".join(l.strip() for l in capsys.readouterr().out.splitlines())
    assert SPEND_NOTE.format(path=spend_count._default_path()) in said


def test_no_spend_note_on_max(tmp_path, capsys, monkeypatch):
    as_checkout(monkeypatch)
    _claude_json(tmp_path, "max_5x")
    cli._announce()
    assert "This Claude seat is billed by usage" not in capsys.readouterr().out


def test_status_never_prints_amounts(tmp_path, capsys, monkeypatch):
    """`blink status` never prints the spend figure's dollars, and nor
    does `blink status --wire`. The wire view runs a whole bus over the
    config, so it does carry the page: it prints the usage message exactly
    as the board gets it, and on a spend page that holds the share of the
    cap as the session figure. That is ruled acceptable (a Max desk's
    percentages print the same way); the dollars and the cents travel only
    in the plan message, which the wire view does not print, and this pins
    that. The fixture is one the reader really sees as a spend seat with
    the figure in it, with a /usage from a minute ago so the page is built
    on it, so the test cannot pass merely because the file was never found
    or the figure was last month's. This docstring used to say there was
    no wire view (final review of 2026-11-04)."""
    import json as _json
    from pc import claude_plan
    now = time.time()
    _claude_json(tmp_path, "enterprise_usage_based", used=4321,
                 fetched_ms=int((now - 60) * 1000))
    seen = claude_plan.PlanReader().look(now)
    assert seen.spend_seat and seen.anchor.used_cents == 4321
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "4321" not in out and "43.21" not in out
    cli.main(["status", "--wire"])
    out = capsys.readouterr().out
    lines = out.splitlines()
    i = lines.index("Wire        the next usage message, exactly as the"
                    " board gets it:")
    wire = _json.loads(lines[i + 1])
    # The page is the spend page, its share 4321 / 25000 of the cap.
    assert wire["src"] == "claude_spend" and wire["session_pct"] == 17.3
    assert not {"usd", "lim"} & set(wire)
    assert not {43.21, 4321, 250.0, 25000} & set(wire.values())
    rest = "\n".join(lines[:i + 1] + lines[i + 2:])
    for amount in ("4321", "43.21", "250.00", "25000"):
        assert amount not in rest, amount


SPEND_LATER = (
    "On a Claude seat billed by usage -- this one now, or one you /login to"
    " later -- it also keeps, in {path} a one-way fingerprint of the account"
    " and organisation ids, each session's id with its highest cost"
    " estimate, and the rises it counted, so the month's spend survives a"
    " restart. Nothing in it leaves this computer.")


def test_disclosure_says_what_a_usage_seat_keeps_on_any_desk(
        tmp_path, capsys, monkeypatch):
    """U35: the shim paragraph said the session id and the running cost
    "are never logged or saved", while on a seat billed by usage
    spend-count.json saves both, with a fingerprint of the account. And
    the paragraph that corrected it was printed only when the config named
    such a seat at install time, so a Max desk that later /logins to one
    was never told. Said on every desk now, the Max one included."""
    from pc import spend_count
    as_checkout(monkeypatch)
    _claude_json(tmp_path, "max_5x")
    cli._announce()
    said = " ".join(l.strip() for l in capsys.readouterr().out.splitlines())
    assert "never logged or saved" not in said
    assert "saved only on a seat billed by usage" in said
    assert SPEND_LATER.format(path=spend_count._default_path()) in said


def test_a_usage_seat_is_told_what_is_kept_once(tmp_path, capsys,
                                                monkeypatch):
    """Round 2, C5: on a seat billed by usage the disclosure said what
    spend-count.json keeps twice, in two overlapping paragraphs: the one
    every desk gets, and the full one this seat gets. The full one, which
    says all the other does, is the one said."""
    from pc import spend_count
    as_checkout(monkeypatch)
    _claude_json(tmp_path, "enterprise_usage_based")
    cli._announce()
    said = " ".join(l.strip() for l in capsys.readouterr().out.splitlines())
    assert SPEND_NOTE.format(path=spend_count._default_path()) in said
    assert SPEND_LATER.format(path=spend_count._default_path()) not in said
    assert said.count(spend_count._default_path()) == 1


def test_the_disclosure_reads_the_seat_once(tmp_path, capsys, monkeypatch):
    """D2: C5 asked the config whether the seat is billed by usage, and
    _announce_spend_seat then asked it again. The answer is passed in."""
    from pc import claude_plan
    as_checkout(monkeypatch)
    _claude_json(tmp_path, "enterprise_usage_based")
    looks = []
    real = claude_plan.PlanReader.look

    def look(self, now):
        looks.append(now)
        return real(self, now)
    monkeypatch.setattr(claude_plan.PlanReader, "look", look)
    cli._announce()
    assert "This Claude seat is billed by usage" in capsys.readouterr().out
    assert len(looks) == 1


def test_status_wire_counts_what_the_daemon_counted(tmp_path, capsys,
                                                    monkeypatch):
    """U19: on a seat billed by usage between /usage runs, `--wire` built
    its count from nothing and printed only the /usage share, while the
    board showed it plus every rise the daemon had counted since. The
    count is loaded from the daemon's file now (and still never saved)."""
    import json as _json
    from pc import claude_plan, spend_count
    now = time.time()
    _claude_json(tmp_path, "enterprise_usage_based", used=4321,
                 fetched_ms=int((now - 60) * 1000))
    view = claude_plan.PlanReader().look(now)
    path = inside(tmp_path, spend_count._default_path())
    daemon = spend_count.SpendCounter(path=path)
    daemon.bind(view.account_fp, claude_plan.month_start_utc(now))
    daemon.observe("s1", 1.00, now - 30)
    daemon.observe("s1", 11.00, now - 20)       # $10.00 counted
    daemon.save()
    with open(path, "rb") as f:
        saved = f.read()
    cli.main(["status", "--wire"])
    lines = capsys.readouterr().out.splitlines()
    i = lines.index("Wire        the next usage message, exactly as the"
                    " board gets it:")
    wire = _json.loads(lines[i + 1])
    assert wire["session_pct"] == 21.3          # (4321 + 1000) / 25000
    with open(path, "rb") as f:
        assert f.read() == saved


def test_config_disclosure_names_seat_fields(tmp_path, capsys, monkeypatch):
    as_checkout(monkeypatch)
    cli._announce()
    said = " ".join(l.strip() for l in capsys.readouterr().out.splitlines())
    assert "learn the plan and the seat type" in said
    assert "weekly figure and no five-hour one, it reads" not in said


# --- `blink install --wsl`, status and uninstall for Claude Code in WSL ---
#
# docs/plans/enterprise-spend.md, section 4. Nothing here starts a real
# wsl.exe or reads a real registry (tests/conftest.py refuses the first):
# the registry is a fake reader handed to cli through cli._wsl_registry, and
# wsl.probe, wsl.running and install_statusline.install_into/uninstall_from
# are recorders. A UNC path on macOS is only a string, so the status test
# watches the file calls themselves for one.

WSL_GUID = "{5C3D2B1A-0000-4000-8000-00000000ABCD}"
OTHER_GUID = "{11111111-2222-4333-8444-555555555555}"


class _FakeReg:
    """wsl's registry reader: `distros` is [(guid, name)], the first the
    default; `policy` the HKLM policy values (None: no key); `broken`
    makes every HKCU read fail."""

    def __init__(self, distros=(), policy=None, broken=False):
        self.distros = list(distros)
        self.policy = policy
        self.broken = broken

    def values(self, hive, path):
        from pc import wsl
        if hive == wsl.HKLM:
            if self.policy is None:
                raise OSError("no policy key")
            return dict(self.policy)
        if self.broken:
            raise OSError("registry")
        if path == wsl.LXSS:
            return {"DefaultDistribution":
                    self.distros[0][0] if self.distros else ""}
        guid = path.rsplit("\\", 1)[1]
        for g, name in self.distros:
            if g == guid:
                return {"DistributionName": name, "State": 1, "Version": 2}
        raise OSError("no such key")

    def subkeys(self, hive, path):
        if self.broken:
            raise OSError("registry")
        return [g for g, _ in self.distros]


def _probe(**kw):
    from pc import wsl
    d = dict(user="zoe", home="/home/zoe", shell="/bin/bash",
             config_dir="/home/zoe/.claude", claude_exe="/home/zoe/.local/bin/claude")
    d.update(kw)
    return wsl.Probe(**d)


def _wsl_desk(monkeypatch, reg, probe=None, running=False):
    """Stand cli up as a Windows desk with `reg` for a registry. Returns
    the list every fake records its calls into."""
    from pc import wsl
    calls = []
    monkeypatch.setattr(cli, "_windows", lambda: True)
    monkeypatch.setattr(cli, "_wsl_registry", lambda: reg)

    def fake_probe(name, *a, **k):
        calls.append(("probe", name))
        return probe

    def fake_running(name, *a, **k):
        calls.append(("running", name))
        return running

    def fake_into(home, wsl_run, **k):
        calls.append(("install_into", home))
        return "Statusline installed. Activity hooks added."

    def fake_from(home, wsl_run, **k):
        calls.append(("uninstall_from", home))
        return "Statusline removed. Activity hooks removed."

    monkeypatch.setattr(wsl, "probe", fake_probe)
    monkeypatch.setattr(wsl, "running", fake_running)
    monkeypatch.setattr(install_statusline, "install_into", fake_into)
    monkeypatch.setattr(install_statusline, "uninstall_from", fake_from)
    monkeypatch.setattr(cli, "restart_service",
                        lambda: calls.append(("restart",)) or "restarted")
    monkeypatch.setattr(cli, "halt_service",
                        lambda: calls.append(("halt",)))

    def fake_run_in(home):
        # What status reads settings.json through inside a running
        # distribution (claude_hooks): no file there.
        def run(argv, stdin=None):
            calls.append(("wsl_run", home.distro_name))
            return 0, argv[-1].encode() + b"\nabsent\n", b""
        return run
    monkeypatch.setattr(install_statusline, "default_wsl_run", fake_run_in)
    monkeypatch.setattr(cli, "_install_service",
                        lambda: calls.append(("service",))
                        or "running (Scheduled Task)")
    return calls


def _inside(calls):
    """The calls that reach into a distribution, in order."""
    return [c[0] for c in calls
            if c[0] in ("probe", "install_into", "uninstall_from")]


def _recorded_wsl(home, name="Ubuntu", guid=WSL_GUID, **kw):
    """Record a WSL home in ~/.blink/config.json, as `install --wsl` would.

    `home` is the caller's home (tmp_path under pytest), and the record is
    written only once it is known to lie under it (tests/support/home):
    this wrote whatever ~/.blink the process saw, so a scratch script that
    imported it would have pointed a real desk's daemon at a made-up
    distribution (review of feat/enterprise-spend, CV1)."""
    from pc import claude_home, user_config, wsl
    fields = dict(linux_user="zoe", linux_home="/home/zoe",
                  config_dir=wsl.unc(name, "/home/zoe/.claude"),
                  config_dir_set=False)
    fields.update(kw)
    h = claude_home.wsl(distro_guid=guid, distro_name=name, **fields)
    inside(home, user_config.path(cli.blink_home()))
    claude_home.save(h)
    return h


def test_plain_install_never_touches_wsl(tmp_path, monkeypatch, capsys):
    """A plain `blink install` on a Windows desk with no Claude Code of its
    own prints one hint, and reads no registry and starts no wsl.exe to
    decide it: section 4, "never automatic"."""
    from pc import wsl
    calls = []
    for fn in ("policy_blocks", "distros", "running", "run", "probe",
               "unc", "_call"):
        monkeypatch.setattr(wsl, fn, lambda *a, _fn=fn, **k:
                            calls.append(_fn))
    monkeypatch.setattr(cli, "_windows", lambda: True)
    monkeypatch.setattr(cli, "_wsl_registry",
                        lambda: calls.append("registry"))
    monkeypatch.setattr(cli, "claude_on_path", lambda home=None: False)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert calls == []
    assert (f"Using Claude Code inside WSL? Run `{cli.command_to_type()}"
            " install --wsl <name>`.") in out


def test_plain_install_has_no_wsl_hint_beside_claude_code(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    monkeypatch.setattr(cli, "_windows", lambda: True)
    monkeypatch.setattr(cli, "claude_on_path", lambda home=None: True)
    _settings(tmp_path, {})
    cli.main(["install"])
    assert "inside WSL" not in capsys.readouterr().out


def test_plain_install_has_no_wsl_hint_off_windows(tmp_path, monkeypatch,
                                                  capsys):
    monkeypatch.setattr(cli, "claude_on_path", lambda home=None: False)
    _settings(tmp_path, {})
    cli.main(["install"])
    assert "inside WSL" not in capsys.readouterr().out


def test_install_wsl_lists(monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg(
        [(WSL_GUID, "Ubuntu"), (OTHER_GUID, "Debian")]))
    assert cli.main(["install", "--wsl"]) == 0
    out = capsys.readouterr().out
    assert "  Ubuntu (default)\n" in out
    assert "  Debian\n" in out
    assert f"{cli.command_to_type()} install --wsl <name>" in out
    # Listing starts nothing.
    assert calls == []


def test_install_wsl_policy(monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")],
                                            policy={"AllowWSL": 0}))
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    assert ("WSL is turned off on this computer by policy."
            in capsys.readouterr().out)
    assert calls == []


def test_install_wsl_registry_error(monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg(broken=True))
    assert cli.main(["install", "--wsl"]) == 1
    assert "Could not list WSL distributions." in capsys.readouterr().out
    assert calls == []


def test_install_wsl_off_windows(monkeypatch, capsys):
    """Off Windows the real registry reader is never made, and nothing
    else is asked either."""
    registry = cli._wsl_registry
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    monkeypatch.setattr(cli, "_wsl_registry", registry)
    monkeypatch.setattr(cli, "_windows", lambda: False)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    assert capsys.readouterr().out == (
        "WSL is only on Windows; there is nothing to install into here.\n")
    assert calls == []


def test_install_wsl_unknown_name(monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg(
        [(WSL_GUID, "Ubuntu"), (OTHER_GUID, "Debian")]))
    assert cli.main(["install", "--wsl", "Arch"]) == 1
    out = capsys.readouterr().out
    assert "Arch" in out
    assert "  Ubuntu (default)\n" in out and "  Debian\n" in out
    assert calls == []


def test_install_wsl_records_guid_not_env(tmp_path, monkeypatch, capsys):
    """The record is the GUID and name, the Linux user and home and the
    config folder; nothing out of the shell's environment. The service is
    installed after the record, since the bus is built once at startup:
    stopped before anything is written, started by the service step."""
    from pc import claude_home, user_config, wsl
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(config_dir="/srv/cc", config_dir_set=True))
    # Matched blind to case, as Windows matches a distribution's name.
    assert cli.main(["install", "--wsl", "ubuntu"]) == 0
    out = capsys.readouterr().out
    rec = user_config.load(cli.blink_home())["claude_home"]
    assert rec == {"kind": "wsl", "config_dir": wsl.unc("Ubuntu", "/srv/cc"),
                   "config_dir_set": True, "distro_guid": WSL_GUID,
                   "distro_name": "Ubuntu", "linux_user": "zoe",
                   "linux_home": "/home/zoe"}
    home = claude_home.current()
    assert home.kind == "wsl"
    assert [c[0] for c in calls] == ["halt", "probe", "install_into",
                                     "service"]
    assert calls[2][1] == home
    # What was written and where, said to the person.
    assert "Statusline installed. Activity hooks added." in out
    assert "/srv/cc/settings.json" in out
    assert "/home/zoe/.blink" in out
    assert home.drop_dir in out


def test_install_wsl_unreadable_distribution(monkeypatch, capsys):
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=None)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = capsys.readouterr().out
    assert "Ubuntu" in out
    assert "Claude Code inside Ubuntu was not changed." in out
    assert "Nothing was changed." not in out and "Done." not in out
    assert _inside(calls) == ["probe"]
    assert claude_home.recorded() is None


def test_install_wsl_failure_records_nothing(monkeypatch, capsys):
    """A write inside the distribution that fails leaves the record as it
    was: a daemon moved to a home with nothing installed in it would read
    an empty folder. The service stopped for the write is started again,
    reading what it read before, and nothing after the step runs (U27)."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())

    def fails(home, wsl_run, **k):
        raise install_statusline.WslInstallError(
            "Could not write Claude Code's settings.json inside WSL.")
    monkeypatch.setattr(install_statusline, "install_into", fails)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = capsys.readouterr().out
    assert "Could not write Claude Code's settings.json inside WSL." in out
    assert "running again, reading what it read before" in out
    assert "Done." not in out
    assert claude_home.recorded() is None
    assert ("restart",) in calls and ("service",) not in calls


def test_install_wsl_prints_the_spend_note(tmp_path, monkeypatch, capsys):
    """Plan A's note, from the distribution's config: at install the user
    is present and the distribution has just been started by the probe,
    so the config is read over UNC once, here."""
    from pc import claude_plan, spend_count
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    (tmp_path / "linux").mkdir()
    _claude_json(tmp_path / "linux", "enterprise_usage_based")
    seen = []
    real = claude_plan.config_path

    def config_path(home=None):
        if home is not None and home.kind == "wsl":
            seen.append(home.config_dir)
            return str(tmp_path / "linux" / ".claude.json")
        return real(home)
    monkeypatch.setattr(claude_plan, "config_path", config_path)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 0
    said = " ".join(l.strip() for l in capsys.readouterr().out.splitlines())
    assert SPEND_NOTE.format(path=spend_count._default_path()) in said
    from pc import wsl
    assert seen == [wsl.unc("Ubuntu", "/home/zoe/.claude")]


def _wsl_seat(monkeypatch, tmp_path, seat):
    from pc import claude_plan
    (tmp_path / "linux").mkdir()
    _claude_json(tmp_path / "linux", seat)
    real = claude_plan.config_path

    def config_path(home=None):
        if home is not None and home.kind == "wsl":
            return str(tmp_path / "linux" / ".claude.json")
        return real(home)
    monkeypatch.setattr(claude_plan, "config_path", config_path)


def _flat(out):
    return " ".join(l.strip() for l in out.splitlines())


@pytest.mark.parametrize("argv", [["install", "--wsl", "Ubuntu"],
                                  ["install"]])
def test_a_wsl_usage_seat_is_told_what_is_kept_once(
        tmp_path, monkeypatch, capsys, argv):
    """Review of round 2, D1: C5 left WSL alone, so `install --wsl` on a
    seat billed by usage printed the short paragraph in the disclosure and
    the full one after the probe. The seat is known only after the probe,
    so the one paragraph waits for it; the disclosure says it will."""
    from pc import spend_count
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    if argv == ["install"]:
        _recorded_wsl(tmp_path)
    _wsl_seat(monkeypatch, tmp_path, "enterprise_usage_based")
    assert cli.main(argv) == 0
    said = _flat(capsys.readouterr().out)
    path = spend_count._default_path()
    assert SPEND_NOTE.format(path=path) in said
    assert SPEND_LATER.format(path=path) not in said
    assert said.count(path) == 1


def test_a_wsl_desk_on_another_seat_gets_the_short_paragraph_once(
        tmp_path, monkeypatch, capsys):
    """D1, the other seat: the short paragraph, said once and after the
    probe, where it used to be said in the disclosure."""
    from pc import spend_count
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    _wsl_seat(monkeypatch, tmp_path, "max_5x")
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 0
    said = _flat(capsys.readouterr().out)
    path = spend_count._default_path()
    assert SPEND_LATER.format(path=path) in said
    assert said.count(path) == 1
    assert said.index(SPEND_LATER.format(path=path)) > said.index(
        "Written inside Ubuntu")
    assert "said once setup has read the seat type inside Ubuntu" in said


def test_install_wsl_no_spend_note_on_max(tmp_path, monkeypatch, capsys):
    from pc import claude_plan
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    (tmp_path / "linux").mkdir()
    _claude_json(tmp_path / "linux", "max_5x")
    monkeypatch.setattr(claude_plan, "config_path",
                        lambda home=None: str(tmp_path / "linux"
                                              / ".claude.json"))
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 0
    assert "This Claude seat is billed by usage" not in capsys.readouterr().out


class _UncWatch:
    """Records every file call on a path into a distribution."""

    def __init__(self, monkeypatch):
        import builtins
        import io
        self.seen = []
        self.paths = []
        for mod, name in ((os, "stat"), (os, "lstat"), (os, "scandir"),
                          (os, "listdir"), (os, "open"), (builtins, "open"),
                          (io, "open")):
            real = getattr(mod, name)
            monkeypatch.setattr(mod, name, self._wrap(real, name))

    def _wrap(self, real, name):
        def call(path=".", *a, **k):
            p = os.fspath(path) if isinstance(path, (str, os.PathLike)) \
                else ""
            if isinstance(p, str) and "wsl.localhost" in p.lower():
                self.seen.append(name)
                self.paths.append(p)
                raise OSError("UNC touched")
            return real(path, *a, **k)
        return call


def test_status_names_the_distro_without_unc(tmp_path, monkeypatch, capsys):
    """Not running: the distribution is named and asked about with
    `wsl -l --running` only. Nothing under \\\\wsl.localhost is opened or
    stat'ed, since that would start it."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      running=False)
    monkeypatch.setattr(cli, "claude_on_path", lambda home=None: False)
    _recorded_wsl(tmp_path)
    watch = _UncWatch(monkeypatch)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Claude Code WSL (Ubuntu), not running" in out
    assert watch.seen == []
    assert calls == [("running", "Ubuntu")]
    # The native Claude Code is not on this desk, so nothing is said of it.
    assert "Claude Code on Windows itself is not read" not in out


def test_status_wire_never_touches_unc_when_stopped(tmp_path,
                                                    monkeypatch, capsys):
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), running=False)
    _recorded_wsl(tmp_path)
    watch = _UncWatch(monkeypatch)
    assert cli.main(["status", "--wire"]) == 0
    assert watch.seen == []


def test_status_running_distro_and_native_ignored(tmp_path,
                                                  monkeypatch, capsys):
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), running=True)
    _recorded_wsl(tmp_path)
    monkeypatch.setattr(cli, "claude_on_path",
                        lambda home=None: home is not None
                        and home.kind == "native")
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Claude Code WSL (Ubuntu), running" in out
    assert "Claude Code on Windows itself is not read" in out


def test_status_says_a_removed_distro_is_gone(tmp_path, monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg([(OTHER_GUID, "Debian")]))
    _recorded_wsl(tmp_path)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert ("Claude Code WSL (Ubuntu), not found -- removed or reinstalled?"
            in out)
    assert calls == []


def test_uninstall_leaves_a_config_json_it_cannot_read(tmp_path,
                                                       monkeypatch, capsys):
    """U26: with config.json not parsing, the WSL record in it could not
    be read, so uninstall never looked inside the distribution, deleted
    the file -- the only record of where Blink was set up -- and said
    "Done. Nothing left". It leaves the file now, says why, and does not
    say done."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]))
    _recorded_wsl(tmp_path)
    config = inside(tmp_path, cli.user_config.path(cli.blink_home()))
    with open(config, "w") as f:
        f.write('{"claude_home": {"kind": "wsl"')
    # A fresh process, as `blink uninstall` is: no last good answer kept.
    from pc import claude_home
    claude_home._last.clear()
    assert cli.main(["uninstall"]) == 1
    out = capsys.readouterr().out
    with open(config) as f:
        assert f.read() == '{"claude_home": {"kind": "wsl"'
    assert config in out and "does not parse" in out
    assert "Done." not in out
    assert "uninstall_from" not in [c[0] for c in calls]


def test_uninstall_skips_a_removed_distro(tmp_path, monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg([(OTHER_GUID, "Debian")]))
    home = _recorded_wsl(tmp_path)
    os.makedirs(home.state_dir)
    (tmp_path / ".blink" / os.path.basename(home.drop_dir)
     / "statusline.json").write_text("{}")
    cli.main(["uninstall"])
    out = capsys.readouterr().out
    assert "Ubuntu" in out and "no longer on this computer" in out
    assert calls == []
    assert not os.path.exists(home.drop_dir)


def test_uninstall_takes_blink_out_of_the_distro(tmp_path,
                                                 monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]))
    home = _recorded_wsl(tmp_path)
    os.makedirs(home.state_dir)
    cli.main(["uninstall"])
    out = capsys.readouterr().out
    assert calls == [("uninstall_from", home)]
    assert "Statusline removed. Activity hooks removed." in out
    assert not os.path.exists(home.drop_dir)


def test_uninstall_says_why_a_distro_was_left(tmp_path, monkeypatch, capsys):
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]))
    _recorded_wsl(tmp_path)

    def fails(home, wsl_run, **k):
        raise install_statusline.WslInstallError(
            "Could not read Claude Code's settings inside WSL.")
    monkeypatch.setattr(install_statusline, "uninstall_from", fails)
    cli.main(["uninstall"])
    out = capsys.readouterr().out
    assert "Could not read Claude Code's settings inside WSL." in out


def test_a_wsl_home_reads_its_drop_folder(tmp_path, monkeypatch):
    """With a WSL home recorded, the status-line payload and the hook
    state are read from the Windows drop folder, not ~/.blink."""
    from pc import statusline_source
    from pc.providers import claude_cli, claude_state
    home = _recorded_wsl(tmp_path)
    assert statusline_source.payload_path() == os.path.join(
        home.drop_dir, "statusline.json")
    assert claude_cli.ClaudeCliProvider().path() == os.path.join(
        home.drop_dir, "statusline.json")
    assert claude_state.ClaudeStateProvider(sweep=False).path() == \
        home.state_dir
    assert home.drop_dir != cli.blink_home()


def test_status_reads_inside_a_running_distro(tmp_path, monkeypatch, capsys):
    """The other half of the test above, and the proof its watch sees a
    UNC read at all: with the distribution running, status does look
    inside it (the settings, for the Activity row)."""
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), running=True)
    _recorded_wsl(tmp_path)
    watch = _UncWatch(monkeypatch)
    cli.main(["status"])
    assert watch.seen


def test_uninstall_finds_a_renamed_distro_by_its_guid(tmp_path,
                                                      monkeypatch, capsys):
    from pc import wsl
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Work")]))
    _recorded_wsl(tmp_path, name="Ubuntu")
    cli.main(["uninstall"])
    (what, home), = calls
    assert what == "uninstall_from"
    assert home.distro_name == "Work"
    assert home.config_dir == wsl.unc("Work", "/home/zoe/.claude")
    assert "Claude Code inside WSL (Work):" in capsys.readouterr().out



def test_plain_install_keeps_a_wsl_record(tmp_path, monkeypatch, capsys):
    """Re-running the installer on a WSL desk sets the distribution up
    again and keeps the record: it must not quietly move the desk back to
    a Windows Claude Code it was told not to read."""
    from pc import claude_home, user_config
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    home = _recorded_wsl(tmp_path)
    before = user_config.load(cli.blink_home())["claude_home"]
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert user_config.load(cli.blink_home())["claude_home"] == before
    assert claude_home.current() == home
    assert ("install_into", home) in calls
    # Stopped before the write inside, so its WslWatch cannot repair the
    # folder under it (W1); started by the service step.
    assert [c[0] for c in calls] == ["halt", "probe", "install_into",
                                     "service"]
    assert out.count("Claude Code inside WSL (Ubuntu), kept") == 1
    assert "Using Claude Code inside WSL?" not in out
    # The Windows settings are not edited on a WSL desk.
    assert _read(tmp_path) == {}


def test_plain_install_after_the_distro_is_gone(tmp_path, monkeypatch,
                                                capsys):
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(OTHER_GUID, "Debian")]))
    home = _recorded_wsl(tmp_path)
    os.makedirs(home.state_dir)
    with open(os.path.join(home.drop_dir, "statusline.json"), "w") as f:
        f.write("{}")
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    assert "Ubuntu" in out and "no longer on this computer" in out
    assert claude_home.recorded().kind == "native"
    assert not os.path.exists(home.drop_dir)
    assert "statusLine" in _read(tmp_path)
    assert not [c for c in calls if c[0] not in ("running", "service")]


def test_install_wsl_over_another_distro(tmp_path, monkeypatch, capsys):
    """`--wsl B` over `--wsl A`: A is taken out of its distribution and
    its drop folder removed before B's record replaces it, or uninstall,
    which reads only the record, would never find A again."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg(
        [(WSL_GUID, "Ubuntu"), (OTHER_GUID, "Debian")]), probe=_probe())
    old = _recorded_wsl(tmp_path)
    os.makedirs(old.state_dir)
    assert cli.main(["install", "--wsl", "Debian"]) == 0
    new = claude_home.current()
    assert new.distro_guid == OTHER_GUID
    assert [c[0] for c in calls] == ["halt", "probe", "install_into",
                                     "uninstall_from", "service"]
    assert calls[3][1] == old
    assert not os.path.exists(old.drop_dir)


def test_install_wsl_again_leaves_the_same_distro(tmp_path,
                                                  monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    home = _recorded_wsl(tmp_path)
    os.makedirs(home.state_dir)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 0
    assert "uninstall_from" not in [c[0] for c in calls]
    assert os.path.isdir(home.drop_dir)


def test_reinstall_into_a_moved_folder_takes_blink_out_of_the_old(
        tmp_path, monkeypatch, capsys):
    """U21: the same distribution, with CLAUDE_CONFIG_DIR (or the default
    user) changed inside it since. The old folder used to keep Blink's
    status line and ten hooks, which ran deleted shims after an uninstall
    and blocked every prompt; and the one chain file per Linux home was
    overwritten with the new folder's. The old folder is taken out first,
    under the old record, then the new one written."""
    from pc import claude_home, wsl
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(config_dir="/home/zoe/work-claude",
                                   config_dir_set=True))
    old = _recorded_wsl(tmp_path)
    os.makedirs(old.state_dir)
    assert cli.main(["install"]) == 0
    assert _inside(calls) == ["probe", "uninstall_from", "install_into"]
    assert calls[[c[0] for c in calls].index("uninstall_from")][1] == old
    new = claude_home.recorded()
    assert new.config_dir == wsl.unc("Ubuntu", "/home/zoe/work-claude")
    assert os.path.isdir(new.drop_dir)


def test_a_moved_folder_that_will_not_undo_stops_the_install(
        tmp_path, monkeypatch, capsys):
    """U21, when the old folder cannot be undone and its Linux account is
    still there (a settings.json that does not parse, say): nothing is
    written inside, the record stays, and the way out is said, rather
    than overwriting the chain file the old folder's own status line is
    kept in. An account that is gone is C3's, below."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(user="amy", home="/home/amy",
                                   config_dir="/home/amy/.claude"))
    old = _recorded_wsl(tmp_path)

    def fails(home, wsl_run, **k):
        calls.append(("uninstall_from", home))
        raise install_statusline.WslInstallError("nope")
    monkeypatch.setattr(install_statusline, "uninstall_from", fails)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = capsys.readouterr().out
    assert _inside(calls) == ["probe", "uninstall_from"]
    assert claude_home.recorded() == old
    assert "another folder inside Ubuntu" in out
    assert "inside Ubuntu remove ~/.blink/out" in out
    assert "Done." not in out


def _old_account_gone(monkeypatch, calls, gone="zoe", undo_ok=()):
    """A distribution where Linux account `gone` no longer exists:
    getent says so (exit 2) and wsl.exe -u for it fails, so uninstall_from
    as that account raises. As anyone else it succeeds when that account
    is in `undo_ok`, and raises otherwise."""
    def run_in(home):
        def run(argv, stdin=None):
            calls.append(("wsl_run", home.linux_user, argv[0]))
            if argv[:2] == ["getent", "passwd"]:
                return (2, b"", b"") if argv[2] == gone else (0, b"x", b"")
            return 0, b"", b""
        return run

    def from_(home, wsl_run, **k):
        calls.append(("uninstall_from", home))
        if home.linux_user in undo_ok:
            return "Statusline removed. Activity hooks removed."
        raise install_statusline.WslInstallError("nope")
    monkeypatch.setattr(install_statusline, "default_wsl_run", run_in)
    monkeypatch.setattr(install_statusline, "uninstall_from", from_)


def test_a_renamed_account_is_taken_out_as_the_one_there_now(
        tmp_path, monkeypatch, capsys):
    """Round 2, C3: the moved-inside step ran as the account Blink was set
    up for. Renamed (usermod -l, same home), that account no longer
    exists, every wsl.exe -u for it fails, and install stopped for good
    with advice to act as a user nobody can be. The folder is taken out
    as the account the distribution has now."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(user="zoey"))
    old = _recorded_wsl(tmp_path)
    _old_account_gone(monkeypatch, calls, undo_ok=("zoey",))
    assert cli.main(["install"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    undone = [c[1] for c in calls if c[0] == "uninstall_from"]
    assert [h.linux_user for h in undone] == ["zoe", "zoey"]
    assert undone[1].config_dir == old.config_dir
    assert _inside(calls)[-1] == "install_into"
    assert claude_home.recorded().linux_user == "zoey"
    assert "Linux account Blink was set up for inside Ubuntu (zoe)" in out
    assert "no longer there" in out


def test_a_deleted_account_with_its_folder_out_of_reach_does_not_block(
        tmp_path, monkeypatch, capsys):
    """C3, the account deleted and its home elsewhere: nobody signs in as
    it, so its folder is said to still hold Blink's entries, with how to
    take them out, and the install goes on rather than stopping forever."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(user="amy", home="/home/amy",
                                   config_dir="/home/amy/.claude"))
    _recorded_wsl(tmp_path)
    _old_account_gone(monkeypatch, calls)
    assert cli.main(["install"]) == 0
    out = " ".join(capsys.readouterr().out.split())
    assert _inside(calls)[-1] == "install_into"
    assert claude_home.recorded().linux_user == "amy"
    assert "no longer there" in out
    assert "could not be taken out of /home/zoe/.claude as amy" in out
    assert "Done." in out


def test_a_save_that_fails_after_an_untouched_old_folder_says_so(
        tmp_path, monkeypatch, capsys):
    """Review of round 2, D3: a move inside the distribution whose old
    folder was left as it was (its account gone, the folder out of reach
    as the new one), then a record save that fails. C1's message said
    Blink "was already taken out of the folder it used before", which
    nothing had done. The new folder still comes back out, and the old
    one is said to be as it was."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(user="amy", home="/home/amy",
                                   config_dir="/home/amy/.claude"))
    _recorded_wsl(tmp_path)
    _old_account_gone(monkeypatch, calls)
    _save_fails(monkeypatch)
    assert cli.main(["install"]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "already taken out" not in out
    assert "being taken back out" in out
    assert "The folder Blink used before inside Ubuntu was left as it was" \
        in out
    assert [h.linux_user for h in (c[1] for c in calls
                                   if c[0] == "uninstall_from")][-1] == "amy"


def test_a_gone_account_whose_home_is_the_new_one_still_stops(
        tmp_path, monkeypatch, capsys):
    """C3, but the new account has the old one's home and the folder
    still could not be undone: writing now would overwrite the chain file
    holding the old folder's own status line (U21), so it stops, and the
    advice names the account there is now."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(user="zoey",
                                   config_dir="/home/zoe/work-claude"))
    old = _recorded_wsl(tmp_path)
    _old_account_gone(monkeypatch, calls)
    assert cli.main(["install"]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert "install_into" not in _inside(calls)
    assert claude_home.recorded() == old
    assert "as zoey" in out
    assert "as the Linux account Blink was set up for" not in out


def test_reinstall_into_the_same_folder_undoes_nothing(tmp_path,
                                                       monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    _recorded_wsl(tmp_path)
    assert cli.main(["install"]) == 0
    assert _inside(calls) == ["probe", "install_into"]


@pytest.mark.parametrize("config_dir, linux_home", [
    ("/home/zoe//.claude", "/home/zoe"),
    ("/home/zoe/x/../.claude", "/home/zoe"),
    ("/home/zoe/./.claude/", "/home/zoe//"),
])
def test_the_same_folder_spelled_another_way_is_not_a_move(
        tmp_path, monkeypatch, capsys, config_dir, linux_home):
    """Round 2, C2: the record's folder is normalised on the way in
    (claude_home.unc, U34), and was compared against the probe's raw
    spelling. A CLAUDE_CONFIG_DIR with `//` or `..` in it then made every
    reinstall a move, and took Blink out of the live folder first."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(config_dir=config_dir, home=linux_home,
                                   config_dir_set=True))
    _recorded_wsl(tmp_path)
    assert cli.main(["install"]) == 0
    assert _inside(calls) == ["probe", "install_into"]


def test_a_kept_wsl_desk_that_fails_says_no_done(tmp_path, monkeypatch,
                                                 capsys):
    """U27: a plain install on a WSL desk whose probe failed said
    "Nothing was changed." and went on to the Codex hooks, the service and
    "Done." It stops at the step now, with the service it stopped started
    again, and says only what is true."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=None)
    _recorded_wsl(tmp_path)
    assert cli.main(["install"]) == 1
    out = capsys.readouterr().out
    assert "Nothing was changed." not in out and "Done." not in out
    assert "Claude Code inside Ubuntu was not changed." in out
    assert [c[0] for c in calls] == ["halt", "probe", "restart"]


def test_install_wsl_over_a_native_record(tmp_path, monkeypatch, capsys):
    """A native record in a CLAUDE_CONFIG_DIR folder: once the WSL record
    replaces it, uninstall would look only where its own environment
    points, so install takes Blink out of that folder first."""
    cc = tmp_path / "cc"
    cc.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(cc))
    (cc / "settings.json").write_text("{}")
    assert cli.main(["install"]) == 0
    assert "statusLine" in json.loads((cc / "settings.json").read_text())
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    capsys.readouterr()
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 0
    got = json.loads((cc / "settings.json").read_text())
    assert "statusLine" not in got and not got.get("hooks")


def test_install_wsl_puts_this_build_in_place(tmp_path, monkeypatch,
                                              capsys):
    """U28: run from a new download, `install --wsl` swaps this build in
    and installs the service, as a plain install does. It used to restart
    whatever the service ran -- on a 1.4.1 desk a daemon that ignores the
    record -- and print "Done" over it."""
    from pc import update
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    as_frozen(monkeypatch)
    download = tmp_path / "Downloads" / "blink"
    download.mkdir(parents=True)
    (download / "blink").write_text("new build")
    monkeypatch.setattr(cli, "_self_path", lambda: str(download / "blink"))
    swapped = []

    def swap_in(staged, installed, version):
        swapped.append(version)
        calls.append(("swap",))
        return True, ""
    monkeypatch.setattr(update, "swap_in", swap_in)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 0
    out = capsys.readouterr().out
    assert swapped == [cli.RELEASE_VERSION]
    assert [c[0] for c in calls] == ["halt", "swap", "probe", "install_into",
                                     "service"]
    assert ("restart",) not in calls
    assert "Blink reads Claude Code inside Ubuntu only" in out


def test_install_wsl_stops_over_a_config_json_it_cannot_record_in(
        tmp_path, monkeypatch, capsys):
    """U25: the record is the WSL desk. With a config.json that does not
    parse, the disclosure said "Changes" it, the settings inside were
    written, the save failed, and uninstall then had no record to undo
    them by. Stopped before anything is started or written."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    os.makedirs(cli.blink_home(), exist_ok=True)
    config = inside(tmp_path, cli.user_config.path(cli.blink_home()))
    with open(config, "w") as f:
        f.write("{half an edit")
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = capsys.readouterr().out
    assert "Blink setup stopped." in out and "Nothing was changed." in out
    assert calls == []
    with open(config) as f:
        assert f.read() == "{half an edit"


def test_install_wsl_takes_back_what_it_could_not_record(
        tmp_path, monkeypatch, capsys):
    """U25's other half: a save that fails at the write (an OSError)
    comes before the previous home is taken apart, and the writes inside
    are taken back out, so the desk is left as it was."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg(
        [(WSL_GUID, "Ubuntu"), (OTHER_GUID, "Debian")]), probe=_probe())
    old = _recorded_wsl(tmp_path)

    def fails(home):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(claude_home, "save", fails)
    assert cli.main(["install", "--wsl", "Debian"]) == 1
    out = capsys.readouterr().out
    assert "PermissionError" in out and "Done." not in out
    assert claude_home.recorded() == old
    undone = [c[1].distro_name for c in calls if c[0] == "uninstall_from"]
    assert undone == ["Debian"]


def _save_fails(monkeypatch):
    from pc import claude_home

    def fails(home):
        raise PermissionError(13, "Permission denied")
    monkeypatch.setattr(claude_home, "save", fails)


def test_a_same_folder_save_that_fails_keeps_what_the_record_names(
        tmp_path, monkeypatch, capsys):
    """Round 2, C1: the same distribution and folder, set up again, and
    the save fails. The record still names that folder, so what was
    written again is what uninstall will find: it stays, and is said to,
    where the message used to say "they are being taken back out" over
    a step that never ran."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    old = _recorded_wsl(tmp_path)
    _save_fails(monkeypatch)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert _inside(calls) == ["probe", "install_into"]
    assert claude_home.recorded() == old
    assert "taken back out" not in out
    assert "still names that folder" in out
    assert "Done." not in out


def test_a_moved_folder_save_that_fails_takes_the_new_one_back_out(
        tmp_path, monkeypatch, capsys):
    """C1, moved inside: the old folder was already taken out, so a new
    one left written but unrecorded would be wired with nothing to find
    it, and an uninstall after would delete the shims its hooks run --
    the exit 2 that blocks every prompt. It is taken back out, and the
    message says Blink is now in neither folder."""
    from pc import claude_home, wsl
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe(config_dir="/home/zoe/work-claude",
                                   config_dir_set=True))
    old = _recorded_wsl(tmp_path)
    _save_fails(monkeypatch)
    assert cli.main(["install"]) == 1
    out = " ".join(capsys.readouterr().out.split())
    assert _inside(calls) == ["probe", "uninstall_from", "install_into",
                              "uninstall_from"]
    undone = [c[1] for c in calls if c[0] == "uninstall_from"]
    assert undone[0] == old
    assert undone[1].config_dir == wsl.unc("Ubuntu", "/home/zoe/work-claude")
    assert claude_home.recorded() == old
    assert "being taken back out" in out
    assert "already taken out of the folder it used before" in out
    assert "Done." not in out


def test_install_wsl_says_when_the_service_did_not_come_back(monkeypatch,
                                                             capsys):
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    monkeypatch.setattr(cli, "_install_service",
                        lambda: "could not start it")
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = capsys.readouterr().out
    assert "Done." not in out
    assert "background\nservice is not running" in out


def test_install_wsl_refuses_a_foreign_home(monkeypatch, capsys):
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    monkeypatch.setattr(cli, "_home_refusal", lambda: True)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = capsys.readouterr().out
    assert "The background service was NOT installed." in out
    assert "Done." not in out


def test_status_says_a_failed_read_is_not_a_parse_error(tmp_path, monkeypatch,
                                                        capsys):
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), running=True)
    _recorded_wsl(tmp_path)

    monkeypatch.setattr(install_statusline, "default_wsl_run",
                        lambda home: lambda argv, stdin=None: (1, b"", b""))
    cli.main(["status"])
    out = capsys.readouterr().out
    assert "Activity    unknown -- settings.json could not be read" in out


def test_status_follows_a_symlinked_settings_json_inside_wsl(
        tmp_path, monkeypatch, capsys):
    """U45: read inside the distribution, where readlink -f resolves a
    settings.json linked into a dotfiles folder, not over UNC, where the
    9P server may not follow it."""
    from pc import install_hooks
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), running=True)
    home = _recorded_wsl(tmp_path)
    doc = {"hooks": {ev: [{"hooks": [{
        "type": "command",
        "command": install_hooks.wsl_hook_command(home, ev)}]}]
        for ev, _ in install_hooks.HOOK_EVENTS}}
    asked = []

    def run(argv, stdin=None):
        asked.append(argv[-2:])
        return (0, b"/home/zoe/dotfiles/claude-settings.json\npresent\n"
                + json.dumps(doc).encode(), b"")
    monkeypatch.setattr(install_statusline, "default_wsl_run",
                        lambda h: run)
    hooks = cli.claude_hooks(home)
    assert hooks.readable and hooks.wired == hooks.total
    assert asked == [["/home/zoe/.claude/settings.json", "any"]]


def test_plain_install_on_a_wsl_desk_discloses_the_wsl_files(
        tmp_path, monkeypatch, capsys):
    """The disclosure a WSL desk reads describes what is written inside
    the distribution and in its drop folder, not the Windows settings.json
    this path never edits; and it names no Linux path or user, since the
    probe that finds them has not run when it prints."""
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    home = _recorded_wsl(tmp_path)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    out = capsys.readouterr().out
    disclosure = out.split("To undo all of it:")[0]
    assert "Changes    Claude Code's settings.json inside WSL (Ubuntu)" \
        in disclosure
    assert f"Creates    {home.drop_dir}" in disclosure
    native = cli.settings_path(cli.claude_home.from_environ())
    assert f"Changes    {native}" not in out
    assert f"Creates    {cli.shim_path()}" not in out
    assert "/home/zoe" not in disclosure and "zoe" not in disclosure
    assert "wsl.localhost" not in disclosure


def test_plain_install_on_a_wsl_desk_ignores_a_broken_native_settings(
        tmp_path, monkeypatch, capsys):
    """The Windows settings.json is not this desk's, so one that does not
    parse neither stops the install nor raises out of the disclosure."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    home = _recorded_wsl(tmp_path)
    _settings(tmp_path).write_text("{ not json")
    assert cli.main(["install"]) == 0
    assert ("install_into", home) in calls
    assert _settings(tmp_path).read_text() == "{ not json"


def test_gone_distro_keeps_its_drop_folder_when_native_check_stops(
        tmp_path, monkeypatch, capsys):
    """The native pre-check refuses an unparseable settings.json: then the
    record and the drop folder are both left as they were, together."""
    from pc import claude_home
    _wsl_desk(monkeypatch, _FakeReg([(OTHER_GUID, "Debian")]))
    home = _recorded_wsl(tmp_path)
    os.makedirs(home.state_dir)
    _settings(tmp_path).write_text("{ not json")
    assert cli.main(["install"]) == 1
    assert claude_home.recorded() == home
    assert os.path.isdir(home.drop_dir)


# --- the final branch review of Plan B ------------------------------------

class _BadKeyReg(_FakeReg):
    """The fake registry with the key of `bad` unreadable."""

    def __init__(self, distros, bad):
        super().__init__(distros)
        self.bad = bad

    def values(self, hive, path):
        if path.endswith("\\" + self.bad):
            raise PermissionError("denied")
        return super().values(hive, path)


def test_plain_install_leaves_an_unchecked_wsl_desk_alone(tmp_path,
                                                          monkeypatch,
                                                          capsys):
    """Finding 1: one unreadable key -- the recorded distribution's -- is
    not that distribution gone. The install stops with the record, the drop
    folder and the Windows settings exactly as they were."""
    from pc import claude_home
    for reg in (_BadKeyReg([(OTHER_GUID, "Debian"), (WSL_GUID, "Ubuntu")],
                           WSL_GUID),
                _FakeReg(broken=True)):
        calls = _wsl_desk(monkeypatch, reg, probe=_probe())
        home = _recorded_wsl(tmp_path)
        os.makedirs(home.state_dir, exist_ok=True)
        _settings(tmp_path, {})
        assert cli.main(["install"]) == 1
        out = capsys.readouterr().out
        assert "Could not check WSL; left it as it was." in out
        assert claude_home.recorded() == home
        assert os.path.isdir(home.drop_dir)
        assert _read(tmp_path) == {}
        assert calls == []


def test_a_bad_neighbour_key_does_not_hide_the_distro(tmp_path,
                                                      monkeypatch, capsys):
    """Another distribution's unreadable key costs that one alone."""
    calls = _wsl_desk(monkeypatch, _BadKeyReg(
        [(OTHER_GUID, "Debian"), (WSL_GUID, "Ubuntu")], OTHER_GUID),
        probe=_probe())
    home = _recorded_wsl(tmp_path)
    _settings(tmp_path, {})
    assert cli.main(["install"]) == 0
    assert ("install_into", home) in calls


def test_status_says_it_could_not_check_wsl(tmp_path, monkeypatch, capsys):
    calls = _wsl_desk(monkeypatch, _FakeReg(broken=True))
    _recorded_wsl(tmp_path)
    watch = _UncWatch(monkeypatch)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Claude Code WSL (Ubuntu), could not check" in out
    assert "not found" not in out
    assert "Activity    not checked -- Ubuntu could not be checked" in out
    assert calls == [] and not watch.seen


def test_status_says_a_distro_was_renamed(tmp_path, monkeypatch, capsys):
    """Finding 7: found by GUID under another name, status says so and
    reads it under the name it has now."""
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Work")]),
                      running=True)
    _recorded_wsl(tmp_path, name="Ubuntu")
    watch = _UncWatch(monkeypatch)
    cli.main(["status"])
    assert ("wsl_run", "Work") in calls and ("wsl_run", "Ubuntu") not in calls
    out = capsys.readouterr().out
    assert "Claude Code WSL (Work), running" in out
    assert (f"renamed from Ubuntu -- run `{cli._repair_command()} install`"
            in out)
    assert watch.paths
    assert all("\\Work\\" in p and "Ubuntu" not in p for p in watch.paths), watch.paths


def test_status_wire_reads_a_renamed_distro_under_its_new_name(
        tmp_path, monkeypatch, capsys):
    """U20: `--wire` built its bus with no home, so its readers asked
    claude_home.current() -- the record, under the old name -- and read
    paths that lead nowhere, while plain status read the new name. The
    bus is pinned to the home status found."""
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Work")]), running=True)
    _recorded_wsl(tmp_path, name="Ubuntu")
    watch = _UncWatch(monkeypatch)
    cli.main(["status", "--wire"])
    assert watch.paths
    assert all("\\Work\\" in p and "Ubuntu" not in p
               for p in watch.paths), watch.paths


def _marked_drop(home, wsl_home):
    """A drop folder as install leaves it: state/, a payload, a marker.
    In `wsl_home`'s drop folder, once it is known to lie under `home`."""
    inside(home, wsl_home.drop_dir)
    os.makedirs(wsl_home.state_dir, exist_ok=True)
    for name in ("statusline.json", "statusline-installed-command"):
        with open(os.path.join(wsl_home.drop_dir, name), "w") as f:
            f.write("x")


def test_the_wsl_helpers_write_only_under_the_callers_home(tmp_path):
    """CV1: handed a home the files would not land under -- a script's
    real ~ while the code resolves another -- they stop before writing."""
    from pc import claude_home
    elsewhere = tmp_path / "not-home"
    elsewhere.mkdir()
    with pytest.raises(AssertionError):
        _recorded_wsl(elsewhere)
    assert claude_home.recorded() is None
    h = claude_home.wsl(WSL_GUID, "Ubuntu", "", "", "", False)
    with pytest.raises(AssertionError):
        _marked_drop(elsewhere, h)
    assert not os.path.exists(h.drop_dir)


def test_codex_hooks_on_a_wsl_desk_have_a_script_to_run(tmp_path,
                                                        monkeypatch, capsys):
    """U24: Codex runs on Windows, and its hooks name ~/.blink/blink-hook.sh,
    but a plain install on a WSL desk writes its shims inside the
    distribution and never wrote that one: all ten Codex hooks ran a
    missing script, and the Codex waiting state was never shown. The
    Codex step writes the script it registers."""
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    _recorded_wsl(tmp_path)
    monkeypatch.setattr(cli, "codex_present", lambda: True)
    registered = []
    monkeypatch.setattr(
        cli.install_codex_hooks, "install",
        lambda p, s: registered.append(s) or "installed (10 events).")
    assert cli.main(["install"]) == 0
    shim = inside(tmp_path, cli.hook_shim_path())
    assert registered == [shim]
    assert os.path.isfile(shim) and os.access(shim, os.X_OK)


def test_a_failed_wsl_uninstall_keeps_what_can_undo_it(tmp_path,
                                                       monkeypatch, capsys):
    """Finding 2: the shims inside are still live and write into the drop
    folder, so the record, that folder and its markers stay, and the
    person is told how to finish by hand."""
    from pc import claude_home
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]))
    home = _recorded_wsl(tmp_path)
    _marked_drop(tmp_path, home)

    def fails(home, wsl_run, **k):
        raise install_statusline.WslInstallError(
            "Could not read Claude Code's settings inside WSL.")
    monkeypatch.setattr(install_statusline, "uninstall_from", fails)
    assert cli.main(["uninstall"]) == 1
    out = capsys.readouterr().out
    assert claude_home.recorded() == home
    assert os.path.exists(os.path.join(home.drop_dir, "statusline-installed-command"))
    assert "remove ~/.blink/out and ~/.blink/blink-*.sh" in out
    assert "statusLine and hooks entries" in out
    assert "Done. Nothing of Blink's is left running." not in out
    # And a second uninstall, once it works, finishes the job.
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]))
    assert cli.main(["uninstall"]) == 0
    assert claude_home.recorded() is None
    assert not os.path.exists(home.drop_dir)


def test_an_unchecked_wsl_uninstall_keeps_the_record(tmp_path,
                                                     monkeypatch, capsys):
    """Finding 1, uninstall's half: a registry that cannot be read is not
    a distribution gone."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg(broken=True))
    home = _recorded_wsl(tmp_path)
    _marked_drop(tmp_path, home)
    assert cli.main(["uninstall"]) == 1
    out = capsys.readouterr().out
    assert "could not check WSL" in out
    assert "no longer on this computer" not in out
    assert "remove ~/.blink/out and ~/.blink/blink-*.sh" in out
    assert claude_home.recorded() == home
    assert os.path.isdir(home.drop_dir)
    assert calls == []


def test_a_previous_distro_that_would_not_undo_keeps_its_folder(tmp_path, 
        monkeypatch, capsys):
    """`--wsl Debian` over Ubuntu, with Ubuntu's undo failing: Ubuntu's
    shims still write into its drop folder, which is not deleted.

    U22: and the install stops there. It used to go on and record Debian,
    while the advice it had just printed said to run uninstall again once
    Ubuntu started -- an uninstall that, reading only the record, would
    never look at Ubuntu again. Debian's entries come back out, Ubuntu's
    record is put back, and the advice is then true."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg(
        [(WSL_GUID, "Ubuntu"), (OTHER_GUID, "Debian")]), probe=_probe())
    old = _recorded_wsl(tmp_path)
    _marked_drop(tmp_path, old)

    def fails(home, wsl_run, **k):
        calls.append(("uninstall_from", home))
        if home.distro_name == "Ubuntu":
            raise install_statusline.WslInstallError("nope")
        return "Statusline removed."
    monkeypatch.setattr(install_statusline, "uninstall_from", fails)
    assert cli.main(["install", "--wsl", "Debian"]) == 1
    out = capsys.readouterr().out
    assert os.path.isdir(old.drop_dir)
    assert "inside Ubuntu remove ~/.blink/out" in out
    assert (f"Run `{cli.command_to_type()} install --wsl Debian` again once"
            " Ubuntu starts") in out
    assert "Blink still reads Ubuntu, not Debian." in out
    assert "Done." not in out
    assert claude_home.recorded() == old
    undone = [c[1].distro_name for c in calls if c[0] == "uninstall_from"]
    assert undone == ["Ubuntu", "Debian"]
    new = claude_home.wsl(OTHER_GUID, "Debian", "", "", "", False)
    assert not os.path.exists(new.drop_dir)


def test_a_previous_record_that_cannot_be_put_back_is_said(
        tmp_path, monkeypatch, capsys):
    """Round 2, C4: U22's path, with the save that puts Ubuntu's record
    back failing too. That failure was swallowed: Debian had just been
    emptied, the record still named it, and the message said "Blink still
    reads Ubuntu, not Debian" -- the opposite. Debian, recorded and
    written, is now kept; the message says Blink reads Debian, that
    Ubuntu is left set up with nothing recording it, and how to take it
    out by hand, since an install or uninstall will no longer find it."""
    from pc import claude_home
    calls = _wsl_desk(monkeypatch, _FakeReg(
        [(WSL_GUID, "Ubuntu"), (OTHER_GUID, "Debian")]), probe=_probe())
    old = _recorded_wsl(tmp_path)
    _marked_drop(tmp_path, old)

    def from_(home, wsl_run, **k):
        calls.append(("uninstall_from", home))
        if home.distro_name == "Ubuntu":
            raise install_statusline.WslInstallError("nope")
        return "Statusline removed."
    monkeypatch.setattr(install_statusline, "uninstall_from", from_)
    real = claude_home.save

    def save(home):
        if home.distro_guid == WSL_GUID:
            raise PermissionError(13, "Permission denied")
        return real(home)
    monkeypatch.setattr(claude_home, "save", save)
    assert cli.main(["install", "--wsl", "Debian"]) == 1
    out = " ".join(capsys.readouterr().out.split())
    new = claude_home.recorded()
    assert new.distro_guid == OTHER_GUID
    undone = [c[1].distro_name for c in calls if c[0] == "uninstall_from"]
    assert undone == ["Ubuntu"]
    assert os.path.isdir(old.drop_dir)
    assert "Blink still reads Ubuntu" not in out
    assert "could not put its record of Ubuntu back (PermissionError)" in out
    assert "Blink reads Debian now" in out
    assert "inside Ubuntu remove ~/.blink/out" in out
    assert "reading what it read before" not in out
    assert ("The background service is running again, reading Claude Code"
            " inside Debian.") in out
    assert "Done." not in out


def test_install_wsl_discloses_before_it_starts_anything(monkeypatch,
                                                         capsys):
    """Finding 4: what `install --wsl` writes, and what it reads, is said
    before the probe starts the distribution and before install_into
    writes inside it."""
    from pc import claude_home, wsl
    calls = _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]),
                      probe=_probe())
    said = {}

    def probe(name, *a, **k):
        said["probe"] = capsys.readouterr().out
        return _probe()

    def into(home, wsl_run, **k):
        said["install_into"] = capsys.readouterr().out
        return "Statusline installed."
    monkeypatch.setattr(wsl, "probe", probe)
    monkeypatch.setattr(install_statusline, "install_into", into)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 0
    before = said["probe"]
    drop = claude_home.wsl(WSL_GUID, "Ubuntu", "", "", "", False).drop_dir
    assert "Changes    Claude Code's settings.json inside WSL (Ubuntu)" \
        in before
    assert f"Creates    {drop}" in before
    assert "Claude Code's transcripts inside Ubuntu, while you use it" \
        in before
    assert "Claude Code's config inside Ubuntu, while you use it" in before
    assert "To undo all of it:" in before
    assert "zoe" not in before and "wsl.localhost" not in before
    from pc import spend_count
    flat = " ".join(l.strip() for l in before.splitlines())
    # What spend-count.json keeps waits for the probe, which is what tells
    # the seat (D1); the disclosure says it will be said. Nothing writes
    # that file until the service, which starts after it has been said.
    assert spend_count._default_path() not in flat
    assert ("What it keeps on a Claude seat billed by usage is said once"
            " setup has read the seat type inside Ubuntu.") in flat
    assert "saved only on a seat billed by usage" in flat
    # Nothing of it waited for the write.
    assert "Changes    Claude Code's settings.json" not in \
        said["install_into"]


def test_install_wsl_says_a_drop_folder_it_cannot_make(tmp_path,
                                                       monkeypatch, capsys):
    """Finding 9: an OSError from making the drop folder or writing a
    marker is said as a sentence, with no traceback and no path."""
    from pc import claude_home
    real_into = install_statusline.install_into
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())
    monkeypatch.setattr(install_statusline, "install_into", real_into)
    monkeypatch.setattr(install_statusline, "default_wsl_run",
                        lambda home: (lambda argv, stdin=None:
                                      pytest.fail("reached Linux")))
    drop = claude_home.wsl(WSL_GUID, "Ubuntu", "", "", "", False).drop_dir
    os.makedirs(os.path.dirname(drop), exist_ok=True)
    with open(drop, "w") as f:          # a file where the folder goes
        f.write("x")
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    out = capsys.readouterr().out
    after = out.split("To undo all of it:")[1]
    assert "Blink setup stopped." in after
    assert "Traceback" not in out
    assert drop not in after and str(tmp_path) not in after
    assert claude_home.recorded() is None


def test_install_wsl_says_a_marker_it_cannot_write(monkeypatch, capsys):
    from pc import claude_home
    _wsl_desk(monkeypatch, _FakeReg([(WSL_GUID, "Ubuntu")]), probe=_probe())

    def fails(home, wsl_run, **k):
        raise PermissionError(13, "Permission denied",
                              os.path.join(home.drop_dir, "marker"))
    monkeypatch.setattr(install_statusline, "install_into", fails)
    assert cli.main(["install", "--wsl", "Ubuntu"]) == 1
    after = capsys.readouterr().out.split("To undo all of it:")[1]
    assert "Blink setup stopped." in after and "PermissionError" in after
    assert "wsl-" not in after
    assert claude_home.recorded() is None
