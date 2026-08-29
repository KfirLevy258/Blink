"""Install the Blink execution-state hooks into a user's Claude Code settings.

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
                                   windows_bash_path,
                                   _sniff_format)

INSTALLED_MARKER_PATH = "~/.blink/hooks-installed-commands"

# The lifecycle events the state machine is derived from, each with the
# matcher its entry is written with (None for events that take none). Tool
# events fire per tool call and match every tool; the rest fire once per turn
# or per session.
NOTIFICATION_MATCHER = "permission_prompt|elicitation_dialog"

HOOK_EVENTS = (
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "*"),
    ("PostToolUse", "*"),
    # Only the notifications that mean "Claude Code is waiting on a person".
    # Installed with no matcher, this fired for every notification type --
    # including idle_prompt, which Claude Code sends sixty seconds after a
    # reply when nobody has typed. Every finished turn therefore flipped
    # from green to amber a minute later, and since waiting outranks running
    # in worst_of(), one idle terminal masked another that was working.
    ("Notification", NOTIFICATION_MATCHER),
    ("Stop", None),
    # Runs INSTEAD of Stop when a turn dies on an API error, and carries
    # error: "rate_limit" among its causes. On a usage gauge that is the
    # headline, so it is worth a hook of its own rather than being left to
    # look like a turn that simply went quiet.
    ("StopFailure", None),
    ("SessionEnd", None),
    # Subagent lifetimes. Both carry agent_id, which is what lets the shim
    # keep one file per agent and the daemon count them exactly without a
    # lock -- see tools/blink-hook.sh.
    ("SubagentStart", "*"),
    ("SubagentStop", "*"),
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
        return f"bash {windows_bash_path(shim_path)} {event}"
    return f"sh {shlex.quote(shim_path)} {event}"


def _marker_path():
    return os.path.expanduser(INSTALLED_MARKER_PATH)


def _read_marker() -> set:
    try:
        with open(_marker_path(), encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except OSError:
        return set()


def _write_marker(commands) -> None:
    os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
    # encoding= because the commands carry the shim's path, which carries the
    # user's home directory -- and a Windows locale cannot encode every name a
    # home directory can have. _load() in install_statusline says the same.
    with open(_marker_path(), "w", encoding="utf-8") as f:
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
    survives a marker file that was lost with the rest of ~/.blink.

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
    repointed = 0
    for event, matcher in HOOK_EVENTS:
        command = hook_command(shim_path, event)
        entries = _entries_for(data, event)

        # Already present, in any group. A reinstall must not stack a second
        # copy that then fires twice per tool call forever.
        #
        # But "present" is not the same as "correct". An entry that matches
        # only via the MARKER names an older shim path -- which is what a
        # migrated home, a run under sudo, or a Windows path-form change
        # produces. This used to `continue`, add nothing, and then overwrite
        # the marker with the new commands: all ten entries were orphaned
        # instantly, invisible to uninstall (which matches expected u marker,
        # both now naming the new path), left invoking a script that does not
        # exist, and a third install appended a duplicate group so both fired.
        # install_statusline has had a whole repoint path for this since it
        # was written; the hooks had none.
        ours = ours_group = None
        for group in entries:
            if not isinstance(group, dict):
                continue
            for h in (group.get("hooks") or []):
                if isinstance(h, dict) and _ours(h.get("command", ""),
                                                 expected, marker):
                    ours, ours_group = h, group
                    break
            if ours is not None:
                break

        if ours is not None:
            if ours.get("command") != command:
                ours["command"] = command
                repointed += 1
            # The matcher is ours to correct too, when the group holds only
            # our hook: an install that predates NOTIFICATION_MATCHER left
            # the Notification entry firing for every notification type.
            if (len(ours_group.get("hooks") or []) == 1
                    and ours_group.get("matcher") != matcher):
                if matcher:
                    ours_group["matcher"] = matcher
                else:
                    ours_group.pop("matcher", None)
                repointed += 1
            continue

        group = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            group["matcher"] = matcher
        entries.append(group)
        added += 1

    _save(settings_path, data, indent, trailing_newline)
    _write_marker(expected)
    if added == 0 and repointed == 0:
        return "Blink state hooks already installed."
    if added == 0:
        return f"Blink state hooks repointed at the new path ({repointed})."
    if repointed:
        return (f"Blink state hooks installed ({added} events,"
                f" {repointed} repointed).")
    return f"Blink state hooks installed ({added} events)."


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
        return "No Blink state hooks to remove."

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
        return "No Blink state hooks to remove."
    return f"Blink state hooks removed ({removed})."
