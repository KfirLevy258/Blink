"""The CLI half of the usage refresh: the install question, the setting,
status and uninstall. HOME is sandboxed by tests/conftest.py."""
import json
import os
import sys

import pytest

from pc import claude_home, cli, user_config
from pc import usage_refresh as ur

QUESTION = ("BLINK can keep your figures current when Claude Code isn't"
            " running, by asking Claude Code's /usage now and then. It sends"
            " no messages and uses no quota. Turn on? ")


def _bh():
    return cli.blink_home()


def _tty(monkeypatch, answers, tty=True):
    asked = []
    it = iter(answers)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: tty, raising=False)

    def fake_input(*a):
        asked.append(a)
        return next(it)
    monkeypatch.setattr("builtins.input", fake_input)
    return asked


def _native():
    return claude_home.from_environ()


@pytest.fixture
def claude_found(monkeypatch):
    monkeypatch.setattr(ur, "find_claude", lambda home: "/x/claude")


def _work(monkeypatch, work):
    class V:
        spend_seat = work
        org_kind = "consumer"
    monkeypatch.setattr(cli, "_refresh_view", lambda home: V())


@pytest.mark.parametrize("answer,default,want", [
    ("", True, True), ("", False, False), ("Y", False, True),
    ("yes", False, True), ("N", True, False), ("No", True, False)])
def test_ask_yes_answers(monkeypatch, answer, default, want):
    _tty(monkeypatch, [answer])
    assert cli._ask_yes("Q? ", default) is want


def test_ask_yes_prompt_tail_follows_default(monkeypatch, capsys):
    _tty(monkeypatch, ["", ""])
    cli._ask_yes("Q? ", True)
    cli._ask_yes("Q? ", False)
    out = capsys.readouterr().out
    assert "Q? [Y/n] " in out and "Q? [y/N] " in out


def test_ask_yes_gives_up_after_three_and_takes_default(monkeypatch):
    asked = _tty(monkeypatch, ["maybe", "what", "huh", "y"])
    assert cli._ask_yes("Q? ", False) is False
    assert len(asked) == 3


def test_ask_yes_does_not_ask_without_a_terminal(monkeypatch):
    asked = _tty(monkeypatch, ["y"], tty=False)
    assert cli._ask_yes("Q? ", True) is None
    assert asked == []


def test_personal_login_defaults_yes(monkeypatch, capsys, claude_found):
    _tty(monkeypatch, [""])
    _work(monkeypatch, False)
    got = cli._install_refresh_step(_native())
    out = capsys.readouterr().out
    assert QUESTION + "[Y/n] " in out
    assert got == "on"
    assert user_config.load(_bh())[ur.SETTING] is True


def test_work_login_defaults_no(monkeypatch, capsys, claude_found):
    _tty(monkeypatch, [""])
    _work(monkeypatch, True)
    got = cli._install_refresh_step(_native())
    assert QUESTION + "[y/N] " in capsys.readouterr().out
    assert got == "off (turn on with: blink config usage-refresh on)"
    assert user_config.load(_bh())[ur.SETTING] is False


def test_team_org_is_a_work_login(monkeypatch, capsys, claude_found):
    class V:
        spend_seat = False
        org_kind = "team"
    monkeypatch.setattr(cli, "_refresh_view", lambda home: V())
    _tty(monkeypatch, [""])
    cli._install_refresh_step(_native())
    assert "[y/N] " in capsys.readouterr().out


def test_non_interactive_leaves_the_key_absent(monkeypatch, claude_found):
    _tty(monkeypatch, [], tty=False)
    _work(monkeypatch, False)
    got = cli._install_refresh_step(_native())
    assert got == "off (turn on with: blink config usage-refresh on)"
    assert ur.SETTING not in user_config.load(_bh())


@pytest.mark.parametrize("value,line", [(True, "on"), (False, "off")])
def test_reinstall_does_not_ask_twice(monkeypatch, value, line):
    os.makedirs(_bh(), exist_ok=True)
    user_config.put(_bh(), ur.SETTING, value)
    asked = _tty(monkeypatch, ["y"])
    assert cli._install_refresh_step(_native()) == line
    assert asked == []


