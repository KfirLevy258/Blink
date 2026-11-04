"""Install the Blink statusline shim into a user's Claude Code settings.

We are editing a file the user owns and did not ask us to touch beyond this one
key. Two rules follow: never lose their existing statusline command (it goes in
the chain file, and uninstall puts it back verbatim), and never rewrite any key
but `statusLine`.
"""
import glob
import hashlib
import json
import os
import shlex
import secrets
import shutil
import stat
import sys
import time

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
        return f"bash {windows_bash_path(shim_path)}"
    return posix_statusline_command(shim_path)


def windows_bash_path(path: str) -> str:
    r"""A path for a bash command line on Windows, spelled in ASCII.

    Claude Code hands hook and status line commands to Git Bash. A command
    with a character outside ASCII in it does not survive the hand-over: on
    a Windows 10 machine whose user name is Hebrew (2026-08-29), bash was
    given the whole of `bash 'C:/Users/משתמש/.blink/blink-hook.sh' SessionEnd`
    as ONE argument -- a script name -- and reported no such file, on every
    hook and every status line render. The identical command with an ASCII
    path ran. So no hook fired and no usage figure was ever written, and a
    board on that machine sat on "connecting 2/2" for good.

    Anything under the profile is therefore written as "$USERPROFILE/...":
    pure ASCII on the wire, expanded by bash itself after the hand-over,
    and it was seen to run on that machine. Forward slashes throughout,
    since a backslash is an escape under bash; bash on Windows takes the
    mixed C:\Users\...\/.blink/x.sh that the expansion produces.
    """
    p = path.replace(chr(92), "/")
    home = os.path.expanduser("~").replace(chr(92), "/").rstrip("/")
    if home and p.lower().startswith(home.lower() + "/"):
        return '"$USERPROFILE' + p[len(home):] + '"'
    return shlex.quote(p)


def _blink_dir(home):
    """The folder the marker and chain file of `home` live in. With no home,
    ~/.blink: the native home's marker_dir, which no record moves, so the
    no-argument callers need not read config.json to learn it."""
    if home is None:
        return os.path.join(os.path.expanduser("~"), ".blink")
    return home.marker_dir


def _chain_path(home=None):
    """The command install() displaced, kept for uninstall() to put back.

    Natively ~/.blink/statusline-chain, as it always was. For a WSL home it
    is the copy inside the distribution, which the shim there runs."""
    # The literal, not claude_home.WSL: claude_home imports user_config,
    # which imports this module, so importing it here at the top would be
    # a cycle, and a lazy import for one string is not worth it.
    if home is not None and home.kind == "wsl":
        return home.inside(".blink", os.path.basename(CHAIN_PATH))
    return os.path.join(_blink_dir(home), os.path.basename(CHAIN_PATH))


def _marker_path(home=None):
    return os.path.join(_blink_dir(home),
                        os.path.basename(INSTALLED_MARKER_PATH))


def _read_marker(home=None) -> str:
    try:
        with open(_marker_path(home), encoding="utf-8") as f:
            return f.read().strip()
    except (OSError, UnicodeDecodeError):
        # UnicodeDecodeError is a ValueError, not an OSError, so a marker
        # file that is not valid UTF-8 (truncated write, disk corruption)
        # needs its own arm here or it slips past this except -- straight
        # into drift_check and shim_content_check, neither of which guards
        # this call, and from there into DriftWatchdog.tick(), which the
        # daemon's wait loop calls unguarded. Every caller already treats ""
        # as "no marker" -- the same answer a missing file gets -- so that is
        # the right answer for an unreadable one too.
        return ""


def _write_marker(command: str, home=None) -> None:
    os.makedirs(os.path.dirname(_marker_path(home)), exist_ok=True)
    with open(_marker_path(home), "w", encoding="utf-8") as f:
        f.write(command + "\n")


