"""`blink config desktop-chat-light` and the Desktop chat rows of `blink status`.

The watcher itself (pc/desktop_chat_activity) is tested on its own; here it
is replaced by fakes, or left real against the sandbox's empty home or a
synthetic Desktop layout under tmp_path (tests/support/desktop_chat_fixtures),
so nothing reads a real Desktop folder. The hooks' live-session count on the
Activity row stays the hooks' alone, whatever the chat is doing.

status's own watcher is polled once, and one poll only takes a baseline, so
it reports availability and never a busy/idle word of its own
(cli._desktop_chat_lines says why). "Desktop chat: busy" / "idle" come from
the running daemon's reading, desktop_chat_activity.READING_FILE in the
Blink home, written here by tests/support/desktop_chat_fixtures or by a real
publishing watcher, or from a watcher handed in that has already been
polling. No fresh reading means no busy/idle line.
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
    # supplies a busy/idle word, even one whose poll would say True, and
    # with no daemon reading on disk there is none to print.
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
    # status's own watcher is one poll, which is no reading of a chat, and
    # no daemon has written one: no busy/idle line.
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


# -- the daemon's reading ----------------------------------------------------
#
# `blink status` is another process; the running daemon's watcher, the one
# with a polling history, writes its verdict to READING_FILE and status
# prints busy/idle from it.

def _reading(tmp_path, availability=desktop_chat_activity.AVAILABLE,
             busy=True, age=5.0, pid=None):
    """A reading as a daemon writes it. Its pid is this test process's,
    which is alive, unless the test names another."""
    return fx.write_reading_file(tmp_path, cli.blink_home(), {
        "availability": availability, "busy": busy,
        "t": time.time() - age,
        "pid": os.getpid() if pid is None else pid})


@pytest.mark.parametrize("busy, said", [(True, "busy"), (False, "idle")])
def test_status_prints_the_daemons_fresh_reading(monkeypatch, capsys,
                                                 tmp_path, busy, said):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE))
    _reading(tmp_path, busy=busy)
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    lines = out.splitlines()
    i = lines.index("Desktop chat light: available (best effort)")
    assert lines[i + 1] == f"Desktop chat: {said}"
    assert sum(ln.startswith("Desktop chat: ") for ln in lines) == 1
    assert "Usage refresh" in out


@pytest.mark.parametrize("busy, said", [(True, "busy"), (False, "idle")])
def test_the_lines_read_the_daemons_fresh_reading(monkeypatch, tmp_path,
                                                  busy, said):
    fake = _fake_watcher(desktop_chat_activity.AVAILABLE, busy=not busy)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", fake)
    _reading(tmp_path, busy=busy)
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)",
        f"Desktop chat: {said}"]
    # The word is the daemon's, never a poll of status's own watcher.
    assert fake.made[-1].polls == 0


def test_no_reading_file_means_no_busy_or_idle_line(monkeypatch):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE, True))
    assert not os.path.exists(os.path.join(
        cli.blink_home(), desktop_chat_activity.READING_FILE))
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)"]


@pytest.mark.parametrize("age", [
    desktop_chat_activity.READING_MAX_AGE_S + 30,      # the app has stopped
    -(desktop_chat_activity.READING_FUTURE_SLACK_S + 60),  # stamped ahead
])
def test_a_reading_that_is_not_fresh_says_no_word(monkeypatch, capsys,
                                                  tmp_path, age):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE))
    _reading(tmp_path, busy=True, age=age)
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: available (best effort)" in out
    assert "Desktop chat: " not in out


@pytest.mark.parametrize("daemon_says", [desktop_chat_activity.UNAVAILABLE,
                                         desktop_chat_activity.OFF])
def test_a_reading_that_is_not_available_says_no_word(monkeypatch, tmp_path,
                                                      daemon_says):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE))
    _reading(tmp_path, availability=daemon_says, busy=True)
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)"]


@pytest.mark.parametrize("status_says, word", [
    (desktop_chat_activity.UNAVAILABLE,
     "not available on this Desktop version"),
    (desktop_chat_activity.OFF, "turned off"),
    (None, "unknown"),
])
def test_status_not_available_itself_says_no_word(monkeypatch, tmp_path,
                                                  status_says, word):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(status_says))
    _reading(tmp_path, busy=True)
    assert cli._desktop_chat_lines(_desk()) == [f"Desktop chat light: {word}"]


@pytest.mark.parametrize("content", [
    "{ not json",
    "[]",
    {"availability": "available", "busy": "yes", "t": 0,
     "pid": os.getpid()},
    {"availability": "available", "busy": 1, "t": time.time(),
     "pid": os.getpid()},
    {"availability": "lit", "busy": True, "t": time.time(),
     "pid": os.getpid()},
    {"availability": "available", "busy": True, "pid": os.getpid()},
    # Fresh and well formed but for the pid: missing, or not a process.
    {"availability": "available", "busy": True, "t": time.time()},
    {"availability": "available", "busy": True, "t": time.time(),
     "pid": "me"},
    {"availability": "available", "busy": True, "t": time.time(),
     "pid": 0},
])
def test_a_malformed_reading_says_no_word_and_does_not_raise(
        monkeypatch, capsys, tmp_path, content):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE))
    fx.write_reading_file(tmp_path, cli.blink_home(), content)
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)"]
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat: " not in out


def test_a_reading_that_raises_costs_only_its_word(monkeypatch, tmp_path):
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE))
    _reading(tmp_path, busy=True)

    def boom(*a, **k):
        raise RuntimeError("reading broke")
    monkeypatch.setattr(desktop_chat_activity, "read_reading", boom)
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)"]


def test_a_handed_in_watcher_is_not_overridden_by_a_reading(tmp_path):
    _reading(tmp_path, busy=True)
    watcher = _fake_watcher(desktop_chat_activity.AVAILABLE, False)()
    assert cli._desktop_chat_lines(_desk(), watcher=watcher) == [
        "Desktop chat light: available (best effort)",
        "Desktop chat: idle"]


def test_a_publishing_daemon_watcher_feeds_status_end_to_end(monkeypatch,
                                                            capsys,
                                                            tmp_path):
    """The daemon's watcher, as default_providers builds it (publish=True),
    over a synthetic layout; status called the way cmd_status calls it, with
    no watcher handed in, and its own one-look watcher over the same
    layout. Clocks are in the recent past so the reading is fresh by the
    wall clock status reads it with."""
    layout = fx.make_layout(tmp_path / "desktop")
    home = cli.blink_home()
    os.makedirs(inside(tmp_path, home), exist_ok=True)
    daemon = _REAL(idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                   ls_dir=layout.ls_dir, platform="win32", blink_home=home,
                   publish=True)

    def factory(**kw):
        assert "publish" not in kw      # status's own never writes
        return _REAL(idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                     ls_dir=layout.ls_dir, platform="win32", **kw)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", factory)
    desk = _desk()

    t0 = time.time() - 60
    assert daemon.poll(t0) is False                 # the baseline: no file
    assert cli._desktop_chat_lines(desk) == [
        "Desktop chat light: available (best effort)"]

    fx.grow_log(layout.idb_dir, 128)
    assert daemon.poll(t0 + 2) is True
    assert cli._desktop_chat_lines(desk) == [
        "Desktop chat light: available (best effort)",
        "Desktop chat: busy"]
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0 and "Desktop chat: busy" in out

    # The 20 s Windows hold lapses with no write; the next poll says idle.
    hold = desktop_chat_activity.QUIET_HOLD_S["win32"]
    assert daemon.poll(t0 + 2 + hold + 2) is False
    assert cli._desktop_chat_lines(desk) == [
        "Desktop chat light: available (best effort)",
        "Desktop chat: idle"]

    # status's own watcher, polled for the availability row, wrote nothing:
    # the reading is still the daemon's, stamped by the daemon's clock.
    rec = desktop_chat_activity.read_reading(home)
    assert rec["t"] == t0 + 2 + hold + 2


def test_wire_builds_its_bus_without_publishing(monkeypatch):
    """`blink status --wire` builds the default providers for one poll. Its
    chat watcher must not overwrite the daemon's reading."""
    from pc import ingest
    seen = {}

    class Stop(Exception):
        pass

    def bus(**kw):
        seen.update(kw)
        raise Stop
    monkeypatch.setattr(ingest, "IngestionBus", bus)
    with pytest.raises(Stop):
        cli._wire_line(None, True)
    assert seen.get("publish_chat") is False


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


