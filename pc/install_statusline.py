"""Install the Clauge statusline shim into a user's Claude Code settings.

We are editing a file the user owns and did not ask us to touch beyond this one
key. Two rules follow: never lose their existing statusline command (it goes in
the chain file, and uninstall puts it back verbatim), and never rewrite any key
but `statusLine`.
"""
import json
import os

CHAIN_PATH = "~/.clauge/statusline-chain"
MARKER = "clauge-statusline.sh"


def _chain_path():
    return os.path.expanduser(CHAIN_PATH)


def _load(settings_path: str) -> dict:
    try:
        with open(settings_path) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def _save(settings_path: str, data: dict) -> None:
    """Write via a temp file so a crash cannot truncate the user's settings."""
    tmp = settings_path + ".clauge-tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
        f.write("\n")
    os.replace(tmp, settings_path)


def install(settings_path: str, shim_path: str) -> str:
    data = _load(settings_path)
    previous = (data.get("statusLine") or {}).get("command", "")

    os.makedirs(os.path.dirname(_chain_path()), exist_ok=True)
    # Guard against chaining the shim to itself on a second install, which
    # would recurse until the status bar hangs.
    if previous and MARKER not in previous:
        with open(_chain_path(), "w") as f:
            f.write(previous + "\n")
        chained = f"chained previous statusline: {previous}"
    else:
        chained = "no previous statusline to chain"

    data["statusLine"] = {"type": "command", "command": f"sh {shim_path}"}
    _save(settings_path, data)
    return f"Clauge statusline installed ({chained})."


def uninstall(settings_path: str) -> str:
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

    _save(settings_path, data)
    try:
        os.remove(_chain_path())
    except OSError:
        pass
    return msg
