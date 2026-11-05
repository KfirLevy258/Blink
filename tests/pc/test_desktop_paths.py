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
import time

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


def _ldb(home, folder, which="ls", mtime=None, name="000003.log"):
    """A FILE inside Local Storage\\leveldb ("ls") or the claude.ai IndexedDB
    leveldb ("idb"), with the mtime given. The evidence is files the app
    writes; a directory's mtime is never set or read by these tests."""
    leaf = dp.LOCAL_STORAGE_LEAF if which == "ls" else dp.INDEXEDDB_LEAF
    return _touch(home, os.path.join(folder, *leaf, name), mtime)


def _skeleton(home, folder):
    """The app's empty Local Storage and IndexedDB folders: nothing written
    yet, or everything deleted after a move."""
    _mkdir(home, os.path.join(folder, *dp.LOCAL_STORAGE_LEAF))
    _mkdir(home, os.path.join(folder, *dp.INDEXEDDB_LEAF))


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


@pytest.mark.parametrize("fresh", ["ls", "idb"])
def test_freshness_counts_local_storage_and_indexeddb(win, tmp_path, fresh):
    """Two targets with no usage cache yet: the one whose newest FILE is
    fresher wins, whichever of the two stores holds it."""
    for name in (NAME, OTHER):
        _marker(tmp_path, _roaming(tmp_path), name)
    stale = "idb" if fresh == "ls" else "ls"
    _ldb(tmp_path, _target(tmp_path, NAME), fresh, mtime=2_000_000)
    _ldb(tmp_path, _target(tmp_path, OTHER), stale, mtime=1_000_000)
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


def _desktop_row_of(desk, capsys):
    cli._source_lines(desk)
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
    assert "            Store build marker found but its folder is gone" in row
    assert not any("moved by the Store build" in l for l in row)


def test_status_says_nothing_extra_for_a_plain_install(win, tmp_path, capsys):
    _cache(tmp_path, _roaming(tmp_path))
    row = _desktop_row(capsys)
    assert not any("Store build" in l for l in row)


def test_status_says_when_a_fresher_roaming_beat_the_marker(win, tmp_path,
                                                             capsys):
    """Back on the exe build. Without the line this desk's report looks
    exactly like a plain install."""
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    row = _desktop_row(capsys)
    assert ("            other copy not read: " + _target(tmp_path)) in row
    assert not any("moved by" in l or "is gone" in l or "ignored" in l
                   for l in row)


def test_status_resolves_the_folder_once(win, tmp_path, capsys, monkeypatch):
    """Source and path from one answer: two resolves could straddle a
    rewrite and print "moved" beside the Roaming path."""
    target = _store_layout(tmp_path)
    calls = []
    real = dp._resolve_windows
    monkeypatch.setattr(dp, "_resolve_windows",
                        lambda: calls.append(1) or real())
    row = _desktop_row(capsys)
    assert ("            data moved by the Store build: " + target) in row
    assert len(calls) == 1


def test_status_names_the_folder_desk_read(win, tmp_path, capsys,
                                           monkeypatch):
    """With a desk, the row explains the folder read_desk's reading came
    from, even if the tree changes before the row prints."""
    # shutil.which under a simulated win32 reaches for _winapi, which a
    # posix host does not have; PATH is not what this test is about.
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    target = _store_layout(tmp_path)
    desk = cli.read_desk(version=(None, None))
    assert desk.desktop_paths.path == target
    # Now the exe build writes Roaming again; the desk was read before that.
    _cache(tmp_path, _roaming(tmp_path), mtime=4_000_000_000)
    cli._source_lines(desk)
    out = capsys.readouterr().out
    assert "data moved by the Store build: " + target in out
    assert "Roaming is newer" not in out


def test_read_desk_resolves_the_folder_once(win, tmp_path, monkeypatch):
    """Every Desktop reader in read_desk under its one hold: the cache,
    Local Storage and the weekly sources. A reader moved outside the hold
    resolves again, and could describe a different folder."""
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    # No cache, so read_desk also asks desktop_app_installed (Local
    # Storage); a signed-in reading would skip that reader.
    _marker(tmp_path, _roaming(tmp_path))
    _mkdir(tmp_path, os.path.join(_target(tmp_path), "Local Storage",
                                  "leveldb"))
    calls = []
    real = dp._resolve_windows
    monkeypatch.setattr(dp, "_resolve_windows",
                        lambda: calls.append(1) or real())
    desk = cli.read_desk(version=(None, None))
    assert desk.desktop_installed
    assert len(calls) == 1


def test_read_desk_resolves_once_with_a_reading(win, tmp_path, monkeypatch):
    """The signed-in path: the cache, then the weekly sources."""
    import time
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), content=_dc.doc(
        [_dc.sample(int(time.time() * 1000))]))
    calls = []
    real = dp._resolve_windows
    monkeypatch.setattr(dp, "_resolve_windows",
                        lambda: calls.append(1) or real())
    desk = cli.read_desk(version=(None, None))
    assert desk.desktop
    assert len(calls) == 1


# --- one answer per poll ----------------------------------------------------

def test_one_answer_holds_and_releases(win, tmp_path):
    target = _store_layout(tmp_path)
    with dp.one_answer() as held:
        assert held.path == target
        # The tree changes mid-poll; every reader in the poll still agrees.
        _cache(tmp_path, _roaming(tmp_path), mtime=4_000_000_000)
        assert claude_desktop.cache_path().startswith(target)
        assert desktop_idb.store_path().startswith(target)
        with dp.one_answer() as inner:
            assert inner is held
    # Released: the next call sees the change.
    assert dp.data_dir() == _roaming(tmp_path)


def test_one_answer_is_per_thread(win, tmp_path):
    import threading
    target = _store_layout(tmp_path)
    seen = []
    with dp.one_answer():
        _cache(tmp_path, _roaming(tmp_path), mtime=4_000_000_000)
        t = threading.Thread(target=lambda: seen.append(dp.data_dir()))
        t.start()
        t.join()
        assert dp.data_dir() == target
    assert seen == [_roaming(tmp_path)]


def test_a_poll_resolves_the_folder_once(win, tmp_path, monkeypatch):
    """Four readers ask per poll, every 2 s on the serial thread."""
    from pc import ingest
    _store_layout(tmp_path)
    calls = []
    real = dp._resolve_windows
    monkeypatch.setattr(dp, "_resolve_windows",
                        lambda: calls.append(1) or real())
    bus = ingest.IngestionBus()
    bus.poll_frames()
    assert len(calls) == 1
    bus.poll_frames()
    assert len(calls) == 2


# --- review 2026-10-30: branches the first tests left unproved ---------------

def test_a_bracket_in_the_profile_path_still_finds_the_package(
        win, tmp_path, monkeypatch):
    """A user name with `[` in it. Unescaped, glob reads `[b]` as a
    character class, matches nothing, and the package marker is missed."""
    local = tmp_path / "a[b]" / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    _marker(tmp_path, _package_roaming(tmp_path))
    target = _target(tmp_path)
    _cache(tmp_path, target)
    assert dp.windows_data_dir() == (target, "marker:" + NAME)


def test_the_virtualised_local_is_followed(win, tmp_path):
    """UNMEASURED layout (see _candidate_targets): the Store redirecting
    Local the way it redirected Roaming."""
    _marker(tmp_path, _roaming(tmp_path))
    target = os.path.join(_local(tmp_path), "Packages", "Claude_pzs8sxrjxfjjc",
                          "LocalCache", "Local", "Claude-Data", NAME)
    _cache(tmp_path, target)
    assert dp.windows_data_dir() == (target, "marker:" + NAME)


def test_the_virtualised_local_cannot_be_escaped(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path), "..")
    _cache(tmp_path, os.path.join(_local(tmp_path), "Packages",
                                  "Claude_pzs8sxrjxfjjc", "LocalCache",
                                  "Local", "Claude-Data"))
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def _fake_profile(monkeypatch, profile):
    """expanduser for the Windows spellings the fallbacks use. On a posix
    host "~\\AppData\\..." does not expand at all, so the call is mapped
    here rather than trusted to the host."""
    def expand(path):
        if path.startswith("~\\"):
            return os.path.join(str(profile), *path[2:].split("\\"))
        return path
    monkeypatch.setattr(dp.os.path, "expanduser", expand)


