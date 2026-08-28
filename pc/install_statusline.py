"""Install the Blink statusline shim into a user's Claude Code settings.

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

CHAIN_PATH = "~/.blink/statusline-chain"

# Records the exact command string the last install() call wrote to
# statusLine, so a later install() can recognize "this is our own shim"
# without pattern-matching the command text. A substring check on the
# command (e.g. "blink-statusline.sh" in previous) is not safe: a
# customer's own script can legitimately contain the shim's filename as a
# substring (a wrapper literally named
# "wrap-blink-statusline.sh-backup.sh", say), and a naive check mistakes
# that for "already installed" -- silently discarding the customer's real
# command with no way to recover it via uninstall(). Comparing against what
# we ourselves last wrote is exact.
#
# This marker is NOT the only way install() recognizes its own command --
# see the is_ours check in install() below. The marker lives in ~/.blink,
# the same directory the shim itself uses as transient scratch space
# (statusline.json), so it is plausible for a user or a cleanup script to
# wipe the whole directory independently of settings.json. A design that
# trusted the marker alone would, after that wipe, see its own previous
# command as unrecognized on the next same-path install and chain it into
# the chain file -- recreating the exact self-invocation loop this file
# exists to prevent, just by a different route. install() therefore treats
# a command as "ours" if it matches EITHER the marker OR the command this
# very call would itself write, so losing ~/.blink does not resurrect the
# bug for the common case (reinstalling at an unchanged shim_path).
INSTALLED_MARKER_PATH = "~/.blink/statusline-installed-command"


def statusline_command(shim_path: str) -> str:
    r"""The exact string to write into statusLine.command.

    On Windows this is `bash <path>`, not `sh <path>`, and that is not a
    stylistic choice. Claude Code rewrites a Windows status line command that
    mentions a .sh file:

        if (D && !f && v.trim().match(/\.sh(\s|$|")/))
            if (!v.trim().startsWith("bash ")) v = `bash ${v}`

    So `sh C:/.../blink-statusline.sh` becomes `bash sh C:/.../...`, and bash
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
        with open(_marker_path(), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write_marker(command: str) -> None:
    os.makedirs(os.path.dirname(_marker_path()), exist_ok=True)
    with open(_marker_path(), "w", encoding="utf-8") as f:
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
            data = json.load(f)
    except FileNotFoundError:
        return {}
    except (ValueError, UnicodeDecodeError) as e:
        raise SettingsUnreadable(f"{settings_path} is not valid JSON ({e})")
    if not isinstance(data, dict):
        # Valid JSON, wrong shape: `[]`, `null`, a bare string. Everything
        # downstream does data.get(...), so this used to surface as an
        # AttributeError from five different call sites -- and in the daemon
        # the drift watchdog's call is on a path that catches only
        # SettingsUnreadable, so an unhandled one killed main() and left
        # KeepAlive restarting it every ten seconds forever.
        #
        # Refusing is also the right answer on the merits: a settings.json
        # that is not a JSON object is not a file we can safely edit, which is
        # exactly what SettingsUnreadable already means to every caller.
        raise SettingsUnreadable(
            f"{settings_path} is valid JSON but not an object"
            f" (it is {type(data).__name__})")
    return data


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
    # Follow a symlink to its target before writing.
    #
    # os.replace() replaces the LINK, so a dotfiles-managed
    # ~/.claude/settings.json -> ~/dotfiles/settings.json became a plain file
    # holding our edit while the original sat orphaned in the repo, still
    # tracked, still showing clean in git status. Every later Claude Code edit
    # then went to the new file. shutil.copymode below follows the link for
    # permissions, which made the loss even harder to notice.
    #
    # realpath, not readlink: a chain of links, or a link into a linked
    # directory, both have to land on the real file.
    if os.path.islink(settings_path):
        settings_path = os.path.realpath(settings_path)

    tmp = settings_path + ".blink-tmp"
    # The temp file is a sibling of the target (it has to be, for os.replace to
    # be atomic), so an absent parent directory fails the write rather than the
    # read that came before it. ~/.claude is absent on a machine where Claude
    # Code has never written a setting -- a case install() otherwise handles
    # fine, since _load() already treats a missing file as {}.
    parent = os.path.dirname(settings_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    # Created 0600 and only widened afterwards, never the other way round.
    #
    # This file can legitimately hold env.ANTHROPIC_API_KEY or apiKeyHelper.
    # open(tmp, "w") created it at the process umask -- typically 0644 -- and
    # copymode narrowed it afterwards, which left the secrets world-readable
    # for the length of the write, and permanently if the process died between
    # the two (the daemon's drift watchdog runs this unattended). A stale temp
    # from such a death is removed first, so O_EXCL cannot refuse forever.
    try:
        os.remove(tmp)
    except OSError:
        pass
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent)
        if trailing_newline:
            f.write("\n")
    # A customer who chmod'd it 0600 gets 0600 back; one who left it at the
    # default gets the default back, not a silently narrowed copy either.
    if os.path.exists(settings_path):
        shutil.copymode(settings_path, tmp)
    os.replace(tmp, settings_path)


def _current_command(data: dict) -> str:
    """settings.json's statusLine.command, or "" -- never an exception.

    `statusLine` is the customer's key as much as ours, and nothing stops it
    holding a string, a list or null. `(data.get("statusLine") or {}).get(...)`
    reads as safe and is not: only None and {} take the `or` branch, so
    `{"statusLine": "my-bar"}` reached .get() on a str and raised.
    """
    sl = data.get("statusLine")
    if not isinstance(sl, dict):
        return ""
    cmd = sl.get("command", "")
    return cmd if isinstance(cmd, str) else ""


def _is_ours(current: str, expected: str = None) -> bool:
    """Is this statusLine.command one WE wrote?

    Two ways to be ours, and both are needed:

      - it matches the marker file, which records the exact string the last
        install wrote. Survives a shim path that has since changed, which the
        text comparison alone cannot.
      - it matches what we WOULD write for this shim path. Survives a marker
        file that was lost -- deleted ~/.blink, a restore from backup.

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
    previous = _current_command(data)
    # shlex.quote: an unquoted path is one space away from a silent no-op on
    # macOS (a very live case -- "/Users/kfir/Application Support/..."). An
    # unquoted `sh /a b/c` splits into three argv words and does nothing.
    # The shim's own self-invocation guard (`[ "$chain_cmd" != "sh $0" ]`)
    # has to keep agreeing with whatever quoting we do here -- see
    # tools/blink-statusline.sh, which mirrors shlex.quote's exact rule in
    # shell so the two sides never drift apart.
    new_command = statusline_command(shim_path)

    os.makedirs(os.path.dirname(_chain_path()), exist_ok=True)
    # Guard against chaining the shim to itself. `previous` counts as ours
    # if EITHER check holds, never by pattern-matching the text:
    #   - stateless: it equals the command THIS call is about to write.
    #     Needs no file to have survived, so a same-path reinstall is still
    #     recognized correctly even if ~/.blink (and the marker in it) was
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
        with open(_chain_path(), "w", encoding="utf-8") as f:
            f.write(previous + "\n")
        chained = f"chained previous statusline: {previous}"
    elif not previous and not marker:
        # Absent statusLine key AND no marker from any earlier install --
        # nothing ties a chain file to a still-live Blink install, so if
        # one exists here it is a ghost from something else entirely (a
        # hand-placed file, leftovers from an unrelated flow). Clear it --
        # otherwise a later uninstall() would "restore" that ghost command
        # as if it were the customer's real previous statusline.
        #
        # Checking the marker (not just "statusLine is absent") matters: a
        # marker surviving from an earlier install means that install's
        # chain content, if any, may still hold the real pre-Blink
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
        # hold the real pre-Blink original and must not be touched.
        chained = "no previous statusline to chain"

    data["statusLine"] = {"type": "command", "command": new_command}
    _save(settings_path, data, indent, trailing_newline)
    _write_marker(new_command)
    return f"Blink statusline installed ({chained})."


def uninstall(settings_path: str, shim_path: str = None) -> str:
    """Undo install(), but only when it is safe to.

    install() has an is_ours guard before it touches statusLine; uninstall()
    needs the exact same guard, or symmetrically. Two ways this goes wrong
    without one:
      - the customer installs Blink, later points statusLine at a NEW
        command of their own (editing settings.json directly, bypassing
        uninstall), then runs uninstall -- which must leave their new
        command alone, not clobber it with stale chain-file content that
        predates it.
      - ~/.blink is wiped, or uninstall runs having never installed --
        data.pop("statusLine") would then delete a command Blink never
        wrote, with no way to recover it.
    So: only touch statusLine when the command currently sitting there is
    recognisably ours -- it matches the marker install() recorded, or (when
    the caller passes shim_path, as the CLI does) it equals the command
    install() would write for that path today. Anything else is left
    completely alone; we say so rather than guessing.
    """
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    current = _current_command(data)

    if not current:
        return "No Blink statusline installed; nothing to do."

    expected = statusline_command(shim_path) if shim_path else None
    if not _is_ours(current, expected):
        # Do not touch settings.json, the chain file, or the marker: we
        # cannot tell what this command is, and guessing wrong here is the
        # unrecoverable failure mode this function exists to avoid.
        return ("Current statusline isn't Blink's (changed since install); "
                "leaving it alone.")

    previous = ""
    try:
        with open(_chain_path(), encoding="utf-8") as f:
            previous = f.read().strip()
    except OSError:
        pass

    if previous:
        data["statusLine"] = {"type": "command", "command": previous}
        msg = f"Restored previous statusline: {previous}"
    else:
        data.pop("statusLine", None)
        msg = "Removed the Blink statusline."

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
    previous = _current_command(_load(settings_path))
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
    print("Blink is about to change one setting in Claude Code.")
    print()
    print(f"  File     {settings_path}")
    print("  Key      statusLine.command  (plus the hooks entries listed above;")
    print("           nothing else in the file is touched)")
    if previous and is_ours:
        chained = ""
        try:
            with open(_chain_path(), encoding="utf-8") as f:
                chained = f.read().strip()
        except OSError:
            pass
        print(f"  Was      {previous}")
        print(f"  Now      {new_command}")
        print()
        print("  That is Blink's own shim from an earlier install, so this")
        print("  updates it in place rather than recording it.")
        if chained:
            print("  The status line it runs after capturing usage is unchanged:")
            print(f"    {chained}")
    elif previous:
        print(f"  Was      {previous}")
        print(f"  Now      {new_command}")
        print()
        print("  Your existing status line keeps working -- Blink records the")
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


# --- anti-drift watchdog --------------------------------------------------
#
# statusLine is a single slot in a file we share with the user and with Claude
# Code's own updates. Anything that rewrites settings.json can drop our command
# without telling anyone, and the symptom is not an error -- it is a panel that
# quietly stops updating while the daemon goes on reporting success. The
# desktop cache makes that worse rather than better: it keeps feeding numbers,
# so the board still looks alive for hours after the CLI hook has gone.
#
# The one thing this must NOT do is fight the user. The distinction it draws:
#
#   marker present, hook gone   -> something wiped it. Put it back.
#   marker absent               -> uninstall() removed it, which is intent.
#                                  Leave it alone forever.
#
# That is why uninstall() removing the marker is load-bearing here, and why
# this checks the marker before it checks anything else.

# Enough reinstalls to survive an update that rewrites settings.json a couple
# of times, and few enough that a genuine disagreement with something else on
# the machine ends in a log line rather than an endless write fight.
MAX_REINSTATEMENTS = 3

WATCHDOG_DISABLE_ENV = "BLINK_NO_WATCHDOG"


def drift_check(settings_path: str, shim_path: str):
    """Put our statusLine hook back if something removed it.

    Returns a description of what it did, or None when there was nothing to
    do. Never raises: this runs inside the daemon's poll loop, and a daemon
    that dies because settings.json was briefly unreadable is a worse outcome
    than a hook that stays missing for another sixty seconds.
    """
    if os.environ.get(WATCHDOG_DISABLE_ENV):
        return None

    # Never installed, or deliberately uninstalled. Both mean hands off.
    if not _read_marker():
        return None

    try:
        data = _load(settings_path)
    except SettingsUnreadable:
        # Usually a file someone is halfway through editing. install() would
        # refuse to write over it too; refusing here keeps that promise
        # rather than waiting to discover it one layer down.
        return None
    except Exception:
        return None

    current = _current_command(data)
    expected = statusline_command(shim_path)
    if current == expected:
        return None

    # Classify BEFORE install(), not after. install() writes a fresh marker,
    # and _is_ours() consults that marker -- so asking afterwards compares the
    # old command against the new marker, never matches, and reports every
    # moved shim as a foreign replacement. Caught by
    # test_a_moved_shim_is_repointed, which is the case an update to this
    # program creates every single time it moves the binary.
    if not current:
        what = "statusline hook had been removed; restored it"
    elif _is_ours(current, expected):
        # Ours by the marker but not the command we would write now -- the
        # shim moved, which is what an update to this program does.
        what = "statusline hook pointed at an old shim path; repointed it"
    else:
        what = ("statusline hook had been replaced; restored it and chained"
                f" the replacement: {current}")

    try:
        install(settings_path, shim_path)
    except Exception as e:
        return f"statusline hook is missing and could not be restored: {e}"
    return what


class DriftWatchdog:
    """drift_check on an interval, with a cap on how hard it insists.

    Interval rather than a file watcher on purpose. A watcher would need a
    dependency this daemon does not otherwise have, for a fault that is
    measured in "since the last CLI update" rather than in milliseconds --
    and the poll loop it rides on is already running.
    """

    def __init__(self, settings_path, shim_path, interval_s=300.0,
                 now=None, check=drift_check):
        import time as _time
        self._settings = settings_path
        self._shim = shim_path
        self._interval = interval_s
        self._now = now or _time.monotonic
        self._check = check
        self._next = self._now()
        self._reinstatements = 0
        self._gave_up = False

    def tick(self):
        """Returns a message worth logging, or None. Call it as often as you like."""
        if self._gave_up:
            return None
        now = self._now()
        if now < self._next:
            return None
        self._next = now + self._interval

        msg = self._check(self._settings, self._shim)
        if msg is None:
            return None

        self._reinstatements += 1
        if self._reinstatements >= MAX_REINSTATEMENTS:
            self._gave_up = True
            return (f"{msg}. That is {self._reinstatements} times now --"
                    " something on this machine keeps removing it, so Blink"
                    " will stop putting it back. Run `blink install` once"
                    " the conflict is resolved.")
        return msg
