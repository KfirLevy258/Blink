"""The Claude Desktop chat as a running session in the activity provider.

pc/providers/claude_state.ClaudeStateProvider takes `chat`, anything with
poll(now) -> bool, and a busy chat counts as one more running session named
"Desktop chat". It never claims idle, waiting, failed or agents: the end of a
reply is seen only 20-50 s late, too uncertain to say "your turn". These
tests hand it fake chats and synthetic hook slots under tmp_path; nothing
here reads a real Desktop folder.
"""
import json
import os
import time

import pytest

from pc import desktop_chat_activity, ingest
from pc.providers import base, claude_state
from pc.providers.claude_state import CHAT_LABEL, ClaudeStateProvider
from tests.support import desktop_chat_fixtures
from tests.support.home import inside

NOW = 1_800_000_000.0


class FakeChat:
    def __init__(self, busy):
        self.busy = busy
        self.polled = []

    def poll(self, now):
        self.polled.append(now)
        return self.busy


class RaisingChat:
    def poll(self, now):
        raise RuntimeError("the watcher broke")


def _slot(home, state_dir, sid, event, t=NOW, name=""):
    """One synthetic hook slot, written under `home` only."""
    os.makedirs(inside(home, state_dir), exist_ok=True)
    payload = {"event": event, "t": t}
    if name:
        payload["name"] = name
    path = inside(home, os.path.join(state_dir, sid + ".state"))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def _provider(tmp_path, chat):
    return ClaudeStateProvider(path=str(tmp_path / "state"), sweep=False,
                               chat=chat)


def test_a_busy_chat_is_exactly_one_running_session(tmp_path):
    counts, names, agents = _provider(tmp_path, FakeChat(True)).scan(NOW)
    assert counts == {base.STATE_RUNNING: 1}
    assert names == {base.STATE_RUNNING: [CHAT_LABEL]}
    assert agents == 0


def test_the_chat_is_polled_with_the_scan_clock(tmp_path):
    chat = FakeChat(False)
    _provider(tmp_path, chat).scan(NOW)
    assert chat.polled == [NOW]


def test_a_lone_busy_chat_gives_a_running_frame_named_desktop_chat(tmp_path):
    frames = _provider(tmp_path, FakeChat(True)).poll(NOW)
    assert len(frames) == 1
    frame = frames[0]
    assert frame.state == base.STATE_RUNNING
    assert frame.n_run == 1
    assert frame.label == "Desktop chat"
    assert frame.n_idle == 0 and frame.n_wait == 0 and frame.n_stuck == 0
    assert frame.n_agents == 0


def test_chat_and_one_running_hook_session_count_two_with_no_label(tmp_path):
    _slot(tmp_path, tmp_path / "state", "s1", "PreToolUse", name="proj")
    frames = _provider(tmp_path, FakeChat(True)).poll(NOW)
    assert len(frames) == 1
    assert frames[0].state == base.STATE_RUNNING
    assert frames[0].n_run == 2
    assert frames[0].label == ""


def test_a_waiting_hook_session_still_wins_over_a_busy_chat(tmp_path):
    _slot(tmp_path, tmp_path / "state", "s1", "Notification", name="proj")
    frames = _provider(tmp_path, FakeChat(True)).poll(NOW)
    assert frames[0].state == base.STATE_WAITING
    assert frames[0].n_wait == 1
    assert frames[0].n_run == 1


def test_a_chat_that_raises_leaves_the_hooks_reading_intact(tmp_path):
    _slot(tmp_path, tmp_path / "state", "s1", "Stop", name="proj")
    with_raise = _provider(tmp_path, RaisingChat()).poll(NOW)
    without = ClaudeStateProvider(path=str(tmp_path / "state"),
                                  sweep=False).poll(NOW)
    assert with_raise == without
    assert with_raise[0].state == base.STATE_IDLE
    assert with_raise[0].n_run == 0


def test_a_lone_chat_that_raises_gives_no_frame(tmp_path):
    assert _provider(tmp_path, RaisingChat()).poll(NOW) == []


def test_an_idle_chat_adds_nothing(tmp_path):
    p = _provider(tmp_path, FakeChat(False))
    assert p.scan(NOW) == ({}, {}, 0)
    assert p.poll(NOW) == []