def test_appdata_falls_back_to_the_profile(win, tmp_path, monkeypatch):
    monkeypatch.delenv("APPDATA")
    _fake_profile(monkeypatch, tmp_path)
    roaming = os.path.join(str(tmp_path), "AppData", "Roaming", "Claude")
    _cache(tmp_path, roaming)
    assert dp.windows_data_dir() == (roaming, "roaming")


def test_localappdata_falls_back_to_the_profile(win, tmp_path, monkeypatch):
    monkeypatch.delenv("LOCALAPPDATA")
    monkeypatch.setenv("APPDATA", str(tmp_path / "elsewhere" / "Roaming"))
    _fake_profile(monkeypatch, tmp_path)
    _marker(tmp_path, os.path.join(os.environ["APPDATA"], "Claude"))
    target = os.path.join(str(tmp_path), "AppData", "Local", "Claude-Data",
                          NAME)
    _cache(tmp_path, target)
    assert dp.windows_data_dir() == (target, "marker:" + NAME)


@pytest.mark.parametrize("name", ["D:evil", "a\\b", "a/b"])
def test_containment_holds_without_the_character_filter(monkeypatch, name):
    """The backstop on its own: with the character filter emptied, the join
    check still refuses a drive reset and a nested path."""
    monkeypatch.setattr(dp, "_BAD_CHARS", ())
    assert not dp._safe_component(name, r"C:\L\Claude-Data", ntpath)


def test_case_variant_markers_are_one_name(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path), NAME.upper())
    _marker(tmp_path, _package_roaming(tmp_path), NAME)
    assert dp.marker_names(_roaming(tmp_path)) == [NAME.upper()]


def test_case_variant_markers_in_one_folder_are_one_name(win, monkeypatch):
    """Both spellings in one folder. A case-insensitive host (APFS by
    default, NTFS) cannot hold the two files, so the listing is faked."""
    monkeypatch.setattr(dp.os, "listdir", lambda folder: [
        dp.MARKER_PREFIX + NAME, dp.MARKER_PREFIX + NAME.upper()])
    assert dp.marker_names("anywhere") == [NAME.upper()]


def test_mixed_case_markers_in_one_folder_are_one_name(win, monkeypatch):
    """Mixed case, so neither spelling is the other's .lower(): an
    all-lower-case name let a case-sensitive dedupe pass (review round 2)."""
    monkeypatch.setattr(dp.os, "listdir", lambda folder: [
        dp.MARKER_PREFIX + "claude_Ab", dp.MARKER_PREFIX + "Claude_ab"])
    assert dp.marker_names("anywhere") == ["Claude_ab"]


def test_mixed_case_markers_across_folders_are_one_name(win, tmp_path,
                                                        monkeypatch):
    """One spelling in Roaming, another in the package's Roaming: the
    targets are looked up for one name, not two."""
    _marker(tmp_path, _roaming(tmp_path), "Claude_ab")
    _marker(tmp_path, _package_roaming(tmp_path), "claude_Ab")
    _cache(tmp_path, _target(tmp_path, "Claude_ab"))
    asked = []
    real = dp._candidate_targets
    monkeypatch.setattr(dp, "_candidate_targets",
                        lambda names, *a: asked.append(list(names))
                        or real(names, *a))
    assert dp.windows_data_dir() == (_target(tmp_path, "Claude_ab"),
                                     "marker:Claude_ab")
    assert asked == [["Claude_ab"]]


def test_evidence_counts_indexeddb_on_its_own(win, tmp_path):
    """Review of 1.4.0 (tests and docs), N3/D9: dropping IndexedDB from the
    evidence once passed the suite. Here the folder has nothing but a file
    in its IndexedDB leveldb: no usage cache, no Local Storage."""
    folder = _target(tmp_path, NAME)
    _ldb(tmp_path, folder, "idb", mtime=2_000_000)
    ev = dp._evidence(folder)
    assert (ev.has_cache, ev.other_mtime) == (False, 2_000_000)
    _marker(tmp_path, _roaming(tmp_path), OTHER)
    _ldb(tmp_path, _target(tmp_path, OTHER), "ls", mtime=1_000_000)
    _marker(tmp_path, _roaming(tmp_path), NAME)
    assert dp.windows_data_dir()[0] == folder


def test_a_fresh_indexeddb_file_does_not_beat_a_cache(win, tmp_path):
    """The cache is the evidence that counts: a folder holding it beats one
    with only fresher Local Storage or IndexedDB files."""
    for name in (NAME, OTHER):
        _marker(tmp_path, _roaming(tmp_path), name)
    _cache(tmp_path, _target(tmp_path, OTHER), mtime=1_000_000)
    _ldb(tmp_path, _target(tmp_path, NAME), "idb", mtime=9_000_000)
    assert dp.windows_data_dir()[0] == _target(tmp_path, OTHER)


# --- 2026-11-04: the Store build that keeps its data in the package ----------
#
# A friend's Windows PC, Blink 1.4.1, Claude Desktop from the Microsoft Store
# (Get-AppxPackage: Claude 4.16336.0.0):
#   - %APPDATA%\Claude does not exist at all;
#   - %LOCALAPPDATA%\Claude-Data exists and holds ONE FILE, README.txt of
#     1111 bytes (2026-11-01 08:27:45), and no data folder -- for days
#     before any data moved there;
#   - the live data is in
#     %LOCALAPPDATA%\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude,
#     with no .user-data-left marker anywhere.
# `blink status` said "not installed" and looked only at Roaming.
#
# The same evening the Store build migrated live: at 23:23:27 the marker
# .user-data-left.claude_pzs8sxrjxfjjc appeared in the package's
# LocalCache\Roaming\Claude, and at 23:23:38 plan-usage-history.json was
# written under Claude-Data\claude_pzs8sxrjxfjjc. A recursive search found
# only that copy afterwards: the package copy was MOVED, not left behind.
# Blink 1.4.1 sent its first `-> usage` at 23:24:01, the next poll. So the
# states a desk passes through are: before (package data, no marker,
# Claude-Data holding a README), the instant of the move (marker, both
# copies), and after (marker, Claude-Data only). All three are tests below.
#
# The layouts are built here, from the sizes and names that were measured;
# none of it is a copy of anything on that machine.
#
# Precedence, which the table below pins row by row. Candidates are: a
# marker's target folder (M), each package's LocalCache\Roaming\Claude that
# shows an empty Local Storage or IndexedDB folder or more (P), and plain
# Roaming\Claude (R). A folder with the usage cache beats every folder
# without one; among cache holders the newest cache wins; among the rest the
# newest FILE in Local Storage or IndexedDB wins. Ties: M, then P, then R.
# Between M's the name decides; between P's the package name decides.
# Nothing with any evidence at all: an existing target, else R.
#
# There is no exclusion for a package folder that carries a marker: its
# leftover cache is older than the live one, and the ranking reads files, so
# a cleanup that bumps a directory moves nothing (review of PR #3,
# 2026-11-05). The Windows measurements behind these layouts are in the
# desktop_paths docstring: the 2026-11-04 friend's PC and the 2026-11-05 VM.

PKG_A = "Claude_pzs8sxrjxfjjc"   # the shape measured
PKG_B = "Claude_0000000000000"   # sorts before PKG_A
PKG_SOURCE_A = "package:" + PKG_A
PKG_SOURCE_B = "package:" + PKG_B


def _pkg_roaming(home, pkg=PKG_A):
    return inside(home, os.path.join(_local(home), "Packages", pkg,
                                     "LocalCache", "Roaming", "Claude"))


def _friends_layout(home, mtime=None):
    """The measured machine before the move: no Roaming\\Claude, Claude-Data
    holding only a 1111-byte README.txt, data in the package's virtualised
    Roaming, no marker."""
    readme = inside(home, os.path.join(_local(home), "Claude-Data",
                                       "README.txt"))
    os.makedirs(os.path.dirname(readme), exist_ok=True)
    with open(readme, "wb") as f:
        f.write(b"\0" * 1111)
    folder = _pkg_roaming(home)
    _cache(home, folder, mtime=mtime)
    return folder


