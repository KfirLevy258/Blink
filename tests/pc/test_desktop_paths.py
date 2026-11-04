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
    assert "            Store build marker ignored, Roaming is newer" in row
    assert not any("moved by" in l or "is gone" in l for l in row)


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


def test_freshness_counts_indexeddb_on_its_own(win, tmp_path):
    """Review of 1.4.0 (tests and docs), N3/D9. The test above is named for
    Local Storage and IndexedDB and only ever touches Local Storage, so
    dropping IndexedDB from _freshness() passed the suite. Here the live side
    has nothing but an IndexedDB folder: no usage cache, no Local Storage."""
    for name in (NAME, OTHER):
        _marker(tmp_path, _roaming(tmp_path), name)
    _cache(tmp_path, _target(tmp_path, OTHER), mtime=1_000_000)
    idb = _mkdir(tmp_path, os.path.join(_target(tmp_path, NAME), "IndexedDB"))
    os.utime(idb, (2_000_000, 2_000_000))
    assert dp._freshness(_target(tmp_path, NAME)) == 2_000_000
    assert dp.windows_data_dir()[0] == _target(tmp_path, NAME)


def test_an_old_indexeddb_does_not_beat_a_newer_cache(win, tmp_path):
    for name in (NAME, OTHER):
        _marker(tmp_path, _roaming(tmp_path), name)
    _cache(tmp_path, _target(tmp_path, OTHER), mtime=2_000_000)
    idb = _mkdir(tmp_path, os.path.join(_target(tmp_path, NAME), "IndexedDB"))
    os.utime(idb, (1_000_000, 1_000_000))
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
# holds Desktop data (P), and plain Roaming\Claude (R). The freshest
# usage-cache / Local Storage / IndexedDB mtime wins. Ties: M, then P, then R
# (R must be strictly fresher to beat anything, as it always had to beat M).
# Between M's the name decides; between P's the package name decides.
# Nothing holding data at all: R, the default.
#
# One exclusion (review, 2026-11-04): a package folder that carries a MARKER
# whose target exists is not a candidate. It is the copy Desktop moved away
# from, and a directory mtime bumped by a cleanup must not let it beat the
# live target. A package with no marker, or whose marker dangles, competes.
# The marker in real Roaming says nothing about a package folder.

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
    # id,                          R,    M,    A,    B,    want, want if the marker sits in A's folder
    ("package alone",              None, None, 5,    None, "A",  "A"),
    ("roaming alone",              5,    None, None, None, "R",  "R"),
    ("nothing at all",             None, None, None, None, "R",  "R"),
    ("package fresher than R",     1,    None, 2,    None, "A",  "A"),
    ("package stale beside R",     2,    None, 1,    None, "R",  "R"),
    ("package ties R",             3,    None, 3,    None, "A",  "A"),
    ("two packages, B fresher",    None, None, 1,    2,    "B",  "B"),
    ("two packages, A fresher",    None, None, 2,    1,    "A",  "A"),
    ("two packages tie",           None, None, 2,    2,    "B",  "B"),
    ("M fresher than package",     None, 2,    1,    None, "M",  "M"),
    ("package fresher than M",     None, 1,    2,    None, "A",  "M"),
    ("M ties package",             None, 2,    2,    None, "M",  "M"),
    ("M stale, R fresher",         3,    1,    None, None, "R",  "R"),
    ("M fresh, R stale",           1,    3,    None, None, "M",  "M"),
    ("M ties R",                   2,    2,    None, None, "M",  "M"),
    ("R freshest of all three",    9,    2,    3,    None, "R",  "R"),
    ("M freshest of all three",    1,    9,    3,    None, "M",  "M"),
    ("package freshest of three",  1,    2,    9,    None, "A",  "M"),
    ("R ties the best package",    4,    2,    4,    None, "A",  "R"),
    ("B beats A beats M beats R",  1,    2,    3,    4,    "B",  "B"),
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
    _, r, m, a, b, want, _ = row
    _scene(tmp_path, r, m, a, b)
    assert dp.windows_data_dir() == _expected(want, tmp_path)


