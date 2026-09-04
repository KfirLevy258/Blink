"""Register Blink's hook shim with Codex's own lifecycle hooks.

Same two rules as pc/install_hooks and pc/install_statusline: never lose
anything the user put there, and never rewrite a key that is not ours. What
differs is whose file it is. ~/.claude/settings.json is edited by a product we
integrate with deliberately; Codex's hooks file belongs to a different vendor
and is documented as a place people write their own automation. So the refusal
path here is not a fallback -- it is the expected behaviour whenever the file
is anything but exactly what we understand. Merge, never clobber; refuse
rather than repair; and every write goes through install_statusline._save,
which writes a sibling temp file and os.replace()s it, so a machine that dies
mid-install leaves Codex a whole hooks file rather than half of one. A
truncated hooks.json would break Codex itself, not merely Blink.

The shape below is pinned in docs/research/codex-hook-contract.md against
codex-cli 0.150.0, where it was established by firing real hooks rather than
by reading the binary. Two constants carry the parts most likely to move:
_HOOKS_PATH_PARTS and _EVENTS_KEY. Re-run that document's probes after a Codex
upgrade rather than discovering a change from a support ticket.

TRUST: Codex requires persisted trust for hook sources and prompts once in its
TUI, recording a `trusted_hash` in the user config.toml. Nothing here can
answer that prompt and nothing here should try -- `blink install` says it is
coming (pc/cli.cmd_install) and the person answers it. The hash covers the
declared COMMAND STRING, not the script's contents (VERIFIED), so shipping new
shim contents at the same path keeps working while a `blink update` that MOVES
the shim invalidates trust and prompts again. That is why the caller must hand
this a stable entry-point path: under `codex exec` a distrusted hook is skipped
silently, with no prompt and no output, which looks exactly like a hook that
was never installed.
"""
import os
import shlex
import sys

from pc.install_statusline import (SettingsUnreadable, _load, _save,
                                   _sniff_format, windows_bash_path)

INSTALLED_MARKER_PATH = "~/.blink/codex-hooks-installed-commands"

# F1: where Codex reads its hooks file from, under CODEX_HOME. Directly in
# CODEX_HOME -- NOT in a `hooks/` subdirectory. Verified in both directions:
# the same file at $CODEX_HOME/hooks.json fired, and at
# $CODEX_HOME/hooks/hooks.json was silently ignored, with a no-file control
# proving the negative was the path and not a broken harness. The
# `hooks/hooks.json` string does appear in the binary, but it belongs to the
# PLUGIN loader; a plugin ships its hooks there, a user does not. Do not
# "correct" this back to a two-part path on the strength of that string.
_HOOKS_PATH_PARTS = ("hooks.json",)

# F2: the key the event map hangs off, or None when the events are the
# top-level object. It is "hooks": the file is
# {"hooks": {"PreToolUse": [...]}}, and the struct behind it carries
# deny_unknown_fields -- so a bare {"PreToolUse": [...]} at the top level is
# rejected outright by Codex, not merely ignored. One constant because it is
# the single thing about this file's shape most likely to move.
_EVENTS_KEY = "hooks"

# The lifecycle events we register, each with the matcher its group is written
# with (None for events that take none).
#
# PermissionRequest is the point of the whole exercise: it is the event Codex
# fires when it is blocked on a person, and it is the one thing its rollout log
# provably cannot tell us (the approval events sit in the never-persisted arm
# of Codex's own policy). Everything else on this list is here either to say a
# session is alive or to CLEAR that prompt -- PostToolUse when the tool was
# approved and ran, UserPromptSubmit when the person typed something else, Stop
# when the turn finished, Interrupt when they refused or pressed Esc, SessionEnd
# when the terminal closed. A waiting state with no way out is worse than no
# waiting state at all, so none of those five may be dropped to save a hook
# call. An observed session showed the clears really are prompt: Interrupt
# landed three seconds after a refused PermissionRequest, the same latency as
# an approval's PostToolUse.
#
# Not registered: PreCompact and PostCompact (they say "still running", which
# the tool events already say) and Codex's Prompt/McpTool/Agent handler kinds
# (we run a command).
HOOK_EVENTS = (
    ("SessionStart", None),
    ("UserPromptSubmit", None),
    ("PreToolUse", "*"),
    ("PostToolUse", "*"),
    ("PermissionRequest", "*"),
    ("Stop", None),
    ("Interrupt", None),
    ("SessionEnd", None),
    # Subagent lifetimes. Whether Codex's payload carries an agent_id is
    # unverified; if it does not, the shim's fallback puts every agent of a
    # session in one file and the count reads 1 instead of N. An undercount,
    # never a crash -- see Task 14 in docs/plans/codex-hook-shim.md.
    ("SubagentStart", "*"),
    ("SubagentStop", "*"),
)


