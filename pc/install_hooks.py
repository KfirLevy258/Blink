"""Install the Clauge execution-state hooks into a user's Claude Code settings.

Same file, same owner, same two rules as pc/install_statusline: never lose
anything the user put there, and never rewrite a key that is not ours.

One structural difference makes this the easier of the two. `statusLine` is a
single slot, so installing into it means displacing whatever was there and
carrying the displaced command in a chain file. `hooks` is a LIST per event,
so ours is appended alongside anyone else's and nothing has to be displaced,
chained or restored. There is no equivalent of the self-invocation guard here
because there is no invocation to inherit.

What we record and remove is tracked by a marker, for the reason spelled out
at length in install_statusline.INSTALLED_MARKER_PATH: recognising our own
entries by pattern-matching the command text would eventually delete a
customer hook that merely mentions our filename.
"""
import json
import os
import shlex
import sys

from pc.install_statusline import (SettingsUnreadable, _load, _save,
                                   _sniff_format)

INSTALLED_MARKER_PATH = "~/.clauge/hooks-installed-commands"

# The lifecycle events the state machine is derived from, and whether the
# event takes a tool matcher. Tool events fire per tool call; the rest fire
# once per turn or per session.
HOOK_EVENTS = (
    ("UserPromptSubmit", False),
    ("PreToolUse", True),
    ("PostToolUse", True),
    ("Notification", False),
    ("Stop", False),
    ("SessionEnd", False),
)


def hook_command(shim_path: str, event: str) -> str:
    """The exact string to write as a hook command.

    `bash` on Windows and forward slashes in the path, for the same reason
    install_statusline.statusline_command does it: Claude Code rewrites a
    Windows command mentioning a .sh file unless it already starts with
    "bash ", and a backslash is an escape character under bash.

    The event name is passed as an argument rather than left for the shim to
    parse out of the payload. The shim is POSIX sh with no JSON parser, and
    this runs on every tool call.
    """
    if sys.platform == "win32":
        quoted = shlex.quote(shim_path.replace(chr(92), "/"))
        return f"bash {quoted} {event}"
    return f"sh {shlex.quote(shim_path)} {event}"


def _marker_path():
    return os.path.expanduser(INSTALLED_MARKER_PATH)


def _read_marker() -> set:
    try:
        with open(_marker_path()) as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def _write_marker(commands) -> None:
    os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
    with open(_marker_path(), "w") as f:
        for c in sorted(commands):
            f.write(c + "\n")


def _remove_marker() -> None:
    try:
        os.remove(_marker_path())
    except OSError:
        pass


def _ours(command: str, expected: set, marker: set) -> bool:
    """Ours if the marker recorded it, or if it is what we would write now.

    Both checks, for the reasons install_statusline._is_ours gives: the marker
    survives a shim path that has since changed, and the computed form
    survives a marker file that was lost with the rest of ~/.clauge.

    The marker is passed in rather than read here: this is called once per
    hook entry inside two nested loops, and reading the same small file a
    couple of dozen times per install is a file read for no new information.
    """
    return command in marker or command in expected


def _entries_for(data, event):
    """The hook list for one event, created if absent. Never replaced.

    Anything already in there belongs to the user or another tool and is
    returned untouched.
    """
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        # Someone put something else entirely under "hooks". Refusing is the
        # only safe move: overwriting it would destroy a config we do not
        # understand.
        raise SettingsUnreadable("'hooks' is not an object")
    lst = hooks.setdefault(event, [])
    if not isinstance(lst, list):
        raise SettingsUnreadable(f"hooks.{event} is not a list")
    return lst


def install(settings_path: str, shim_path: str) -> str:
    """Add our hook to each lifecycle event. Idempotent."""
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)

    expected = {hook_command(shim_path, ev) for ev, _ in HOOK_EVENTS}
    marker = _read_marker()
    added = 0
    for event, takes_matcher in HOOK_EVENTS:
        command = hook_command(shim_path, event)
        entries = _entries_for(data, event)

        # Already present, in any group. A reinstall must not stack a second
        # copy that then fires twice per tool call forever.
        if any(_ours(h.get("command", ""), expected, marker)
               for group in entries if isinstance(group, dict)
               for h in (group.get("hooks") or [])
               if isinstance(h, dict)):
            continue

        group = {"hooks": [{"type": "command", "command": command}]}
        if takes_matcher:
            group["matcher"] = "*"
        entries.append(group)
        added += 1

    _save(settings_path, data, indent, trailing_newline)
    _write_marker(expected)
    if added == 0:
        return "Clauge state hooks already installed."
    return f"Clauge state hooks installed ({added} events)."


def uninstall(settings_path: str, shim_path: str = None) -> str:
    """Remove only our hook entries, and only the ones we can prove are ours.

    Symmetric with install by construction: it drops exactly the commands the
    marker recorded, plus the ones this call would itself write, and leaves
    every other entry in place. An empty group left behind by that removal is
    dropped too, and an event whose list ends up empty loses its key, so
    uninstalling returns settings.json to the shape it had.
    """
    try:
        data = _load(settings_path)
    except SettingsUnreadable:
        return "settings.json could not be read; left it alone."

    expected = ({hook_command(shim_path, ev) for ev, _ in HOOK_EVENTS}
                if shim_path else set())
    marker = _read_marker()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        _remove_marker()
        return "No Clauge state hooks to remove."

    removed = 0
    for event, _ in HOOK_EVENTS:
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        kept_groups = []
        for group in entries:
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept = [h for h in inner
                    if not (isinstance(h, dict)
                            and _ours(h.get("command", ""), expected,
                                      marker))]
            removed += len(inner) - len(kept)
            if kept:
                group["hooks"] = kept
                kept_groups.append(group)
            # A group whose only hook was ours is dropped entirely rather
            # than left as an empty shell with a dangling matcher.
        if kept_groups:
            hooks[event] = kept_groups
        else:
            hooks.pop(event, None)

    if not hooks:
        data.pop("hooks", None)

    indent, trailing_newline = _sniff_format(settings_path)
    _save(settings_path, data, indent, trailing_newline)
    _remove_marker()
    if removed == 0:
        return "No Clauge state hooks to remove."
    return f"Clauge state hooks removed ({removed})."