def _remove_marker(home=None) -> None:
    try:
        os.remove(_marker_path(home))
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
        with open(settings_path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        return {}
    return _parse_doc(raw, settings_path)


def _parse_doc(raw: bytes, label: str) -> dict:
    """settings.json's bytes as a document, or SettingsUnreadable naming
    `label`. Apart from _load so a WSL install, which reads the bytes
    through wsl.exe rather than from a file, refuses exactly what a native one
    does; `label` is then a description, never a path inside Linux."""
    try:
        # utf-8 explicitly: Windows would otherwise decode with the ANSI code
        # page and die on any non-ASCII character anywhere in the file.
        # utf-8-SIG, because Windows PowerShell 5's Set-Content -Encoding utf8
        # writes a byte-order mark, json.load refuses a document that starts
        # with one, and a file someone fixed by hand that way would read as
        # unparseable. Identical to utf-8 for every file without a BOM.
        data = json.loads(raw.decode("utf-8-sig"))
    except (ValueError, UnicodeDecodeError) as e:
        raise SettingsUnreadable(f"{label} is not valid JSON ({e})")
    except RecursionError:
        # Arrays nested deeper than the parser's stack: not a ValueError,
        # and it used to escape every caller that promised to handle a bad
        # file -- `blink config` among them.
        raise SettingsUnreadable(f"{label} is not valid JSON"
                                 " (nested too deeply)")
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
            f"{label} is valid JSON but not an object"
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
    return _sniff_text(text)


def _sniff_text(text: str):
    """_sniff_format on text already read: a WSL install has the bytes from
    wsl.exe, not a file to open."""
    trailing_newline = text.endswith("\n")
    indent = 2
    for line in text.splitlines():
        stripped = line.lstrip(" \t")
        leading = line[: len(line) - len(stripped)]
        if leading:
            indent = leading if "\t" in leading else len(leading)
            break
    return indent, trailing_newline


def _dump(data: dict, indent, trailing_newline: bool) -> str:
    """The text _save writes, for a writer that is not _save."""
    return json.dumps(data, indent=indent) + ("\n" if trailing_newline else "")


# How old a temp sibling must be before a write may sweep it. A write takes
# milliseconds, so ten minutes cannot be one still in flight -- it is one
# that was killed between creating the temp file and renaming it.
STALE_TEMP_S = 10 * 60

_TEMP_SUFFIX = ".blink-tmp"


def temp_siblings(path: str):
    """Every temp file _save may have left beside `path`, sorted.

    The one place that knows the naming, so the sweep below and uninstall's
    file list cannot drift apart from what _save actually creates. That
    includes the fixed name every release up to 1.3.4 used,
    `<path>.blink-tmp`, which no writer of this version creates.
    """
    found = glob.glob(glob.escape(path) + ".*" + _TEMP_SUFFIX)
    legacy = path + _TEMP_SUFFIX
    if os.path.lexists(legacy):
        found.append(legacy)
    return sorted(found)


def _remove(path: str) -> None:
    """os.remove, and on a refusal clear the read-only bit and try once more.

    Windows will not delete a read-only file at all -- a copy carried over
    from a synced folder, or one an editor marked -- and these temp files
    are copies of somebody's settings, which is the reason to get rid of
    them rather than shrug.
    """
    try:
        os.remove(path)
    except PermissionError:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        os.remove(path)


def _sweep_stale_temps(path: str, now=None) -> None:
    """Remove temp siblings a killed write left behind.

    They matter more than their size: each is a copy of settings.json, which
    can hold ANTHROPIC_API_KEY, under a name nothing would ever look for
    again. The random names only once they are stale -- a fresh one may be
    another writer's, mid-write. The legacy fixed name always: nothing of
    this version writes it, so it is never in flight, and the oldest
    releases created it at the umask, readable by every account.
    """
    now = time.time() if now is None else now
    legacy = path + _TEMP_SUFFIX
    for t in temp_siblings(path):
        try:
            if t == legacy or now - os.path.getmtime(t) > STALE_TEMP_S:
                _remove(t)
        except OSError:
            pass


# How many random names to try before giving up. A collision on 64 random
# bits is a once-in-the-universe event; ten is for a directory full of
# leftovers, not for odds.
_TEMP_TRIES = 10


def _open_temp(path: str):
    """(fd, name) for a new private temp file beside `path`.

    Not tempfile.mkstemp. On Python 3.11 -- the interpreter the Windows build
    ships -- mkstemp reads PermissionError as "that name is taken" and tries
    the next one, up to os.TMP_MAX, which on Windows is about two billion:
    a folder that refuses new files hung install, uninstall, `blink config`
    and the drift watchdog's repair on the daemon's serial loop. Here only a
    taken name is retried, and only a few times; anything else is the answer.
    """
    flags = (os.O_WRONLY | os.O_CREAT | os.O_EXCL
             | getattr(os, "O_BINARY", 0))
    for attempt in range(_TEMP_TRIES):
        name = f"{path}.{secrets.token_hex(8)}{_TEMP_SUFFIX}"
        try:
            return os.open(name, flags, 0o600), name
        except FileExistsError:
            if attempt + 1 == _TEMP_TRIES:
                raise


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
    # the two (the daemon's drift watchdog runs this unattended).
    #
    # _open_temp creates it 0600 under a name nobody else holds. The fixed
    # name this used to use let two writers -- `blink config` and the
    # daemon's watchdog, or two shells -- share one temp file, and a write
    # that failed left it behind for the next one to trip on; now a failure
    # removes its own temp file on the way out.
    _sweep_stale_temps(settings_path)
    fd, tmp = _open_temp(settings_path)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent)
            if trailing_newline:
                f.write("\n")
        # A customer who chmod'd it 0600 gets 0600 back; one who left it at
        # the default gets the default back, not a silently narrowed copy.
        if os.path.exists(settings_path):
            shutil.copymode(settings_path, tmp)
        # Retried on Windows' WinError 5 / 32 only: an editor or an
        # antivirus scan holding the target for a moment is not a failed
        # write. POSIX has no such state -- EACCES there is a real refusal,
        # and retrying it only made a failed write wait three seconds.
        from pc import update
        update._replace(tmp, settings_path, posix_retry=False)
    except BaseException:
        try:
            _remove(tmp)
        except OSError:
            pass
        raise


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


