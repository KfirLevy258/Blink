"""Install the Clauge statusline shim into a user's Claude Code settings.

We are editing a file the user owns and did not ask us to touch beyond this one
key. Two rules follow: never lose their existing statusline command (it goes in
the chain file, and uninstall puts it back verbatim), and never rewrite any key
but `statusLine`.
"""
import json
import os

CHAIN_PATH = "~/.clauge/statusline-chain"

# Records the exact command string the last install() call wrote to
# statusLine, so a later install() can recognize "this is our own shim"
# without pattern-matching the command text. A substring check on the
# command (e.g. "clauge-statusline.sh" in previous) is not safe: a
# customer's own script can legitimately contain the shim's filename as a
# substring (a wrapper literally named
# "wrap-clauge-statusline.sh-backup.sh", say), and a naive check mistakes
# that for "already installed" -- silently discarding the customer's real
# command with no way to recover it via uninstall(). Comparing against what
# we ourselves last wrote is exact.
#
# This marker is NOT the only way install() recognizes its own command --
# see the is_ours check in install() below. The marker lives in ~/.clauge,
# the same directory the shim itself uses as transient scratch space
# (statusline.json), so it is plausible for a user or a cleanup script to
# wipe the whole directory independently of settings.json. A design that
# trusted the marker alone would, after that wipe, see its own previous
# command as unrecognized on the next same-path install and chain it into
# the chain file -- recreating the exact self-invocation loop this file
# exists to prevent, just by a different route. install() therefore treats
# a command as "ours" if it matches EITHER the marker OR the command this
# very call would itself write, so losing ~/.clauge does not resurrect the
# bug for the common case (reinstalling at an unchanged shim_path).
INSTALLED_MARKER_PATH = "~/.clauge/statusline-installed-command"


def _chain_path():
    return os.path.expanduser(CHAIN_PATH)


def _marker_path():
    return os.path.expanduser(INSTALLED_MARKER_PATH)


def _read_marker() -> str:
    try:
        with open(_marker_path()) as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_marker(command: str) -> None:
    os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
    with open(_marker_path(), "w") as f:
        f.write(command + "\n")


def _remove_marker() -> None:
    try:
        os.remove(_marker_path())
    except OSError:
        pass


def _load(settings_path: str) -> dict:
    try:
        with open(settings_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _sniff_format(settings_path: str):
    """(indent, trailing_newline) matching the existing file, so a
    hand-formatted settings.json is not silently reformatted on every
    install/uninstall. Defaults (2 spaces, trailing newline) apply when the
    file doesn't exist yet or gives no indentation to sniff (e.g. it's a
    single-line/minified document).
    """
    try:
        with open(settings_path) as f:
            text = f.read()
    except FileNotFoundError:
        return 2, True

    trailing_newline = text.endswith("\n")
    indent = 2
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        leading = line[: len(line) - len(stripped)]
        if leading:
            indent = leading if "\t" in leading else len(leading)
            break
    return indent, trailing_newline


def _save(settings_path: str, data: dict, indent, trailing_newline: bool) -> None:
    """Write via a temp file so a crash cannot truncate the user's settings."""
    tmp = settings_path + ".clauge-tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=indent)
        if trailing_newline:
            f.write("\n")
    os.replace(tmp, settings_path)


def install(settings_path: str, shim_path: str) -> str:
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    previous = (data.get("statusLine") or {}).get("command", "")
    new_command = f"sh {shim_path}"

    os.makedirs(os.path.dirname(_chain_path()), exist_ok=True)
    # Guard against chaining the shim to itself. `previous` counts as ours
    # if EITHER check holds, never by pattern-matching the text:
    #   - stateless: it equals the command THIS call is about to write.
    #     Needs no file to have survived, so a same-path reinstall is still
    #     recognized correctly even if ~/.clauge (and the marker in it) was
    #     wiped since the last install.
    #   - persisted: it equals the marker recorded by the last install().
    #     This is what recognizes a reinstall at a *different* shim_path as
    #     still being ours -- the stateless check alone can't, since the
    #     command text legitimately changes when the path does.
    # A foreign command can equal neither (short of an adversarial customer
    # literally choosing our exact former command text, which is the same
    # irreducible edge every exact-match scheme has) and is always chained.
    is_ours = previous == new_command or previous == _read_marker()
    if previous and not is_ours:
        with open(_chain_path(), "w") as f:
            f.write(previous + "\n")
        chained = f"chained previous statusline: {previous}"
    else:
        chained = "no previous statusline to chain"

    data["statusLine"] = {"type": "command", "command": new_command}
    _save(settings_path, data, indent, trailing_newline)
    _write_marker(new_command)
    return f"Clauge statusline installed ({chained})."


def uninstall(settings_path: str) -> str:
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    previous = ""
    try:
        with open(_chain_path()) as f:
            previous = f.read().strip()
    except OSError:
        pass

    if previous:
        data["statusLine"] = {"type": "command", "command": previous}
        msg = f"Restored previous statusline: {previous}"
    else:
        data.pop("statusLine", None)
        msg = "Removed the Clauge statusline."

    _save(settings_path, data, indent, trailing_newline)
    try:
        os.remove(_chain_path())
    except OSError:
        pass
    _remove_marker()
    return msg