# -- uninstall ---------------------------------------------------------------

def test_uninstall_removes_the_reading_and_its_temp_and_only_those(tmp_path):
    """The daemon's reading is the app's own file, so uninstall takes it, and
    the pid-scoped temp a killed write left. Neighbours with similar names
    stay."""
    home = cli.blink_home()
    reading = fx.write_reading_file(
        tmp_path, home, {"availability": "available", "busy": True, "t": 1.0})
    tmp = reading + ".4242.tmp"
    keep = [reading + "x.tmp", reading + ".notours"]
    for p in [tmp] + keep:
        with open(inside(tmp_path, p), "w", encoding="utf-8") as fh:
            fh.write("{}")
    cli.main(["uninstall"])
    assert not os.path.exists(reading)
    assert not os.path.exists(tmp)
    for p in keep:
        assert os.path.exists(p), p


def test_status_ignores_a_reading_older_than_the_max_age(monkeypatch, capsys,
                                                         tmp_path):
    """A daemon that died mid-busy: its last word stops being quoted once it
    is READING_MAX_AGE_S old, though the file is still there."""
    fx.write_reading_file(tmp_path, cli.blink_home(), {
        "availability": "available", "busy": True,
        "t": time.time() - desktop_chat_activity.READING_MAX_AGE_S - 5,
        "pid": os.getpid()})
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE))
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: available (best effort)" in out
    assert "Desktop chat:" not in out


