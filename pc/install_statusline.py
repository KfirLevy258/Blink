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
import sys

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


def statusline_command(shim_path: str) -> str:
    r"""The exact string to write into statusLine.command.

    On Windows this is `bash <path>`, not `sh <path>`, and that is not a
    stylistic choice. Claude Code rewrites a Windows status line command that
    mentions a .sh file:

        if (D && !f && v.trim().match(/\.sh(\s|$|")/))
            if (!v.trim().startsWith("bash ")) v = `bash ${v}`

    So `sh C:/.../clauge-statusline.sh` becomes `bash sh C:/.../...`, and bash
    then looks for a script named literally "sh" and fails on every render.
    Starting with "bash " opts out of that rewrite.

    The path is also written with forward slashes: the command runs under
    bash, where a backslash is an escape character, so C:\Users\... would be
    mangled. Git Bash accepts C:/Users/... unchanged.
    """
    if sys.platform == "win32":
        return f"bash {shlex.quote(shim_path.replace(chr(92), '/'))}"
    return f"sh {shlex.quote(shim_path)}"


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


class SettingsUnreadable(Exception):
    """settings.json is there but cannot be parsed.

    Its own type because the only safe response is to stop. Treating an
    unparseable file as {} would have us write a fresh one over whatever the
    customer actually had -- and a file that fails to parse is usually a file
    someone is halfway through editing, not a file they wanted replaced.
    """


def _load(settings_path: str) -> dict:
    try:
        # utf-8 explicitly: Windows would otherwise decode with the ANSI code
        # page and die on any non-ASCII character anywhere in the file.
        with open(settings_path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError) as e:
        raise SettingsUnreadable(f"{settings_path} is not valid JSON ({e})")


def _sniff_format(settings_path: str):
    """(indent, trailing_newline) matching the existing file, so a
    hand-formatted settings.json is not silently reformatted on every
    install/uninstall. Defaults (2 spaces, trailing newline) apply when the
    file doesn't exist yet or gives no indentation to sniff (e.g. it's a
    single-line/minified document).
    """
    try:
        with open(settings_path, encoding="utf-8") as f:
            text = f.read()
    except (FileNotFoundError, UnicodeDecodeError):
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
    # The temp file is a sibling of the target (it has to be, for os.replace to
    # be atomic), so an absent parent directory fails the write rather than the
    # read that came before it. ~/.claude is absent on a machine where Claude
    # Code has never written a setting -- a case install() otherwise handles
    # fine, since _load() already treats a missing file as {}.
    parent = os.path.dirname(settings_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as f:
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


def _is_ours(current: str, expected: str = None) -> bool:
    """Is this statusLine.command one WE wrote?

    Two ways to be ours, and both are needed:

      - it matches the marker file, which records the exact string the last
        install wrote. Survives a shim path that has since changed, which the
        text comparison alone cannot.
      - it matches what we WOULD write for this shim path. Survives a marker
        file that was lost -- deleted ~/.clauge, a restore from backup.

    A foreign command can equal neither, short of a customer literally choosing
    our exact former command text, which is the irreducible edge in any
    exact-match scheme.

    One function because it was three, written out longhand at each call site
    with different operand orders and one of them missing the marker check.
    Whether a command is ours decides whether it gets preserved or overwritten,
    so three nearly-identical spellings of it is three chances to lose someone's
    status line.
    """
    if not current:
        return False
    return current == _read_marker() or (expected is not None
                                         and current == expected)


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
    new_command = statusline_command(shim_path)

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
    if previous and not _is_ours(previous, new_command):
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

    expected = statusline_command(shim_path) if shim_path else None
    if not _is_ours(current, expected):
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


def _announce(settings_path: str, shim_path: str, undo_hint: str = None) -> None:
    """Say what is about to change, before changing it.

    Install is deliberately unattended -- it asks nothing, because the product
    is meant to be plug-and-play. That makes disclosure the only thing standing
    between us and silently editing a file the user owns, so it is not
    optional and it runs before the first write, not after. Printed even when
    stdout is redirected: a log that records what changed is the point.
    """
    previous = (_load(settings_path).get("statusLine") or {}).get("command", "")
    new_command = statusline_command(shim_path)
    # The SAME is_ours test install() applies, for the same reason it applies
    # it: what happens to `previous` depends entirely on whether it is a
    # customer command or our own shim from an earlier install. Reading
    # `previous` without that test described an upgrade-in-place as "your
    # command is recorded and still runs" -- said of a command that was
    # (correctly) not recorded at all, because the real customer command was
    # already in the chain file. A disclosure that guesses at the branch is
    # worse than no disclosure: it is the one thing here nobody can verify
    # afterwards.
    is_ours = _is_ours(previous, new_command)
    print("Clauge is about to change one setting in Claude Code.")
    print()
    print(f"  File     {settings_path}")
    print("  Key      statusLine.command  (nothing else in the file is touched)")
    if previous and is_ours:
        chained = ""
        try:
            with open(_chain_path()) as f:
                chained = f.read().strip()
        except OSError:
            pass
        print(f"  Was      {previous}")
        print(f"  Now      {new_command}")
        print()
        print("  That is Clauge's own shim from an earlier install, so this")
        print("  updates it in place rather than recording it.")
        if chained:
            print("  The status line it runs after capturing usage is unchanged:")
            print(f"    {chained}")
    elif previous:
        print(f"  Was      {previous}")
        print(f"  Now      {new_command}")
        print()
        print("  Your existing status line keeps working -- Clauge records the")
        print("  command above and runs it after capturing usage, so your bar")
        print("  renders exactly as before.")
    else:
        print("  Was      (no status line configured)")
        print(f"  Now      {new_command}")
    print()
    print(f"  To undo:  {undo_hint or 'python3 -m pc.install_statusline uninstall'}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())


# There is no main() here any more, nor a __main__ guard, nor
# _default_shim_path(). This module was once runnable on its own; pc/cli.py now
# calls _load, _announce, install and uninstall directly, no test invoked the
# entry point, and a second way to edit someone's settings.json -- with its own
# argument parsing and its own default shim path -- is a second thing to keep
# correct for no one's benefit.