def test_an_idle_chat_leaves_a_hook_reading_alone(tmp_path):
    _slot(tmp_path, tmp_path / "state", "s1", "Stop", name="proj")
    counts, names, _ = _provider(tmp_path, FakeChat(False)).scan(NOW)
    assert counts == {base.STATE_IDLE: 1}
    assert names == {base.STATE_IDLE: ["proj"]}


def test_the_chat_never_claims_idle_waiting_failed_or_agents(tmp_path):
    counts, _, agents = _provider(tmp_path, FakeChat(True)).scan(NOW)
    for state in (base.STATE_IDLE, base.STATE_WAITING, base.STATE_FAILED,
                  base.STATE_STUCK, base.STATE_UNKNOWN):
        assert state not in counts
    assert agents == 0


def test_session_states_excludes_the_chat(tmp_path):
    _slot(tmp_path, tmp_path / "state", "s1", "PreToolUse", name="proj")
    chat = FakeChat(True)
    states, agents = _provider(tmp_path, chat).session_states(NOW)
    assert states == {"s1": (base.STATE_RUNNING, "proj")}
    assert agents == 0
    # Not even asked: the Codex union and the hooks census read this view.
    assert chat.polled == []


def test_no_chat_is_the_hooks_alone(tmp_path):
    p = ClaudeStateProvider(path=str(tmp_path / "state"), sweep=False)
    assert p.scan(NOW) == ({}, {}, 0)


def test_default_providers_give_the_activity_provider_a_chat_watcher():
    providers = ingest.default_providers(sweep=False)
    states = [p for p in providers if isinstance(p, ClaudeStateProvider)]
    assert len(states) == 1
    chat = states[0]._chat
    assert isinstance(chat, desktop_chat_activity.DesktopChatActivity)
    # The sandbox's home, resolved at the call, never the real one.
    assert chat._blink_home == os.path.join(os.path.expanduser("~"),
                                            ".blink")


def _chat_of(providers):
    return next(p for p in providers
                if isinstance(p, ClaudeStateProvider))._chat


def test_the_daemons_watcher_publishes_and_a_one_shot_one_does_not():
    # The default is the daemon's: the bridge builds its bus with it.
    assert _chat_of(ingest.default_providers(sweep=False))._publish_on is True
    assert _chat_of(ingest.default_providers(
        sweep=False, publish_chat=False))._publish_on is False
    # IngestionBus passes it through when it builds its own providers.
    assert _chat_of(ingest.IngestionBus(
        now=lambda: NOW)._providers)._publish_on is True
    assert _chat_of(ingest.IngestionBus(
        now=lambda: NOW, publish_chat=False)._providers)._publish_on is False


def test_a_one_shot_poll_of_the_default_providers_writes_no_reading(
        tmp_path):
    """Even publishing, a watcher's first poll is a baseline, and a one-shot
    caller must not overwrite the daemon's reading with its False."""
    home = os.path.join(os.path.expanduser("~"), ".blink")
    os.makedirs(inside(tmp_path, home), exist_ok=True)
    bus = ingest.IngestionBus(now=lambda: NOW)
    bus.poll_frames()
    assert not os.path.exists(os.path.join(
        home, desktop_chat_activity.READING_FILE))


def test_the_daemons_scan_publishes_the_reading(tmp_path):
    layout = desktop_chat_fixtures.make_layout(tmp_path / "desktop")
    home = str(tmp_path / ".blink")
    os.makedirs(inside(tmp_path, home), exist_ok=True)
    watcher = desktop_chat_activity.DesktopChatActivity(
        idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
        ls_dir=layout.ls_dir, platform="win32", blink_home=home,
        publish=True)
    p = _provider(tmp_path, watcher)
    counts, _, _ = _reply(layout, p)
    assert counts == {base.STATE_RUNNING: 1}
    assert desktop_chat_activity.read_reading(home, now=NOW + 2) == {
        "availability": desktop_chat_activity.AVAILABLE, "busy": True,
        "t": NOW + 2}


def test_the_default_watcher_finds_no_desktop_in_the_sandbox():
    providers = ingest.default_providers(sweep=False)
    state = next(p for p in providers if isinstance(p, ClaudeStateProvider))
    assert state._chat.poll(time.time()) is False
    assert state._chat.availability() == desktop_chat_activity.UNAVAILABLE