def _is_ours(current: str, expected: str = None, marker: str = "") -> bool:
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

    The marker's text is passed in, not read here: there is one marker per
    home (claude_home), and a WSL install's lives in its own folder, so only
    the caller knows which file to read. A read in here once took the native
    one whatever home it was asked about (cli._holds_ours, Task 2's review).
    """
    if not current:
        return False
    return (bool(marker) and current == marker) or (
        expected is not None and current == expected)


def merge_install(doc: dict, marker: str, chain_text, shim_cmd: str):
    """install() on a document: (doc, new_marker, chain_action, message).

    Pure -- no file is read or written -- so a WSL install, which reads
    settings.json through wsl.exe and writes it back inside Linux, applies
    exactly the rules a native install does, and the two cannot drift apart.
    `doc` is changed in place and returned.

    chain_action is what to do with the chain file: ("write", text),
    ("delete",), or None to leave it as it is. new_marker is what to record.

    `chain_text` is what the chain file holds now, or None. Install decides
    without it -- whether the command there is ours is the whole question,
    and the chain file is never consulted to answer it -- but merge_uninstall
    needs it, and the two take the same arguments so a caller reads the same
    things for both.
    """
    del chain_text  # see the docstring
    previous = _current_command(doc)
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
    if previous and not _is_ours(previous, shim_cmd, marker):
        action = ("write", previous + "\n")
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
        action = ("delete",)
        chained = "no previous statusline to chain"
    else:
        # Either previous is ours (a reinstall over our own shim), or
        # statusLine is currently absent but a marker survives from an
        # earlier install. Either way the chain file, if any, may still
        # hold the real pre-Blink original and must not be touched.
        action = None
        chained = "no previous statusline to chain"

    doc["statusLine"] = {"type": "command", "command": shim_cmd}
    return doc, shim_cmd, action, f"Blink statusline installed ({chained})."


def merge_uninstall(doc: dict, marker: str, chain_text, shim_cmd=None):
    """uninstall() on a document: (doc, new_marker, chain_action, message).

    doc is None when settings.json must not be written at all -- nothing of
    ours is there, or what is there is not recognisably ours -- and then the
    marker and chain file are returned untouched too. Otherwise new_marker
    is "" (remove it) and the chain file is deleted, its command, if any,
    put back in statusLine. `shim_cmd` is the command install would write
    today, or None to go by the marker alone.

    Why this needs install()'s is_ours guard, symmetrically. Two ways this
    goes wrong without one:
      - the customer installs Blink, later points statusLine at a NEW
        command of their own (editing settings.json directly, bypassing
        uninstall), then runs uninstall -- which must leave their new
        command alone, not clobber it with stale chain-file content that
        predates it.
      - ~/.blink is wiped, or uninstall runs having never installed --
        data.pop("statusLine") would then delete a command Blink never
        wrote, with no way to recover it.
    """
    current = _current_command(doc)
    if not current:
        return None, marker, None, "No Blink statusline installed; nothing to do."
    if not _is_ours(current, shim_cmd, marker):
        # Do not touch settings.json, the chain file, or the marker: we
        # cannot tell what this command is, and guessing wrong here is the
        # unrecoverable failure mode this function exists to avoid.
        return (None, marker, None,
                "Current statusline isn't Blink's (changed since install); "
                "leaving it alone.")
    previous = (chain_text or "").strip()
    if previous:
        doc["statusLine"] = {"type": "command", "command": previous}
        msg = f"Restored previous statusline: {previous}"
    else:
        doc.pop("statusLine", None)
        msg = "Removed the Blink statusline."
    return doc, "", ("delete",), msg


def _read_chain(home=None):
    try:
        with open(_chain_path(home), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return None


def _apply_chain(action, home=None) -> None:
    if action is None:
        return
    if action[0] == "write":
        with open(_chain_path(home), "w", encoding="utf-8") as f:
            f.write(action[1])
        return
    try:
        os.remove(_chain_path(home))
    except OSError:
        pass


def install(settings_path: str, shim_path: str, home=None) -> str:
    """Point statusLine at the shim, chaining whatever was there.

    `home` (claude_home.ClaudeHome) names the marker and chain file; None
    is the native home's, ~/.blink, as it always was. The decisions are
    merge_install's; this reads and writes the native files around it."""
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    # shlex.quote: an unquoted path is one space away from a silent no-op on
    # macOS (a very live case -- "/Users/kfir/Application Support/..."). An
    # unquoted `sh /a b/c` splits into three argv words and does nothing.
    # The shim's own self-invocation guard (`[ "$chain_cmd" != "sh $0" ]`)
    # has to keep agreeing with whatever quoting we do here -- see
    # tools/blink-statusline.sh, which mirrors shlex.quote's exact rule in
    # shell so the two sides never drift apart.
    new_command = statusline_command(shim_path)
    os.makedirs(os.path.dirname(_chain_path(home)), exist_ok=True)
    data, marker, action, msg = merge_install(
        data, _read_marker(home), None, new_command)
    # The chain file before settings.json, as it always was: a crash between
    # the two leaves the customer's command recorded and settings unchanged,
    # never settings pointing at us with their command recorded nowhere.
    _apply_chain(action, home)
    _save(settings_path, data, indent, trailing_newline)
    _write_marker(marker, home)
    return msg


def uninstall(settings_path: str, shim_path: str = None, home=None) -> str:
    """Undo install(), but only when it is safe to (merge_uninstall says
    when): only touch statusLine when the command currently sitting there
    is recognisably ours -- it matches the marker install() recorded, or
    (when the caller passes shim_path, as the CLI does) it equals the
    command install() would write for that path today. Anything else is
    left completely alone; we say so rather than guessing.

    `home` names the marker and chain file, as for install().
    """
    indent, trailing_newline = _sniff_format(settings_path)
    data = _load(settings_path)
    expected = statusline_command(shim_path) if shim_path else None
    data, marker, action, msg = merge_uninstall(
        data, _read_marker(home), _read_chain(home), expected)
    if data is None:
        return msg
    _save(settings_path, data, indent, trailing_newline)
    _apply_chain(action, home)
    _remove_marker(home)
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
    is_ours = _is_ours(previous, new_command, _read_marker())
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


# --- inside a WSL distribution ---------------------------------------------
#
# Claude Code runs inside Linux while the daemon stays on Windows with the
# board (docs/plans/enterprise-spend.md, section 4). settings.json is merged
# here, on Windows, by merge_install -- the code a native install runs -- and
# only the bytes cross into the distribution, through wsl.exe, as the user.
#
# Not written through the UNC path (\\wsl.localhost\...), though Windows can
# open one. os.replace there replaces a symlink with a plain file, which is
# the dotfiles loss _save's comment records, and it writes as whatever 9P
# maps the Windows account to rather than as the user Claude Code runs as.
# A script inside Linux can follow the link (readlink -f), re-check the
# file did not change since it was read, and mv a temp file beside the real
# target, which keeps the link and is atomic on the target's own filesystem.
#
# Nothing below names a path inside the distribution in an exception or a
# message: the drift check's messages go into bridge.log, the file customers
# send us, and a Linux path is somebody's user name and project folder.

# claude_home.WSL, as a literal for the import-cycle reason _chain_path gives.
_WSL = "wsl"

# Exit status of the write script for "the file is not the one we read".
_CHANGED = 75

# Read one file, following links to the real one. Prints the resolved path,
# then "present" or "absent", then the bytes. A resolved path with a newline
# in it would make that first line ambiguous, so it is refused rather than
# parsed. readlink -f fails on some systems for a file that does not exist
# yet -- the first install on a fresh account -- and the path as given is
# then the right answer, since there is no link to follow.
_READ = r"""p=$1
t=$(readlink -f -- "$p" 2>/dev/null) && [ -n "$t" ] || t=$p
case $t in *'
'*) exit 65 ;; esac
printf '%s\n' "$t"
if [ -e "$t" ]; then
  printf 'present\n'
  exec cat -- "$t"
fi
printf 'absent\n'
"""

# Write stdin to $1, atomically, refusing if $1 is not what was read: $2 is
# the SHA-256 of the bytes read, "absent" for a file that was not there, or
# "-" for a file of ours that nobody else writes. $3 is "keep" (the mode the
# customer gave settings.json, 0600 for a new one, as _save does) or an
# octal mode.
#
# The temp file is a sibling of the target, named <target>.<pid>.blink-tmp
# -- the shape temp_siblings() matches -- because mv is only atomic within
# one filesystem, and the target is the resolved file, so a link into a
# dotfiles folder on another mount still gets an atomic rename. `cp -p`
# first to carry the mode and ownership over, then truncated by `cat >`; a
# new file is made 0600 under umask 077 and only widened after.
#
# The re-check and the mv are two steps, and Claude Code could write in
# between; _save has the same window natively, and it is milliseconds where
# the read-to-write gap the check closes is a round trip through wsl.exe.
# The traps remove the temp on any way out, including `timeout` killing the
# script, since each temp is a copy of settings that may hold an API key.
_WRITE = r"""t=$1 want=$2 mode=$3
case $t in /*) ;; *) exit 64 ;; esac
case $want in
  -) ;;
  absent) [ -e "$t" ] && exit 75 ;;
  *) [ -f "$t" ] || exit 75
     got=$(sha256sum < "$t") || exit 70
     [ "${got%% *}" = "$want" ] || exit 75 ;;
esac
d=${t%/*}
mkdir -p -- "${d:-/}" || exit 73
tmp=$t.$$.blink-tmp
trap 'rm -f -- "$tmp"' EXIT
trap 'exit 1' HUP INT TERM
if [ "$mode" = keep ] && [ -f "$t" ]; then
  cp -p -- "$t" "$tmp" || exit 73
else
  (umask 077 && : > "$tmp") || exit 73
  if [ "$mode" != keep ]; then chmod "$mode" "$tmp" || exit 73; fi
fi
cat > "$tmp" || exit 74
mv -f -- "$tmp" "$t" || exit 74
trap - EXIT
"""

# The Linux path of a Windows folder, and proof it is reachable from there.
# wslpath only converts; with automount off in /etc/wsl.conf it can still
# print /mnt/c/..., which then does not exist, hence the -d.
_OUT_DIR = r"""p=$(wslpath -u "$1" 2>/dev/null) && [ -n "$p" ] && [ -d "$p" ] || exit 1
printf '%s\n' "$p"
"""

_CAT_OR_NOTHING = 'cat -- "$1" 2>/dev/null; exit 0'
_RM = 'rm -f -- "$@"'

# The shims a WSL install puts beside the settings, inside the distribution.
WSL_SHIMS = ("blink-statusline.sh", "blink-hook.sh")


class WslInstallError(Exception):
    """An install or uninstall inside a distribution could not finish.

    The message is for the person at the keyboard and never holds a path
    inside Linux (see the section comment above)."""


def default_wsl_run(home):
    """The wsl_run every caller but a test uses: argv run inside `home`'s
    distribution as its Linux user, (rc, stdout, stderr) back."""
    from pc import wsl

    def run(argv, stdin=None):
        return wsl.run(home.distro_name, argv, user=home.linux_user,
                       stdin=stdin)
    return run


def posix_statusline_command(shim_path: str) -> str:
    """statusline_command as a POSIX shell spells it, whatever this
    computer is: what Claude Code inside Linux runs."""
    return f"sh {shlex.quote(shim_path)}"


def wsl_shim_path(home, name: str) -> str:
    """Where shim `name` lives inside the distribution, as Linux spells it:
    the Linux home's .blink, the same place a native install uses."""
    return home.linux_home.rstrip("/") + "/.blink/" + name


def wsl_statusline_command(home) -> str:
    """The status line command a WSL install writes: `sh '<linux_home>/
    .blink/blink-statusline.sh'`, so _is_ours and the shim's self-chain
    guard read it exactly as they read a native one."""
    return posix_statusline_command(wsl_shim_path(home, "blink-statusline.sh"))


def _linux_path(home, path: str) -> str:
    """The Linux spelling of `path`, a UNC path into `home`'s distribution
    (claude_home builds config_dir that way). Case-blind on the prefix, as
    Windows is about a distribution's name."""
    from pc import claude_home
    root = claude_home._UNC_ROOT + home.distro_name
    if path[:len(root)].lower() == root.lower() and (
            len(path) == len(root) or path[len(root)] == "\\"):
        return path[len(root):].replace("\\", "/") or "/"
    if path.startswith("/"):
        return path
    raise WslInstallError("Blink's record of this WSL distribution does not"
                          " name a folder inside it. Run `blink install"
                          " --wsl` again.")


class _WslSettings:
    """One read of settings.json inside the distribution."""

    def __init__(self, path, sha, doc, indent, trailing_newline):
        self.path = path        # the resolved file, as Linux spells it
        self.sha = sha          # SHA-256 of the bytes read, or "absent"
        self.doc = doc
        self.indent = indent
        self.trailing_newline = trailing_newline


def _wsl_read_settings(home, wsl_run) -> _WslSettings:
    linux = _linux_path(home, home.join(home.config_dir, "settings.json"))
    rc, out, _ = wsl_run(["sh", "-c", _READ, "sh", linux])
    head, _, rest = out.partition(b"\n")
    state, sep, body = rest.partition(b"\n")
    if rc != 0 or not sep or state not in (b"present", b"absent"):
        raise WslInstallError("Could not read Claude Code's settings inside"
                              " WSL.")
    try:
        path = head.decode("utf-8")
    except UnicodeDecodeError:
        raise WslInstallError("Could not read Claude Code's settings inside"
                              " WSL.")
    if state == b"absent":
        return _WslSettings(path, "absent", {}, 2, True)
    doc = _parse_doc(body, "Claude Code's settings.json inside WSL")
    indent, trailing = _sniff_text(body.decode("utf-8", "replace"))
    return _WslSettings(path, hashlib.sha256(body).hexdigest(), doc, indent,
                        trailing)


def _wsl_write(wsl_run, path: str, want: str, mode: str, data: bytes,
               what: str) -> None:
    rc, _, _ = wsl_run(["sh", "-c", _WRITE, "sh", path, want, mode],
                       stdin=data)
    if rc == _CHANGED:
        raise WslInstallError(
            f"{what} inside WSL changed while Blink was writing it, so"
            " Blink wrote nothing. Run it again.")
    if rc != 0:
        raise WslInstallError(f"Could not write {what} inside WSL.")


def _wsl_rm(wsl_run, *paths) -> bool:
    """rm -f inside the distribution; False when wsl.exe or rm failed."""
    return wsl_run(["sh", "-c", _RM, "sh", *paths])[0] == 0


def _wsl_out_dir(home, wsl_run) -> str:
    rc, out, _ = wsl_run(["sh", "-c", _OUT_DIR, "sh", home.drop_dir])
    try:
        linux = out.decode("utf-8").rstrip("\n")
    except UnicodeDecodeError:
        linux = ""
    if rc != 0 or not linux.startswith("/") or "\n" in linux:
        raise WslInstallError(
            "WSL cannot see this computer's drives (automount is off in"
            " /etc/wsl.conf), so Claude Code there could not hand Blink its"
            " usage. Turn automount on, run `wsl --shutdown`, and install"
            " again.")
    return linux


def _wsl_apply_chain(home, wsl_run, action) -> None:
    chain = wsl_shim_path(home, os.path.basename(CHAIN_PATH))
    if action is None:
        return
    if action[0] == "write":
        _wsl_write(wsl_run, chain, "-", "600", action[1].encode("utf-8"),
                   "Blink's status line chain file")
    else:
        _wsl_rm(wsl_run, chain)


def install_into(home, wsl_run, *, hooks=True, files=True) -> str:
    """install() and install_hooks.install() for a WSL home, in one read and
    one write of settings.json inside the distribution.

    `wsl_run(argv, stdin=None) -> (rc, stdout, stderr)` runs argv there as
    the user (default_wsl_run). `files` also writes the shims and
    $HOME/.blink/out; `hooks` merges the hooks as well as the status line.
    The drift check repairs with both off: natively it puts back the status
    line alone, and so does this.

    Order, as natively: the chain file before settings.json, so a failure
    between them leaves the customer's command recorded and their settings
    unchanged; the markers, on Windows, only after the write succeeded.
    Raises WslInstallError or SettingsUnreadable, with nothing written to
    settings.json in either case.

    The returned text is for the interactive install note only and must
    never be logged: when a status line was chained it quotes the
    customer's own command, which carries their paths. The drift check,
    whose messages do reach bridge.log, discards it (_wsl_drift).
    """
    from pc import install_hooks

    if files:
        # Windows makes the drop folder first: wslpath's answer is checked
        # for existence, and the shims could not create a folder on an NTFS
        # path that Linux sees only through the automount anyway.
        os.makedirs(home.state_dir, exist_ok=True)
        from pc import wsl_watch
        wsl_watch._sweep_shim_temps(home.drop_dir, time.time())
        out_dir = _wsl_out_dir(home, wsl_run)
        from pc import cli
        for name in WSL_SHIMS:
            # Encoded from _shim_source's text, which text mode has already
            # turned to LF on Windows: a CRLF shim fails under sh on its
            # first `case ... in\r` (cli._write_shim says the same).
            _wsl_write(wsl_run, wsl_shim_path(home, name), "-", "755",
                       cli._shim_source(name).encode("utf-8"), "a Blink shim")
        _wsl_write(wsl_run, wsl_shim_path(home, "out"), "-", "600",
                   (out_dir + "\n").encode("utf-8"), "Blink's out file")

    st = _wsl_read_settings(home, wsl_run)
    doc, sl_marker, action, msg = merge_install(
        st.doc, _read_marker(home), None, wsl_statusline_command(home))
    lines = [msg]
    if hooks:
        doc, hk_marker, added, repointed = install_hooks.merge_install(
            doc, install_hooks._read_marker(home),
            lambda ev: install_hooks.wsl_hook_command(home, ev))
        lines.append(install_hooks.install_message(added, repointed))

    _wsl_apply_chain(home, wsl_run, action)
    _wsl_write(wsl_run, st.path, st.sha, "keep",
               _dump(doc, st.indent, st.trailing_newline).encode("utf-8"),
               "Claude Code's settings.json")
    _write_marker(sl_marker, home)
    if hooks:
        install_hooks._write_marker(hk_marker, home)
    return " ".join(lines)


def uninstall_from(home, wsl_run) -> str:
    """uninstall() and install_hooks.uninstall() for a WSL home, and the
    shims and out file taken out of the distribution, as a native uninstall
    takes ~/.blink's. settings.json is written only if something of ours
    was in it.

    Like install_into's, the returned text is for the person running
    uninstall and must never be logged: "Restored previous statusline"
    quotes the customer's own command."""
    from pc import install_hooks

    st = _wsl_read_settings(home, wsl_run)
    chain = wsl_shim_path(home, os.path.basename(CHAIN_PATH))
    rc, out, _ = wsl_run(["sh", "-c", _CAT_OR_NOTHING, "sh", chain])
    # _CAT_OR_NOTHING exits 0 for a missing chain file, so a non-zero rc is
    # wsl.exe itself failing or timing out -- not "no chain". Read as empty,
    # merge_uninstall would drop statusLine instead of restoring the
    # customer's command and then delete the only copy of it (review of
    # e000823). Stop before anything is written.
    if rc != 0:
        raise WslInstallError("Could not read Blink's status line chain file"
                              " inside WSL.")
    chain_text = out.decode("utf-8", "replace") if out else None

    sl_doc, sl_marker, action, msg = merge_uninstall(
        st.doc, _read_marker(home), chain_text, wsl_statusline_command(home))
    hk_doc, removed = install_hooks.merge_uninstall(
        st.doc, install_hooks._read_marker(home),
        lambda ev: install_hooks.wsl_hook_command(home, ev))
    if sl_doc is not None or removed:
        _wsl_write(wsl_run, st.path, st.sha, "keep",
                   _dump(st.doc, st.indent,
                         st.trailing_newline).encode("utf-8"),
                   "Claude Code's settings.json")
    if sl_doc is not None:
        _wsl_apply_chain(home, wsl_run, action)
        _remove_marker(home)
    install_hooks._remove_marker(home)
    lines = [msg, install_hooks.uninstall_message(removed)]
    if not _wsl_rm(wsl_run,
                   *(wsl_shim_path(home, n) for n in WSL_SHIMS + ("out",))):
        lines.append("Could not remove Blink's shims inside WSL; they do"
                     " nothing without the settings above.")
    return " ".join(lines)


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


def drift_check(settings_path: str, shim_path: str, home=None, wsl_run=None):
    """Put our statusLine hook back if something removed it.

    Returns a description of what it did, or None when there was nothing to
    do. Never raises: this runs inside the daemon's poll loop, and a daemon
    that dies because settings.json was briefly unreadable is a worse outcome
    than a hook that stays missing for another sixty seconds.

    `home` is the install whose marker decides; None is the native one. A
    WSL home is checked and repaired through `wsl_run` (default_wsl_run
    when None) -- see _wsl_drift for why it cannot share the native path.
    """
    if os.environ.get(WATCHDOG_DISABLE_ENV):
        return None

    # Never installed, or deliberately uninstalled. Both mean hands off.
    marker = _read_marker(home)
    if not marker:
        return None

    if home is not None and home.kind == _WSL:
        return _wsl_drift(home, marker, wsl_run or default_wsl_run(home))

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
    elif _is_ours(current, expected, marker):
        # Ours by the marker but not the command we would write now -- the
        # shim moved, which is what an update to this program does.
        what = "statusline hook pointed at an old shim path; repointed it"
    else:
        what = ("statusline hook had been replaced; restored it and chained"
                f" the replacement: {current}")

    try:
        install(settings_path, shim_path, home)
    except Exception as e:
        return f"statusline hook is missing and could not be restored: {e}"
    return what


def _wsl_drift(home, marker, wsl_run):
    """drift_check for a WSL home.

    Not the native path with other arguments, for two reasons. The native
    command form on Windows is `bash "$USERPROFILE/..."`, which inside Linux
    names nothing: a repair through install() would have written that into
    the distribution's settings and broken a working status line on every
    tick. And the settings path a WSL home has is a UNC string, which
    install()'s os.replace would write straight over a symlinked settings.json
    -- the dotfiles loss _save's comment describes. So the check reads and
    the repair writes through wsl_run, with the distribution's own command.

    The messages go into bridge.log, the file customers send us, so they
    name no path inside the distribution and not the replacement command,
    which is the customer's own script with its own path in it.
    """
    try:
        state = _wsl_read_settings(home, wsl_run)
    except (WslInstallError, SettingsUnreadable):
        return None
    except Exception:
        return None
    current = _current_command(state.doc)
    expected = wsl_statusline_command(home)
    if current == expected:
        return None
    if not current:
        what = "statusline hook inside WSL had been removed; restored it"
    elif _is_ours(current, expected, marker):
        what = ("statusline hook inside WSL pointed at an old shim path;"
                " repointed it")
    else:
        what = ("statusline hook inside WSL had been replaced; restored it"
                " and chained the replacement")
    try:
        install_into(home, wsl_run, hooks=False, files=False)
    except Exception:
        return ("statusline hook inside WSL is missing and could not be"
                " restored")
    return what


def shim_content_check(shims, home=None):
    """Rewrite any installed shim whose CONTENTS have fallen behind.

    A different fault from drift_check above, and the one that shipped.
    drift_check asks whether settings.json still points at us; this asks
    whether the file it points at is still the file we would write. `blink
    update` swaps the program directory and restarts the service -- it has
    never rewritten a shim -- so every install that arrived by the documented
    upgrade path ran a new daemon against whatever shim its original install
    left behind. The symptom is not an error: the hook runs, the path exists,
    `blink status` reports ten of ten events, and the only thing missing is
    the field the new daemon came to read.

    `shims` is a sequence of (path, name) pairs. Returns a description of what
    it rewrote, or None when there was nothing to do -- which is the normal
    case on every tick after the first.
    """
    if os.environ.get(WATCHDOG_DISABLE_ENV):
        return None

    # Same rule as drift_check: a missing marker means the user uninstalled,
    # and that is never overridden. Without it a daemon still winding down
    # would put back the shims `blink uninstall` had just removed.
    if not _read_marker(home):
        return None

    from . import cli

    repaired = []
    for path, name in shims:
        if cli.shim_is_current(path, name):
            continue
        try:
            cli._write_shim(path, name)
        except Exception as e:
            repaired.append(f"{name} is out of date and could not be replaced: {e}")
            continue
        repaired.append(f"{name} was out of date; replaced it")

    return "; ".join(repaired) if repaired else None


class DriftWatchdog:
    """drift_check on an interval, with a cap on how hard it insists.

    Interval rather than a file watcher on purpose. A watcher would need a
    dependency this daemon does not otherwise have, for a fault that is
    measured in "since the last CLI update" rather than in milliseconds --
    and the poll loop it rides on is already running.
    """

    def __init__(self, settings_path, shim_path, interval_s=300.0,
                 now=None, check=drift_check, *, shims=(), home=None):
        import time as _time
        self._settings = settings_path
        self._shim = shim_path
        # The install this watchdog guards, whose marker decides whether to
        # act; None is the native one (claude_home).
        self._home = home
        self._interval = interval_s
        self._now = now or _time.monotonic
        self._check = check
        # Keyword-only and defaulted to nothing, so a caller that only cares
        # about settings.json drift -- which is every caller this class had
        # until the hook shim turned out to go stale -- keeps working and
        # keeps its ticks free of any filesystem read it did not ask for.
        self._shims = tuple(shims)
        self._next = self._now()
        self._reinstatements = 0
        self._gave_up = False

    def tick(self):
        """Returns a message worth logging, or None. Call it as often as you like."""
        now = self._now()
        if now < self._next:
            return None
        self._next = now + self._interval

        # Two faults, two answers, one interval. Neither check is free -- one
        # parses settings.json, the other reads every installed shim off disk
        # -- so both sit behind the not-yet-due return above rather than
        # running on every turn of the poll loop.
        found = []

        # The give-up cap belongs to settings.json alone. It exists because
        # something else on the machine was rewriting a file we share with it,
        # and losing that argument quietly is better than writing forever.
        # Nothing else on the machine writes our shims, so a stale shim is not
        # that argument: it does not count towards the cap, and giving up on
        # the statusLine hook must not leave the shims rotting.
        if not self._gave_up:
            msg = self._check(self._settings, self._shim, self._home)
            if msg is not None:
                self._reinstatements += 1
                if self._reinstatements >= MAX_REINSTATEMENTS:
                    self._gave_up = True
                    msg = (f"{msg}. That is {self._reinstatements} times now --"
                           " something on this machine keeps removing it, so Blink"
                           " will stop putting it back. Run `blink install` once"
                           " the conflict is resolved.")
                found.append(msg)

        if self._shims:
            stale = shim_content_check(self._shims, self._home)
            if stale:
                found.append(stale)

        return "; ".join(found) if found else None


def wsl_drift_watchdog(home, wsl_run=None):
    """The DriftWatchdog for a WSL home, for pc/wsl_watch to tick on its
    own thread while the distribution is in use -- never the daemon's main
    thread, whose watchdog ticks on the serial loop, where a wsl.exe that
    takes its 30 s timeout would hold the board's pongs past their window.

    drift_check through `wsl_run` (default_wsl_run when None), and no
    `shims`: shim_content_check rewrites a stale shim through cli._write_shim
    at the path it is given, which for this home is a UNC path, and an
    os.replace there writes over whatever is at that path inside Linux
    (_wsl_drift's docstring). The settings and shim paths are unused on
    this path -- _wsl_drift finds both inside the distribution -- and are
    left empty so nothing can open them by mistake."""
    run = wsl_run or default_wsl_run(home)

    def check(settings_path, shim_path, h):
        return drift_check(settings_path, shim_path, h, wsl_run=run)
    return DriftWatchdog("", "", check=check, home=home)