def test_the_friends_layout_reads_the_package_folder(win, tmp_path):
    folder = _friends_layout(tmp_path)
    assert not os.path.exists(_roaming(tmp_path))
    readme = os.path.join(_local(tmp_path), "Claude-Data", "README.txt")
    assert os.path.getsize(readme) == 1111
    assert os.listdir(os.path.join(_local(tmp_path), "Claude-Data")) == [
        "README.txt"]
    assert dp.windows_data_dir() == (folder, PKG_SOURCE_A)
    assert not dp.marker_without_data()
    assert dp.resolve().in_package
    assert not dp.resolve().moved


def test_every_reader_follows_the_package_folder(win, tmp_path):
    """All four readers, and the providers that wrap them, through the one
    answer: the usage cache, Local Storage, IndexedDB and Cowork."""
    folder = _friends_layout(tmp_path)
    paths = [claude_desktop.cache_path(),
             desktop_local_storage.store_path(),
             desktop_idb.store_path(),
             cowork_audit.sessions_dir(),
             claude_desktop.ClaudeDesktopProvider().path(),
             ClaudeDesktopLocalStorageProvider().path()]
    for p in paths:
        assert os.path.commonpath([p, folder]) == folder, p


def test_the_package_answer_is_held_for_a_whole_poll(win, tmp_path):
    """one_answer() pins the package folder too: Roaming turning fresher
    mid-poll must not move any of the four readers."""
    folder = _friends_layout(tmp_path, mtime=1_000_000)
    with dp.one_answer() as held:
        assert held.path == folder
        _cache(tmp_path, _roaming(tmp_path), mtime=4_000_000_000)
        for p in (claude_desktop.cache_path(),
                  desktop_local_storage.store_path(),
                  desktop_idb.store_path(),
                  cowork_audit.sessions_dir()):
            assert os.path.commonpath([p, folder]) == folder, p
    # Outside the hold the fresher Roaming is read, as it should be.
    assert dp.data_dir() == _roaming(tmp_path)


def test_desktop_reading_finds_a_cache_in_the_package_folder(win, tmp_path):
    import time
    folder = _pkg_roaming(tmp_path)
    _cache(tmp_path, folder,
           content=_dc.doc([_dc.sample(int(time.time() * 1000))]))
    reading = cli.desktop_reading()
    assert reading is not None
    assert reading[0] == claude_desktop.VERDICT_FRAME


def test_desktop_history_reads_the_package_folder(win, tmp_path):
    _cache(tmp_path, _pkg_roaming(tmp_path), content=_dc.doc(
        [_dc.sample(_dc.T_MS - 60_000, sd=20), _dc.sample(_dc.T_MS, sd=30)]))
    samples = DesktopHistoryProvider().samples(_dc.NOW)
    assert [s["u"]["sd"] for s in samples] == [20, 30]


def test_desktop_app_installed_sees_local_storage_in_the_package(win,
                                                                  tmp_path):
    assert not cli.desktop_app_installed()
    _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path), "Local Storage",
                                  "leveldb"))
    assert cli.desktop_app_installed()


# What counts as "holds Desktop data": the usage cache FILE, or a Local
# Storage or IndexedDB folder. A config file, a marker or an empty folder
# does not -- a package folder the Store creates on install is not Desktop's
# data.

def _make_data(home, folder, form):
    if form == "usage cache":
        _cache(home, folder)
    elif form == "local storage":
        _mkdir(home, os.path.join(folder, "Local Storage", "leveldb"))
    elif form == "local storage dir only":
        _mkdir(home, os.path.join(folder, "Local Storage"))
    else:
        _mkdir(home, os.path.join(folder, "IndexedDB"))


@pytest.mark.parametrize("form", ["usage cache", "local storage",
                                  "local storage dir only", "indexeddb"])
def test_each_form_of_desktop_data_counts(win, tmp_path, form):
    _make_data(tmp_path, _pkg_roaming(tmp_path), form)
    assert dp.windows_data_dir() == (_pkg_roaming(tmp_path), PKG_SOURCE_A)


@pytest.mark.parametrize("junk", ["config.json", "claude_desktop_config.json",
                                  "Preferences", dp.MARKER_PREFIX + "x"])
def test_a_package_folder_with_only_config_is_not_data(win, tmp_path, junk):
    _touch(tmp_path, os.path.join(_pkg_roaming(tmp_path), junk))
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def test_an_empty_package_folder_is_not_data(win, tmp_path):
    _mkdir(tmp_path, _pkg_roaming(tmp_path))
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def test_a_usage_cache_that_is_a_folder_is_not_data(win, tmp_path):
    _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path),
                                  "plan-usage-history.json"))
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


# The contest table. Each row gives an mtime (in millions of seconds) for
# the layers that hold data (None: the layer is absent). Layers: R is plain
# Roaming, M the target of a marker, A and B two packages. `want` is "R",
# "M", "A" or "B".

ROWS = [
    # id,                          R,    M,    A,    B,    want
    ("package alone",              None, None, 5,    None, "A"),
    ("roaming alone",              5,    None, None, None, "R"),
    ("nothing at all",             None, None, None, None, "R"),
    ("package fresher than R",     1,    None, 2,    None, "A"),
    ("package stale beside R",     2,    None, 1,    None, "R"),
    ("package ties R",             3,    None, 3,    None, "A"),
    ("two packages, B fresher",    None, None, 1,    2,    "B"),
    ("two packages, A fresher",    None, None, 2,    1,    "A"),
    ("two packages tie",           None, None, 2,    2,    "B"),
    ("M fresher than package",     None, 2,    1,    None, "M"),
    ("package fresher than M",     None, 1,    2,    None, "A"),
    ("M ties package",             None, 2,    2,    None, "M"),
    ("M stale, R fresher",         3,    1,    None, None, "R"),
    ("M fresh, R stale",           1,    3,    None, None, "M"),
    ("M ties R",                   2,    2,    None, None, "M"),
    ("R freshest of all three",    9,    2,    3,    None, "R"),
    ("M freshest of all three",    1,    9,    3,    None, "M"),
    ("package freshest of three",  1,    2,    9,    None, "A"),
    ("R ties the best package",    4,    2,    4,    None, "A"),
    ("B beats A beats M beats R",  1,    2,    3,    4,    "B"),
]


def _scene(home, r, m, a, b, marker_in="roaming"):
    if r is not None:
        _cache(home, _roaming(home), mtime=r * 1_000_000)
    if m is not None:
        _marker(home, _roaming(home) if marker_in == "roaming"
                else _pkg_roaming(home))
        _cache(home, _target(home), mtime=m * 1_000_000)
    if a is not None:
        _cache(home, _pkg_roaming(home, PKG_A), mtime=a * 1_000_000)
    if b is not None:
        _cache(home, _pkg_roaming(home, PKG_B), mtime=b * 1_000_000)


def _expected(want, home):
    return {"R": (_roaming(home), "roaming"),
            "M": (_target(home), "marker:" + NAME),
            "A": (_pkg_roaming(home, PKG_A), PKG_SOURCE_A),
            "B": (_pkg_roaming(home, PKG_B), PKG_SOURCE_B)}[want]


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_the_freshest_candidate_wins(win, tmp_path, row):
    _, r, m, a, b, want = row
    _scene(tmp_path, r, m, a, b)
    assert dp.windows_data_dir() == _expected(want, tmp_path)


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_the_same_contest_with_the_marker_in_the_package(win, tmp_path, row):
    """A marker may sit in the package's Roaming instead (Mor Gal's layout
    had it in both). Where it sits changes nothing: the same folder wins."""
    _, r, m, a, b, want = row
    _scene(tmp_path, r, m, a, b, marker_in="package")
    assert dp.windows_data_dir() == _expected(want, tmp_path)


# --- the four states the friend's PC passed through (2026-11-04) -------------

def test_before_the_move_the_package_folder_is_read(win, tmp_path):
    """(a) Package data, no marker, Claude-Data holding only README.txt."""
    folder = _friends_layout(tmp_path)
    answer = dp.resolve()
    assert (answer.path, answer.source) == (folder, PKG_SOURCE_A)
    assert not answer.dangling


