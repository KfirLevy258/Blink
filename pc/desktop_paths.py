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

The second Store layout, measured 2026-11-04 on a friend's Windows PC running
Blink 1.4.1 beside Claude Desktop from the Store: no %APPDATA%\\Claude at
all; %LOCALAPPDATA%\\Claude-Data present but holding only a 1111-byte
README.txt (2026-11-01), no data folder; and the live usage history in
%LOCALAPPDATA%\\Packages\\Claude_pzs8sxrjxfjjc\\LocalCache\\Roaming\\Claude,
with no `.user-data-left` marker anywhere. This is the Store's own
virtualised Roaming used as the data folder, which is what older Store
builds did (anthropics/claude-code issues #26073 and #48362, and the 1.4.0
review that flagged the gap). Until then this module only ever looked INSIDE
a package folder for a marker, never at it as the data folder, so `blink
status` said Desktop was not installed and looked at Roaming alone.

The same evening that Store build (Claude 4.16336.0.0) migrated while watched:
a `.user-data-left.<name>` marker appeared in the package's Roaming at
23:23:27, plan-usage-history.json was written under Claude-Data\\<name> at
23:23:38, and a search of %LOCALAPPDATA% then found only that copy -- the
package copy was moved, not left behind. So one desk passes through all three
states this module reads: package data with no marker, marker plus a data
folder (briefly both copies), and marker plus Claude-Data only.

This is not the only Store layout. On 2026-09-30 an ARM64 Windows 11 VM ran
an MSIX build that wrote straight into the real %APPDATA%\\Claude, with no
marker (docs/README-full.md). That is an older build, but a desk that has not
updated still has it, so plain Roaming stays the default and the answer
whenever nothing better is found.

How the folder is chosen. Three kinds of candidate compete: the target of
a marker (M), each package's LocalCache\\Roaming\\Claude that holds Desktop
data (P), and plain %APPDATA%\\Claude (R). "Holds Desktop data" is the usage
cache FILE, or a Local Storage or IndexedDB folder: a package folder the
Store made on install and the app never wrote to is not a candidate, and a
config file or a marker alone does not make it one. The rules:

  - Freshest wins, among all three kinds, by one measure (_freshness: the
    newest mtime of the usage cache, Local Storage and IndexedDB). A stale
    leftover therefore never beats live data, whichever kind it is.
  - Ties go M, then P, then R: R has always had to be strictly fresher, and
    the app's own marker is a stronger statement than a folder that merely
    holds files. Among several M the name breaks a tie; among several P the
    package folder name does.
  - Nothing holds data: R, the default, which is also what the old code
    answered when a marker named a folder that was gone (`dangling`).
  - A marker whose target is gone does not hide a package that holds data.
  - A package folder that carries a marker whose target EXISTS is not a
    candidate at all: it is what Desktop left behind when it moved, and a
    cleanup that bumps one of its directory mtimes must not let it beat the
    live target. Only a package with no marker (the 2026-11-04 "before"
    state), or whose marker dangles, competes. `blink status` says when such
    a folder was skipped, and when a marker-less package beat a target.
  - Two markers with both targets present are possible (an install, an
    uninstall, a reinstall under a new name), and sorted order would pick
    one for no reason. The one written to most recently -- usage cache,
    Local Storage or IndexedDB -- is the one the app is using. Equal
    freshness falls back to the name, so one tree always gives one answer.
  - A stale marker does not win for ever. Someone who went back to the exe
    build has a marker and an old Claude-Data left behind while the app
    writes Roaming again. When Roaming is strictly fresher than the best
    candidate, Roaming is read, and `blink status` says the marker (or the
    package folder) was ignored.
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
# "package:<folder>": the package folder's own name, e.g.
# "package:Claude_pzs8sxrjxfjjc". A label for `blink status` and for logs of
# which folder was read; it is never joined into a path.
SOURCE_PACKAGE_PREFIX = "package:"

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
    return [roaming_claude] + [_package_roaming(p) for p in packages]


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


def _package_roaming(package):
    return os.path.join(package, "LocalCache", "Roaming", "Claude")


