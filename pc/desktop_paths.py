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

What this adds over the first fix, each for a case it left open:

  - Freshest wins. Two markers with both targets present are possible (an
    install, an uninstall, a reinstall under a new name), and "first in
    sorted order" picks one for no reason. The one written to most recently
    is the one the app is using.
  - A stale marker does not win for ever. Someone who went back to the exe
    build has a marker and an old Claude-Data left behind while the app
    writes Roaming again. When Roaming is strictly fresher than the best
    target, Roaming is read.
  - Names are checked hard before they are joined into a path: no
    separators, no `:` (ntpath.join(local, "Claude-Data", "D:evil") is
    "D:evil", a drive reset), no `.` or `..`, nothing Windows would silently
    trim, and the joined result must sit directly under Claude-Data. NTFS
    should never list a name that fails this, which is why it is cheap
    insurance rather than a fix.

Everything resolves when called. Nothing touches home at import
(tests/test_sandbox.py enforces that), and the resolver never raises into
the daemon: any failure falls back to Roaming. It runs once per poll, every
60 s; the listdir and the glob cost nothing worth caching.

Nothing here opens a file. It lists names and reads mtimes, and logs neither.
"""
import glob
import os
import sys

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
    # marker named.
    if name.strip(" .") != name:
        return False
    joined = pathmod.normpath(pathmod.join(root, name))
    return pathmod.dirname(joined) == pathmod.normpath(root)


def marker_names(folder):
    """The names `.user-data-left.<name>` files in `folder` point at.

    Matched without regard to case, because Windows names are. A name that
    fails _safe_component is dropped here, so no caller ever joins one.
    """
    try:
        entries = os.listdir(folder)
    except OSError:
        return []
    names = []
    prefix = MARKER_PREFIX.lower()
    for entry in entries:
        if not entry.lower().startswith(prefix):
            continue
        name = entry[len(MARKER_PREFIX):]
        if name in names:
            continue
        # Checked against a relative root here and again against each real
        # root in _candidate_targets: the name alone, then the join.
        if _safe_component(name, RELOCATED_DIRNAME):
            names.append(name)
    return names


def _marker_folders(roaming_claude, local):
    """Where markers are looked for: real Roaming first, then the Store
    package's virtualised Roaming, in sorted order."""
    pattern = os.path.join(glob.escape(local), "Packages", "Claude_*",
                           "LocalCache", "Roaming", "Claude")
    return [roaming_claude] + sorted(glob.glob(pattern))


def _candidate_targets(names, local):
    """[(name, folder)] for each marker name whose data folder exists.

    The real %LOCALAPPDATA%\\Claude-Data is what was measured. The package's
    virtualised Local is UNMEASURED: no build has been seen writing there,
    but the Store can redirect Local the same way it redirected Roaming, and
    looking costs one glob.
    """
    out = []
    seen = set()
    for name in names:
        roots = [os.path.join(local, RELOCATED_DIRNAME)]
        pattern = os.path.join(glob.escape(local), "Packages", "Claude_*",
                               "LocalCache", "Local", RELOCATED_DIRNAME)
        roots += sorted(glob.glob(pattern))
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


def _resolve_windows():
    """(path, source, dangling) -- dangling is True when a marker was found
    but none of its data folders exists."""
    roaming, local = _windows_roots()
    names = []
    for folder in _marker_folders(roaming, local):
        for name in marker_names(folder):
            if name not in names:
                names.append(name)
    if not names:
        return roaming, SOURCE_ROAMING, False
    targets = _candidate_targets(names, local)
    if not targets:
        return roaming, SOURCE_ROAMING, True
    # Freshest first; equal freshness falls back to name, then path, so the
    # same tree always gives the same answer.
    targets.sort(key=lambda t: (-_freshness(t[1]), t[0], t[1]))
    name, folder = targets[0]
    if _freshness(roaming) > _freshness(folder):
        # The exe build writing Roaming again beside a marker it left behind.
        return roaming, SOURCE_ROAMING, False
    return folder, SOURCE_MARKER_PREFIX + name, False


def _windows_answer():
    try:
        return _resolve_windows()
    except Exception:
        # Never into the daemon. Roaming is where every build before the
        # relocation wrote, so it is the least surprising wrong answer.
        return _windows_roots()[0], SOURCE_ROAMING, False


def windows_data_dir():
    """(path, source) on Windows: source is "roaming" or "marker:<name>"."""
    path, source, _ = _windows_answer()
    return path, source


def data_dir():
    """Claude Desktop's data folder on this platform, whether or not it
    exists -- absence is a normal state, handled by the readers."""
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude")
    if os.name == "nt":
        return windows_data_dir()[0]
    return os.path.expanduser("~/.config/Claude")


def data_dir_source():
    """How data_dir() was chosen, for `blink status`: "roaming",
    "marker:<name>", or "default" off Windows."""
    if sys.platform == "darwin" or os.name != "nt":
        return SOURCE_DEFAULT
    return windows_data_dir()[1]


def marker_without_data():
    """True on Windows when a Store-build marker names a data folder that is
    not there. Status says so: it is what a bug report needs."""
    if sys.platform == "darwin" or os.name != "nt":
        return False
    return _windows_answer()[2]


def usage_cache_path():
    return os.path.join(data_dir(), USAGE_CACHE_NAME)


def local_storage_dir():
    return os.path.join(data_dir(), *LOCAL_STORAGE_LEAF)


def indexeddb_dir():
    return os.path.join(data_dir(), *INDEXEDDB_LEAF)


def cowork_sessions_dir():
    return os.path.join(data_dir(), COWORK_SESSIONS_NAME)