def test_after_the_move_the_marker_still_leads_to_claude_data(win, tmp_path):
    """(b) What 1.4.1 already handled: the marker in the package folder, the
    data under Claude-Data\\<name>, the package copy gone (it was moved)."""
    _friends_layout(tmp_path)
    os.remove(os.path.join(_pkg_roaming(tmp_path), "plan-usage-history.json"))
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path))
    assert os.path.exists(os.path.join(_local(tmp_path), "Claude-Data",
                                       "README.txt"))
    assert dp.windows_data_dir() == (_target(tmp_path), "marker:" + NAME)


def test_the_marker_alone_before_the_data_lands_keeps_the_package(win,
                                                                   tmp_path):
    """The window between the marker and the cache (23:23:27 and 23:23:38 on
    the friend's PC): the marker names a folder that is not there yet, and
    the package still holds the data. 1.4.1 fell back to an absent Roaming
    here."""
    folder = _friends_layout(tmp_path)
    _marker(tmp_path, _pkg_roaming(tmp_path))
    answer = dp.resolve()
    assert (answer.path, answer.source) == (folder, PKG_SOURCE_A)
    assert answer.dangling and answer.not_read == ()


@pytest.mark.parametrize("package_mtime,target_mtime,want", [
    (1_000_000, 2_000_000, "M"),    # copied: the target is the newer write
    (2_000_000, 2_000_000, "M"),    # moved with its mtime kept: a tie
    (2_000_000, 1_000_000, "A"),    # the package cache written after: newer
], ids=["target newer", "tie", "package newer"])
def test_both_copies_at_once_the_newer_cache_wins(
        win, tmp_path, package_mtime, target_mtime, want):
    """Both copies on disk with the marker in the package's own folder. Not
    observed on any machine (the friend's package copy was moved, not left
    behind), so this pins the rule rather than a measurement: the newer
    cache is read, a tie goes to the marker's target."""
    _friends_layout(tmp_path, mtime=package_mtime)
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=target_mtime)
    assert dp.windows_data_dir() == _expected(want, tmp_path)


@pytest.mark.parametrize("package_mtime,target_mtime,want", [
    (1_000_000, 2_000_000, "M"),
    (2_000_000, 2_000_000, "M"),
    (2_000_000, 1_000_000, "A"),
], ids=["target newer", "tie", "package newer"])
def test_both_copies_with_the_marker_only_in_roaming_pick_the_newer(
        win, tmp_path, package_mtime, target_mtime, want):
    """The same contest with the marker in real Roaming: where the marker
    sits changes nothing, the newer cache decides and a tie goes to the
    folder the app pointed at."""
    _friends_layout(tmp_path, mtime=package_mtime)
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=target_mtime)
    assert dp.windows_data_dir() == _expected(want, tmp_path)


def test_a_bumped_leftover_directory_does_not_beat_the_live_target(
        win, tmp_path):
    """Review of PR #3, #3. Marker only in Roaming, the live cache in
    Claude-Data, and a leftover package copy with an old cache and an
    IndexedDB folder emptied after the move, so its DIRECTORY mtime is the
    newest thing on disk. Directory mtimes are not read."""
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=500_000)
    idb = _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path), "IndexedDB"))
    os.utime(idb, (9_000_000, 9_000_000))
    answer = dp.resolve()
    assert (answer.path, answer.source) == (_target(tmp_path),
                                            "marker:" + NAME)
    assert answer.not_read == (_pkg_roaming(tmp_path),)


def test_a_bumped_leftover_with_only_an_emptied_folder_loses(win, tmp_path):
    """Review of PR #3, #3, second form: the leftover has no cache at all,
    just an emptied IndexedDB folder whose mtime is T+60."""
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    idb = _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path),
                                        *dp.INDEXEDDB_LEAF))
    os.utime(idb, (1_000_060, 1_000_060))
    assert dp.windows_data_dir() == (_target(tmp_path), "marker:" + NAME)


def test_a_marked_package_with_a_newer_cache_beats_an_old_target(
        win, tmp_path):
    """Review of PR #3, #4: the marker sits in the package, the target's
    cache is 71 days old and the package's was written now. The old rule
    skipped the package whatever its age; the newer cache is read."""
    now = 1_800_000_000
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=now - 71 * 86400)
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=now)
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert answer.not_read == (_target(tmp_path),)


def test_a_cache_beats_a_fresher_package_with_only_local_storage(
        win, tmp_path):
    """Review of PR #3, #1. The live cache is in Roaming, written 10 minutes
    ago; a marker-less package holds only a Local Storage file, written a
    minute ago. The cache holder wins: poll returned 0 frames from the
    package before."""
    now = 1_800_000_000
    _cache(tmp_path, _roaming(tmp_path), mtime=now - 600)
    _ldb(tmp_path, _pkg_roaming(tmp_path), "ls", mtime=now - 60)
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def test_a_cache_beats_a_fresher_package_even_with_a_dangling_marker(
        win, tmp_path):
    """#1 again, with a marker that names nothing."""
    now = 1_800_000_000
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _roaming(tmp_path), mtime=now - 600)
    _ldb(tmp_path, _pkg_roaming(tmp_path), "ls", mtime=now - 60)
    assert dp.resolve().source == "roaming"


def test_an_empty_target_does_not_hide_a_package_that_holds_data(
        win, tmp_path, capsys):
    """The marker's folder exists but is empty: the package folder is the
    only copy and must be read."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _mkdir(tmp_path, _target(tmp_path))
    _cache(tmp_path, _pkg_roaming(tmp_path),
           content=_dc.doc([_dc.sample(int(time.time() * 1000))]))
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert answer.not_read == ()
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     usage cache parsed")
    assert not any(w in l for l in row
                   for w in ("ignored", "skipped", "passed over", "other copy"))


def test_a_target_holding_only_empty_folders_does_not_hide_the_cache(
        win, tmp_path, capsys):
    """Review of PR #3, #2. The marker came 11 s before the cache on the
    friend's PC, and a target that is only Local Storage and IndexedDB
    skeletons is the signed-out shape. The package holds the only cache:
    it is read, and status contradicts nothing."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _skeleton(tmp_path, _target(tmp_path))
    _cache(tmp_path, _pkg_roaming(tmp_path),
           content=_dc.doc([_dc.sample(int(time.time() * 1000))]))
    assert dp.resolve().source == PKG_SOURCE_A
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     usage cache parsed")
    assert not any(w in l for l in row
                   for w in ("no usage cache", "skipped", "also looked"))


def test_a_readme_only_target_does_not_hide_a_package_that_holds_data(
        win, tmp_path):
    """A marker's folder holding only README.txt (a state constructed here;
    the measured README sits in Claude-Data itself, not in the per-install
    folder) has no cache, so the package that has one is read."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _touch(tmp_path, os.path.join(_target(tmp_path), "README.txt"))
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert answer.not_read == ()


def test_a_marker_that_dangles_does_not_hide_its_package(win, tmp_path):
    """With the marker in the package and no data folder yet, the package
    is the only copy."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert answer.not_read == ()


def test_a_marker_in_one_package_does_not_decide_between_packages(
        win, tmp_path):
    """Packages compete on their caches like everything else: A carries the
    marker and the newest cache, so A is read, and the two older copies are
    named as not read."""
    _marker(tmp_path, _pkg_roaming(tmp_path, PKG_A))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path, PKG_A), mtime=9_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path, PKG_B), mtime=2_000_000)
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert set(answer.not_read) == {_pkg_roaming(tmp_path, PKG_B),
                                    _target(tmp_path)}


def test_a_marker_with_a_different_case_prefix_in_a_package_is_followed(
        win, tmp_path):
    """Windows names are case-insensitive: a marker spelled
    `.User-Data-Left.<name>` in the package's Roaming still names the
    target. Fails without the lower() in marker_names."""
    _marker(tmp_path, _pkg_roaming(tmp_path), prefix=".User-Data-Left.")
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    assert dp.resolve().source == "marker:" + NAME