@pytest.mark.parametrize("row", ROWS, ids=[r[0] for r in ROWS])
def test_the_same_contest_with_the_marker_in_the_package(win, tmp_path, row):
    """A marker may sit in the package's Roaming instead (Mor Gal's layout
    had it in both). Then package A is the copy Desktop moved away from, so
    it does not compete while the marker's target exists: the last column
    differs from the first table exactly where A used to win against M.
    Package B has no marker and still competes."""
    _, r, m, a, b, _, want = row
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
    """The 11 seconds between 23:23:27 (marker) and 23:23:38 (data): the
    marker names a folder that is not there yet, and the package still holds
    the data. 1.4.1 fell back to an absent Roaming here."""
    folder = _friends_layout(tmp_path)
    _marker(tmp_path, _pkg_roaming(tmp_path))
    answer = dp.resolve()
    assert (answer.path, answer.source) == (folder, PKG_SOURCE_A)
    assert answer.dangling and not answer.ignored


@pytest.mark.parametrize("package_mtime,target_mtime,want", [
    (1_000_000, 2_000_000, "M"),    # copied: the target is the newer write
    (2_000_000, 2_000_000, "M"),    # moved with its mtime kept: a tie
    (2_000_000, 1_000_000, "M"),    # a leftover touched after the move
], ids=["target newer", "tie", "package newer"])
def test_both_copies_at_once_the_marked_package_is_the_leftover(
        win, tmp_path, package_mtime, target_mtime, want):
    """(c) The instant of the move, both copies on disk, the marker in the
    package's own folder: the package copy is what Desktop is leaving, so the
    target answers however the mtimes fall."""
    _friends_layout(tmp_path, mtime=package_mtime)
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=target_mtime)
    assert dp.windows_data_dir() == _expected(want, tmp_path)


@pytest.mark.parametrize("package_mtime,target_mtime,want", [
    (1_000_000, 2_000_000, "M"),
    (2_000_000, 2_000_000, "M"),
    (2_000_000, 1_000_000, "A"),
], ids=["target newer", "tie", "package newer"])
def test_both_copies_with_the_marker_only_in_roaming_pick_the_fresher(
        win, tmp_path, package_mtime, target_mtime, want):
    """The marker is in real Roaming and the package folder carries none, so
    nothing says the package copy was left behind: freshness decides, and a
    tie goes to the folder the app pointed at."""
    _friends_layout(tmp_path, mtime=package_mtime)
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=target_mtime)
    assert dp.windows_data_dir() == _expected(want, tmp_path)


def test_a_bumped_leftover_directory_does_not_beat_the_live_target(
        win, tmp_path):
    """The reviewer's reproduction (2026-11-04). Marker in the package's own
    Roaming, target present with a usage cache; the leftover package copy has
    an IndexedDB folder whose mtime was bumped after the move (a cleanup
    deleting entries under it) so it is newer than anything in the target."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=500_000)
    idb = _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path), "IndexedDB"))
    os.utime(idb, (9_000_000, 9_000_000))
    assert dp._freshness(_pkg_roaming(tmp_path)) > dp._freshness(
        _target(tmp_path))
    answer = dp.resolve()
    assert (answer.path, answer.source) == (_target(tmp_path),
                                            "marker:" + NAME)
    assert answer.package_skipped == (_pkg_roaming(tmp_path),)
    assert not answer.marker_passed_over


def test_a_marker_that_dangles_does_not_exclude_its_package(win, tmp_path):
    """The exclusion needs the target to EXIST: with the marker in the
    package and no data folder yet, the package is the only copy."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_A
    assert answer.package_skipped == ()


def test_a_marker_in_one_package_does_not_exclude_another(win, tmp_path):
    """The exclusion is per folder: A carries the marker, B does not."""
    _marker(tmp_path, _pkg_roaming(tmp_path, PKG_A))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path, PKG_A), mtime=9_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path, PKG_B), mtime=2_000_000)
    answer = dp.resolve()
    assert answer.source == PKG_SOURCE_B
    assert answer.package_skipped == (_pkg_roaming(tmp_path, PKG_A),)
    assert answer.marker_passed_over


def test_a_marker_with_a_different_case_still_excludes(win, tmp_path):
    _marker(tmp_path, _pkg_roaming(tmp_path), name=NAME.upper())
    _cache(tmp_path, _target(tmp_path, NAME.upper()), mtime=1_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=9_000_000)
    assert dp.resolve().source == "marker:" + NAME.upper()