def codex_home() -> str:
    """CODEX_HOME or ~/.codex, exactly as pc/providers/codex_cli does it.

    Codex itself honours CODEX_HOME. Writing to ~/.codex on a machine that
    redirects it would register a hook nothing ever reads -- and because a
    distrusted or unread hook produces no output at all, the symptom would be
    a board that simply never mentions Codex.
    """
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def hooks_file() -> str:
    return os.path.join(codex_home(), *_HOOKS_PATH_PARTS)


def hook_command(shim_path: str, event: str) -> str:
    """The exact string to write as a hook command.

    `bash` and forward slashes on Windows for the reason
    install_statusline.windows_bash_path spells out at length: a non-ASCII home
    directory does not survive the hand-over to Git Bash. Codex also accepts a
    separate `commandWindows` key, but we do not use it: it collapses into
    `command` before the trust hash is taken, so it buys nothing here and adds
    a second string to keep in step with the first.

    The event name is passed as an argument rather than left for the shim to
    parse out of the payload: the shim is POSIX sh with no JSON parser, and
    this runs on every tool call.

    The trailing `codex` is what sends the slots to ~/.blink/state-codex. It is
    the whole difference between this registration and the Claude one, and
    without it every Codex session on the machine is reported to the board as a
    Claude session against a Claude account's limits.
    """
    if sys.platform == "win32":
        return f"bash {windows_bash_path(shim_path)} {event} codex"
    return f"sh {shlex.quote(shim_path)} {event} codex"


def _marker_path():
    return os.path.expanduser(INSTALLED_MARKER_PATH)


def _read_marker() -> set:
    try:
        with open(_marker_path(), encoding="utf-8") as f:
            return {ln.strip() for ln in f if ln.strip()}
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError, so a marker that
        # is not valid UTF-8 -- a truncated write, a half-flushed file from a
        # power cut -- needs its own arm here or it escapes install() as a
        # crash rather than a refusal. install_statusline._read_marker grew the
        # same arm for the same reason. Every caller already treats an empty
        # set as "no marker recorded", which is the right answer for a marker
        # we cannot read as well as for one that is not there.
        return set()