def test_a_package_that_only_holds_the_marker_has_nothing_left_behind(
        win, tmp_path):
    """The ordinary after state: the package folder keeps just the marker."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path))
    answer = dp.resolve()
    assert answer.source == "marker:" + NAME
    assert answer.not_read == ()


def test_readme_in_claude_data_is_not_a_data_folder(win, tmp_path):
    """(d) Claude-Data\\README.txt is a file with no marker naming it. It is
    not a candidate, and it does not disturb the package answer."""
    folder = _friends_layout(tmp_path)
    assert not dp.marker_names(os.path.join(_local(tmp_path), "Claude-Data"))
    assert dp.windows_data_dir() == (folder, PKG_SOURCE_A)


@pytest.mark.parametrize("where", ["roaming", "package", "claude-data"])
def test_a_readme_is_never_mistaken_for_a_marker(win, tmp_path, where):
    """README.txt in any of the three folders is not a `.user-data-left.`
    file, so no name is taken from it."""
    folder = {"roaming": _roaming(tmp_path),
              "package": _pkg_roaming(tmp_path),
              "claude-data": os.path.join(_local(tmp_path), "Claude-Data"),
              }[where]
    _touch(tmp_path, os.path.join(folder, "README.txt"))
    assert dp.marker_names(folder) == []
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def test_a_marker_naming_the_readme_follows_nothing(win, tmp_path):
    """A marker called .user-data-left.README.txt would point at
    Claude-Data\\README.txt, which is a file: isdir fails, the target is
    dangling, and the package data answers."""
    _friends_layout(tmp_path)
    _marker(tmp_path, _pkg_roaming(tmp_path), name="README.txt")
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert answer.dangling


def test_a_dangling_marker_does_not_hide_package_data(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _pkg_roaming(tmp_path))
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert answer.dangling and answer.not_read == ()


def test_a_package_cache_that_lost_to_roaming_is_named(win, tmp_path):
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    answer = dp.resolve()
    assert answer.source == "roaming"
    assert answer.not_read == (_pkg_roaming(tmp_path),)


def test_a_target_cache_that_lost_to_roaming_is_named(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    answer = dp.resolve()
    assert answer.source == "roaming"
    assert answer.not_read == (_target(tmp_path),)


def test_folders_without_a_cache_are_never_named_as_not_read(win, tmp_path):
    """Review of PR #3, #9: the flags came from different tests of the same
    folders and disagreed. Now one rule: only a folder holding a cache that
    lost is named. An empty target losing to Roaming, and to a package,
    says nothing; so does a package with only Local Storage files."""
    _marker(tmp_path, _roaming(tmp_path))
    _mkdir(tmp_path, _target(tmp_path))
    _ldb(tmp_path, _pkg_roaming(tmp_path, PKG_B), "ls", mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    assert dp.resolve().not_read == ()
    os.remove(os.path.join(_roaming(tmp_path), "plan-usage-history.json"))
    _cache(tmp_path, _pkg_roaming(tmp_path, PKG_A), mtime=2_000_000)
    assert dp.resolve().not_read == ()


def test_a_stale_package_leftover_never_beats_a_live_target(win, tmp_path):
    """The migration: the old build's data is still in the package's
    Roaming, and the marker's target is where the app writes now."""
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _target(tmp_path), mtime=2_000_000)
    assert dp.windows_data_dir() == (_target(tmp_path), "marker:" + NAME)


def test_a_stale_package_leftover_never_beats_a_live_roaming(win, tmp_path):
    """Back on the exe build, Store package left behind."""
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def test_a_package_with_a_live_indexeddb_beats_a_roaming_with_old_files(
        win, tmp_path):
    """Neither side has the cache, so the newest FILE decides. (The old
    test asserted that a fresh IndexedDB directory beat a cache; that was
    review finding #1.)"""
    _ldb(tmp_path, _roaming(tmp_path), "ls", mtime=1_000_000)
    _ldb(tmp_path, _pkg_roaming(tmp_path), "idb", mtime=2_000_000)
    assert dp.windows_data_dir()[0] == _pkg_roaming(tmp_path)


def test_a_package_with_only_a_skeleton_beats_an_empty_target(win,
                                                               tmp_path):
    """Review of PR #3, #7: an existing empty target tied a package whose
    only data was a Local Storage folder, and won. A folder with some
    evidence beats one with none."""
    _marker(tmp_path, _roaming(tmp_path))
    _touch(tmp_path, os.path.join(_target(tmp_path), "README.txt"))
    _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path), "Local Storage"))
    assert dp.resolve().source == PKG_SOURCE_A


def test_nothing_with_evidence_keeps_the_existing_target(win, tmp_path):
    """No evidence anywhere: an existing marker target answers (the desk
    has installed Desktop and not signed in), else Roaming."""
    _marker(tmp_path, _roaming(tmp_path))
    _touch(tmp_path, os.path.join(_target(tmp_path), "README.txt"))
    assert dp.resolve().source == "marker:" + NAME


def test_the_package_source_is_one_path_component(win, tmp_path):
    """The source string carries the package folder's name, from a listing
    of Packages. It is a label, never joined into a path."""
    _cache(tmp_path, _pkg_roaming(tmp_path))
    source = dp.resolve().source
    assert source == PKG_SOURCE_A
    assert "/" not in source and "\\" not in source


# --- glob metacharacters in LOCALAPPDATA ------------------------------------

def _glob_hostile_names():
    names = ["a[b]", "a[!x]", "u[ab]"]
    if os.name != "nt":      # Windows refuses these in a file name
        names += ["a*b", "a?b"]
    return names


@pytest.mark.parametrize("profile", _glob_hostile_names())
def test_glob_metacharacters_in_localappdata_still_find_the_package(
        win, tmp_path, monkeypatch, profile):
    local = tmp_path / profile / "AppData" / "Local"
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    folder = _pkg_roaming(tmp_path)
    assert profile in folder
    _cache(tmp_path, folder)
    assert dp.windows_data_dir() == (folder, PKG_SOURCE_A)


def _glob_decoys():
    pairs = [("u[ab]", "ua"), ("a[!x]", "ay")]
    if os.name != "nt":      # Windows refuses these in a file name
        pairs += [("a*b", "aXb"), ("a?b", "aXb")]
    return pairs


@pytest.mark.parametrize("profile,neighbour", _glob_decoys())
def test_a_glob_pattern_in_localappdata_does_not_reach_a_neighbour(
        win, tmp_path, monkeypatch, profile, neighbour):
    """`u[ab]` read as a character class would match the neighbour `ua`, and
    `a*b` / `a?b` match `aXb`, so a desk whose profile folder is named that
    would read somebody else's data. The decoy is fresher on purpose, and
    each pair fails without glob.escape."""
    monkeypatch.setenv("LOCALAPPDATA",
                       str(tmp_path / profile / "AppData" / "Local"))
    mine = _pkg_roaming(tmp_path)
    _cache(tmp_path, mine, mtime=1_000_000)
    decoy = inside(tmp_path, os.path.join(
        str(tmp_path), neighbour, "AppData", "Local", "Packages", PKG_A,
        "LocalCache", "Roaming", "Claude"))
    _cache(tmp_path, decoy, mtime=2_000_000)
    assert dp.windows_data_dir() == (mine, PKG_SOURCE_A)


# --- what `blink status` says ----------------------------------------------

def test_status_says_the_data_is_in_the_package_folder(win, tmp_path, capsys):
    import time
    folder = _pkg_roaming(tmp_path)
    _cache(tmp_path, folder,
           content=_dc.doc([_dc.sample(int(time.time() * 1000))]))
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     usage cache parsed")
    assert ("            data found in the Store package folder: " + folder
            ) in row
    assert not any("moved by the Store build" in l for l in row)
    assert not any("looked at" in l for l in row)


def test_status_for_the_friends_layout(win, tmp_path, capsys):
    import time
    folder = _friends_layout(tmp_path)
    _cache(tmp_path, folder,
           content=_dc.doc([_dc.sample(int(time.time() * 1000))]))
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     usage cache parsed")
    assert ("            data found in the Store package folder: " + folder
            ) in row
    # The whole block, pinned. The friend's layout has no marker, so no line
    # may claim one was ignored, passed over or skipped (the guard on
    # marker_passed_over once went unpinned: review, 2026-11-05).
    assert len(row) == 2, row
    assert not any(w in l for l in row
                   for w in ("ignored", "skipped", "passed over", "marker"))