def test_status_ignores_a_fresh_reading_whose_daemon_is_gone(monkeypatch,
                                                              capsys,
                                                              tmp_path):
    """A daemon that died mid-busy a few seconds ago, with none started in
    its place: on macOS and Linux its pid is asked about and the reading is
    refused at once, not after READING_MAX_AGE_S. The liveness answer is
    faked; no process is signalled."""
    _reading(tmp_path, busy=True, age=3.0, pid=4242)
    monkeypatch.setattr(desktop_chat_activity, "_writer_gone",
                        lambda pid: pid == 4242)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        _fake_watcher(desktop_chat_activity.AVAILABLE))
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: available (best effort)" in out
    assert "Desktop chat:" not in out


def test_a_restarted_daemon_never_shows_the_old_ones_busy(monkeypatch,
                                                          capsys, tmp_path):
    """The review finding of 2026-11-25. An old daemon's watcher writes
    "busy"; a new one starts. Its first poll is the baseline, so it writes
    no verdict, but status must not print the old "busy" meanwhile: the
    old reading is removed. After its second poll status reads the new
    daemon's own word. Both stand in this test process, so the pid check
    passes for each and only the removal is under test."""
    layout = fx.make_layout(tmp_path / "desktop")
    home = cli.blink_home()
    os.makedirs(inside(tmp_path, home), exist_ok=True)

    def daemon():
        return _REAL(idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                     ls_dir=layout.ls_dir, platform="win32",
                     blink_home=home, publish=True)

    def factory(**kw):
        assert "publish" not in kw
        return _REAL(idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                     ls_dir=layout.ls_dir, platform="win32", **kw)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", factory)
    desk = _desk()

    t0 = time.time() - 30
    old = daemon()
    old.poll(t0)
    fx.grow_log(layout.idb_dir, 128)
    assert old.poll(t0 + 2) is True
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0 and "Desktop chat: busy" in out

    new = daemon()                      # the restart
    assert new.poll(t0 + 6) is False
    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: available (best effort)" in out
    assert "Desktop chat: busy" not in out
    assert "Desktop chat: " not in out
    assert cli._desktop_chat_lines(desk) == [
        "Desktop chat light: available (best effort)"]

    assert new.poll(t0 + 8) is False
    assert cli._desktop_chat_lines(desk) == [
        "Desktop chat light: available (best effort)",
        "Desktop chat: idle"]
    assert desktop_chat_activity.read_reading(home)["t"] == t0 + 8