def _write_marker(commands) -> None:
    os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
    # encoding= because the commands carry the shim's path, which carries the
    # user's home directory -- and a Windows locale cannot encode every name a
    # home directory can have.
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

    Both checks: the marker survives a shim path that has since changed, and
    the computed form survives a marker file lost with the rest of ~/.blink.
    Never a substring match on the command text -- a customer hook that merely
    mentions our filename would then be repointed or, in Task 9, deleted.
    """
    return command in marker or command in expected


def _read_hooks_file(hooks_path: str):
    """(indent, trailing_newline, data) for an existing hooks file.

    Every way of failing to read another vendor's file comes back as
    SettingsUnreadable, because that is the one exception the caller is
    documented to expect and the one that means "changed nothing". _load
    already converts absent (-> {}), unparseable and not-an-object; what it
    does not convert is the rest of OSError -- a hooks.json that is a
    directory, one owned by another user, one on a filesystem that went away
    mid-install. Left raw, those escape `blink install` as a traceback about
    someone else's file.
    """
    try:
        indent, trailing_newline = _sniff_format(hooks_path)
        return indent, trailing_newline, _load(hooks_path)
    except SettingsUnreadable:
        raise
    except OSError as e:
        raise SettingsUnreadable(f"{hooks_path} cannot be read ({e})")


def _write_hooks_file(hooks_path, data, indent, trailing_newline) -> None:
    """Write the file back, with a failed write reported the same way a failed
    read is.

    _save's only `except OSError` wraps the removal of a stale temp file; the
    os.open / os.fdopen / os.replace that do the actual work are bare. So a
    CODEX_HOME that is readable but not WRITABLE -- a hooks.json laid down
    under sudo, a read-only dotfiles mount, a full volume -- came back out of
    here as a raw PermissionError naming hooks.json.blink-tmp, a temp file the
    user has never heard of, from the middle of two functions that promise
    otherwise: install says it raises SettingsUnreadable, and uninstall says it
    never raises at all.

    SettingsUnreadable rather than a new exception type of its own, even though
    nothing here failed to READ. It is this codebase's single "stop, and know
    that nothing changed" signal, and both callers in pc/cli already catch it
    and only it; a second type would be caught nowhere until someone edited
    that file too, which is a traceback in the meantime and dead code
    afterwards. "Nothing changed" is honest for every failure it covers,
    because the write is temp-file-then-rename: the target keeps its old bytes
    whether we died opening the temp, filling it, or renaming it over.

    The temp file itself may survive a mid-write failure. It is deliberately
    not cleaned up here -- its name is _save's business, not ours, and _save
    already removes a stale one before each write, so it cannot block the
    retry. A stray sibling is a far smaller sin than reaching into another
    module's naming scheme to delete a file in someone else's directory.
    """
    try:
        _save(hooks_path, data, indent, trailing_newline)
    except OSError as e:
        raise SettingsUnreadable(f"{hooks_path} cannot be written ({e})")


def _event_map(data):
    """The object holding the per-event lists, created if absent.

    Never replaced. Whatever is in there belongs to the user or to another
    tool, and a hooks file whose event map is not an object is a file we do
    not understand -- which is the one situation where doing nothing is the
    only safe move. setdefault rather than assignment for the same reason:
    a top-level `description`, which Codex allows, has to survive us.
    """
    if _EVENTS_KEY is None:
        return data
    events = data.setdefault(_EVENTS_KEY, {})
    if not isinstance(events, dict):
        raise SettingsUnreadable(f"'{_EVENTS_KEY}' is not an object")
    return events


def _entries_for(data, event):
    """The matcher-group list for one event, created if absent."""
    events = _event_map(data)
    lst = events.setdefault(event, [])
    if not isinstance(lst, list):
        raise SettingsUnreadable(f"{event} is not a list")
    return lst


def install(hooks_path: str, shim_path: str) -> str:
    """Add our hook to each lifecycle event. Idempotent.

    Raises SettingsUnreadable, and changes nothing, when the file is there and
    cannot be read, cannot be parsed, is not shaped the way we understand, or
    cannot be written back. Nothing is written until every event has been
    merged successfully, so a refusal that surfaces on the tenth event leaves
    the file byte-identical.
    The caller reports that and carries on: the activity light is a nicety,
    and someone else's config is not ours to repair.
    """
    indent, trailing_newline, data = _read_hooks_file(hooks_path)

    expected = {hook_command(shim_path, ev) for ev, _ in HOOK_EVENTS}
    marker = _read_marker()
    added = 0
    repointed = 0
    for event, matcher in HOOK_EVENTS:
        command = hook_command(shim_path, event)
        entries = _entries_for(data, event)

        # Already present, in any group -- a reinstall must not stack a second
        # copy that then fires twice per tool call forever. But "present" is
        # not "correct": an entry that matches only via the MARKER names an
        # older shim path, which is what `blink update` produces every time it
        # moves the binary. Left as-is those entries are orphaned instantly,
        # invisible to uninstall, and a third install appends a duplicate.
        ours = ours_group = None
        for group in entries:
            # A group that is not an object is not something we can read, but
            # it is also not a reason to refuse the whole file: skipping it
            # leaves it exactly as we found it, and the alternative is an
            # AttributeError from .get() on a bare string someone typed.
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
            # The matcher is ours to correct too, but only when the group
            # holds our hook ALONE -- rewriting a matcher on a group we share
            # with someone else would change when their hook fires.
            if (len(ours_group.get("hooks") or []) == 1
                    and ours_group.get("matcher") != matcher):
                if matcher:
                    ours_group["matcher"] = matcher
                else:
                    ours_group.pop("matcher", None)
                repointed += 1
            continue

        # Appended alongside whatever is already registered for this event,
        # never in place of it. Codex runs every group for an event, so
        # someone else's audit hook keeps firing exactly as it did.
        group = {"hooks": [{"type": "command", "command": command}]}
        if matcher:
            group["matcher"] = matcher
        entries.append(group)
        added += 1

    _write_hooks_file(hooks_path, data, indent, trailing_newline)
    try:
        _write_marker(expected)
    except OSError:
        # The marker is an optimisation, not the record of record: without it
        # a same-path reinstall is still recognised by the computed form, and
        # only a reinstall at a MOVED path degrades (to an appended duplicate,
        # which the next install repoints). Raising here instead would report
        # a failed install for a hooks file that was written correctly a line
        # earlier -- the one report guaranteed to send someone looking in the
        # wrong file.
        pass
    if added == 0 and repointed == 0:
        return "Codex state hooks already installed."
    if added == 0:
        return f"Codex state hooks repointed at the new path ({repointed})."
    if repointed:
        return (f"Codex state hooks installed ({added} events,"
                f" {repointed} repointed).")
    return f"Codex state hooks installed ({added} events)."


def _events_to_edit(data):
    """The event map as it is on disk, without creating one.

    _event_map() is install's helper and it setdefault()s the key into being,
    which is right when the next step is to add something and wrong here: a
    file with no hooks key at all must come out of uninstall with no hooks key
    at all, not with an empty one we invented and then had to remember to take
    back out.

    Returns {} for "there is nothing of ours in here", and raises
    SettingsUnreadable for a shape install would have refused.
    """
    if _EVENTS_KEY is None:
        return data
    events = data.get(_EVENTS_KEY)
    if events is None:
        return {}
    if not isinstance(events, dict):
        raise SettingsUnreadable(f"'{_EVENTS_KEY}' is not an object")
    return events


def uninstall(hooks_path: str, shim_path: str = None) -> str:
    """Remove only our entries, and only the ones we can prove are ours.

    Symmetric with install by construction: it drops exactly the commands the
    marker recorded plus the ones this call would itself write, and leaves
    every other entry in place. An empty group left behind by that removal is
    dropped too, and an event whose list ends up empty loses its key -- so a
    machine that has uninstalled has a hooks file shaped the way it was before
    Blink ever ran, rather than a skeleton of empty lists.

    Never raises. `blink uninstall` runs this on every machine, including the
    many that never had a Codex hook, and a removal step that can abort the
    uninstall over someone else's config is worse than one that reports what
    it could not do. Every failure comes back as a sentence and an unchanged
    file: the five cases are a file we cannot read, a file we cannot parse, a
    file whose shape install itself would refuse, a file we can read but
    cannot write back, and a file with nothing of ours in it.

    That fourth case is the one that made this docstring a lie for a while.
    `blink uninstall` removes the login service at step [1/5] and calls this at
    [4/5]; a raw PermissionError escaping here took step [5/5] with it and left
    a machine with no service and every file still installed -- the half-undone
    state this whole design exists to avoid. The person asked to be
    uninstalled, so a Codex hook we cannot remove is a line in the report, not
    a reason to stop.

    shim_path is optional because the caller does not always still know it --
    `blink uninstall` may run after the shim has been deleted. The marker
    alone is enough to identify what we wrote; the computed commands are the
    belt to its braces, for a ~/.blink that was removed first.
    """
    try:
        indent, trailing_newline, data = _read_hooks_file(hooks_path)
    except SettingsUnreadable as e:
        # Never "repair" a file we cannot parse by writing a fresh one over
        # it. It is someone's config, probably mid-edit -- and this is a
        # destructive operation, which is the last place to start guessing.
        return f"Codex hooks file could not be read ({e}); left it alone."

    expected = ({hook_command(shim_path, ev) for ev, _ in HOOK_EVENTS}
                if shim_path else set())
    marker = _read_marker()

    try:
        events = _events_to_edit(data)
        for event, _ in HOOK_EVENTS:
            if event in events and not isinstance(events[event], list):
                raise SettingsUnreadable(f"{event} is not a list")
    except SettingsUnreadable as e:
        # install() refuses these shapes rather than rewriting them, and
        # uninstall has to refuse the same ones or the pair is not symmetric:
        # a file it declined to add to is a file it does not understand well
        # enough to delete from either. Checked for every event BEFORE
        # anything is removed, so the refusal is all-or-nothing exactly as
        # install's is -- a bad tenth event cannot leave the first nine
        # stripped. The marker survives, because it is the only remaining
        # proof that the entries still in that file are ours.
        return (f"Codex hooks file is not one we understand ({e});"
                f" left it alone.")

    removed = 0
    for event, _ in HOOK_EVENTS:
        entries = events.get(event)
        if not isinstance(entries, list):
            continue
        kept_groups = []
        dropped_here = 0
        for group in entries:
            # A group that is not an object, or whose hooks are not a list, is
            # one person's typo. install() steps over those rather than
            # refusing, so this steps over them too -- and stepping over means
            # copying them through untouched, never dropping them.
            if not isinstance(group, dict):
                kept_groups.append(group)
                continue
            inner = group.get("hooks")
            if not isinstance(inner, list):
                kept_groups.append(group)
                continue
            kept = [h for h in inner
                    if not (isinstance(h, dict)
                            and _ours(h.get("command", ""), expected, marker))]
            dropped_here += len(inner) - len(kept)
            if kept:
                # Someone else's hook in a group we created keeps the group
                # alive. Dropping the group to tidy up our own entry would
                # silently take their automation with it.
                group["hooks"] = kept
                kept_groups.append(group)
            # A group whose only hook was ours is dropped entirely rather than
            # left as an empty shell with a dangling matcher.
        if not dropped_here:
            # Nothing of ours here: leave the list object exactly as we found
            # it rather than writing back an equal copy. It costs nothing and
            # it keeps "we changed only what was ours" true of the data as
            # well as of the bytes.
            continue
        removed += dropped_here
        if kept_groups:
            events[event] = kept_groups
        else:
            events.pop(event, None)

    if removed == 0:
        # Not a failure: `blink uninstall` calls this on machines that never
        # installed the Codex hook, and on machines where someone already took
        # the entries out by hand. The file is not rewritten at all -- not even
        # re-serialised -- so a hooks file we have nothing in comes out of an
        # uninstall byte-identical, and one that never existed is not created.
        _remove_marker()
        return "No Codex state hooks to remove."

    if _EVENTS_KEY is not None and not events:
        # The last event went, so the wrapper goes with it. The FILE stays,
        # holding {}: we cannot prove we were the ones who created it, Codex
        # is free to write it again, and deleting another vendor's file is
        # more than removing our entries from it authorises.
        data.pop(_EVENTS_KEY, None)

    # _save writes a sibling temp file and os.replace()s it, so an uninstall
    # that dies mid-write leaves Codex a whole hooks file rather than half of
    # one. A truncated hooks.json breaks Codex itself, not merely Blink.
    try:
        _write_hooks_file(hooks_path, data, indent, trailing_newline)
    except SettingsUnreadable as e:
        # The marker stays, for the same reason the parse refusal above keeps
        # it: our entries are still in that file, and once the shim path moves
        # the marker is the only thing that can still prove they are ours. A
        # later uninstall, run when the directory is writable again, is then
        # able to finish the job -- and this call changed nothing, so there is
        # nothing for it to reconcile.
        return f"Codex hooks file could not be written ({e}); left it alone."
    _remove_marker()
    return f"Codex state hooks removed ({removed})."
