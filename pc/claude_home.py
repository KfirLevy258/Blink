"""Where Claude Code's files are: one record, asked by everything that reads
or writes them.

Until 2026-11-04 each module worked this out for itself. cli.settings_path()
was ~/.claude/settings.json whatever CLAUDE_CONFIG_DIR said, though Claude
Code 2.1.316 reads $CLAUDE_CONFIG_DIR/settings.json when it is set (read in
its bundle); burn_reader and claude_plan did honour the variable, but as the
daemon's own environment had it, and a login service started by launchd or
systemd is never handed it. So on a desk that exports the variable in
.zshrc, install wrote the status line and hooks where Claude Code never
looks, while the transcripts and the plan were read from a folder the shell
would not have named (docs/open-bugs/ultra-burn-followups.md).

The fix is to decide once, at `blink install`, where the user is present
and the shell has the variable, and write it down in user_config under
"claude_home". The background service reads the record and not its own
environment. A desk installed by an earlier release has no record, and is
read from the environment exactly as before.

Two kinds. "native" is Claude Code on this computer. "wsl" is Claude Code
inside a WSL distribution while the daemon stays on Windows with the board:
its config_dir is a Windows UNC path into the distribution
(\\\\?\\UNC\\wsl.localhost\\<name>\\...), and the shims there write into a
Windows folder the daemon owns, %USERPROFILE%\\.blink\\wsl-<guid>. On macOS
and Linux such a path is only a string; nothing here opens one.

Nothing is resolved at import (tests/test_sandbox.py): every path is worked
out when it is asked for, so tests/conftest.py's HOME applies.
"""
import dataclasses
import os
import stat
from typing import Optional

from pc import user_config

KEY = user_config.CLAUDE_HOME

NATIVE = "native"
WSL = "wsl"

# The UNC root of a running distribution's files. The \\?\ form, not
# \\wsl.localhost\, so a deep project path is not cut at MAX_PATH.
_UNC_ROOT = "\\\\?\\UNC\\wsl.localhost\\"


@dataclasses.dataclass(frozen=True)
class ClaudeHome:
    kind: str
    # The folder Claude Code keeps settings.json and projects/ in. Windows
    # side: for WSL, a UNC path into the distribution.
    config_dir: str
    # Whether CLAUDE_CONFIG_DIR chose config_dir. With it unset Claude Code
    # keeps .claude.json in the home folder, not in config_dir; with it set,
    # in config_dir (claude_plan.config_path).
    config_dir_set: bool
    transcripts: str
    # Where the shims write statusline.json and state/: ~/.blink natively,
    # %USERPROFILE%\.blink\wsl-<guid> for a distribution.
    drop_dir: str
    state_dir: str
    # Where install keeps the markers that say what it wrote. Apart for each
    # home, so a WSL install's marker never tells the native drift check
    # that a native install happened.
    marker_dir: str
    distro_guid: Optional[str] = None
    distro_name: Optional[str] = None
    linux_user: Optional[str] = None
    linux_home: Optional[str] = None

    def join(self, base, *parts):
        """`base` and `parts` joined the way this home's paths are spelled:
        os.path.join natively, backslashes for a UNC path into WSL, whatever
        platform the code runs on."""
        if self.kind != WSL:
            return os.path.join(base, *parts)
        return "\\".join([base.rstrip("\\")] + [p.strip("\\") for p in parts])

    def user_dir(self):
        """The home folder, where .claude.json sits when CLAUDE_CONFIG_DIR
        did not choose config_dir: ~ natively, the Linux home in WSL."""
        if self.kind != WSL:
            return os.path.expanduser("~")
        return unc(self.distro_name, self.linux_home)

    def inside(self, *parts):
        """A path under the home folder, as this computer reaches it."""
        return self.join(self.user_dir(), *parts)


def _blink_home():
    # cli.blink_home(), repeated rather than imported: cli imports this.
    return os.path.join(os.path.expanduser("~"), ".blink")


def unc(name, linux_path, long_paths=True):
    """The Windows path of `linux_path` inside distribution `name`. The one
    builder: pc/wsl's unc is this function, since two had drifted on a
    trailing slash. `long_paths` False is the short \\\\wsl$\\ form, which
    a path longer than MAX_PATH cannot use."""
    rest = linux_path.strip("/").replace("/", "\\")
    base = (_UNC_ROOT if long_paths else "\\\\wsl$\\") + name
    return base + ("\\" + rest if rest else "")


def _native(config_dir, config_dir_set):
    blink = _blink_home()
    return ClaudeHome(kind=NATIVE, config_dir=config_dir,
                      config_dir_set=bool(config_dir_set),
                      transcripts=os.path.join(config_dir, "projects"),
                      drop_dir=blink,
                      state_dir=os.path.join(blink, "state"),
                      marker_dir=blink)


