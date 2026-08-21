"""Install the Clauge statusline shim into a user's Claude Code settings.

We are editing a file the user owns and did not ask us to touch beyond this one
key. Two rules follow: never lose their existing statusline command (it goes in
the chain file, and uninstall puts it back verbatim), and never rewrite any key
but `statusLine`.
"""
import json
import os
import shlex
import shutil

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
    # open(tmp, "w") creates at the process umask (typically 0644), not the
    # mode the original file had. This file can legitimately hold
    # env.ANTHROPIC_API_KEY or apiKeyHelper, so a customer who chmod'd it
    # 0600 must get 0600 back, not a silently widened copy.
    if os.path.exists(settings_path):
        shutil.copymode(settings_path, tmp)
    os.replace(tmp, settings_path)


def install(settings_path: str, shim_path: str) -> str:
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    previous = (data.get("statusLine") or {}).get("command", "")
    # shlex.quote: an unquoted path is one space away from a silent no-op on
    # macOS (a very live case -- "/Users/kfir/Application Support/..."). An
    # unquoted `sh /a b/c` splits into three argv words and does nothing.
    # The shim's own self-invocation guard (`[ "$chain_cmd" != "sh $0" ]`)
    # has to keep agreeing with whatever quoting we do here -- see
    # tools/clauge-statusline.sh, which mirrors shlex.quote's exact rule in
    # shell so the two sides never drift apart.
    new_command = f"sh {shlex.quote(shim_path)}"

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
    marker = _read_marker()
    is_ours = previous == new_command or previous == marker
    if previous and not is_ours:
        with open(_chain_path(), "w") as f:
            f.write(previous + "\n")
        chained = f"chained previous statusline: {previous}"
    elif not previous and not marker:
        # Absent statusLine key AND no marker from any earlier install --
        # nothing ties a chain file to a still-live Clauge install, so if
        # one exists here it is a ghost from something else entirely (a
        # hand-placed file, leftovers from an unrelated flow). Clear it --
        # otherwise a later uninstall() would "restore" that ghost command
        # as if it were the customer's real previous statusline.
        #
        # Checking the marker (not just "statusLine is absent") matters: a
        # marker surviving from an earlier install means that install's
        # chain content, if any, may still hold the real pre-Clauge
        # original even though statusLine was since cleared by some other
        # means (hand edit, settings migration). Treating "no statusLine"
        # alone as proof of "nothing to protect" deleted exactly that
        # original in the reproduction this comment is guarding against.
        try:
            os.remove(_chain_path())
        except OSError:
            pass
        chained = "no previous statusline to chain"
    else:
        # Either previous is ours (a reinstall over our own shim), or
        # statusLine is currently absent but a marker survives from an
        # earlier install. Either way the chain file, if any, may still
        # hold the real pre-Clauge original and must not be touched.
        chained = "no previous statusline to chain"

    data["statusLine"] = {"type": "command", "command": new_command}
    _save(settings_path, data, indent, trailing_newline)
    _write_marker(new_command)
    return f"Clauge statusline installed ({chained})."


def uninstall(settings_path: str, shim_path: str = None) -> str:
    """Undo install(), but only when it is safe to.

    install() has an is_ours guard before it touches statusLine; uninstall()
    needs the exact same guard, or symmetrically. Two ways this goes wrong
    without one:
      - the customer installs Clauge, later points statusLine at a NEW
        command of their own (editing settings.json directly, bypassing
        uninstall), then runs uninstall -- which must leave their new
        command alone, not clobber it with stale chain-file content that
        predates it.
      - ~/.clauge is wiped, or uninstall runs having never installed --
        data.pop("statusLine") would then delete a command Clauge never
        wrote, with no way to recover it.
    So: only touch statusLine when the command currently sitting there is
    recognisably ours -- it matches the marker install() recorded, or (when
    the caller passes shim_path, as the CLI does) it equals the command
    install() would write for that path today. Anything else is left
    completely alone; we say so rather than guessing.
    """
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    current = (data.get("statusLine") or {}).get("command", "")

    if not current:
        return "No Clauge statusline installed; nothing to do."

    marker = _read_marker()
    expected = f"sh {shlex.quote(shim_path)}" if shim_path else None
    is_ours = current == marker or (expected is not None and current == expected)
    if not is_ours:
        # Do not touch settings.json, the chain file, or the marker: we
        # cannot tell what this command is, and guessing wrong here is the
        # unrecoverable failure mode this function exists to avoid.
        return ("Current statusline isn't Clauge's (changed since install); "
                "leaving it alone.")

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


def _default_shim_path() -> str:
    """This repo's tools/clauge-statusline.sh, from this file's location."""
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(repo_root, "tools", "clauge-statusline.sh")


def main(argv=None) -> int:
    """Minimal CLI so install()/uninstall() are actually reachable. Full
    user-facing docs (README section, etc.) are a separate task -- this just
    gives them a command to run."""
    import argparse

    parser = argparse.ArgumentParser(
        description="Install or remove the Clauge statusline shim in Claude "
                     "Code's settings.")
    parser.add_argument(
        "--settings", default="~/.claude/settings.json",
        help="Path to Claude Code's settings.json (default: %(default)s)")
    parser.add_argument(
        "--shim", default=None,
        help="Path to the Clauge statusline shim "
             "(default: this repo's tools/clauge-statusline.sh)")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("install", help="Install the Clauge statusline shim.")
    sub.add_parser("uninstall", help="Remove the Clauge statusline shim.")

    args = parser.parse_args(argv)
    settings_path = os.path.expanduser(args.settings)
    shim_path = args.shim or _default_shim_path()

    if args.action == "install":
        print(install(settings_path, shim_path))
    else:
        print(uninstall(settings_path, shim_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
