"""pc/desktop_paths: where Claude Desktop's data folder is, Store build or not.

Every layout here is built under tmp_path by the code below. None of it is
captured from a real machine: the real folders sit beside the owner's account
and their conversations. The Windows layouts follow the one Mor Gal
(Mr-M33533K5/Blink) measured on Windows 11 with Desktop 3.6556.2.0 from the
Store, 2026-10-29 -- see the module docstring.

Windows is simulated by setting sys.platform and os.name, the same two things
the readers test. os.path stays posixpath on a Mac or Linux host, so the
joins come out with "/", which is fine: what is under test is which folder
is chosen, not how its name is spelled. The one place spelling matters, the
drive reset in a name like "D:evil", is tested against ntpath directly.
APPDATA and LOCALAPPDATA are already under tmp_path (tests/conftest.py).
"""
import ntpath
import os
import posixpath
import sys

import pytest

from pc import cli, cowork_audit, desktop_idb, desktop_local_storage
from pc import desktop_paths as dp
from pc.providers import claude_desktop
from pc.providers.claude_desktop_ls import ClaudeDesktopLocalStorageProvider
from pc.providers.weekly_anchor import DesktopHistoryProvider
from tests.support import desktop_cache as _dc
from tests.support.home import inside

NAME = "claude_pzs8sxrjxfjjc"   # the shape measured; any safe name will do
OTHER = "claude_aaaaaaaaaaaaa"  # sorts before NAME


