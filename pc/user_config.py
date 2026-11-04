"""The owner's own settings for this computer: <blink_home>/config.json.

Most of what a person can choose lives on the BOARD -- the provider choice is
picked on its settings screen, persisted in its flash and announced in
`pref` -- because the person choosing it is looking at the panel (see
IngestionBus.set_preferred). The one computer-side choice so far was a bare
marker file, `no-auto-update` (update.auto_update_allowed). A status-page
lookup is a choice about what THIS COMPUTER does on the network, with a
value rather than a presence, so it gets a small JSON object here.

Read and written through install_statusline._load/_save, the same pair that
edits the customer's settings.json, so this file gets every lesson that pair
already paid for: a document that does not parse is refused rather than
written over, a symlink is followed to its target rather than replaced, the
temp file is private and unique and removed if the write fails, and a
Windows handle held for a moment is retried rather than reported.

Read fresh on every call rather than cached: the file is a few dozen bytes,
the daemon reads it every few seconds at most, and reading it fresh is what
lets `blink config` take effect without restarting the login service.

Keys so far: "status_page" (status_page.SETTING), and CLAUDE_HOME, which
`blink install` writes: where Claude Code's files are, as the install found
them (pc/claude_home). It is a record rather than a choice, and it is here
because the background service has to read it and does not see the
installing shell's environment.

`blink_home` is passed in, never computed here. A path resolved at import
reads the real ~ before tests/conftest.py has redirected it.
"""
import os

from pc import install_statusline

FILE_NAME = "config.json"

# {"kind": "native"|"wsl", "config_dir", "config_dir_set": bool, ...}: see
# claude_home.to_record for the whole shape. One key for both kinds: `blink
# install` writes "native", `blink install --wsl <name>` writes "wsl" (the
# distribution's GUID and name, the Linux user and home, the config folder
# found by the probe -- never a value from the shell's environment). The
# Claude page reads one home, so `--wsl` replaces whatever was recorded,
# taking Blink out of that home first; a plain `blink install` keeps a WSL
# record whose distribution is still installed, and replaces it with the
# native one only when the distribution is gone.
CLAUDE_HOME = "claude_home"

# Raised by load() for a file that is there and is not a JSON object. The
# same type install_statusline already raises for settings.json, so a caller
# handles one exception for both.
Unreadable = install_statusline.SettingsUnreadable


def path(blink_home):
    return os.path.join(blink_home, FILE_NAME)


def load(blink_home) -> dict:
    """Every setting. {} when there is no file.

    Raises Unreadable for a file that does not parse, and OSError for one
    that cannot be read at all. Neither is turned into {}: an empty answer
    reads as "everything off", and a setting that flips to off because of
    one failed read is a setting that does something the owner did not ask.
    """
    return install_statusline._load(path(blink_home))


def put(blink_home, key, value) -> None:
    """Set one key, keeping every other.

    Raises Unreadable rather than replacing a file that does not parse --
    usually one somebody is halfway through editing -- and OSError when it
    cannot write. Unlike board.json this is something the person asked for,
    and saying "done" over a failed write would be a lie they act on.
    """
    d = load(blink_home)
    d[key] = value
    install_statusline._save(path(blink_home), d, 2, True)