def test_no_claude_code_says_so_and_does_not_ask(monkeypatch):
    monkeypatch.setattr(ur, "find_claude", lambda home: None)
    asked = _tty(monkeypatch, ["y"])
    assert cli._install_refresh_step(_native()) == (
        "off (Claude Code isn't installed; see"
        " docs/README-full.md#keeping-figures-fresh)")
    assert asked == []


def test_wsl_home_is_never_asked(monkeypatch, claude_found):
    asked = _tty(monkeypatch, ["y"])
    wsl = claude_home.wsl("g", "Ubuntu", "u", "/home/u", "/home/u/.claude",
                          False)
    assert cli._install_refresh_step(wsl) == "not available for WSL yet"
    assert asked == []
    assert not os.path.exists(os.path.join(_bh(), "config.json"))


def test_install_has_a_usage_refresh_step():
    assert cli._install_steps() == (7 if sys.platform == "win32" else 6)


def test_install_prints_the_step(monkeypatch, capsys):
    monkeypatch.setattr(ur, "find_claude", lambda home: None)
    cli.main(["install"])
    out = capsys.readouterr().out
    n = cli._install_steps()
    # Last, after the service: the step counts to the total it was given.
    assert f"[{n}/{n}] Usage refresh ... " in out


def test_config_on_clears_the_reason_it_went_off(monkeypatch, capsys):
    os.makedirs(_bh(), exist_ok=True)
    ur.save_state(_bh(), ur.State(last_run_at=5.0, fails=2,
                                  failing_since=3.0, off_reason="cost"))
    user_config.put(_bh(), ur.SETTING, False)
    assert cli.main(["config", "usage-refresh", "on"]) == 0
    s = ur.load_state(_bh())
    assert (s.off_reason, s.fails, s.failing_since) == ("", 0, None)
    assert s.last_run_at == 5.0
    assert user_config.load(_bh())[ur.SETTING] is True


def test_config_off_keeps_state(monkeypatch):
    os.makedirs(_bh(), exist_ok=True)
    ur.save_state(_bh(), ur.State(fails=2))
    cli.main(["config", "usage-refresh", "off"])
    assert ur.load_state(_bh()).fails == 2


@pytest.mark.parametrize("setup,row", [
    ({}, "not set up"),
    ({"set": False}, "off"),
    ({"set": True}, "on"),
    ({"set": True, "ago": 12 * 60}, "on, last ran 12 min ago"),
    ({"set": False, "reason": "cost"},
     "off: Claude Code's /usage used the model"),
    ({"set": False, "reason": "failing"},
     "off: /usage kept failing; run claude and /login, then turn it on"
     " again"),
])
def test_status_row(setup, row):
    import time
    os.makedirs(_bh(), exist_ok=True)
    if "set" in setup:
        user_config.put(_bh(), ur.SETTING, setup["set"])
    if "ago" in setup or "reason" in setup:
        ur.save_state(_bh(), ur.State(
            last_run_at=time.time() - setup.get("ago", 0),
            off_reason=setup.get("reason", "")))
    assert cli._refresh_status(False) == row


def test_status_prints_the_row(capsys):
    cli.main(["status"])
    assert "Usage refresh not set up" in capsys.readouterr().out


def test_uninstall_removes_state_temp_siblings_and_folder(tmp_path):
    os.makedirs(os.path.join(_bh(), "refresh"))
    state = os.path.join(_bh(), "usage-refresh.json")
    ur.save_state(_bh(), ur.State(fails=1))
    sib = state + ".tmp"
    open(sib, "w").write("{}")
    open(os.path.join(_bh(), "refresh", "x"), "w").write("")
    assert cli.main(["uninstall"]) == 0
    assert not os.path.exists(state)
    assert not os.path.exists(sib)
    assert not os.path.exists(os.path.join(_bh(), "refresh"))


def test_update_asks_only_when_interactive_and_absent(monkeypatch, capsys,
                                                      claude_found):
    asked = _tty(monkeypatch, [""], tty=False)
    assert cli._update_refresh_question() == ""
    assert asked == []
    _tty(monkeypatch, [""])
    _work(monkeypatch, False)
    assert cli._update_refresh_question() == "on"
    assert cli._update_refresh_question() == ""   # answered: not again
