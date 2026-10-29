"""Claude Desktop's data folder, including the Store build's relocated one.

The layout under test is the one measured on a real Store install (see the
module docstring): %APPDATA%\\Claude holding only a `.user-data-left.<name>`
marker, and the data under %LOCALAPPDATA%\\Claude-Data\\<name>. Built here
from nothing -- no file is copied from a real machine.
"""
import os
import sys

import pytest

from pc import cowork_audit, desktop_idb, desktop_local_storage, desktop_paths
from pc.providers import claude_desktop

NAME = "claude_pzs8sxrjxfjjc"


@pytest.fixture
def windows(monkeypatch, tmp_path):
    """Windows path logic on any host. APPDATA and LOCALAPPDATA are already
    pointed under tmp_path by conftest."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")
    roaming = tmp_path / "AppData" / "Roaming" / "Claude"
    local = tmp_path / "AppData" / "Local"
    return roaming, local


def _store_build(roaming, local, marker_in=None):
    (marker_in or roaming).mkdir(parents=True, exist_ok=True)
    (marker_in or roaming).joinpath(".user-data-left." + NAME).write_bytes(b"")
    target = local / "Claude-Data" / NAME
    target.mkdir(parents=True)
    return target


def test_a_plain_install_reads_roaming(windows):
    roaming, _ = windows
    roaming.mkdir(parents=True)
    assert desktop_paths.data_dir() == str(roaming)


def test_no_install_at_all_still_answers_roaming(windows):
    roaming, _ = windows
    assert desktop_paths.data_dir() == str(roaming)


def test_the_store_build_follows_its_marker(windows):
    roaming, local = windows
    target = _store_build(roaming, local)
    assert desktop_paths.data_dir() == str(target)


def test_a_marker_in_the_package_local_cache_is_followed(windows):
    roaming, local = windows
    cache = (local / "Packages" / ("Claude_" + NAME.split("_")[1])
             / "LocalCache" / "Roaming" / "Claude")
    target = _store_build(roaming, local, marker_in=cache)
    assert desktop_paths.data_dir() == str(target)


def test_a_marker_whose_target_is_missing_falls_back(windows):
    roaming, _ = windows
    roaming.mkdir(parents=True)
    (roaming / (".user-data-left." + NAME)).write_bytes(b"")
    assert desktop_paths.data_dir() == str(roaming)


def test_a_leftover_claude_data_without_a_marker_is_ignored(windows):
    roaming, local = windows
    roaming.mkdir(parents=True)
    (local / "Claude-Data" / NAME).mkdir(parents=True)
    assert desktop_paths.data_dir() == str(roaming)


@pytest.mark.parametrize("bad", ["..", "."])
def test_a_marker_cannot_step_out_of_claude_data(windows, bad):
    roaming, local = windows
    roaming.mkdir(parents=True)
    (roaming / (".user-data-left." + bad)).write_bytes(b"")
    (local / "Claude-Data").mkdir(parents=True)
    assert desktop_paths.data_dir() == str(roaming)


def test_every_desktop_reader_uses_the_relocated_folder(windows):
    roaming, local = windows
    target = str(_store_build(roaming, local))
    assert claude_desktop.cache_path() == os.path.join(
        target, "plan-usage-history.json")
    assert desktop_local_storage.store_path() == os.path.join(
        target, "Local Storage", "leveldb")
    assert desktop_idb.store_path().startswith(target + os.sep)
    assert cowork_audit.sessions_dir() == os.path.join(
        target, "local-agent-mode-sessions")


def test_macos_is_unchanged(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert desktop_paths.data_dir() == os.path.expanduser(
        "~/Library/Application Support/Claude")