def test_status_for_a_package_with_no_cache_yet(win, tmp_path, capsys):
    """Local Storage but no usage cache: signed out. The row must still name
    the folder it is reading, beside `looked at` for the file it wants."""
    folder = _pkg_roaming(tmp_path)
    _mkdir(tmp_path, os.path.join(folder, "Local Storage", "leveldb"))
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     no usage cache")
    assert "            looked at " + os.path.join(
        folder, "plan-usage-history.json") in row
    assert ("            data found in the Store package folder: " + folder
            ) in row


def test_status_says_when_roaming_beat_a_package(win, tmp_path, capsys):
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    row = _desktop_row(capsys)
    assert ("            other copy not read: " + _pkg_roaming(tmp_path)
            ) in row
    assert not any("data found in" in l for l in row)


def test_status_lists_every_place_it_looked(win, tmp_path, capsys):
    """The friend's output named Roaming and nothing else, which sent us
    looking in the wrong place. Two packages, one marker name: every folder
    that was tried is on the screen."""
    _marker(tmp_path, _roaming(tmp_path), NAME)
    _mkdir(tmp_path, _pkg_roaming(tmp_path, PKG_A))
    _mkdir(tmp_path, _pkg_roaming(tmp_path, PKG_B))
    row = _desktop_row(capsys)
    cache = "plan-usage-history.json"
    assert row[0].startswith("Desktop     no usage cache")
    assert ("            looked at "
            + os.path.join(_roaming(tmp_path), cache)) in row
    for folder in (_pkg_roaming(tmp_path, PKG_A),
                   _pkg_roaming(tmp_path, PKG_B),
                   _target(tmp_path)):
        assert ("            also looked at "
                + os.path.join(folder, cache)) in row, folder


def test_status_says_when_there_is_no_package_folder_at_all(win, tmp_path,
                                                            capsys):
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     no usage cache")
    packages = os.path.join(_local(tmp_path), "Packages")
    assert ("            no Store package folder under " + packages
            + " (looked for Claude_*)") in row


def test_status_does_not_claim_a_missing_package_when_one_exists(
        win, tmp_path, capsys):
    _mkdir(tmp_path, _pkg_roaming(tmp_path))
    row = _desktop_row(capsys)
    assert not any("no Store package folder" in l for l in row)
    assert ("            also looked at " + os.path.join(
        _pkg_roaming(tmp_path), "plan-usage-history.json")) in row


def test_status_is_unchanged_off_windows(monkeypatch, tmp_path, capsys):
    """macOS has one place; no `also looked at` and no package line."""
    monkeypatch.setattr(sys, "platform", "darwin")
    row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     no usage cache")
    assert sum(l.startswith("            looked at ") for l in row) == 1
    assert not any("also looked" in l or "package" in l for l in row)



# --- review 2026-11-04 and 2026-11-05: what status says when copies meet ----

def test_status_says_when_a_package_beat_a_marker_target(win, tmp_path,
                                                          capsys):
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    folder = _pkg_roaming(tmp_path)
    _cache(tmp_path, folder, mtime=2_000_000)
    row = _desktop_row(capsys)
    assert ("            data found in the Store package folder: " + folder
            ) in row
    assert ("            other copy not read: " + _target(tmp_path)) in row
    assert not any("skipped" in l or "ignored" in l for l in row)


def test_status_names_an_older_leftover_copy_it_did_not_read(
        win, tmp_path, capsys):
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=2_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    row = _desktop_row(capsys)
    assert ("            data moved by the Store build: " + _target(tmp_path)
            ) in row
    assert ("            other copy not read: " + _pkg_roaming(tmp_path)
            ) in row
    assert not any("data found in" in l or "skipped" in l for l in row)


def test_status_is_quiet_for_the_ordinary_after_state(win, tmp_path, capsys):
    """The package folder holds only the marker: nothing was left behind."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path))
    row = _desktop_row(capsys)
    assert not any(w in l for l in row
                   for w in ("skipped", "ignored", "data found", "other copy"))


def test_status_is_quiet_when_the_package_keeps_empty_folders(
        win, tmp_path, capsys):
    """Review of PR #3, #11: after the move the package kept its empty
    Local Storage and IndexedDB folders beside the marker, and every status
    printed a "skipped" line for the ordinary state."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _skeleton(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path))
    row = _desktop_row(capsys)
    assert not any(w in l for l in row
                   for w in ("skipped", "ignored", "data found", "other copy"))


def test_status_does_not_list_a_cache_holder_as_a_place_with_nothing(
        win, tmp_path, capsys):
    """Review of PR #3, #6: the "no usage cache" block printed "also looked
    at <folder>\\plan-usage-history.json" for a file that exists. A held
    answer where the reading is missing but a copy lost names it once, as
    not read, and never as a place looked at."""
    pkg = _pkg_roaming(tmp_path)
    _cache(tmp_path, pkg, mtime=1_000_000)
    package_dir = os.path.join(_local(tmp_path), "Packages", PKG_A)
    held = dp.Answer(_roaming(tmp_path), dp.SOURCE_ROAMING,
                     not_read=(pkg,), packages=(package_dir,),
                     roaming=_roaming(tmp_path), local=_local(tmp_path))
    with dp.one_answer(held):
        row = _desktop_row(capsys)
    assert row[0].startswith("Desktop     no usage cache")
    assert ("            other copy not read: " + pkg) in row
    assert not any("also looked at" in l and pkg in l for l in row)


def test_status_says_when_the_folder_lookup_failed(win, tmp_path, capsys,
                                                    monkeypatch):
    """Review of PR #3, #14: the fallback printed the 1.4.1 block, Roaming
    alone, with nothing saying the lookup had failed."""
    def boom(*a, **k):
        raise RuntimeError("not an OSError")
    monkeypatch.setattr(dp, "_packages", boom)
    assert dp.resolve().resolve_failed
    row = _desktop_row(capsys)
    assert ("            folder lookup failed (RuntimeError), Roaming used"
            " as a fallback") in row
    assert not any("no Store package folder" in l or "also looked" in l
                   for l in row)


def test_a_working_lookup_is_not_flagged_as_failed(win, tmp_path, capsys):
    row = _desktop_row(capsys)
    assert not dp.resolve().resolve_failed
    assert not any("lookup failed" in l for l in row)



def test_status_never_repeats_the_looked_at_path(win, tmp_path, capsys):
    """`also looked at` is for the OTHER places. The first place, the one
    `looked at` already printed, must not come back under it: nothing found
    (that place is Roaming), and a package with no cache yet (that place is
    the package folder)."""
    cache = "plan-usage-history.json"
    row = _desktop_row(capsys)
    first = next(l for l in row if l.startswith("            looked at "))
    assert first == "            looked at " + os.path.join(
        _roaming(tmp_path), cache)
    assert ("            also looked at " + first.split("looked at ", 1)[1]
            ) not in row
    assert sum(_roaming(tmp_path) in l for l in row) == 1

    folder = _pkg_roaming(tmp_path)
    _mkdir(tmp_path, os.path.join(folder, "Local Storage", "leveldb"))
    row = _desktop_row(capsys)
    first = next(l for l in row if l.startswith("            looked at "))
    assert first == "            looked at " + os.path.join(folder, cache)
    assert sum(os.path.join(folder, cache) in l for l in row) == 1
    # And Roaming, now one of the other places, is listed exactly once.
    assert ("            also looked at "
            + os.path.join(_roaming(tmp_path), cache)) in row


# --- review 2026-11-04: the looked-at list is for status alone ---------------

def test_the_looked_at_list_is_not_built_by_a_resolve(win, tmp_path,
                                                       monkeypatch):
    """The daemon resolves every 2 s and only `blink status` reads the list.
    A resolve does the marker work it always did (one _safe_component per
    name and root); building `looked` costs another per name and root, and
    must happen when asked for, not before."""
    _marker(tmp_path, _roaming(tmp_path))
    _mkdir(tmp_path, _pkg_roaming(tmp_path, PKG_A))
    _mkdir(tmp_path, _pkg_roaming(tmp_path, PKG_B))
    calls = []
    real = dp._safe_component
    monkeypatch.setattr(dp, "_safe_component",
                        lambda *a, **k: calls.append(1) or real(*a, **k))
    assert "looked" not in dp.Answer._fields
    answer = dp.resolve()
    after_resolve = len(calls)
    assert after_resolve > 0
    assert len(answer.looked) == 1 + 2 + 3     # Roaming, 2 packages, 3 roots
    assert len(calls) == after_resolve + 3
    assert dp.resolve().packages_root == os.path.join(_local(tmp_path),
                                                      "Packages")