@pytest.fixture
def win(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(os, "name", "nt")


# --- layout builders. Each takes the caller's home and refuses to write
# outside it (tests/support/home.py).

def _roaming(home):
    return inside(home, os.path.join(os.environ["APPDATA"], "Claude"))


def _local(home):
    return inside(home, os.environ["LOCALAPPDATA"])


def _target(home, name=NAME):
    return os.path.join(_local(home), "Claude-Data", name)


def _package_roaming(home):
    return os.path.join(_local(home), "Packages", "Claude_pzs8sxrjxfjjc",
                        "LocalCache", "Roaming", "Claude")


def _touch(home, path, mtime=None):
    path = inside(home, path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _marker(home, folder, name=NAME, prefix=dp.MARKER_PREFIX):
    return _touch(home, os.path.join(folder, prefix + name))


def _cache(home, folder, mtime=None, content=None):
    path = inside(home, os.path.join(folder, "plan-usage-history.json"))
    os.makedirs(folder, exist_ok=True)
    if content is None:
        content = _dc.doc([_dc.sample(_dc.T_MS)])
    with open(path, "wb") as f:
        f.write(_dc.as_bytes(content))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def _mkdir(home, path):
    os.makedirs(inside(home, path), exist_ok=True)
    return path


def _store_layout(home, name=NAME):
    """What the Store build left on the measured machine: a marker in
    Roaming and the data under Claude-Data."""
    _marker(home, _roaming(home), name)
    target = _target(home, name)
    _cache(home, target)
    return target


# --- 1, 2: plain installs ------------------------------------------------

def test_plain_install_reads_roaming(win, tmp_path):
    _cache(tmp_path, _roaming(tmp_path))
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")
    assert dp.data_dir_source() == "roaming"


def test_nothing_installed_still_names_roaming(win, tmp_path):
    path, source = dp.windows_data_dir()
    assert (path, source) == (_roaming(tmp_path), "roaming")
    assert not os.path.exists(path)
    assert not dp.marker_without_data()


# --- 3 to 6: following the marker -------------------------------------------

def test_roaming_marker_is_followed(win, tmp_path):
    target = _store_layout(tmp_path)
    assert dp.windows_data_dir() == (target, "marker:" + NAME)
    assert dp.data_dir() == target


def test_marker_in_the_package_roaming_is_followed(win, tmp_path):
    _marker(tmp_path, _package_roaming(tmp_path))
    target = _target(tmp_path)
    _cache(tmp_path, target)
    assert dp.windows_data_dir() == (target, "marker:" + NAME)


def test_marker_whose_target_is_missing_falls_back(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path))
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")
    assert dp.marker_without_data()


def test_claude_data_without_a_marker_is_ignored(win, tmp_path):
    """A leftover Claude-Data must not stop us reading a live Roaming."""
    _cache(tmp_path, _target(tmp_path))
    _cache(tmp_path, _roaming(tmp_path))
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")
    assert not dp.marker_without_data()


# --- 7, 8: names --------------------------------------------------------------

BAD_NAMES = [".", "..", "a\\b", "a/b", "D:evil", "", "name.", "name ",
             "nul\0byte", " lead"]


@pytest.mark.parametrize("name", BAD_NAMES)
def test_unsafe_names_are_refused_under_ntpath(name):
    root = r"C:\Users\x\AppData\Local\Claude-Data"
    assert not dp._safe_component(name, root, ntpath)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_unsafe_names_are_refused_under_posixpath(name):
    assert not dp._safe_component(name, "/x/Claude-Data", posixpath)


def test_the_drive_reset_is_real():
    """Why `:` is refused: ntpath throws the root away. If this ever stops
    being true the check is still right, just no longer load-bearing."""
    assert ntpath.join(r"C:\Local", "Claude-Data", "D:evil") == "D:evil"


@pytest.mark.parametrize("name", [NAME, "Claude", "a.b", "a b"])
def test_plain_names_pass(name):
    assert dp._safe_component(name, r"C:\L\Claude-Data", ntpath)
    assert dp._safe_component(name, "/l/Claude-Data", posixpath)


def _creatable(name):
    """Bad names this host's filesystem will let a fixture create. A real
    Windows refuses `:` and splits on `\\`, and trims a trailing dot or
    space itself -- which is the case this guards against."""
    if "/" in name or "\0" in name:
        return False
    if os.name == "nt":
        return not any(c in name for c in ":\\") and name == name.rstrip(" .")
    return True


@pytest.mark.parametrize("name", [n for n in BAD_NAMES if _creatable(n)])
def test_unsafe_marker_names_are_never_followed(win, tmp_path, name):
    """Even with a folder there for the bad name to reach -- Claude-Data
    itself for "" and ".", its parent for "..", a sibling for the rest."""
    _marker(tmp_path, _roaming(tmp_path), name)
    _cache(tmp_path, os.path.join(_local(tmp_path), "Claude-Data"))
    _cache(tmp_path, os.path.join(_local(tmp_path), "Claude-Data",
                                  name.rstrip(" .") or "x"))
    _cache(tmp_path, _roaming(tmp_path), mtime=1)
    assert dp.marker_names(_roaming(tmp_path)) == []
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def test_upper_case_marker_is_followed(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path), prefix=".USER-DATA-LEFT.")
    target = _target(tmp_path)
    _cache(tmp_path, target)
    assert dp.windows_data_dir() == (target, "marker:" + NAME)


# --- 9, 10: more than one marker --------------------------------------------

@pytest.mark.parametrize("fresh", [NAME, OTHER])
def test_the_fresher_of_two_targets_wins(win, tmp_path, fresh):
    for name in (NAME, OTHER):
        _marker(tmp_path, _roaming(tmp_path), name)
        _cache(tmp_path, _target(tmp_path, name),
               mtime=2_000_000 if name == fresh else 1_000_000)
    assert dp.windows_data_dir() == (_target(tmp_path, fresh),
                                     "marker:" + fresh)


def test_equal_freshness_picks_the_same_one_every_time(win, tmp_path):
    for name in (NAME, OTHER):
        _marker(tmp_path, _roaming(tmp_path), name)
        _cache(tmp_path, _target(tmp_path, name), mtime=1_000_000)
    picks = {dp.windows_data_dir() for _ in range(5)}
    assert picks == {(_target(tmp_path, OTHER), "marker:" + OTHER)}


