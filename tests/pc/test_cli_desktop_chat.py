"""`blink config desktop-chat-light` and the Desktop chat rows of `blink status`.

The watcher itself (pc/desktop_chat_activity) is tested on its own; here it
is replaced by fakes, or left real against the sandbox's empty home or a
synthetic Desktop layout under tmp_path (tests/support/desktop_chat_fixtures),
so nothing reads a real Desktop folder. The hooks' live-session count on the
Activity row stays the hooks' alone, whatever the chat is doing.

status's own watcher is polled once, and one poll only takes a baseline, so
it reports availability and never a busy/idle word (cli._desktop_chat_lines
says why). "Desktop chat: busy" / "idle" come only from a watcher handed in
that has already been polling.
"""
import argparse
import json
import os
import time

import pytest

from pc import cli, desktop_chat_activity
from tests.support import desktop_chat_fixtures as fx
from tests.support.home import inside


def _config(setting=None, value=None):
    return cli.cmd_config(argparse.Namespace(setting=setting, value=value))


def _config_json():
    with open(os.path.join(cli.blink_home(), "config.json"),
              encoding="utf-8") as f:
        return json.load(f)


def _fake_watcher(availability, busy=False, raises=None):
    class Fake:
        made = []

        def __init__(self, *a, **kw):
            if raises == "init":
                raise RuntimeError("no watcher")
            self.kw = kw
            self.polls = 0
            Fake.made.append(self)

        def poll(self, now):
            self.polls += 1
            if raises == "poll":
                raise RuntimeError("poll broke")
            return busy

        def availability(self):
            if raises == "availability":
                raise RuntimeError("availability broke")
            return availability
    return Fake


def _desk(installed=True):
    """A Desk read from the sandbox, with Desktop marked installed."""
    desk = cli.read_desk()
    return desk._replace(desktop_installed=installed)


# -- blink config ------------------------------------------------------------

def test_config_off_then_on_writes_config_json(capsys):
    assert _config("desktop-chat-light", "off") == 0
    assert _config_json()[desktop_chat_activity.SETTING] is False
    out = capsys.readouterr().out
    assert "desktop-chat-light: off" in out
    assert "The activity light comes only from Claude Code and Codex." in out
    assert desktop_chat_activity.enabled(cli.blink_home()) is False

    assert _config("desktop-chat-light", "on") == 0
    assert _config_json()[desktop_chat_activity.SETTING] is True
    out = capsys.readouterr().out
    assert "desktop-chat-light: on" in out
    assert "nothing leaves the computer" in out
    assert desktop_chat_activity.enabled(cli.blink_home()) is True


def test_config_lists_the_setting_on_by_default(capsys):
    assert _config() == 0
    lines = capsys.readouterr().out.splitlines()
    row = [ln for ln in lines if ln.startswith("desktop-chat-light")]
    assert len(row) == 1
    assert row[0].split()[1] == "on"
    assert "Claude Desktop chat is answering (best effort)" in row[0]
    # Every row keeps its state in its own column, the long name included.
    for ln in lines:
        assert ln.split()[1] in ("on", "off", "unreadable")


def test_config_reads_an_unreadable_file_as_unreadable(capsys, tmp_path):
    home = cli.blink_home()
    os.makedirs(inside(tmp_path, home), exist_ok=True)
    with open(inside(tmp_path, os.path.join(home, "config.json")), "w",
              encoding="utf-8") as f:
        f.write("{ not json")
    assert _config("desktop-chat-light") == 0
    assert "desktop-chat-light: unreadable" in capsys.readouterr().out


def test_a_missing_key_means_on_and_the_off_key_means_off():
    assert cli._CONFIG["desktop-chat-light"].key == (
        desktop_chat_activity.SETTING)
    assert desktop_chat_activity.config_state(cli.blink_home()) is True
    _config("desktop-chat-light", "off")
    assert desktop_chat_activity.config_state(cli.blink_home()) is False
    _config("desktop-chat-light", "on")
    assert desktop_chat_activity.config_state(cli.blink_home()) is True


def test_status_page_and_usage_refresh_rows_are_unchanged_in_shape(capsys):
    _config()
    out = capsys.readouterr().out
    assert any(ln.startswith("status-page ") for ln in out.splitlines())
    assert any(ln.startswith("usage-refresh ") for ln in out.splitlines())


# -- the status rows ---------------------------------------------------------