def from_environ(environ=None):
    """The native home `environ` (os.environ when None) names, ignoring any
    record: what `blink install` records, and what a desk with no record is
    read from. CLAUDE_CONFIG_DIR outranks HOME, as it does for Claude Code.

    Joined from the home directory rather than expanded from "~/.claude":
    on Windows expanduser keeps the slash it was given, and the Windows VM
    suite (2026-10-31) measured the root as "...\\dir0/.claude\\projects".
    """
    value = (os.environ if environ is None else environ).get(
        "CLAUDE_CONFIG_DIR")
    if value:
        return _native(os.path.expanduser(value), True)
    return _native(os.path.join(os.path.expanduser("~"), ".claude"), False)


def wsl(distro_guid, distro_name, linux_user, linux_home, config_dir,
        config_dir_set):
    """A distribution's home. `config_dir` is the UNC path of the folder the
    install probe found; the drop folder is named for the GUID, which
    survives a rename of the distribution, without its braces and in lower
    case so the folder name is the same however the registry spelled it."""
    drop = os.path.join(_blink_home(),
                        "wsl-" + distro_guid.strip("{}").lower())
    h = ClaudeHome(kind=WSL, config_dir=config_dir,
                   config_dir_set=bool(config_dir_set),
                   transcripts="", drop_dir=drop,
                   state_dir=os.path.join(drop, "state"), marker_dir=drop,
                   distro_guid=distro_guid, distro_name=distro_name,
                   linux_user=linux_user, linux_home=linux_home)
    return dataclasses.replace(h, transcripts=h.join(config_dir, "projects"))


def to_record(home):
    """What user_config keeps: the fields a home cannot be rebuilt without.
    The rest are derived again on every read, so a record cannot disagree
    with itself."""
    rec = {"kind": home.kind, "config_dir": home.config_dir,
           "config_dir_set": home.config_dir_set}
    if home.kind == WSL:
        rec.update(distro_guid=home.distro_guid,
                   distro_name=home.distro_name,
                   linux_user=home.linux_user, linux_home=home.linux_home)
    return rec


def from_record(rec):
    """The home a record describes, or None for anything that is not a
    whole record -- a hand edit, a later release's kind -- which is then
    read as no record at all rather than half of one."""
    if not isinstance(rec, dict):
        return None
    cfg, cset = rec.get("config_dir"), rec.get("config_dir_set")
    if not (isinstance(cfg, str) and cfg and isinstance(cset, bool)):
        return None
    if rec.get("kind") == NATIVE:
        return _native(cfg, cset)
    if rec.get("kind") == WSL:
        fields = [rec.get(k) for k in ("distro_guid", "distro_name",
                                       "linux_user", "linux_home")]
        if not all(isinstance(v, str) and v for v in fields):
            return None
        return wsl(*fields, config_dir=cfg, config_dir_set=cset)
    return None


# config.json path -> (signature, home or None): the last good read of each
# file. Keyed by path so a test's HOME never sees another test's answer.
_last = {}


def recorded():
    """The recorded home, or None. Read through a stat signature, since the
    readers ask on every poll; a file that cannot be read gives the last
    good answer, because a half-written config.json must not move the
    daemon to another folder for a poll."""
    path = user_config.path(_blink_home())
    try:
        st = os.stat(path)
        sig = (st.st_mtime_ns, st.st_size, st.st_ino)
    except OSError:
        sig = None
    seen = _last.get(path)
    if seen is not None and seen[0] == sig and sig is not None:
        return seen[1]
    try:
        doc = user_config.load(_blink_home())
    except (user_config.Unreadable, OSError):
        return seen[1] if seen is not None else None
    home = from_record(doc.get(KEY))
    _last[path] = (sig, home)
    return home


def native(environ=None):
    """This computer's own Claude Code: the folder recorded by `blink
    install` when there is a native record, else from_environ(environ)."""
    return _native_from(recorded(), environ)


def _native_from(rec, environ=None):
    if rec is not None and rec.kind == NATIVE:
        return rec
    return from_environ(environ)


def current():
    """The home the readers read: a recorded WSL home, else native().
    One read of the record for both questions."""
    rec = recorded()
    if rec is not None and rec.kind == WSL:
        return rec
    return _native_from(rec)


def save(home):
    """Record `home` as the one this install is for. Raises what
    user_config.put raises: Unreadable for a config.json that does not
    parse, OSError for one that cannot be written."""
    user_config.put(_blink_home(), KEY, to_record(home))
    _last.pop(user_config.path(_blink_home()), None)


def legacy_settings_path():
    """~/.claude/settings.json: where every release before the record wrote
    the status line and hooks, whatever CLAUDE_CONFIG_DIR said."""
    return os.path.join(os.path.expanduser("~"), ".claude", "settings.json")


def regular_nonempty(path):
    """A regular file with something in it; one stat, False on any error."""
    try:
        st = os.stat(path)
    except (OSError, ValueError):
        return False
    return stat.S_ISREG(st.st_mode) and st.st_size > 0