def test_a_held_answer_lists_the_same_places_after_the_tree_changes(
        win, tmp_path):
    """one_answer() semantics: `looked` works from the held answer's own
    fields, so a package that appears mid-run is not listed."""
    _mkdir(tmp_path, _pkg_roaming(tmp_path, PKG_A))
    with dp.one_answer() as held:
        before = held.looked
        _mkdir(tmp_path, _pkg_roaming(tmp_path, PKG_B))
        assert dp.resolve().looked == before



# --- review of PR #3, 2026-11-05: the remaining findings -------------------

def test_directory_mtimes_are_never_the_evidence(win, tmp_path):
    """Root cause of #1-#4, #7, #9, #11. A bumped directory beside a cache
    changes nothing, and a directory with no file in it is no evidence of
    writes."""
    _cache(tmp_path, _roaming(tmp_path), mtime=1_000_000)
    idb = _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path),
                                        *dp.INDEXEDDB_LEAF))
    os.utime(idb, (9_000_000, 9_000_000))
    ev = dp._evidence(_pkg_roaming(tmp_path))
    assert ev.other_mtime == float("-inf") and ev.skeleton
    assert dp.windows_data_dir() == (_roaming(tmp_path), "roaming")


def test_a_directory_inside_leveldb_is_not_a_file_write(win, tmp_path):
    """Only FILE entries count: a subdirectory with a fresh mtime (something
    created and deleted inside it) is not the app writing."""
    folder = _pkg_roaming(tmp_path)
    _ldb(tmp_path, folder, "ls", mtime=1_000_000)
    sub = _mkdir(tmp_path, os.path.join(folder, *dp.LOCAL_STORAGE_LEAF,
                                        "tmp"))
    os.utime(sub, (9_000_000, 9_000_000))
    assert dp._evidence(folder).other_mtime == 1_000_000


def test_evidence_never_opens_a_file(win, tmp_path, monkeypatch):
    _cache(tmp_path, _roaming(tmp_path))
    _ldb(tmp_path, _roaming(tmp_path), "ls")
    _ldb(tmp_path, _roaming(tmp_path), "idb")
    opened = []
    real = open
    monkeypatch.setattr("builtins.open", lambda *a, **k: opened.append(a)
                        or real(*a, **k))
    dp._evidence(_roaming(tmp_path))
    dp.resolve()
    assert opened == []


def test_a_new_target_root_is_looked_at_and_followed(win, tmp_path,
                                                      monkeypatch):
    """`looked` and the candidates use one list of roots (review, cut
    items): a root added once is in both, or the test fails."""
    extra = os.path.join(_local(tmp_path), "ExtraRoot")
    real = dp._target_roots
    monkeypatch.setattr(dp, "_target_roots",
                        lambda *a: real(*a) + [extra])
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, os.path.join(extra, NAME))
    answer = dp.resolve()
    assert answer.path == os.path.join(extra, NAME)
    assert os.path.join(extra, NAME) in answer.looked


def test_a_package_admitted_on_indexeddb_alone_is_installed(win, tmp_path,
                                                             monkeypatch):
    """Review of PR #3, #8: the resolver admitted a package on its IndexedDB
    folder alone and read_desk said "not installed" beside "data found in
    the Store package folder"."""
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path), "IndexedDB"))
    assert dp.resolve().source == PKG_SOURCE_A
    assert cli.desktop_app_installed()
    assert cli.read_desk(version=(None, None)).desktop_installed


def test_status_does_not_say_not_installed_for_an_indexeddb_package(
        win, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "claude_on_path", lambda: False)
    _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path), "IndexedDB"))
    desk = cli.read_desk(version=(None, None))
    row = _desktop_row_of(desk, capsys)
    assert row[0] == ("Desktop     no usage cache (Claude Desktop installed"
                      " but never signed in)")
    assert not any("not installed" in l for l in row)


# --- review of PR 3, finding 13: one answer for the whole status run -------

@pytest.mark.parametrize("wire", [False, True])
def test_status_reads_its_session_row_from_the_desktop_rows_folder(
        tmp_path, monkeypatch, wire):
    """read_desk's answer used to be held only for read_desk and the Desktop
    row; _claude_page's Local Storage read and the --wire bus poll each
    resolved again, so a tree that changed in between put the Session row on
    another folder than the Desktop row named. The tree is simulated by a
    resolver that names a new folder on every call."""
    import argparse
    n = []

    def resolver():
        n.append(1)
        return dp.Answer(str(tmp_path / ("folder%d" % len(n))),
                         dp.SOURCE_DEFAULT)

    monkeypatch.setattr(dp, "_resolve", resolver)
    with dp.one_answer() as held:
        pass
    desk = cli.Desk(
        claude_text=None, claude_ver=None, on_path=False, desktop=False,
        desktop_installed=False, desktop_reading=None, codex=False,
        hooks=cli.claude_hooks(), weekly=False, desktop_paths=held)
    monkeypatch.setattr(cli, "read_desk", lambda *a, **k: desk)
    seen = []

    def page(*_a, **_k):
        # What the Local Storage read and the bus poll both ask.
        seen.append(dp.data_dir())
        return (None, None)

    monkeypatch.setattr(cli, "_claude_page", page)
    monkeypatch.setattr(cli, "_wire_line", lambda: page())
    monkeypatch.setattr(cli, "_session_window_line", lambda *a, **k: None)
    monkeypatch.setattr(cli, "_codex_session_line", lambda *a, **k: None)
    cli.cmd_status(argparse.Namespace(wire=wire))
    assert seen == [held.path]


# --- final review of PR #3, 2026-11-05 ---------------------------------------

def test_a_cache_holders_leveldb_folders_are_never_listed(win, tmp_path,
                                                          monkeypatch):
    """A folder with the cache is ranked by the cache alone, so listing its
    two leveldb folders is cost with no effect on the answer. Both the
    helper and a whole resolve must leave them alone, while a folder
    without the cache is still listed (the ranking needs it)."""
    held = _roaming(tmp_path)
    _cache(tmp_path, held, mtime=1_000_000)
    _ldb(tmp_path, held, "ls", mtime=2_000_000)
    _ldb(tmp_path, held, "idb", mtime=2_000_000)
    bare = _pkg_roaming(tmp_path)
    _ldb(tmp_path, bare, "ls", mtime=3_000_000)
    listed = []
    real = dp._newest_file
    monkeypatch.setattr(dp, "_newest_file",
                        lambda folder: listed.append(folder) or real(folder))
    ev = dp._evidence(held)
    assert listed == [] and ev.has_cache and ev.other_mtime == float("-inf")
    dp.resolve()
    assert all(not f.startswith(held) for f in listed)
    assert any(f.startswith(bare) for f in listed)


@pytest.mark.skipif(not hasattr(os, "symlink") or sys.platform == "win32",
                    reason="needs an unprivileged symlink")
def test_a_symlink_is_not_a_file_write(win, tmp_path):
    """_newest_file stats with follow_symlinks=False and counts only real
    files: a link to a fresh file elsewhere is not the app writing here."""
    folder = _pkg_roaming(tmp_path)
    _ldb(tmp_path, folder, "ls", mtime=1_000_000)
    outside = _touch(tmp_path, os.path.join(str(tmp_path), "elsewhere.log"),
                     9_000_000)
    ls = os.path.join(folder, *dp.LOCAL_STORAGE_LEAF)
    os.symlink(outside, inside(tmp_path, os.path.join(ls, "link.log")))
    assert dp._newest_file(ls) == (1_000_000, False)


def test_a_pin_inside_an_outer_hold_is_ignored(win, tmp_path):
    """The docstring's promise: the outer answer is yielded, not the pin."""
    _cache(tmp_path, _roaming(tmp_path))
    pinned = dp.Answer(_pkg_roaming(tmp_path), "package:x")
    with dp.one_answer() as outer:
        with dp.one_answer(pinned) as got:
            assert got is outer and got is not pinned
        assert dp.windows_data_dir()[0] == _roaming(tmp_path)


