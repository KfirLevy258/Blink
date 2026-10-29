"""Where Claude Desktop keeps its user data on this platform.

One answer, shared by every reader of Desktop's files -- the usage cache,
Local Storage, IndexedDB and the Cowork session logs all live under the same
folder, and each of them once computed it separately, so all four went blind
together when the folder moved.

It moved on Windows. The Microsoft Store (MSIX) build relocates its data out
of %APPDATA%\\Claude to %LOCALAPPDATA%\\Claude-Data\\<name> and leaves only an
empty marker behind, `.user-data-left.<name>`. Measured on Windows 11, Desktop
3.6556.2.0 from the Store, 2026-10-29: %APPDATA%\\Claude held the marker
`.user-data-left.claude_pzs8sxrjxfjjc` and nothing else, and
%LOCALAPPDATA%\\Claude-Data\\claude_pzs8sxrjxfjjc held plan-usage-history.json,
Local Storage, IndexedDB and local-agent-mode-sessions. The package's own
virtualised Roaming folder (Packages\\Claude_*\\LocalCache\\Roaming\\Claude)
carried the same marker, so it is checked too.

The marker is followed rather than the Claude-Data folder being guessed at:
the suffix names the folder exactly, and a leftover Claude-Data from an
uninstalled build is not a reason to stop reading a live %APPDATA%\\Claude.
"""
import glob
import os
import sys

MARKER_PREFIX = ".user-data-left."
RELOCATED_ROOT = "Claude-Data"


def _roaming() -> str:
    # Fall back rather than crash: a Windows session with no APPDATA is
    # broken in ways that are not ours to fix, but it must not take the
    # daemon with it.
    appdata = os.environ.get("APPDATA") or os.path.expanduser(
        "~\\AppData\\Roaming")
    return os.path.join(appdata, "Claude")


def _local() -> str:
    return os.environ.get("LOCALAPPDATA") or os.path.expanduser(
        "~\\AppData\\Local")


def _marker_names(folder):
    """The relocation targets named by markers in `folder`, if any."""
    try:
        entries = os.listdir(folder)
    except OSError:
        return []
    names = []
    for e in sorted(entries):
        if not e.startswith(MARKER_PREFIX):
            continue
        name = e[len(MARKER_PREFIX):]
        # The name becomes a path component. Anything that could step out
        # of Claude-Data is not a marker we wrote, so it is not followed.
        if not name or name in (".", "..") or "/" in name or "\\" in name:
            continue
        names.append(name)
    return names


def _relocated_dir(roaming):
    """%LOCALAPPDATA%\\Claude-Data\\<name> when a marker points there and it
    exists, else None."""
    local = _local()
    folders = [roaming] + sorted(glob.glob(os.path.join(
        local, "Packages", "Claude_*", "LocalCache", "Roaming", "Claude")))
    for folder in folders:
        for name in _marker_names(folder):
            target = os.path.join(local, RELOCATED_ROOT, name)
            if os.path.isdir(target):
                return target
    return None


def data_dir() -> str:
    """Claude Desktop's user-data folder.

    Returned whether or not it exists: absence is a normal state (the app may
    simply not be installed) and is handled by each reader.
    """
    if sys.platform == "darwin":
        return os.path.expanduser("~/Library/Application Support/Claude")
    if os.name == "nt":
        roaming = _roaming()
        try:
            relocated = _relocated_dir(roaming)
        except Exception:
            relocated = None
        return relocated or roaming
    return os.path.expanduser("~/.config/Claude")