@pytest.mark.parametrize("word, said", [
    (desktop_chat_activity.AVAILABLE, "available (best effort)"),
    (desktop_chat_activity.UNAVAILABLE,
     "not available on this Desktop version"),
    (desktop_chat_activity.OFF, "turned off"),
])
def test_status_says_each_availability(monkeypatch, word, said):
    fake = _fake_watcher(word, busy=True)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", fake)
    # The availability row alone: status's own one-shot watcher never
    # supplies a busy/idle word, even one whose poll would say True.
    assert cli._desktop_chat_lines(_desk()) == [f"Desktop chat light: {said}"]
    assert fake.made[-1].polls == 0


@pytest.mark.parametrize("busy, said", [(True, "busy"), (False, "idle")])
def test_a_polling_watcher_says_busy_or_idle(busy, said):
    watcher = _fake_watcher(desktop_chat_activity.AVAILABLE, busy)()
    assert cli._desktop_chat_lines(_desk(), watcher=watcher) == [
        "Desktop chat light: available (best effort)",
        f"Desktop chat: {said}"]


@pytest.mark.parametrize("word", [desktop_chat_activity.UNAVAILABLE,
                                  desktop_chat_activity.OFF])
def test_a_polling_watcher_that_is_not_available_says_no_word(word):
    watcher = _fake_watcher(word, busy=True)()
    lines = cli._desktop_chat_lines(_desk(), watcher=watcher)
    assert len(lines) == 1 and lines[0].startswith("Desktop chat light: ")


@pytest.mark.parametrize("answer", [None, 1, "busy"])
def test_a_poll_that_is_not_a_bool_says_no_word(answer):
    watcher = _fake_watcher(desktop_chat_activity.AVAILABLE, answer)()
    assert cli._desktop_chat_lines(_desk(), watcher=watcher) == [
        "Desktop chat light: available (best effort)"]


# -- the real watcher on a synthetic Desktop ----------------------------------
#
# win32's rule, because it needs no learned churn sub-folder: on macOS the
# IndexedDB signal says nothing for several minutes, and that is the module's
# to test, not status's.

def _sent_now():
    return [{"eventName": desktop_chat_activity.SENT,
             "eventTimestamp": int(time.time() * 1000)}]


# Taken at import, so a test that patches the module's class with a factory
# built on this still reaches the real one.
_REAL = desktop_chat_activity.DesktopChatActivity


def _real_watcher(layout):
    return _REAL(
        idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
        ls_dir=layout.ls_dir, platform="win32")


def test_a_pending_sent_alone_never_makes_status_say_busy(monkeypatch,
                                                          tmp_path):
    layout = fx.make_layout(tmp_path / "desktop")
    fx.write_queue(layout.ls_dir, _sent_now())
    made = []

    def factory(**kw):
        made.append(kw)
        return _real_watcher(layout)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", factory)
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)"]
    assert made == [{"blink_home": cli.blink_home()}]


def test_a_pending_sent_with_no_write_reads_idle_on_a_polling_watcher(
        tmp_path):
    layout = fx.make_layout(tmp_path / "desktop")
    fx.write_queue(layout.ls_dir, _sent_now())
    watcher = _real_watcher(layout)
    watcher.poll(time.time())           # the baseline, as the daemon's has
    assert cli._desktop_chat_lines(_desk(), watcher=watcher) == [
        "Desktop chat light: available (best effort)",
        "Desktop chat: idle"]


def test_a_write_between_polls_reads_busy_on_a_polling_watcher(tmp_path):
    layout = fx.make_layout(tmp_path / "desktop")
    watcher = _real_watcher(layout)
    watcher.poll(time.time())
    fx.grow_log(layout.idb_dir, 128)
    assert cli._desktop_chat_lines(_desk(), watcher=watcher) == [
        "Desktop chat light: available (best effort)",
        "Desktop chat: busy"]


def test_the_status_watcher_uses_the_blink_home(monkeypatch):
    fake = _fake_watcher(desktop_chat_activity.AVAILABLE)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", fake)
    cli._desktop_chat_lines(_desk())
    assert fake.made[-1].kw == {"blink_home": cli.blink_home()}


def test_turned_off_from_config_with_the_real_watcher(capsys):
    _config("desktop-chat-light", "off")
    capsys.readouterr()
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: turned off"]


def test_a_refused_blob_subfolder_reads_not_available_in_status(monkeypatch,
                                                                tmp_path):
    layout = fx.make_layout(tmp_path / "desktop")
    fx.refuse_scandir(monkeypatch, desktop_chat_activity,
                      os.path.join(layout.blob_dir, "1"))

    def factory(**kw):
        return _REAL(idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                     ls_dir=layout.ls_dir, platform="darwin", **kw)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", factory)
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: not available on this Desktop version"]