def test_on_windows_a_dead_daemons_busy_is_not_shown_before_the_new_poll(
        monkeypatch, capsys, tmp_path):
    """The reviewer's finding against the round before: on Windows the pid
    was not asked about, so after a restart and before the new watcher's
    first poll status printed the old daemon's "busy", believed on its age
    alone. Simulated Windows here: no POSIX liveness, os.kill a tripwire,
    and a fake kernel32 that knows this process and no other, so the old
    daemon's pid (4242) is no such process. The old reading is fresh and
    still on disk, the new daemon's watcher is built and not yet polled, and
    status shows no busy/idle line at all."""
    from pc.providers import claude_state
    layout = fx.make_layout(tmp_path / "desktop")
    home = cli.blink_home()
    os.makedirs(inside(tmp_path, home), exist_ok=True)
    # The old daemon's last word: "busy", three seconds ago, its pid gone.
    reading = _reading(tmp_path, busy=True, age=3.0, pid=4242)

    def kill(*a, **k):
        raise AssertionError("os.kill called where it would terminate")
    k32 = fx.FakeKernel32(
        procs={os.getpid(): desktop_chat_activity._STILL_ACTIVE})
    monkeypatch.setattr(claude_state, "_PID_LIVENESS_AVAILABLE", False)
    monkeypatch.setattr(claude_state.os, "kill", kill)
    monkeypatch.setattr(desktop_chat_activity, "_load_kernel32", k32.load)

    def factory(**kw):
        assert "publish" not in kw
        return _REAL(idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                     ls_dir=layout.ls_dir, platform="win32", **kw)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity", factory)
    # The restart: the new daemon's watcher exists and has not polled.
    _REAL(idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
          ls_dir=layout.ls_dir, platform="win32", blink_home=home,
          publish=True)
    assert os.path.exists(reading)

    rc, out = _status_with_desktop(monkeypatch, capsys)
    assert rc == 0
    assert "Desktop chat light: available (best effort)" in out
    assert "Desktop chat: busy" not in out
    assert "Desktop chat: " not in out
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)"]
    assert 4242 in [c[3] for c in k32.calls if c[0] == "OpenProcess"]
    assert k32.open_handles == set()


def test_on_windows_a_live_daemons_busy_is_still_shown(
        monkeypatch, capsys, tmp_path):
    """The fix must not silence the working case: on simulated Windows a
    fresh "busy" whose writer kernel32 says is still running is printed,
    os.kill is never reached, and the handle is closed."""
    from pc.providers import claude_state
    layout = fx.make_layout(tmp_path / "desktop")
    _reading(tmp_path, busy=True, age=3.0, pid=4343)

    def kill(*a, **k):
        raise AssertionError("os.kill called where it would terminate")
    k32 = fx.FakeKernel32(procs={4343: desktop_chat_activity._STILL_ACTIVE})
    monkeypatch.setattr(claude_state, "_PID_LIVENESS_AVAILABLE", False)
    monkeypatch.setattr(claude_state.os, "kill", kill)
    monkeypatch.setattr(desktop_chat_activity, "_load_kernel32", k32.load)
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        lambda **kw: _REAL(
                            idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                            ls_dir=layout.ls_dir, platform="win32", **kw))
    assert cli._desktop_chat_lines(_desk()) == [
        "Desktop chat light: available (best effort)",
        "Desktop chat: busy"]
    assert 4343 in [c[3] for c in k32.calls if c[0] == "OpenProcess"]
    assert k32.open_handles == set()


def test_status_one_shot_look_leaves_the_daemons_busy(monkeypatch, tmp_path):
    """status's own watcher polls once and must not remove the reading a
    running daemon wrote: the next status still reads "busy"."""
    layout = fx.make_layout(tmp_path / "desktop")
    monkeypatch.setattr(desktop_chat_activity, "DesktopChatActivity",
                        lambda **kw: _REAL(
                            idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
                            ls_dir=layout.ls_dir, platform="win32", **kw))
    _reading(tmp_path, busy=True)
    for _ in range(2):
        assert cli._desktop_chat_lines(_desk()) == [
            "Desktop chat light: available (best effort)",
            "Desktop chat: busy"]
