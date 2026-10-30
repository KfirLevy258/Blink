"""Where Claude Desktop keeps its data folder, on every platform.

One module, because four readers look inside that folder -- the usage cache
(pc/providers/claude_desktop), Local Storage (pc/desktop_local_storage),
IndexedDB (pc/desktop_idb) and the Cowork session audits (pc/cowork_audit)
-- and each used to work the folder out for itself. When the Microsoft Store
build of Desktop moved it, all four went blind together: no five-hour figure,
no weekly anchor, and `blink status` saying Desktop was not installed on a
desk where it was open.

Found and measured by Mor Gal (Mr-M33533K5/Blink), PR #3, 2026-10-29, on
Windows 11 with Claude Desktop 3.6556.2.0 from the Store:

  - %APPDATA%\\Claude held one thing, an empty file named
    `.user-data-left.claude_pzs8sxrjxfjjc`.
  - %LOCALAPPDATA%\\Claude-Data\\claude_pzs8sxrjxfjjc held what used to be in
    Roaming: plan-usage-history.json, Local Storage, IndexedDB and
    local-agent-mode-sessions.
  - The package's own virtualised Roaming,
    %LOCALAPPDATA%\\Packages\\Claude_*\\LocalCache\\Roaming\\Claude, carried
    the same marker.

So the marker is the app's own record of where its data went, and that is
what this follows. It does not guess at Claude-Data: a Claude-Data folder
with no marker pointing at it is ignored.

This is not the only Store layout. On 2026-09-30 an ARM64 Windows 11 VM ran
an MSIX build that wrote straight into the real %APPDATA%\\Claude, with no
marker (docs/README-full.md). That is an older build, but a desk that has not
updated still has it, so plain Roaming stays the default and the answer
whenever nothing better is found.

How the folder is chosen:

  - Freshest wins. Two markers with both targets present are possible (an
    install, an uninstall, a reinstall under a new name), and sorted order
    would pick one for no reason. The one written to most recently -- usage
    cache, Local Storage or IndexedDB -- is the one the app is using. Equal
    freshness falls back to the name, so one tree always gives one answer.
  - A stale marker does not win for ever. Someone who went back to the exe
    build has a marker and an old Claude-Data left behind while the app
    writes Roaming again. When Roaming is strictly fresher than the best
    target, Roaming is read, and `blink status` says the marker was ignored.
  - Two live installs (the exe and the Store build both in use, in turn)
    therefore alternate: the reading follows whichever wrote last. That is
    by design -- both report the same account, and the last one written
    holds the newer figure.
  - Names are checked hard before they are joined into a path: no
    separators, no `:` (ntpath.join(local, "Claude-Data", "D:evil") is
    "D:evil", a drive reset), no NUL, no `.` or `..`, nothing Windows would
    silently trim, and the joined result must sit directly under
    Claude-Data. NTFS should never list a name that fails this, which is why
    it is cheap insurance rather than a fix.

Everything resolves when called. Nothing touches home at import
(tests/test_sandbox.py enforces that), and the resolver never raises into
the daemon: any failure falls back to Roaming.

What it costs, and why one_answer() exists. The daemon polls every 2 s
(claude_usage_bridge.FAST_POLL_INTERVAL_S), on the thread that reads the
board's serial port, and one poll asked for the folder four times: the
Desktop cache, Local Storage, the weekly history and the anchor seeders. A
resolve is a listdir of %APPDATA%\\Claude, one glob of
%LOCALAPPDATA%\\Packages\\Claude_*, a listdir of each package's Roaming, and
a few stats. Measured 2026-10-30 on macOS APFS, warm, against a synthetic
profile with 400 Packages entries in the Store layout: 0.39 ms per resolve,
1.6 ms a poll, while the virtualised-Local glob still ran once per marker
name; 0.20 ms per resolve with one Packages glob, and one resolve a poll.
NTFS with Defender is unmeasured and will be several times that. So
pc/ingest holds one answer for the whole poll
(one_answer()), which also means the four readers in a poll cannot straddle
an mtime change and read two different folders; `blink status` holds one
the same way, so its lines cannot contradict each other. Outside a hold
every call resolves afresh. There is no timed cache, so nothing goes stale
under a test that moves HOME between calls.

Nothing here opens a file. It lists names and reads mtimes, and logs neither.
"""
import contextlib
import glob
import os
import sys
import threading
from typing import NamedTuple