def test_claude_state_still_names_the_label_constant():
    assert claude_state.CHAT_LABEL == "Desktop chat"


# -- the real watcher over synthetic Desktop folders -------------------------
#
# Platform "win32" makes any log growth after the baseline count as activity,
# so a grown log is the whole of a "reply" here. Everything lives under
# tmp_path; blink_home is the sandboxed home's .blink.

def _watcher(layout, tmp_path):
    return desktop_chat_activity.DesktopChatActivity(
        idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
        ls_dir=layout.ls_dir, platform="win32",
        blink_home=str(tmp_path / ".blink"))


def _reply(layout, provider, t0=NOW):
    """Baseline poll, one write, then the poll that sees it."""
    provider.scan(t0)
    desktop_chat_fixtures.grow_log(layout.idb_dir, 10)
    return provider.scan(t0 + 2)


def _write_config(tmp_path, doc):
    home = str(tmp_path / ".blink")
    os.makedirs(inside(tmp_path, home), exist_ok=True)
    with open(inside(tmp_path, os.path.join(home, "config.json")), "w",
              encoding="utf-8") as f:
        json.dump(doc, f)


def test_a_real_watcher_over_a_growing_log_lights_the_dot(tmp_path):
    layout = desktop_chat_fixtures.make_layout(tmp_path / "desktop")
    p = _provider(tmp_path, _watcher(layout, tmp_path))
    counts, names, _ = _reply(layout, p)
    assert counts == {base.STATE_RUNNING: 1}
    assert names == {base.STATE_RUNNING: [CHAT_LABEL]}


def test_the_setting_off_counts_nothing_even_with_active_folders(tmp_path):
    layout = desktop_chat_fixtures.make_layout(tmp_path / "desktop")
    _write_config(tmp_path, {desktop_chat_activity.SETTING: False})
    watcher = _watcher(layout, tmp_path)
    p = _provider(tmp_path, watcher)
    assert _reply(layout, p) == ({}, {}, 0)
    assert p.poll(NOW + 4) == []
    assert watcher.availability() == desktop_chat_activity.OFF


def test_the_setting_off_leaves_hook_sessions_alone(tmp_path):
    layout = desktop_chat_fixtures.make_layout(tmp_path / "desktop")
    _write_config(tmp_path, {desktop_chat_activity.SETTING: False})
    _slot(tmp_path, tmp_path / "state", "s1", "PreToolUse", name="proj")
    p = _provider(tmp_path, _watcher(layout, tmp_path))
    counts, names, _ = _reply(layout, p)
    assert counts == {base.STATE_RUNNING: 1}
    assert names == {base.STATE_RUNNING: ["proj"]}


def _broken_layouts(tmp_path):
    """name -> folders for a watcher over a layout that is missing,
    unreadable or not what this code expects."""
    root = tmp_path / "broken"
    root.mkdir()
    a_file = root / "a-file"
    a_file.write_text("not a folder", encoding="utf-8")
    empty = root / "empty"
    empty.mkdir()
    ok = desktop_chat_fixtures.make_layout(tmp_path / "ok")
    # A "log" that is a folder: named as a log, nothing a leveldb writes.
    # No real log sits beside it, so nothing in that folder is a log.
    odd = tmp_path / "odd"
    (odd / "idb" / "000003.log").mkdir(parents=True)
    none = str(root / "none")
    return {
        "missing": (none, none, none),
        "a file instead of a folder": (str(a_file),) * 3,
        "no log in it": (str(empty),) * 3,
        "a log that is a folder": (str(odd / "idb"), ok.blob_dir, ok.ls_dir),
    }


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_broken_layouts_never_stop_the_hook_state_flowing(tmp_path, platform):
    _slot(tmp_path, tmp_path / "state", "s1", "Stop", name="proj")
    for name, (idb, blob, ls) in _broken_layouts(tmp_path).items():
        watcher = desktop_chat_activity.DesktopChatActivity(
            idb_dir=idb, blob_dir=blob, ls_dir=ls, platform=platform,
            blink_home=str(tmp_path / ".blink"))
        bus = ingest.IngestionBus(providers=[_provider(tmp_path, watcher)],
                                  now=lambda: NOW)
        for _ in range(2):
            frames = bus.poll_frames()
        state = [f for f in frames if f.provider == claude_state.PROVIDER_ID]
        assert len(state) == 1, name
        assert state[0].state == base.STATE_IDLE, name
        assert state[0].n_run == 0, name
        assert state[0].label == "proj", name
        # Every one of these is a layout the watcher cannot read, so `blink
        # status` must say so. A "log" that is a folder used to pass the
        # name check and read as available; only a regular file is a log.
        assert (watcher.availability()
                == desktop_chat_activity.UNAVAILABLE), name