# --- review of PR #3, 2026-11-06: failures said, not swallowed ---------------

def _refuse(monkeypatch, name, target):
    """Make os.<name>(target) raise PermissionError, everything else real.
    A permission error simulated rather than made with chmod: root and
    Windows ACLs both ignore a mode bit, and the test must mean the same
    everywhere."""
    real = getattr(os, name)

    def refusing(path, *a, **k):
        if os.fspath(path) == target:
            raise PermissionError(13, "refused")
        return real(path, *a, **k)
    monkeypatch.setattr(os, name, refusing)


def test_a_failed_lookup_carries_the_exception_class(win, tmp_path,
                                                     monkeypatch):
    def broken():
        raise TypeError("C:\\Users\\someone\\secret")
    monkeypatch.setattr(dp, "_resolve_windows", broken)
    answer = dp.resolve()
    assert answer.resolve_failed and answer.source == "roaming"
    assert answer.error == "TypeError"


def test_the_daemon_logs_a_failed_lookup_once_without_a_path(
        win, tmp_path, monkeypatch, capsys):
    """The fallback repeats every poll; the log says so once, by class name.
    The exception's message holds a path, and a path holds the account
    name, so it must not reach the log."""
    from pc import ingest

    def broken():
        raise TypeError("C:\\Users\\someone\\secret")
    monkeypatch.setattr(dp, "_resolve_windows", broken)
    bus = ingest.IngestionBus(providers=[], now=lambda: 1_000_000)
    bus.poll()
    bus.poll()
    err = capsys.readouterr().err
    assert err.count("Desktop folder lookup failed (TypeError)") == 1
    assert "someone" not in err and "secret" not in err


def test_the_daemon_logs_the_folder_source_when_it_changes(
        win, tmp_path, capsys):
    from pc import ingest
    bus = ingest.IngestionBus(providers=[], now=lambda: 1_000_000)
    bus.poll()
    assert capsys.readouterr().err.count("Desktop data folder: roaming") == 1
    bus.poll()
    assert capsys.readouterr().err == ""
    _cache(tmp_path, _pkg_roaming(tmp_path))
    bus.poll()
    err = capsys.readouterr().err
    assert f"Desktop data folder: {PKG_SOURCE_A}" in err
    assert str(tmp_path) not in err


def test_off_windows_the_one_folder_is_not_logged(tmp_path, capsys):
    from pc import ingest
    bus = ingest.IngestionBus(providers=[], now=lambda: 1_000_000)
    bus.poll()
    assert capsys.readouterr().err == ""


def test_a_refused_cache_is_unreadable_not_absent(win, tmp_path,
                                                  monkeypatch):
    """isfile() said False for a cache it was refused, so the live package
    folder dropped to "no cache" and an older Roaming cache won with no
    trace. It may still lose, but status now names it."""
    _cache(tmp_path, _roaming(tmp_path), mtime=1_000_000)
    live = _pkg_roaming(tmp_path)
    _cache(tmp_path, live, mtime=2_000_000)
    _refuse(monkeypatch, "stat",
            os.path.join(live, "plan-usage-history.json"))
    answer = dp.resolve()
    assert answer.path == _roaming(tmp_path)
    assert live in answer.unreadable


def test_a_refused_leveldb_listing_is_unreadable(win, tmp_path, monkeypatch):
    folder = _pkg_roaming(tmp_path)
    ls = os.path.join(folder, *dp.LOCAL_STORAGE_LEAF)
    _ldb(tmp_path, folder, "ls", mtime=1_000_000)
    _refuse(monkeypatch, "scandir", ls)
    assert dp._newest_file(ls) == (float("-inf"), True)
    assert folder in dp.resolve().unreadable


def test_an_absent_folder_is_not_unreadable(win, tmp_path):
    missing = os.path.join(str(tmp_path), "nowhere")
    assert dp._newest_file(missing) == (float("-inf"), False)
    assert not dp._evidence(missing).unreadable
    assert dp.resolve().unreadable == ()


def test_a_file_gone_between_listing_and_stat_is_skipped(tmp_path,
                                                         monkeypatch):
    """A leveldb compaction deletes files all the time; one that goes
    between the listing and its stat must not cost the rest of the folder
    its evidence."""
    folder = str(tmp_path / "ldb")
    kept = _touch(tmp_path, os.path.join(folder, "000005.log"), 2_000_000)

    class Gone:
        name = "000004.log"

        def is_file(self, follow_symlinks=True):
            return True

        def stat(self, follow_symlinks=True):
            raise FileNotFoundError(2, "gone")

    real = os.scandir

    class Listing:
        def __init__(self, path):
            self._it = real(path)

        def __enter__(self):
            return iter([Gone()] + list(self._it))

        def __exit__(self, *exc):
            self._it.close()

    monkeypatch.setattr(os, "scandir", Listing)
    assert dp._newest_file(folder) == (os.path.getmtime(kept), False)


def test_an_unlistable_packages_folder_is_not_called_absent(
        win, tmp_path, monkeypatch, capsys):
    root = _mkdir(tmp_path, os.path.join(_local(tmp_path), "Packages"))
    _refuse(monkeypatch, "listdir", root)
    answer = dp.resolve()
    assert answer.packages == () and root in answer.unreadable
    row = _desktop_row(capsys)
    assert f"            could not be read: {root}" in row
    assert not any("no Store package folder" in l for l in row)


def test_a_missing_packages_folder_is_still_said_plainly(win, tmp_path,
                                                         capsys):
    row = _desktop_row(capsys)
    assert any("no Store package folder under" in l for l in row)
    assert not any("could not be read" in l for l in row)


def test_status_names_a_newer_folder_that_has_no_cache_yet(win, tmp_path,
                                                           capsys):
    """Roaming's old exe-build cache is read over a Store folder with newer
    app files and no cache: the rule. Status says so, or a panel that never
    moves has no visible reason."""
    _cache(tmp_path, _roaming(tmp_path), mtime=1_000_000)
    store = _pkg_roaming(tmp_path)
    _ldb(tmp_path, store, "ls", mtime=2_000_000)
    answer = dp.resolve()
    assert answer.path == _roaming(tmp_path)
    assert answer.newer_without_cache == (store,)
    row = _desktop_row(capsys)
    assert f"            newer app files, no usage cache yet: {store}" in row


def test_an_older_folder_without_a_cache_is_not_called_newer(win, tmp_path):
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    _ldb(tmp_path, _pkg_roaming(tmp_path), "ls", mtime=1_000_000)
    assert dp.resolve().newer_without_cache == ()


def test_one_failed_store_lookup_does_not_hide_the_other(win, tmp_path,
                                                         monkeypatch):
    """Both paths used to be built in one tuple inside one try: an IndexedDB
    lookup that raised threw Local Storage's evidence away with it."""
    _mkdir(tmp_path, os.path.join(_roaming(tmp_path), "Local Storage"))

    def broken():
        raise OSError("x")
    monkeypatch.setattr(desktop_idb, "store_path", broken)
    assert cli.desktop_app_installed()
    monkeypatch.setattr(desktop_local_storage, "store_path", broken)
    assert not cli.desktop_app_installed()


def test_a_marker_name_breaks_a_tie_before_the_path(win, tmp_path):
    """Two marker targets with equal caches. OTHER sorts first by name but
    sits under the package's virtualised Local, which sorts after the real
    Claude-Data by path, so only the name tie-break picks OTHER."""
    _marker(tmp_path, _roaming(tmp_path), NAME)
    _marker(tmp_path, _roaming(tmp_path), OTHER)
    real = _target(tmp_path, NAME)
    virt = os.path.join(_local(tmp_path), "Packages", PKG_A, "LocalCache",
                        "Local", "Claude-Data", OTHER)
    _mkdir(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, real, mtime=1_000_000)
    _cache(tmp_path, virt, mtime=1_000_000)
    assert sorted([real, virt]) == [real, virt]
    assert dp.windows_data_dir() == (virt, "marker:" + OTHER)