MARKER_PREFIX = ".user-data-left."
RELOCATED_DIRNAME = "Claude-Data"

USAGE_CACHE_NAME = "plan-usage-history.json"
LOCAL_STORAGE_LEAF = ("Local Storage", "leveldb")
INDEXEDDB_LEAF = ("IndexedDB", "https_claude.ai_0.indexeddb.leveldb")
COWORK_SESSIONS_NAME = "local-agent-mode-sessions"

SOURCE_ROAMING = "roaming"
SOURCE_DEFAULT = "default"
SOURCE_MARKER_PREFIX = "marker:"

# Characters no marker name may hold. "/" and "\\" would leave Claude-Data;
# ":" resets the drive under ntpath, and also names an NTFS alternate data
# stream; NUL ends the string early for the OS.
_BAD_CHARS = ("/", "\\", ":", "\0")


def _windows_roots():
    """(%APPDATA%\\Claude, %LOCALAPPDATA%), each with the profile fallback.

    A Windows session with no APPDATA is broken in ways that are not ours to
    fix, but it must not take the daemon with it -- the same fallback the
    four readers had before this module.
    """
    appdata = os.environ.get("APPDATA") or os.path.expanduser(
        "~\\AppData\\Roaming")
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser(
        "~\\AppData\\Local")
    return os.path.join(appdata, "Claude"), local


def _safe_component(name, root, pathmod=os.path):
    """Is `name` one plain folder name that stays directly under `root`?

    `pathmod` is os.path in production. The tests hand it ntpath, so the
    drive-reset case is proved on a Mac too, where posixpath would join
    "D:evil" harmlessly and prove nothing.
    """
    if not name or name in (".", ".."):
        return False
    if any(c in name for c in _BAD_CHARS):
        return False
    # Windows drops trailing dots and spaces from a path component, so
    # "name." and "name " open "name" -- a different folder from the one the
    # marker named. Leading ones are refused too: Windows keeps them, but no
    # Store name has been seen with one, and refusing costs nothing.
    if name.strip(" .") != name:
        return False
    # The backstop. Every name the checks above let through already stays
    # under root, so this decides nothing today; it is here so that loosening
    # one of them cannot turn a marker into a way out of Claude-Data.
    # tests/pc/test_desktop_paths.py proves it holds on its own.
    joined = pathmod.normpath(pathmod.join(root, name))
    return pathmod.dirname(joined) == pathmod.normpath(root)


def marker_names(folder):
    """The names `.user-data-left.<name>` files in `folder` point at.

    Matched without regard to case, because Windows names are, and
    de-duplicated the same way. A name that fails _safe_component is dropped
    here, so no caller ever joins one.
    """
    try:
        entries = os.listdir(folder)
    except OSError:
        return []
    names = []
    seen = set()
    prefix = MARKER_PREFIX.lower()
    for entry in sorted(entries):
        if not entry.lower().startswith(prefix):
            continue
        name = entry[len(MARKER_PREFIX):]
        if name.lower() in seen:
            continue
        # Checked against a relative root here and again against each real
        # root in _candidate_targets: the name alone, then the join.
        if _safe_component(name, RELOCATED_DIRNAME):
            seen.add(name.lower())
            names.append(name)
    return names


def _packages(local):
    """Claude's Store package folders, sorted. One glob per resolve: both the
    virtualised Roaming and the virtualised Local hang off these. Escaped,
    because a profile folder can have `[` in its name, which glob would read
    as a character class and then match nothing."""
    pattern = os.path.join(glob.escape(local), "Packages", "Claude_*")
    return sorted(glob.glob(pattern))


def _marker_folders(roaming_claude, packages):
    """Where markers are looked for: real Roaming first, then each Store
    package's virtualised Roaming."""
    return [roaming_claude] + [
        os.path.join(p, "LocalCache", "Roaming", "Claude") for p in packages]


def _candidate_targets(names, local, packages):
    """[(name, folder)] for each marker name whose data folder exists.

    The real %LOCALAPPDATA%\\Claude-Data is what was measured. The package's
    virtualised Local is UNMEASURED: no build has been seen writing there,
    but the Store can redirect Local the same way it redirected Roaming, and
    looking costs one stat per name.
    """
    roots = [os.path.join(local, RELOCATED_DIRNAME)] + [
        os.path.join(p, "LocalCache", "Local", RELOCATED_DIRNAME)
        for p in packages]
    out = []
    seen = set()
    for name in names:
        for root in roots:
            if not _safe_component(name, root):
                continue
            folder = os.path.join(root, name)
            key = os.path.normcase(os.path.normpath(folder))
            if key in seen or not os.path.isdir(folder):
                continue
            seen.add(key)
            out.append((name, folder))
    return out