def test_a_marker_with_no_target_loses_to_one_with(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path), OTHER)
    _marker(tmp_path, _roaming(tmp_path), NAME)
    _cache(tmp_path, _target(tmp_path, NAME))
    assert dp.windows_data_dir() == (_target(tmp_path, NAME), "marker:" + NAME)
    assert not dp.marker_without_data()


def test_freshness_counts_local_storage_and_indexeddb(win, tmp_path):
    """A target with no usage cache yet but a live Local Storage still
    beats one whose files are all old."""
    for name in (NAME, OTHER):
        _marker(tmp_path, _roaming(tmp_path), name)
    _cache(tmp_path, _target(tmp_path, OTHER), mtime=1_000_000)
    ls = _mkdir(tmp_path, os.path.join(_target(tmp_path, NAME),
                                       "Local Storage", "leveldb"))
    os.utime(ls, (2_000_000, 2_000_000))
    assert dp.windows_data_dir()[0] == _target(tmp_path, NAME)


# --- 11, 12: a stale side ---------------------------------------------------

def test_stale_marker_loses_to_a_fresher_roaming(win, tmp_path):
    """Back on the exe build: the old marker and Claude-Data are left
    behind, and the app writes Roaming again."""
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")
    assert not dp.marker_without_data()


def test_target_wins_over_a_stale_roaming_copy(win, tmp_path):
    """The normal migration: the app moved its data and anything still in
    Roaming is from before."""
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _roaming(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _target(tmp_path), mtime=2_000_000)
    assert dp.windows_data_dir() == (_target(tmp_path), "marker:" + NAME)


def test_equal_freshness_keeps_the_target(win, tmp_path):
    """Strictly fresher, so a tie goes to the folder the app pointed at."""
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _roaming(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    assert dp.windows_data_dir()[0] == _target(tmp_path)


# --- 13: never raises -------------------------------------------------------

def test_a_failing_resolver_falls_back_to_roaming(win, tmp_path, monkeypatch):
    _store_layout(tmp_path)

    def boom(*a, **k):
        raise RuntimeError("not an OSError")
    monkeypatch.setattr(dp.os, "listdir", boom)
    assert dp.data_dir() == _roaming(tmp_path)
    assert dp.data_dir_source() == "roaming"
    assert not dp.marker_without_data()


def test_a_failing_glob_falls_back_to_roaming(win, tmp_path, monkeypatch):
    _store_layout(tmp_path)

    def boom(*a, **k):
        raise ValueError("anything")
    monkeypatch.setattr(dp.glob, "glob", boom)
    assert dp.data_dir() == _roaming(tmp_path)


# --- 14, 15: every reader moves with it ---------------------------------------

def test_every_reader_moves_to_the_target(win, tmp_path):
    target = _store_layout(tmp_path)
    paths = [claude_desktop.cache_path(),
             desktop_local_storage.store_path(),
             desktop_idb.store_path(),
             cowork_audit.sessions_dir(),
             claude_desktop.ClaudeDesktopProvider().path(),
             ClaudeDesktopLocalStorageProvider().path()]
    for p in paths:
        assert os.path.commonpath([p, target]) == target, p


def test_desktop_history_reads_the_target(win, tmp_path):
    target = _target(tmp_path)
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, target, content=_dc.doc(
        [_dc.sample(_dc.T_MS - 60_000, sd=20), _dc.sample(_dc.T_MS, sd=30)]))
    samples = DesktopHistoryProvider().samples(_dc.NOW)
    assert [s["u"]["sd"] for s in samples] == [20, 30]


def test_desktop_reading_finds_a_cache_in_the_target(win, tmp_path):
    import time
    target = _target(tmp_path)
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, target, content=_dc.doc(
        [_dc.sample(int(time.time() * 1000))]))
    assert not os.path.exists(os.path.join(_roaming(tmp_path),
                                           "plan-usage-history.json"))
    reading = cli.desktop_reading()
    assert reading is not None
    assert reading[0] == claude_desktop.VERDICT_FRAME