@pytest.mark.parametrize("platform", ["win32", "darwin"])
def test_blink_status_over_a_directory_named_log_says_not_available(
        tmp_path, monkeypatch, platform):
    """`blink status` builds its watcher from blink_home alone, so the folders
    come from desktop_paths here; the real class runs over a layout whose only
    "log" is a folder, and the row must say the layout is not recognised."""
    import argparse
    from pc import cli
    ok = desktop_chat_fixtures.make_layout(tmp_path / "ok")
    odd = tmp_path / "odd" / "idb"
    (odd / "000003.log").mkdir(parents=True)
    monkeypatch.setattr(desktop_chat_activity.sys, "platform", platform)
    monkeypatch.setattr(desktop_chat_activity.desktop_paths, "indexeddb_dir",
                        lambda: str(odd))
    monkeypatch.setattr(desktop_chat_activity.desktop_paths,
                        "indexeddb_blob_dir", lambda: ok.blob_dir)
    monkeypatch.setattr(desktop_chat_activity.desktop_paths,
                        "local_storage_dir", lambda: ok.ls_dir)
    desk = cli.read_desk()._replace(desktop_installed=True)
    assert cli._desktop_chat_lines(desk) == [
        "Desktop chat light: not available on this Desktop version"]


def test_a_refused_blob_subfolder_leaves_the_hooks_alone(tmp_path,
                                                         monkeypatch):
    layout = desktop_chat_fixtures.make_layout(tmp_path / "desktop")
    _slot(tmp_path, tmp_path / "state", "s1", "PreToolUse", name="proj")
    watcher = desktop_chat_activity.DesktopChatActivity(
        idb_dir=layout.idb_dir, blob_dir=layout.blob_dir,
        ls_dir=layout.ls_dir, platform="darwin",
        blink_home=str(tmp_path / ".blink"))
    desktop_chat_fixtures.refuse_scandir(
        monkeypatch, desktop_chat_activity,
        os.path.join(layout.blob_dir, "1"))
    p = _provider(tmp_path, watcher)
    for i in range(3):
        counts, names, _ = p.scan(NOW + 2 * i)
        assert counts == {base.STATE_RUNNING: 1}
        assert names == {base.STATE_RUNNING: ["proj"]}
    assert watcher.availability() == desktop_chat_activity.UNAVAILABLE


def test_a_queue_that_is_not_json_leaves_the_hooks_alone(tmp_path):
    layout = desktop_chat_fixtures.make_layout(tmp_path / "desktop")
    desktop_chat_fixtures.write_queue(layout.ls_dir, "{{ not json")
    _slot(tmp_path, tmp_path / "state", "s1", "Stop", name="proj")
    p = _provider(tmp_path, _watcher(layout, tmp_path))
    counts, _, _ = p.scan(NOW)
    assert counts == {base.STATE_IDLE: 1}


def test_a_missing_desktop_with_no_hooks_gives_no_frame_and_no_error(
        tmp_path):
    watcher = desktop_chat_activity.DesktopChatActivity(
        idb_dir=str(tmp_path / "x"), blob_dir=str(tmp_path / "y"),
        ls_dir=str(tmp_path / "z"), blink_home=str(tmp_path / ".blink"))
    bus = ingest.IngestionBus(providers=[_provider(tmp_path, watcher)],
                              now=lambda: NOW)
    assert bus.poll_frames() == []


def test_the_default_bus_polls_clean_in_the_sandbox():
    bus = ingest.IngestionBus(now=lambda: NOW)
    bus.poll_frames()