def _freshness(folder):
    """The newest mtime among the things the app rewrites as it runs: the
    usage cache, the Local Storage leveldb and the IndexedDB folder.
    -inf when none of them is there."""
    best = float("-inf")
    for leaf in ((USAGE_CACHE_NAME,), LOCAL_STORAGE_LEAF, ("IndexedDB",)):
        try:
            best = max(best, os.path.getmtime(os.path.join(folder, *leaf)))
        except OSError:
            continue
    return best


class Answer(NamedTuple):
    """One resolve, with what `blink status` needs to explain it.

    source: "default" off Windows, else "roaming" or "marker:<name>".
    dangling: a marker was found, and none of its data folders exists.
    ignored: a marker's data folder exists, but Roaming is fresher and won.
    """
    path: str
    source: str
    dangling: bool = False
    ignored: bool = False

    @property
    def moved(self) -> bool:
        return self.source.startswith(SOURCE_MARKER_PREFIX)


def _resolve_windows():
    roaming, local = _windows_roots()
    packages = _packages(local)
    names = []
    seen = set()
    for folder in _marker_folders(roaming, packages):
        for name in marker_names(folder):
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    if not names:
        return Answer(roaming, SOURCE_ROAMING)
    targets = _candidate_targets(names, local, packages)
    if not targets:
        return Answer(roaming, SOURCE_ROAMING, dangling=True)
    # Freshest first; equal freshness falls back to name, then path.
    scored = sorted((-_freshness(f), n, f) for n, f in targets)
    neg_fresh, name, folder = scored[0]
    if _freshness(roaming) > -neg_fresh:
        # The exe build writing Roaming again beside a marker it left behind.
        return Answer(roaming, SOURCE_ROAMING, ignored=True)
    return Answer(folder, SOURCE_MARKER_PREFIX + name)


def _resolve():
    if sys.platform == "darwin":
        return Answer(os.path.expanduser(
            "~/Library/Application Support/Claude"), SOURCE_DEFAULT)
    if os.name != "nt":
        return Answer(os.path.expanduser("~/.config/Claude"), SOURCE_DEFAULT)
    try:
        return _resolve_windows()
    except Exception:
        # Never into the daemon. Roaming is where every build before the
        # relocation wrote, so it is the least surprising wrong answer.
        return Answer(_windows_roots()[0], SOURCE_ROAMING)


_held = threading.local()


@contextlib.contextmanager
def one_answer(answer=None):
    """Resolve once, and give every reader inside the block that folder.

    `answer` pins one already taken: status reuses read_desk's. A nested
    hold keeps the outer answer. Per thread, so the serial thread's poll
    never lends its answer to a call on another thread.
    """
    outer = getattr(_held, "answer", None)
    if outer is not None:
        yield outer
        return
    _held.answer = answer if answer is not None else _resolve()
    try:
        yield _held.answer
    finally:
        _held.answer = None


def resolve():
    """The Answer in force: the held one inside one_answer(), else fresh."""
    held = getattr(_held, "answer", None)
    return held if held is not None else _resolve()


def windows_data_dir():
    """(path, source): source is "roaming" or "marker:<name>" on Windows."""
    answer = resolve()
    return answer.path, answer.source


def data_dir():
    """Claude Desktop's data folder on this platform, whether or not it
    exists -- absence is a normal state, handled by the readers."""
    return resolve().path


def data_dir_source():
    """How data_dir() was chosen: "roaming", "marker:<name>", or "default"
    off Windows."""
    return resolve().source


def marker_without_data():
    """True on Windows when a Store-build marker names a data folder that is
    not there."""
    return resolve().dangling


def usage_cache_path():
    return os.path.join(data_dir(), USAGE_CACHE_NAME)


def local_storage_dir():
    return os.path.join(data_dir(), *LOCAL_STORAGE_LEAF)


def indexeddb_dir():
    return os.path.join(data_dir(), *INDEXEDDB_LEAF)


def cowork_sessions_dir():
    return os.path.join(data_dir(), COWORK_SESSIONS_NAME)