def test_desktop_app_installed_sees_local_storage_in_the_target(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path))
    assert not cli.desktop_app_installed()
    _mkdir(tmp_path, os.path.join(_target(tmp_path), "Local Storage",
                                  "leveldb"))
    assert cli.desktop_app_installed()


# --- 16: macOS and Linux unchanged --------------------------------------------

@pytest.mark.skipif(os.name == "nt", reason="posixpath spelling")
def test_macos_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "darwin")
    base = os.path.expanduser("~/Library/Application Support/Claude")
    assert dp.data_dir() == base
    assert dp.data_dir_source() == "default"
    assert not dp.marker_without_data()
    assert claude_desktop.cache_path() == os.path.expanduser(
        "~/Library/Application Support/Claude/plan-usage-history.json")
    assert desktop_local_storage.store_path() == os.path.expanduser(
        "~/Library/Application Support/Claude/Local Storage/leveldb")
    assert desktop_idb.store_path() == os.path.expanduser(
        "~/Library/Application Support/Claude/IndexedDB/"
        "https_claude.ai_0.indexeddb.leveldb")
    assert cowork_audit.sessions_dir() == os.path.expanduser(
        "~/Library/Application Support/Claude/local-agent-mode-sessions")


@pytest.mark.skipif(os.name == "nt", reason="posixpath spelling")
def test_linux_is_unchanged(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    assert dp.data_dir() == os.path.expanduser("~/.config/Claude")
    assert dp.data_dir_source() == "default"
    assert claude_desktop.cache_path() == os.path.expanduser(
        "~/.config/Claude/plan-usage-history.json")
    assert desktop_local_storage.store_path() == os.path.expanduser(
        "~/.config/Claude/Local Storage/leveldb")
    assert desktop_idb.store_path() == os.path.expanduser(
        "~/.config/Claude/IndexedDB/https_claude.ai_0.indexeddb.leveldb")
    assert cowork_audit.sessions_dir() == os.path.expanduser(
        "~/.config/Claude/local-agent-mode-sessions")


def test_a_marker_means_nothing_off_windows(monkeypatch, tmp_path):
    """Only Windows has a Store build. A stray marker-shaped file in the
    macOS folder must not send the readers anywhere."""
    monkeypatch.setattr(sys, "platform", "darwin")
    base = os.path.expanduser("~/Library/Application Support/Claude")
    _marker(tmp_path, base)
    _cache(tmp_path, os.path.join(base, "Claude-Data", NAME))
    assert dp.data_dir() == base
    assert not dp.marker_without_data()


# --- 17: blink status -------------------------------------------------------

def _desktop_row(capsys):
    cli._source_lines()
    lines = capsys.readouterr().out.splitlines()
    i = next(i for i, l in enumerate(lines) if l.startswith("Desktop     "))
    row = [lines[i]]
    for l in lines[i + 1:]:
        if not l.startswith("            "):
            break
        row.append(l)
    return row


def test_status_names_the_folder_the_store_build_moved_to(win, tmp_path,
                                                          capsys):
    target = _store_layout(tmp_path)
    row = _desktop_row(capsys)
    assert ("            data moved by the Store build: " + target) in row
    assert not any("marker found" in l for l in row)


def test_status_says_when_the_marker_points_at_nothing(win, tmp_path, capsys):
    _marker(tmp_path, _roaming(tmp_path))
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     no usage cache")
    assert ("            Store build marker found, its data folder is"
            " missing") in row
    assert not any("moved by the Store build" in l for l in row)


def test_status_says_nothing_extra_for_a_plain_install(win, tmp_path, capsys):
    _cache(tmp_path, _roaming(tmp_path))
    row = _desktop_row(capsys)
    assert not any("Store build" in l for l in row)