def test_a_package_that_only_holds_the_marker_is_not_called_skipped(
        win, tmp_path):
    """The ordinary after state: the package folder keeps just the marker.
    Nothing was left behind, so status has nothing to say about it."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path))
    answer = dp.resolve()
    assert answer.source == "marker:" + NAME
    assert answer.package_skipped == ()


def test_readme_in_claude_data_is_not_a_data_folder(win, tmp_path):
    """(d) Claude-Data\\README.txt is a file with no marker naming it. It is
    not a candidate, and it does not disturb the package answer."""
    folder = _friends_layout(tmp_path)
    assert not dp.marker_names(os.path.join(_local(tmp_path), "Claude-Data"))
    assert dp.windows_data_dir() == (folder, PKG_SOURCE_A)
    assert dp.resolve().looked.count(
        os.path.join(_local(tmp_path), "Claude-Data", "README.txt")) == 0


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
    assert answer.dangling and not answer.ignored


def test_a_package_that_lost_to_roaming_is_flagged(win, tmp_path):
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    answer = dp.resolve()
    assert answer.source == "roaming"
    assert answer.package_ignored and not answer.ignored


def test_a_marker_that_lost_to_roaming_is_flagged_as_before(win, tmp_path):
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _roaming(tmp_path), mtime=2_000_000)
    answer = dp.resolve()
    assert answer.ignored and not answer.package_ignored


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


def test_a_package_with_a_live_indexeddb_beats_a_stale_cache(win, tmp_path):
    """Freshness is the same three things for a package as for a target."""
    _cache(tmp_path, _roaming(tmp_path), mtime=1_000_000)
    idb = _mkdir(tmp_path, os.path.join(_pkg_roaming(tmp_path), "IndexedDB"))
    os.utime(idb, (2_000_000, 2_000_000))
    assert dp.windows_data_dir()[0] == _pkg_roaming(tmp_path)


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


def test_a_glob_pattern_in_localappdata_does_not_reach_a_neighbour(
        win, tmp_path, monkeypatch):
    """`u[ab]` read as a character class would match the neighbour `ua`, so
    a desk whose profile folder is named that would read somebody else's
    data. The decoy is fresher on purpose."""
    monkeypatch.setenv("LOCALAPPDATA",
                       str(tmp_path / "u[ab]" / "AppData" / "Local"))
    mine = _pkg_roaming(tmp_path)
    _cache(tmp_path, mine, mtime=1_000_000)
    decoy = inside(tmp_path, os.path.join(
        str(tmp_path), "ua", "AppData", "Local", "Packages", PKG_A,
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
    assert "            Store package folder ignored, Roaming is newer" in row
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


# --- review 2026-11-04: what status says when a package and a marker meet ----

def test_status_says_when_a_package_beat_a_marker_target(win, tmp_path,
                                                          capsys):
    _marker(tmp_path, _roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    folder = _pkg_roaming(tmp_path)
    _cache(tmp_path, folder, mtime=2_000_000)
    row = _desktop_row(capsys)
    assert ("            data found in the Store package folder: " + folder
            ) in row
    assert "            Store build marker ignored, package folder is newer" in row
    assert not any("skipped" in l or "Roaming is newer" in l for l in row)


def test_status_says_when_a_leftover_package_folder_was_skipped(
        win, tmp_path, capsys):
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path), mtime=1_000_000)
    _cache(tmp_path, _pkg_roaming(tmp_path), mtime=2_000_000)
    row = _desktop_row(capsys)
    assert ("            data moved by the Store build: " + _target(tmp_path)
            ) in row
    assert ("            Store package folder skipped, its marker says the"
            " data moved") in row
    assert not any("data found in" in l or "marker ignored" in l for l in row)


def test_status_is_quiet_for_the_ordinary_after_state(win, tmp_path, capsys):
    """The package folder holds only the marker: nothing was left behind."""
    _marker(tmp_path, _pkg_roaming(tmp_path))
    _cache(tmp_path, _target(tmp_path))
    row = _desktop_row(capsys)
    assert not any("skipped" in l or "ignored" in l or "data found" in l
                   for l in row)


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
