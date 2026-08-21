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
# we ourselves last wrote is exact. Updating this marker on every install()
# (not just the first) also means a *reinstall pointed at a different
# shim_path* is still recognized as ours: without a stored marker, the new
# command text ("sh <new path>") would differ from the old one, previous
# install()'s command would look like a foreign statusline, and we would
# chain our own old command into the chain file -- recreating the
# self-invocation loop by another route, just one install cycle later.
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
    # Guard against chaining the shim to itself: only skip chaining when
    # `previous` is EXACTLY the command our own last install() wrote (see
    # INSTALLED_MARKER_PATH above), never by pattern-matching the text.
    if previous and previous != _read_marker():
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