def test_no_desktop_folders_reads_not_available_with_the_real_watcher():
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: not available on this Desktop version"]


def test_no_rows_without_claude_desktop(monkeypatch):
    fake = _fake_watcher(desktop_chat_activity.AVAILABLE, True)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", fake)
    desk = _desk(installed=False)._replace(desktop=False)
    assert cli._desktop_chat_lines(desk) == []
    assert fake.made == []


@pytest.mark.parametrize("where", ["init", "poll", "availability"])
def test_a_watcher_that_raises_costs_only_its_rows(monkeypatch, where):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE, True,
                                      raises=where))
    lines = cli._desktop_chat_lines(_desk())
    assert lines[0].startswith("Desktop chat light: ")
    assert all("busy" not in ln for ln in lines)


@pytest.mark.parametrize("where, expect", [
    ("poll", ["Desktop chat light: available (best effort)"]),
    ("availability", ["Desktop chat light: unknown"]),
])
def test_a_polling_watcher_that_raises_costs_only_its_word(where, expect):
    watcher = _fake_watcher(desktop_chat_activity.AVAILABLE, True,
                            raises=where)()
    assert cli._desktop_chat_lines(_desk(), watcher=watcher) == expect


def _status_with_desktop(monkeypatch, capsys):
    real = cli.read_desk
    monkeypatch.setattr(
        cli, "read_desk",
        lambda *a, **kw: real(*a, **kw)._replace(desktop_installed=True))
    rc = cli.cmd_status(argparse.Namespace(wire=False))
    return rc, capsys.readouterr().out


def test_status_prints_the_rows_on_a_desktop_desk(monkeypatch, capsys):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE, True))
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: available (best effort)" in out
    # status's own watcher is one poll, which is no reading of a chat.
    assert "Desktop chat: " not in out
    assert "Usage refresh" in out


def test_status_with_a_pending_sent_alone_says_no_busy(monkeypatch, capsys,
                                                       tmp_path):
    layout = fx.make_layout(tmp_path / "desktop")
    fx.write_queue(layout.ls_dir, _sent_now())
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        lambda **kw: _real_watcher(layout))
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: available (best effort)" in out
    assert "Desktop chat: busy" not in out


@pytest.mark.parametrize("where", ["init", "poll", "availability"])
def test_status_survives_a_watcher_that_raises(monkeypatch, capsys, where):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE, True,
                                      raises=where))
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: " in out
    assert "Usage refresh" in out


# -- the hooks census --------------------------------------------------------

def test_the_live_session_count_excludes_the_chat(monkeypatch, tmp_path):
    from pc import claude_home
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE, True))
    state_dir = claude_home.current().state_dir
    os.makedirs(inside(tmp_path, state_dir), exist_ok=True)
    with open(inside(tmp_path, os.path.join(state_dir, "s1.state")), "w",
              encoding="utf-8") as f:
        json.dump({"event": "PreToolUse", "t": time.time()}, f)
    assert cli._live_sessions() == 1


def test_the_live_session_count_is_zero_with_only_a_chat(monkeypatch):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE, True))
    assert cli._live_sessions() == 0


# -- the install note --------------------------------------------------------

def _install_desk(hooks_light=True, codex=False):
    hooks = cli.ClaudeHooks(10 if hooks_light else 0, 10, True, False, True)
    return _desk()._replace(desktop=True, hooks=hooks, codex=codex)


# Each desk is read before sys.platform is faked, so read_desk never looks
# for a Desktop under another platform's paths.

# The install note's light rows describe what Blink's HOOKS give a desk, and a
# desk with broken hooks is told it has no activity light (tests/test_cli.py
# pins both). The chat light is reported by `blink status` on its own lines
# instead, so it never appears here, on any platform, hooks working or not.

@pytest.mark.parametrize("platform", ["darwin", "win32", "linux"])
@pytest.mark.parametrize("hooks_light", [True, False])
def test_the_install_note_never_names_the_chat_light(monkeypatch, platform,
                                                     hooks_light):
    desk = _install_desk(hooks_light=hooks_light)
    monkeypatch.setattr(cli.sys, "platform", platform)
    shown, missing = cli._light_rows(desk)
    assert not any("Desktop chat" in s for s in shown + missing)


def test_the_install_note_keeps_the_code_tab_rows(monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    shown, _ = cli._light_rows(_install_desk())
    assert ("the activity light, while you work in Desktop's Code tab"
            in shown)
    _, missing = cli._light_rows(_install_desk(hooks_light=False))
    assert any("Code tab" in m for m in missing)