def _holds_data(folder):
    """Does `folder` hold Desktop data: the usage cache as a file, or a Local
    Storage or IndexedDB folder?

    The bar for a package folder to count as a data folder at all. Stricter
    than "exists": the Store creates the package's LocalCache on install, and
    a folder that only ever held a marker or a config file is not where the
    app writes. Looser than _freshness on purpose: a Local Storage folder
    with no leveldb in it yet (signed out, first launch) is still the app's
    folder, and `blink status` should read "installed, never signed in"
    from it. Stats only, never opens a file.
    """
    return (os.path.isfile(os.path.join(folder, USAGE_CACHE_NAME))
            or os.path.isdir(os.path.join(folder, LOCAL_STORAGE_LEAF[0]))
            or os.path.isdir(os.path.join(folder, INDEXEDDB_LEAF[0])))


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

    source: "default" off Windows, else "roaming", "marker:<name>" or
        "package:<package folder name>".
    dangling: a marker was found, and none of its data folders exists.
    ignored: a marker's data folder exists, but Roaming is fresher and won.
    package_ignored: a package folder holds data, but Roaming is fresher
        and won.
    marker_passed_over: a marker's data folder holds data, but a package folder
        with no marker of its own is fresher and won.
    package_skipped: package folders that hold data and were not
        considered, because a marker in the same folder points at a data
        folder that holds data: what is left in them is the copy Desktop moved
        away from.
    packages: the Claude_* package folders found, whether or not they hold
        data.
    roaming, local, names: what `looked` and `packages_root` are worked out
        from, on demand. Empty off Windows.

    `looked` is every data folder tried, the answer's included, in the order
    tried; `blink status` lists them when nothing is found, because naming
    Roaming alone sent us to the wrong place. It is a property and not a
    field on purpose: only status reads it, and the daemon resolves every
    2 s. A reviewer measured the eager list at about a third of what this
    module added to a resolve (2026-11-04). It does no I/O, only string
    work on the held fields, so a held answer still describes one moment.
    """
    path: str
    source: str
    dangling: bool = False
    ignored: bool = False
    package_ignored: bool = False
    marker_passed_over: bool = False
    package_skipped: tuple = ()
    packages: tuple = ()
    roaming: str = ""
    local: str = ""
    names: tuple = ()

    @property
    def moved(self) -> bool:
        return self.source.startswith(SOURCE_MARKER_PREFIX)

    @property
    def in_package(self) -> bool:
        return self.source.startswith(SOURCE_PACKAGE_PREFIX)

    @property
    def packages_root(self) -> str:
        """Where packages were looked for; "" off Windows."""
        return os.path.join(self.local, "Packages") if self.local else ""

    @property
    def looked(self) -> tuple:
        if not self.roaming:
            return ()
        out = [self.roaming] + [_package_roaming(p) for p in self.packages]
        for name in self.names:
            for root in [os.path.join(self.local, RELOCATED_DIRNAME)] + [
                    os.path.join(p, "LocalCache", "Local", RELOCATED_DIRNAME)
                    for p in self.packages]:
                if _safe_component(name, root):
                    out.append(os.path.join(root, name))
        return tuple(out)


def _resolve_windows():
    roaming, local = _windows_roots()
    packages = _packages(local)
    # Marker names, from real Roaming and from each package's Roaming. The
    # package listings are kept: a package whose OWN folder carries a marker
    # is handled differently below.
    names = []
    seen = set()
    package_names = {}
    for folder in _marker_folders(roaming, packages):
        found = marker_names(folder)
        if folder != roaming:
            package_names[folder] = found
        for name in found:
            if name.lower() not in seen:
                seen.add(name.lower())
                names.append(name)
    targets = _candidate_targets(names, local, packages) if names else []
    # A target is LIVE, the app's current copy, only when it holds Desktop
    # data by the same test a package folder must pass. "The directory
    # exists" is not enough: a fresh install creates Claude-Data/<name> with
    # only a README.txt before sign-in, and a move that aborts after mkdir
    # leaves it empty. Counting that as live would make the package folder,
    # the only copy of the data, a skipped leftover, and Desktop would read
    # as "never signed in" for as long as the target stayed empty (review,
    # 2026-11-05). The empty target still ranks (at -inf, so any data beats
    # it, and it still answers "installed, never signed in" when nothing
    # else holds data); it just cannot hide a folder that holds data.
    held = [(n, f) for n, f in targets if _holds_data(f)]
    live = {n.lower() for n, _ in held}
    where = dict(packages=tuple(packages), roaming=roaming, local=local,
                 names=tuple(names))
    # (-freshness, kind, name, folder, source): freshest first; on a tie a
    # marker's target (kind 0) before a package (kind 1); then name, then
    # path, so one tree always gives one answer.
    ranked = [(-_freshness(f), 0, n, f, SOURCE_MARKER_PREFIX + n)
              for n, f in targets]
    skipped = []
    for p in packages:
        folder = _package_roaming(p)
        if not _holds_data(folder):
            continue
        # A marker in this very folder, whose data folder exists, is the
        # app saying "my data left here". What is still in the folder is the
        # copy it moved away from, and a directory mtime bumped by a cleanup
        # (IndexedDB emptied after the move, say) must not make it outrank
        # the live target (review, 2026-11-04). A marker whose target is not
        # there (yet), or is there but holds no data yet, says nothing of
        # the kind: the data has not landed, and this folder is the only
        # copy.
        if any(n.lower() in live for n in package_names.get(folder, ())):
            skipped.append(folder)
            continue
        name = os.path.basename(p)
        ranked.append((-_freshness(folder), 1, name, folder,
                       SOURCE_PACKAGE_PREFIX + name))
    ranked.sort()
    where["package_skipped"] = tuple(skipped)
    dangling = bool(names) and not targets
    if not ranked:
        return Answer(roaming, SOURCE_ROAMING, dangling=dangling, **where)
    neg_fresh, kind, _, folder, source = ranked[0]
    if _freshness(roaming) > -neg_fresh:
        # The exe build writing Roaming again beside a marker (or a package
        # folder) it left behind.
        return Answer(roaming, SOURCE_ROAMING, dangling=dangling,
                      ignored=bool(targets),
                      package_ignored=any(r[1] == 1 for r in ranked),
                      **where)
    return Answer(folder, source, dangling=dangling,
                  marker_passed_over=(kind == 1 and bool(held)), **where)


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
        roaming = _windows_roots()[0]
        return Answer(roaming, SOURCE_ROAMING, roaming=roaming)


_held = threading.local()


@contextlib.contextmanager
def one_answer(answer=None):
    """Resolve once, and give every reader inside the block that folder.

    `answer` pins one already taken: status reuses read_desk's. A nested
    hold keeps the outer answer, and that includes a pinned `answer` passed
    inside an outer hold: the pin is ignored and the outer answer yielded.
    Nothing does that today -- status pins only after read_desk's hold has
    closed, and never inside a poll. Per thread, so the serial thread's poll
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
    """(path, source): source is "roaming", "marker:<name>" or
    "package:<folder>" on Windows."""
    answer = resolve()
    return answer.path, answer.source


def data_dir():
    """Claude Desktop's data folder on this platform, whether or not it
    exists -- absence is a normal state, handled by the readers."""
    return resolve().path


def data_dir_source():
    """How data_dir() was chosen: "roaming", "marker:<name>",
    "package:<folder>", or "default" off Windows."""
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
