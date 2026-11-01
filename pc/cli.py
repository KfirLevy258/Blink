"""One binary: the setup, the status check, and the bridge itself.

A customer downloads a single file and runs it. There is no Python to install,
no virtualenv to build, no repository to clone, and nothing left behind that
they have to keep in place -- `blink install` copies the binary into
~/.blink/bin and points the login service at that copy, so the download is
disposable the moment it finishes.

This replaces install.sh. The shell version needed Python 3.9+ on the machine,
built a virtualenv, and pulled two packages from PyPI at install time -- three
things that could fail on a customer's machine for reasons they could do
nothing about, and one of them (an unpinned PyPI resolve) meant two people
installing a week apart could get different software.

The status line shim stays a shell script on purpose. It runs on EVERY status
line render, many times a minute, and a frozen binary takes 100-400 ms to start
because it unpacks itself first -- that delay would land in the customer's
prompt. A few lines of sh start in single-digit milliseconds.
"""
import argparse
import contextlib
import glob
import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import time
from typing import NamedTuple

from pc import (install_codex_hooks, install_hooks, install_statusline, ota,
                protocol, status_page, statusline_source, update,
                user_config, win_driver)
from pc import theme as theme_mod
from pc import theme_server
# Eagerly, unlike the other providers, which are imported inside the functions
# that use them to keep the frozen binary's start-up cheap. This one costs
# nothing to import (os, and claude_state, which is json/os/sys/time) and it
# has to be a module attribute rather than a local: `blink status` reads it,
# and a test has to be able to replace it without a real slot directory in
# reach -- the scan behind it deletes files.
from pc.providers import codex_state
from pc.version import RELEASE_VERSION

# Resolved per call, not at import. These used to be module constants, which
# meant every path was fixed by whatever HOME happened to be when the module
# loaded -- untestable without a subprocess, and quietly wrong for any caller
# that changes HOME.
LABEL = "com.blink.bridge"


def _home():
    return os.path.expanduser("~")


def blink_home():
    return os.path.join(_home(), ".blink")


def bin_dir():
    return os.path.join(blink_home(), "bin")


def installed_bin():
    # .exe on Windows: without the extension the copy is not executable, and
    # the Scheduled Task would register a path Windows refuses to launch.
    name = "blink.exe" if sys.platform == "win32" else "blink"
    return os.path.join(bin_dir(), name)


def shim_path():
    return os.path.join(blink_home(), "blink-statusline.sh")


def hook_shim_path():
    return os.path.join(blink_home(), "blink-hook.sh")


def log_path():
    return os.path.join(blink_home(), "bridge.log")


def launcher_path():
    """The script the Scheduled Task runs, so the bridge starts with no window.

    A console program started by a Scheduled Task gets a console window, and
    on the first Windows machine it was installed on that window sat on the
    desktop for as long as the service ran (2026-08-29: "it should be some
    service in the background, not a UI app"). wscript.exe can start a
    program with its window hidden; this is the one-line script that asks
    it to. Written in ASCII with the profile taken from the environment,
    because a non-ASCII path inside a .vbs depends on the machine's code
    page, and the machine in question had a Hebrew user name.
    """
    return os.path.join(blink_home(), "blink-bridge.vbs")


LAUNCHER_VBS = "\n".join((
    "' Starts the BLINK bridge with no window. Written by `blink install`;",
    "' the Scheduled Task \"Blink bridge\" runs it at every logon.",
    'Set sh = CreateObject("WScript.Shell")',
    'home = sh.ExpandEnvironmentStrings("%USERPROFILE%")',
    # VBScript doubles a quote to embed one: this runs
    #   "<home>\.blink\bin\blink.exe" run --log "<home>\.blink\bridge.log"
    # with window style 0 (hidden) and without waiting for it to finish.
    'sh.Run """" & home & "\\.blink\\bin\\blink.exe"" run --log """'
    ' & home & "\\.blink\\bridge.log""", 0, False',
    "",
))


def pid_path():
    """Where the running daemon records its pid: ~/.blink/bridge.pid.

    Derived from the program's own location (its directory's parent), not
    from ~. A login service runs in the user's environment rather than the
    one that registered it, so the two can disagree about what ~ is -- and
    when they do, the pid lands where nothing will look for it.

    Beside the program, not inside its directory: an update rotates
    ~/.blink/bin to bin.old with the old daemon still running from it, and a
    pid file that moved with the directory is one restart() can no longer
    find -- leaving the old daemon holding the serial port against the new.
    """
    return os.path.join(blink_home(), "bridge.pid")


def settings_path():
    return os.path.join(_home(), ".claude", "settings.json")


def plist_path():
    return os.path.join(_home(), "Library", "LaunchAgents", LABEL + ".plist")


def unit_path():
    return os.path.join(_home(), ".config", "systemd", "user",
                        "blink-bridge.service")


# Windows has no launchd and no systemd. A Scheduled Task with an at-logon
# trigger is the equivalent that needs no admin rights and no service wrapper.
TASK_NAME = "Blink bridge"

# The oldest Claude Code that carries usage figures in its status line payload.
# 2.1.0 does not carry rate_limits at all; 2.1.100 does. Below this every step
# of the install succeeds and the panel stays blank forever.
MIN_CLAUDE = (2, 1, 100)

# Set by the tests to skip registering a real login service. The launchd label
# and the systemd unit name are constants while everything else is scoped to
# $HOME, so without this a test under a temporary HOME still boots out the
# real agent of whoever is logged in.
#
# Also skipped, with no variable set, when HOME is not the account's own home.
# BLINK_SKIP_SERVICE needs the caller to think of it, and on 2026-10-30
# nobody did: a reviewer imported blink_main.py with HOME pointed at a
# scratch folder, it ran `blink install`, and launchctl -- which acts on the
# gui/<uid> domain whatever HOME says -- booted out the owner's real
# com.blink.bridge and bootstrapped a stray daemon against the real board.
# BLINK_ALLOW_FOREIGN_HOME=1 says the caller means it: the CI install script
# runs a real install under a throwaway HOME on a runner, and nothing else
# does. POSIX only: Windows has no password database to ask, and its
# Scheduled Task is per user and not at risk from a HOME variable it does not
# read.
_ALLOW_FOREIGN_HOME = "BLINK_ALLOW_FOREIGN_HOME"


def _account_home():
    """The home directory the account database names for this user, or None
    where there is no such thing to ask (Windows) or this uid has no entry
    (an arbitrary one in a container). Asked of pwd and not of $HOME,
    because $HOME is the thing being checked."""
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_dir
    except (ImportError, KeyError, OSError):
        return None


def _foreign_home() -> bool:
    if os.environ.get(_ALLOW_FOREIGN_HOME) == "1":
        return False
    real = _account_home()
    if not real:
        return False
    mine = os.path.expanduser("~")
    try:
        return not os.path.samefile(real, mine)
    except OSError:
        # One of them does not exist. A HOME that is not there is not the
        # account's home; compare the names so a missing real one (a
        # container) does not refuse by accident.
        return os.path.realpath(real) != os.path.realpath(mine)


def _skip_service():
    return os.environ.get("BLINK_SKIP_SERVICE") == "1" or _foreign_home()


def _home_refusal() -> bool:
    """Is the service skipped because HOME is foreign, not because the caller
    asked with BLINK_SKIP_SERVICE=1?

    The two end alike in the code and must not end alike for the person: a
    test or CI that set the variable chose to have no service and is told
    "Done" as before, but a customer whose HOME tripped the check has no
    service and has to be told so, and told how to get one (review of
    fp/safety-guards, S1)."""
    return os.environ.get("BLINK_SKIP_SERVICE") != "1" and _foreign_home()


def _say_home_refused(what: str, consequence: str) -> None:
    """The plain paragraph for a command that skipped the service because
    HOME is foreign. `what` completes "The background service was NOT ...".
    No serial data, only the two paths."""
    print()
    print(f"The background service was NOT {what}.")
    print(f"HOME ({os.path.expanduser('~')}) is not this account's home"
          f" ({_account_home()}), and the service manager is shared by every")
    print("HOME on this machine, so acting on it from here could replace the")
    print(f"real one. {consequence}")
    print(f"If this really is where you want it, run this again with"
          f" {_ALLOW_FOREIGN_HOME}=1.")


def _skip_reason() -> str:
    """What the line that reports a skip puts in its brackets."""
    if os.environ.get("BLINK_SKIP_SERVICE") == "1":
        return "BLINK_SKIP_SERVICE=1"
    return (f"HOME is not this account's home; set {_ALLOW_FOREIGN_HOME}=1"
            " to use it anyway")


def _frozen() -> bool:
    return getattr(sys, "frozen", False)


def _self_path() -> str:
    """The binary to copy into place. sys.executable is the frozen binary."""
    return sys.executable if _frozen() else os.path.abspath(sys.argv[0])


# The script a checkout is started through. It lives at the top of the
# tree, one level above this package.
_ENTRY_SCRIPT = "blink_main.py"


def _checkout_dir() -> str:
    """Top of the source tree this file was loaded from. A frozen build has
    no tree, so nothing frozen should ask."""
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.abspath(os.path.join(here, os.pardir))


def _checkout_argv() -> list:
    """argv that starts a checkout, minus the subcommand: this interpreter
    and the entry script, by absolute path. A path and not `-m pc.cli`,
    because a service started outside the tree has no package to find
    (_service_command() has the full account)."""
    entry = os.path.join(_checkout_dir(), _ENTRY_SCRIPT)
    return [sys.executable, entry]


# What cmd.exe treats specially in an unquoted word. list2cmdline quotes only
# on whitespace, which is right for the C runtime's argv parsing but not for
# the shell in front of it: `C:\Users\R&D\...` pasted bare runs `D\...` as a
# second command. Not a full cmd quoter -- a Windows path cannot hold `"`,
# and the rest of cmd's grammar never appears in one -- just enough that a
# path we print pastes back as itself. Not `%`: cmd expands %VAR% inside
# quotes too, so quoting it protects nothing, and it would newly quote a
# path like C:\Users\50%\... that pasted fine bare. Not `^` either: cmd's
# escape, but an ordinary character to PowerShell, where a bare path holding
# one works and a quoted one needs the `& ` below (review, 2026-10-25).
# And PowerShell's own three, ' $ and `, since a bare word holding one is
# read as a string, a variable or an escape there.
_WIN_NEEDS_QUOTES = frozenset(' \t"&()\'$`')

# Live inside PowerShell's double quotes: $name expands and ` escapes, so a
# word holding either is single-quoted instead, where nothing is special but
# the quote itself, written twice. An apostrophe alone gets double quotes,
# where it is ordinary -- bare, it opens a PowerShell string (review,
# 2026-10-25).
_PS_LIVE_IN_DOUBLE_QUOTES = frozenset("$`")


def _win_quote(arg: str) -> str:
    if arg and not (set(arg) & _WIN_NEEDS_QUOTES):
        return arg
    if set(arg) & _PS_LIVE_IN_DOUBLE_QUOTES:
        return "'" + arg.replace("'", "''") + "'"
    return f'"{arg}"'


def _win_line(argv) -> str:
    """argv as a line for PowerShell, the shell Windows Terminal opens.

    Words are quoted only where _WIN_NEEDS_QUOTES says, so a line that needs
    none is byte for byte what it always was and runs in cmd.exe too. When
    the PROGRAM itself had to be quoted, the line starts with `& `: without
    PowerShell's call operator a quoted first word is a string to print, and
    the spaced profile's undo line echoed its own path and did nothing
    (review, 2026-10-25). cmd.exe rejects that `& `; someone pasting into
    cmd with a spaced profile drops it. Git Bash is not served.
    """
    words = [_win_quote(a) for a in argv]
    line = " ".join(words)
    if words and words[0] != argv[0]:
        line = "& " + line
    return line


def _quote_line(argv) -> str:
    """argv as one line a person can paste into this platform's shell.

    On Windows, PowerShell's form: see _win_line. On POSIX, shlex.join,
    which leaves a word of plain path characters alone -- every path an
    install has printed so far -- and quotes anything else, including
    non-ASCII letters, `~` and `!`, which then pastes back as itself.
    """
    if sys.platform == "win32":
        return _win_line(argv)
    return shlex.join(argv)


def _program_argv() -> list:
    """This program as argv, minus the subcommand.

    The one place the frozen/checkout split is decided for anything that
    starts the program: the launchd and systemd services take it, and so
    does every line printed for a person to paste, which is what keeps a
    service and the advice next to it from naming two different programs.
    The Windows task is the exception -- its launcher hard-codes blink.exe
    -- and a Windows checkout therefore registers nothing; see
    _SchtasksBackend.checkout_refusal.
    """
    if _frozen():
        return [installed_bin()]
    return _checkout_argv()


def command_to_type() -> str:
    """_program_argv() as one pasteable line: what install, status and
    uninstall print whenever they tell a person to run this program.

    It describes the process that is printing, not whatever happens to be
    installed. Run a checkout's blink_main.py on a desk that also carries
    the frozen build and the hints name the checkout, since that is the
    code the reader is actually exercising. (Hints that must reach the
    REGISTERED service, such as repairs, use _repair_command instead.)
    Plain `blink update` / `blink driver` mentions in win_driver,
    install_statusline and the bridge's update notice stay as the README
    spells them: they never named a path, and cli imports those modules,
    so they cannot import this.

    Why it is separate from installed_bin(): that is where the frozen
    binary is copied to, which the updater, swap-in and process matching
    all need, and for a long time it was also what got printed. A checkout
    install copies nothing there, so on 2026-10-12 gapietro's bench Mac,
    installing from a checkout, announced it was creating
    ~/.blink/bin/blink, gave an undo line pointing into that empty folder,
    and suggested deleting the downloaded folder -- the very folder the
    service now ran from.

    Words are quoted only where this platform's shell would split or
    expand them (_quote_line). Until 2026-10-25 the frozen path went out
    bare, and a Windows profile such as C:\\Users\\John Smith received an
    undo line that PowerShell cut at the space. On Windows the result is
    PowerShell syntax, `& ` prefix included; _win_line covers cmd.exe.
    """
    return _quote_line(_program_argv())


def _shim_source(name: str = "blink-statusline.sh") -> str:
    """A shim's text, from the bundle when frozen, the tree when not.

    One source of truth either way -- tools/ is what the build embeds, so the
    shipped shim and the one in the repository cannot drift apart. Any new
    shim added here must also be added to the --add-data list in
    tools/build_binary.sh, or it will work from a checkout and be missing
    from every shipped binary.
    """
    if _frozen():
        base = getattr(sys, "_MEIPASS", os.path.dirname(_self_path()))
        return open(os.path.join(base, name), encoding="utf-8").read()
    return open(os.path.join(_checkout_dir(), "tools", name),
                encoding="utf-8").read()


def _write_shim(path: str, name: str) -> None:
    # newline="\n", or Windows writes the shim with CRLF and Git Bash then
    # fails on `case ... in\r` on every status line render and every tool
    # call. The CI CRLF check only ever looked at the files in the repository,
    # never at the copy this function installs.
    # Temp file plus os.replace, NOT open(path, "w").
    #
    # Truncate-in-place has a window in which this file is empty or half
    # written, and Claude Code runs this exact script on EVERY tool call. A
    # hook that starts inside that window hands `sh` an empty file, or a
    # truncated trailing line -- which exits non-zero and gives the tool a
    # FAILING hook. The shim's own closing `exit 0` exists precisely so that
    # Blink having a bad day never becomes the user's bad day, and truncating
    # the script defeats it from outside.
    #
    # The window used to be one write during an explicit install, which is bad
    # but rare and observed. This branch made DriftWatchdog call it from the
    # daemon's poll loop, so the same window now opens on a timer, in the
    # background, on a machine whose owner is working. install_statusline._save
    # already writes this way for the user's settings, and the shim's own slot
    # write already uses a pid-named temp plus mv -f; this was the one write
    # that skipped the idiom, and it is the one that became recurring.
    #
    # The temp file is a sibling so os.replace is atomic, and the mode is set
    # BEFORE the rename so the file is never briefly present and non-executable.
    tmp = "%s.blink-tmp.%d" % (path, os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(_shim_source(name))
        os.chmod(tmp, 0o755)
        os.replace(tmp, path)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def shim_is_current(path: str, name: str) -> bool:
    """Is the shim at `path` byte-identical to the one we would install?

    Byte-identical, not "close enough": the one difference that has actually
    bitten was line endings, and a comparison that normalised them would have
    called the broken file healthy. Read in binary and compare exactly.

    False on any failure to read. The caller's response to "stale" and to "I
    cannot tell" is the same -- write it again -- and this runs inside the
    daemon's poll loop, where raising is the one outcome worse than a shim
    that stays stale for another five minutes.
    """
    try:
        with open(path, "rb") as f:
            installed = f.read()
    except OSError:
        return False

    try:
        expected = _shim_source(name).encode("utf-8")
    except (OSError, UnicodeDecodeError):
        # The bundle itself is unreadable -- or, since _shim_source opens
        # with encoding="utf-8", readable but not valid UTF-8, which raises
        # UnicodeDecodeError (a ValueError, not an OSError, so it needs its
        # own arm here). Either way there is nothing to compare against.
        # Claiming the installed shim is stale would start a rewrite that
        # cannot succeed and would log the failure every tick; claiming it
        # is current changes nothing. Say current and let the louder failure
        # surface elsewhere.
        return True

    return installed == expected


# ---------------------------------------------------------------- Claude Code


def claude_version():
    """(text, tuple) for the Claude Code on PATH, or (None, None)."""
    exe = shutil.which("claude")
    if not exe:
        return None, None
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True,
                             timeout=30).stdout.strip()
    except Exception:
        return None, None
    head = out.split()[0] if out else ""
    pieces = head.split(".")[:3]
    # Every component has to be a number, or we did not parse a version.
    # Mapping the odd ones to 0 meant "claude version unknown" came back as
    # (0, 0, 0) -- below every floor, so a perfectly good install was reported
    # as TOO OLD, and callers testing `ver is None` never saw it coming.
    if not pieces or not all(x.isdigit() for x in pieces):
        return (out or None), None
    parts = [int(x) for x in pieces]
    while len(parts) < 3:
        parts.append(0)
    return (out or None), tuple(parts)


def desktop_reading():
    """(VERDICT_*, frame or None) from ONE read of Claude Desktop's usage
    cache, or None when there is no cache file at all.

    The file, not the application bundle. An installed app that has never run
    has nothing for us to read, and a machine where the app was removed but
    the cache remains still has a (stale, and reported as stale) reading. The
    file is the thing this product actually depends on, so the file is what
    gets asked about.

    Read once per desk, by read_desk(), and every Desktop line of install and
    status prints from that one answer. They used to ask separately -- an
    exists() for the light, a parse for the row -- and the app rewrites the
    file every few minutes, so the two could describe different files. The
    split also let the app's own empty file read first as written usage,
    then as "not signed in", which told a signed-in person to sign in
    (review, 2026-10-25).
    """
    from pc.providers import claude_desktop
    try:
        provider = claude_desktop.ClaudeDesktopProvider()
        if not os.path.exists(provider.path()):
            return None
        return provider.verdict(time.time())
    except Exception:
        return None


def desktop_app_installed() -> bool:
    """Has Claude Desktop ever run here, even if it has recorded no usage yet?

    desktop_reading() asks for the usage cache, which Desktop writes only
    after sign-in and its first usage check. Its data folder appears on first
    launch, before either. Measured on a Windows 11 VM, 2026-09-30: installed
    and not yet signed in, the folder held Local Storage and config.json and
    no plan-usage-history.json, so install said "With none of them installed
    the panel will connect and then sit blank" on a machine with Claude
    Desktop on it. Signing in wrote the cache within a minute.

    Local Storage, not the data folder itself. The folder is also where
    claude_desktop_config.json lives, which MCP setup tools create on
    machines that never ran Desktop, and it survives an uninstall -- on
    Linux, where there is no Desktop, it can only be such a leftover. Local
    Storage is written by the app's own browser engine and nothing else
    (review, 2026-10-19).
    """
    from pc import desktop_local_storage
    try:
        # The "Local Storage" folder, one above the leveldb store: the
        # Windows measurement saw the folder, not what was inside it.
        return os.path.isdir(os.path.dirname(desktop_local_storage.store_path()))
    except Exception:
        return False


def desktop_staleness_note(age_s):
    """Why a Claude Desktop reading is old -- or nothing, when it is not.

    Claude Desktop does not write its usage cache on a clock. It writes after
    a usage fetch, and it only fetches while somebody is actually at the
    machine: step away, lock the screen or let the machine sleep and the file
    simply stops growing. The app is still running and still right, it is
    just not asking. A reading hours old is therefore the ordinary shape of
    "nobody was here", not a fault, and a panel showing one is not stuck.

    Measured from Claude.app 1.40609.0 on 2026-08-31, by reading its own
    bundle and then watching it run: the poll is 15 minutes by default and
    5 only for the half hour after the menubar panel has been opened, it
    skips the fetch entirely once the system has been idle ten minutes or
    the screen is locked, and it fetches at launch and again every time the
    menubar icon is opened. Opening that icon is the one action that
    refreshes on demand: focusing the app window fetches nothing, because
    the chat runs in a web view that never touches this file.

    None of those intervals is quoted below on purpose. They belong to an
    application we do not control, they have already changed twice (a flat
    5 minutes when pc/providers/claude_desktop.py was written, then the
    tray-gated 5/15 split around 2026-08-21 -- arriving without an app
    update, so the cadence is server-controlled and can move again), and a
    number that goes quietly wrong is worse here than no
    number: the reader needs the shape, which does not change, and the one
    action that works.
    """
    from pc.providers import claude_desktop
    if age_s is None or age_s < claude_desktop.STALE_AFTER_S:
        return []
    # "Menu bar" is a Mac word; a Windows desk was told to click an icon in a
    # menu bar it does not have (Windows 11 VM, 2026-09-30). The tray icon is
    # the same app's same entry point, but that it refreshes on demand there
    # too is carried over from the Mac measurement above, not measured.
    where = ("in the notification area (system tray)" if sys.platform == "win32"
             else "in your menu bar")
    return [
        "Claude Desktop refreshes this only while you are at the machine,",
        "and stops once it has been idle a while -- so a reading this old",
        "usually means nobody was here, not that anything is broken.",
        f"Click the Claude icon {where} to refresh it now.",
    ]


def codex_present() -> bool:
    """Has Codex CLI written a rollout log on this machine?

    The logs, not the binary, for the reason desktop_reading() gives: the
    file the daemon reads is the thing that matters.
    """
    from pc.providers import codex_cli
    try:
        return bool(codex_cli.recent_rollouts(limit=1))
    except Exception:
        return False


def claude_on_path() -> bool:
    """Is there a `claude` on PATH, whether or not it answered --version?

    claude_version() returns (None, None) on ANY failure -- a 30 s timeout, an
    npm shim whose node is gone, a version printed only to stderr -- so "no
    version" is not "not installed". Telling someone with a broken but present
    CLI that Claude Code "is not installed" sends them to reinstall the wrong
    thing (found by review, 2026-09-30, with three fake `claude`s on PATH).
    """
    return shutil.which("claude") is not None


class ClaudeHooks(NamedTuple):
    """What settings.json says about Blink's Claude Code hooks.

    `wired` counts EVENTS with at least one Blink hook, not entries: a
    duplicated Stop group from a hand edit or a settings-sync tool counted
    11 of 10 and made install say "run install again" on every run, with all
    ten events wired (review, 2026-10-19). `readable` is False when
    settings.json does not parse, and then nothing else here is known --
    which is "unknown", not "missing".
    """
    wired: int
    total: int
    readable: bool
    disabled: bool      # "disableAllHooks": true -- Claude Code runs none
    shim: bool          # the file the hooks run exists

    @property
    def light(self) -> bool:
        """Will Claude Code sessions -- the Code tab's included -- light the
        pip? Every condition here is one the Activity row in `blink status`
        reports on, so the install note and status cannot disagree with it.
        A shim that is merely out of date still counts: the daemon repairs it
        (DriftWatchdog), and the pip lights meanwhile."""
        return (self.readable and not self.disabled and self.shim
                and self.wired == self.total)


def claude_hooks():
    """Read Blink's Claude Code hooks out of settings.json, once.

    Counted from settings.json rather than from the shim's presence on disk:
    the file existing proves an install ran once, not that Claude Code is
    still configured to call it. One function, because the install note and
    `blink status` each used to decide the activity light for themselves, and
    on a Desktop-only desk with no hooks status said "activity light from the
    Code tab" three lines above "hooks not installed -- the busy/idle pip
    will stay dark" (review, 2026-09-30).
    """
    total = len(install_hooks.HOOK_EVENTS)
    shim = os.path.exists(hook_shim_path())
    try:
        data = install_statusline._load(settings_path())
    except install_statusline.SettingsUnreadable:
        return ClaudeHooks(0, total, False, False, shim)
    hooks = data.get("hooks")
    # isinstance at every level, not `or {}` / `or []`: only None and empty
    # containers take the `or` branch, so a string under "hooks" crashed
    # status, and so did {"PreToolUse": true} one level down -- after a
    # `blink install` that had otherwise printed "Done." (review,
    # 2026-10-19). Same trap _current_command documents for statusLine.
    if not isinstance(hooks, dict):
        hooks = {}

    def wired(event):
        command = install_hooks.hook_command(hook_shim_path(), event)
        groups = hooks.get(event)
        for group in groups if isinstance(groups, list) else []:
            inner = group.get("hooks") if isinstance(group, dict) else None
            for h in inner if isinstance(inner, list) else []:
                if isinstance(h, dict) and h.get("command") == command:
                    return True
        return False

    # Claude Code's own off switch, and it covers the Code tab's bundled copy
    # too: with it set, every hook above is present and none of them runs.
    disabled = data.get("disableAllHooks") is True
    return ClaudeHooks(sum(1 for event, _ in install_hooks.HOOK_EVENTS
                           if wired(event)),
                       total, True, disabled, shim)


def weekly_countdown_known() -> bool:
    """Does the panel have a weekly reset to count down to right now?

    For a desk without the Claude Code status line the only source is the
    remembered anchor (pc/weekly_anchor.py), so ask it rather than promising
    one. "Once it has been learned" was printed both on desks that can never
    learn it -- Desktop chat without Cowork has no source for it at all -- and
    on desks that already had it, where the panel was counting down a weekly
    reset while install said it was coming later.

    Asked the way the daemon asks, not by reading the file. Its provider
    also withdraws an anchor that Desktop's own weekly-percentage history
    contradicts (weekly_anchor.refuted_by), and a refuted anchor stays
    loadable for up to eight weeks -- so a load-and-project check called the
    countdown known while the wire carried weekly_resets_in_s -1 (review,
    2026-10-19).

    With no anchor at all, the daemon seeds one on its first poll from a
    Cowork session (IngestionBus._seed_anchor_once), and install starts the
    daemon seconds before it prints its note. The same seeders, read-only,
    so a Cowork desk is not told "Missing" about a countdown it is about to
    show.
    """
    from pc import cowork_audit, desktop_idb, weekly_anchor
    from pc.providers.weekly_anchor import (DesktopHistoryProvider,
                                            WeeklyAnchorProvider)
    try:
        provider = WeeklyAnchorProvider(
            history_provider=DesktopHistoryProvider())
        if provider.poll(time.time()):
            return True
        if weekly_anchor.load(weekly_anchor.anchor_path()) is not None:
            return False        # known once, and now projected out or refuted
        return (cowork_audit.seven_day_reset() is not None
                or desktop_idb.seven_day_reset() is not None)
    except Exception:
        return False


class Desk(NamedTuple):
    """Everything the install note and `blink status` say about which
    sources this machine has, read once.

    One reader, because the two used to read the machine separately and
    disagreed in ways only a review found: status told a Claude Code plus
    Desktop desk it had "no Claude Code status line", a Codex desk with an
    unsigned Desktop never heard "sign in", and the install note's light
    did not match the Activity row (review, 2026-10-19). Both callers now
    branch on these fields and nothing else.
    """
    claude_text: object         # `claude --version` output, or None
    claude_ver: object          # parsed version tuple, or None
    on_path: bool               # a `claude` on PATH, answering or not
    desktop: bool               # Desktop has written usage (signed in)
    desktop_installed: bool     # Desktop has run here, signed in or not
    desktop_reading: object     # desktop_reading(): (verdict, frame) or None
    codex: bool
    hooks: ClaudeHooks
    weekly: bool
    # desktop_paths.Answer the Desktop lines above were read under, so the
    # status rows explaining the folder name the one actually read.
    desktop_paths: object = None

    @property
    def cli_answers(self) -> bool:
        return self.claude_text is not None

    @property
    def cli_silent(self) -> bool:
        """On PATH, but --version said nothing. Not "absent": telling that
        person Claude Code is not installed sends them to reinstall the
        wrong thing."""
        return self.claude_text is None and self.on_path

    @property
    def desktop_waiting(self) -> bool:
        """Signed in -- the app writes its cache only after sign-in -- but
        not one sample in it yet. Not "not signed in": that told someone
        who had just signed in to sign in (review, 2026-10-25)."""
        from pc.providers import claude_desktop
        return (self.desktop_reading is not None and
                self.desktop_reading[0] == claude_desktop.VERDICT_UNFILLED)

    @property
    def desktop_caveat(self) -> str:
        """What a row crediting Desktop must add when the cache it credits
        gave no reading: nothing for a reading, the app's empty file or no
        file, and otherwise a pointer to the Desktop row. "Running on Claude
        Desktop alone" sat above a Desktop row saying the file did not parse
        (review, 2026-10-25)."""
        from pc.providers import claude_desktop as cd
        verdict = self.desktop_reading[0] if self.desktop_reading else None
        if verdict == cd.VERDICT_UNREADABLE:
            return " (but its usage cache could not be read; see the Desktop row)"
        if verdict in (cd.VERDICT_NOT_OURS, cd.VERDICT_GARBLED):
            return " (but its usage cache did not parse; see the Desktop row)"
        return ""

    @property
    def unsigned_desktop(self) -> bool:
        return (self.desktop_installed and not self.desktop
                and not self.desktop_waiting)


def read_desk(version=None) -> Desk:
    """Read the machine once. `version` is a claude_version() result the
    caller already has: `blink install` used to ask twice, and a `claude`
    that hangs costs its 30 s timeout per ask (measured 60.0 s, review
    2026-10-19). The weekly question is only asked where a desk can need it
    -- it may read Desktop's IndexedDB, and a CLI desk has no use for it."""
    from pc import desktop_paths
    from pc.providers import claude_desktop
    text, ver = claude_version() if version is None else version
    # Every Desktop question below under one folder answer: the cache, Local
    # Storage and the weekly sources must describe the same folder.
    with desktop_paths.one_answer() as folder:
        reading = desktop_reading()
        # Any cache but the empty one is usage the app wrote: one we cannot
        # parse included, which the Desktop status row reports on its own.
        desktop = (reading is not None
                   and reading[0] != claude_desktop.VERDICT_UNFILLED)
        installed = reading is not None or desktop_app_installed()
        weekly = text is None and desktop and weekly_countdown_known()
    return Desk(claude_text=text, claude_ver=ver,
                on_path=text is not None or claude_on_path(),
                desktop=desktop,
                # The cache in any state is the app's own file.
                desktop_installed=installed,
                desktop_reading=reading,
                codex=codex_present(),
                hooks=claude_hooks(),
                weekly=weekly,
                desktop_paths=folder)


def _announce_codex_hooks() -> None:
    """Say what is about to happen in Codex, before it happens.

    Install is deliberately unattended, so disclosure is the only thing
    standing between us and configuring a SECOND vendor's tool without saying
    so -- and this one is not the tool the customer came here for. The trust
    prompt is the part that has to be said out loud: Codex asks once, in its
    own interface, minutes or days later, and an unexplained dialog from a
    tool the person did not think they were configuring is a support incident
    rather than a feature.

    Wrapped continuation lines start lower case on purpose. The house rule is
    that every sentence starts with a capital; a line that continues one is
    not a new sentence, and capitalising it would read as a stutter.
    """
    print("Blink is about to add a hook to Codex as well.")
    print()
    print(f"  File     {install_codex_hooks.hooks_file()}")
    print("  Why      Codex does not record permission prompts in its session")
    print("           log, so without this a Codex session waiting on you")
    print("           looks idle on the panel.")
    print("  Note     The first time the hook runs, Codex asks you once")
    print("           whether to trust it. That prompt is expected, and Blink")
    print("           cannot answer it for you. Say yes and the panel can show")
    print("           a Codex session waiting on you; say no and everything")
    print("           else still works.")
    print()


def _install_codex_hooks() -> str:
    """Register the shim with Codex, if there is a Codex here to register with.

    Detected rather than asked about, the same way the Claude Code steps are:
    the product is meant to be plug-and-play. Absent Codex, nothing is written
    at all -- creating a hooks file for a tool that is not installed would
    leave a stranger's configuration on the machine.

    No pointer is written into Codex's config.toml, and none is needed:
    hooks.json is discovered by location alone, verified by firing a real hook
    against a config that mentioned hooks nowhere
    (docs/research/codex-hook-contract.md, F3).
    """
    if not codex_present():
        return "no Codex on this machine, nothing to do"
    # Private to the user, for the same reason ~/.blink/state is: these files
    # name the projects someone has open, and the default umask would leave
    # them readable by every account on the machine. Created here rather than
    # left to the shim so the mode is right from the first hook onward.
    state_dir = os.path.join(blink_home(), "state-codex")
    os.makedirs(state_dir, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass                 # Windows, where the mode does not apply
    try:
        return install_codex_hooks.install(install_codex_hooks.hooks_file(),
                                           hook_shim_path())
    except install_statusline.SettingsUnreadable as e:
        # Same judgement as the Claude hooks step: the status line is the
        # product and the activity light is a nicety. A hooks file we cannot
        # safely edit costs the user a pip, and is not worth failing an
        # install that has otherwise worked -- still less worth "repairing"
        # another vendor's config by writing our idea of it over theirs.
        return f"skipped ({e})"


def _note_rows(label, items):
    """A "Shown"/"Missing" block: label in a column, each item wrapped under it.

    A list rather than a sentence so a test can ask WHICH list an item is in.
    Substring tests passed with the activity light moved back under Missing --
    the exact regression the Code-tab measurement was meant to fix (review,
    2026-09-30).
    """
    import textwrap
    for i, item in enumerate(items):
        lines = textwrap.wrap(item, 46)
        head = f"       {label:<8} " if i == 0 else " " * 16
        print(head + lines[0])
        for more in lines[1:]:
            print(" " * 18 + more)


def _light_rows(desk):
    """The activity light's (shown, missing) items for a desk with no
    command-line Claude Code.

    Two sources, decided separately. Codex sessions light the pip from
    Codex's own session log (codex_cli.parse_rollout_state); its hooks only
    add "waiting" on top. Claude sessions -- here, Desktop's Code tab -- need
    Blink's Claude hooks. A Desktop-plus-Codex desk with broken Claude hooks
    was told "no activity light" while its wire line said "state":"running"
    from a Codex session (review, 2026-10-19).
    """
    shown, missing = [], []
    claude = desk.desktop and desk.hooks.light
    if claude and desk.codex:
        shown.append("the activity light, for Codex sessions and Desktop's"
                     " Code tab")
    elif claude:
        shown.append("the activity light, while you work in Desktop's"
                     " Code tab")
    elif desk.codex:
        shown.append("the activity light, for Codex sessions")
    if desk.desktop and not desk.hooks.light:
        missing.append("the activity light for Desktop's Code tab -- "
                       + _hooks_gap(desk.hooks))
    return shown, missing


def _hooks_gap(hooks):
    """Why Blink's Claude hooks will not run, in the words that fix it.

    Never "run install again" on its own: this prints at the end of the
    install that just tried, and when step 3 refused the hooks section the
    re-run refuses it again, so that advice looped forever (review,
    2026-10-19).
    """
    if not hooks.readable:
        return "settings.json does not parse; fix it, then run install again"
    if hooks.disabled:
        return ("settings.json sets disableAllHooks, so Claude Code runs"
                " none of them")
    if not hooks.shim:
        return "the hook shim is missing; run install again"
    if hooks.wired == 0:
        return ("Blink could not add its hooks to settings.json (see the"
                " Activity hooks step above)")
    return (f"only {hooks.wired} of {hooks.total} hooks are in settings.json"
            " (see the Activity hooks step above)")


def _note_if_no_claude_code(desk):
    """Say what this machine will and will not show, when Claude Code is absent.

    "Absent" means no `claude` on PATH. Steps 2 and 3 of the install write a
    status line and a set of hooks into ~/.claude/settings.json; the status
    line needs the command-line Claude Code to ever render, while the hooks
    also run inside Claude Desktop's Code tab (see the Desktop branch below).
    A desk in this state half works, and before this note it had no way to
    find out which half.

    A note, not a refusal, and not a failed step. The edits are correct and
    stay correct: install Claude Code tomorrow and it all starts working with
    nothing to redo. Same reasoning as _warn_if_claude_too_old below.

    Every line below is read off the machine (read_desk) rather than written
    in advance. The fixed version promised the activity light on desks where
    step 3 had just failed, a weekly countdown on desks that can never learn
    one, and "every 5 to 15 minutes" for a five-hour figure that moves within
    seconds of a turn -- all found by review, 2026-09-30.
    """
    if desk.cli_answers:
        return
    print()
    if desk.cli_silent:
        # Present but not answering. "Not installed" would send them to
        # reinstall something that is there; cmd_status says the same. Not
        # "the panel needs it to answer" either: the daemon never runs
        # `claude --version`, and if `claude` itself works its status line
        # feeds the panel regardless (review, 2026-10-19).
        print("  !  There is a `claude` on your PATH, but `claude --version`")
        print("     did not answer, so Blink cannot tell which version it is")
        print("     or whether it works. Run it yourself to see why.")
        if desk.desktop or desk.codex:
            source = " and ".join(
                n for n, on in (("Claude Desktop", desk.desktop),
                                ("Codex", desk.codex)) if on)
            print(f"     Meanwhile the panel also reads {source}.")
        return

    if not desk.desktop and desk.codex:
        # A Codex machine. Its light comes from the same session log as its
        # limits; Codex's hooks only add "waiting" on top, so this does not
        # hang on step 4 having been approved.
        print("  !  Claude Code is not installed, so the panel runs on Codex")
        print("     alone: both Codex limits and their countdowns, and the")
        print("     activity light, from the session log Codex already")
        if desk.unsigned_desktop:
            # Ahead of "add Claude Code": the Desktop already on the machine
            # fills the Claude page with nothing to install (review,
            # 2026-10-19).
            print("     writes. Claude Desktop is installed but not signed in;")
            print("     sign in to it and the Claude page fills by itself.")
        elif desk.desktop_waiting:
            print("     writes. Claude Desktop is signed in but hasn't written a")
            print("     reading yet; it does within a few minutes, and the Claude")
            print("     page fills by itself.")
        else:
            print("     writes. The Claude page stays empty until you add")
            print("     Claude Code; the status line just installed starts")
            print("     working by itself when you do.")
        return
    if desk.unsigned_desktop:
        # Installed, not yet used: the one empty panel that fixes itself
        # without installing anything. See desktop_app_installed.
        print("  !  Claude Desktop is installed but has not recorded any usage")
        print("     yet. Open it and sign in: Desktop writes its first figures")
        print("     within a minute, and the panel picks them up on its own,")
        print("     with nothing here to redo.")
        return
    if desk.desktop_waiting:
        # Signed in, first sample not taken yet. Also fixes itself, and
        # "sign in" here would be wrong (review, 2026-10-25).
        print("  !  Claude Desktop is signed in but hasn't written a reading")
        print("     yet; it does within a few minutes, and the panel picks it")
        print("     up on its own, with nothing here to redo.")
        return
    if not desk.desktop:
        # No source exists. This is not a reduced panel, it is an empty
        # one, and saying anything softer would be misleading.
        print("  !! Nothing on this machine reports usage yet.")
        print("     Blink reads figures that Claude Code, Claude Desktop or")
        print("     Codex have already worked out. With none of them installed")
        print("     the panel will connect and then sit blank.")
        print()
        print("       npm install -g @anthropic-ai/claude-code@latest")
        print()
        print("     Nothing here needs redoing afterwards -- it starts on its own.")
        return

    # Desktop's Code tab runs a Claude Code of its own from inside the app
    # bundle, which `which` never finds, and it reads ~/.claude/settings.json
    # like any other: measured 2026-09-26 on one Mac, Desktop 2.9939.2
    # (bundled 2.1.281), started with --setting-sources=user,project,local,
    # and the hooks step 3 had written fired on a real session. It does not
    # run the status line -- Desktop drives it headless, over stream-json.
    # The same held on Windows 11 ARM64, Desktop 2.16120 (bundled 2.1.284),
    # 2026-09-30, with no `bash` on PATH. There is no Desktop for Linux.
    # docs/research/claude-desktop-window-sources.md has both methods.
    #
    # The five-hour figure comes from Desktop's Local Storage, written
    # within seconds of a turn; only the weekly percentage waits on the
    # usage cache Desktop rewrites on its own schedule. "Live figures" under
    # Missing contradicted the README's own measurement (review, 2026-10-19).
    shown = ["both usage percentages",
             "the five-hour countdown while a window is running, and how "
             "fast it is filling in between"]
    missing = []
    if desk.weekly:
        shown.append("the weekly countdown")
    else:
        missing.append("the weekly countdown (below)")
    light_shown, light_missing = _light_rows(desk)
    shown += light_shown
    if desk.codex:
        shown.append("Codex, on its own page")
    missing += light_missing
    missing.append("an up-to-the-minute weekly percentage: it follows"
                   " Desktop's own refresh")

    who = "Claude Desktop and Codex" if desk.codex else "Claude Desktop alone"
    print("  !  Claude Code is not installed on the command line, so the")
    print(f"     panel runs on {who}. That works, with less on it:")
    print()
    _note_rows("Shown", shown)
    _note_rows("Missing", missing)
    print()
    if not desk.weekly:
        # Why, not just what. Searched across every file, LevelDB store and
        # cache the desktop app writes, 2026-08-28: no seven-day reset time
        # for a chat-only desk. Cowork records one, which is why this is not
        # "never". The weekly dial shows "--" meanwhile, not a rate: the
        # daemon sends only the five-hour burn and the firmware draws no
        # rate on the weekly gauge (usage_view.c) -- "shows a rate" was
        # printed here until review, 2026-10-19.
        print("     Claude Desktop chat does not record when the week resets.")
        print("     Blink learns it from Claude Code's status line or a Cowork")
        print("     session; until one reports it, the weekly dial has no")
        print("     countdown.")
        print()
    print("     The status line just installed starts working by itself if")
    print("     you add Claude Code on the command line later, and brings")
    print("     an up-to-the-minute weekly figure"
          + ("" if desk.weekly else " and its countdown") + " with it:")
    print()
    print("       npm install -g @anthropic-ai/claude-code@latest")


def _warn_if_claude_too_old(desk):
    ver = desk.claude_ver
    if ver is None or ver >= MIN_CLAUDE:
        return
    # A warning, not a refusal. Everything installed is correct and stays
    # correct, so the moment they update it starts working with nothing to
    # redo; refusing would make them run this again for no reason.
    m = ".".join(str(n) for n in MIN_CLAUDE)
    print()
    print(f"  !! Your Claude Code is {desk.claude_text}, and Blink needs {m} or newer.")
    print("     Older versions do not put the usage figures in the status line")
    print("     at all, so the panel will sit blank until you update:")
    print()
    print("       npm install -g @anthropic-ai/claude-code@latest")
    print()
    print("     Nothing else needs redoing -- it starts working on its own.")


# -------------------------------------------------------------------- service


# How long the supervisor waits, after asking the daemon to stop, before it
# kills it. The daemon defers a SIGTERM that lands inside a firmware write or
# an app swap until the work returns (claude_usage_bridge.no_exit_while),
# because esptool killed half way through slot 0 leaves a board that will
# not boot until it is reflashed by hand. That deferral is only as long as
# this: launchd's default ExitTimeOut is 20 s and systemd's TimeoutStopSec
# 90 s, and past them comes SIGKILL, for esptool too (final review,
# 2026-10-27, SHOULD 1).
#
# 180 s, derived from the longest thing guarded:
#   - pc/ota.flash: an eFuse probe of a few seconds, then write_flash at the
#     115200 baud the CH340 needs, about 75 s for the app image (pc/ota.py),
#     then ONE retry of the same write if the first failed. 2 x 75 s plus
#     the probe is about 155 s, and the retry is the attempt that repairs a
#     slot the first left bad, so it has to fit as well.
#   - update.apply: unpack and rename, which take a second, behind a
#     self-test of the downloaded program that normally takes two and was
#     measured at 97 s on a machine under load average 89 (pc/update.py).
#     A self-test still running at 180 s is killed before any rename, which
#     loses nothing: the old program is untouched and the staging directory
#     is cleared by the next attempt.
# An ordinary stop costs nothing extra: the daemon exits within a second of
# SIGTERM whenever it is not inside a guarded block.
#
# Windows has no equivalent to set. `schtasks /end` and taskkill /f end the
# process outright, with no signal and no grace period to configure, and the
# SIGTERM deferral is POSIX only (exit_on_sigterm), so on Windows the guard
# does nothing and no setting in the task could change that.
STOP_GRACE_S = 180


_PLIST_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD plist_path() 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}  </array>
  <!-- Frozen Python buffers stdout when it is a file, so without this the
       log lags minutes behind the daemon and reads as a hang. -->
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- The bridge waits for a board rather than exiting, so KeepAlive is a
       backstop for a crash, not the normal path. ThrottleInterval keeps a
       repeatable crash from becoming a spin. -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <!-- SIGTERM to SIGKILL: long enough for a firmware write to finish
       (STOP_GRACE_S in pc/cli.py). launchd's default is 20 s. -->
  <key>ExitTimeOut</key><integer>{grace}</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

def _systemd_exec(argv) -> str:
    """ExecStart, with every argument quoted.

    systemd splits ExecStart on whitespace, so a bare " ".join() broke on any
    space in the interpreter path, the checkout path or $HOME: with a spaced
    home the unit resolved the executable as the first fragment and failed
    with "Failed to locate executable", while `blink install` still printed
    "running (systemd)". The launchd and schtasks backends already quote
    per-argument; systemd was the only one that did not, and the CI scenario
    that uses a spaced home asserts only that the unit FILE exists.

    systemd's own quoting: double quotes, with \\ and \" escaped inside.
    And % doubled and $ doubled, because quoting does not stop either: systemd
    expands %-specifiers and $VARIABLES inside a quoted word too, so a
    profile like /home/50%/ or a checkout under /src/$work/ was run as some
    other path (review, 2026-10-25). _SystemdBackend.registered_argv undoes
    all four.
    """
    out = []
    for a in argv:
        a = a.replace("\\", "\\\\").replace('"', '\\"')
        a = a.replace("%", "%%").replace("$", "$$")
        out.append('"%s"' % a)
    return " ".join(out)


def _undouble(word: str) -> str:
    """One ExecStart word with _systemd_exec's doubled % and $ undone, left
    to right, so a literal "%%" written as "%%%%" comes back as "%%"."""
    out, i = [], 0
    while i < len(word):
        out.append(word[i])
        i += 2 if word[i] in "%$" and word[i + 1:i + 2] == word[i] else 1
    return "".join(out)


def _unit_text(argv) -> str:
    """The systemd unit for a service running `argv`. The one writer, used
    by install() and by the tests that read it back, as _plist_text is for
    launchd."""
    return _UNIT_TEMPLATE.format(command=_systemd_exec(argv),
                                 grace=STOP_GRACE_S)


_UNIT_TEMPLATE = """[Unit]
Description=Blink USB bridge

[Service]
ExecStart={command}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10
# A firmware write must be allowed to finish; see STOP_GRACE_S in pc/cli.py.
# mixed, because the default (control-group) sends SIGTERM to esptool as
# well as to the daemon, which kills the write at second zero whatever the
# daemon defers. mixed sends it to the daemon alone, and SIGKILL to what is
# left once the daemon has exited (or at the timeout). The one thing usually
# left is a browser the theme builder started cold through xdg-open, which
# now dies by SIGKILL instead of SIGTERM when the service stops; accepted.
TimeoutStopSec={grace}
KillMode=mixed

[Install]
WantedBy=default.target
"""


def _xml_escape(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _service_command():
    """What the login service should run.

    Frozen, that is the copy in ~/.blink/bin. From a checkout it is this
    interpreter and the repo's entry SCRIPT -- not `-m pc.cli`.

    `-m` was wrong and silently so. A login service starts with no working
    directory of ours (launchd uses /), so `python -m pc.cli` could not import
    `pc` at all: the agent crash-looped on ModuleNotFoundError every
    ThrottleInterval, forever, writing the same line to bridge.log -- while
    `blink status` reported "registered with launchd", because registration
    is not health. Found on the author's own machine 2026-08-28, where it had
    been failing unnoticed.

    Running the script BY PATH fixes it without a WorkingDirectory or a
    PYTHONPATH: Python puts a script's own directory on sys.path, and
    blink_main.py sits at the repo root next to `pc/`.
    """
    return _program_argv() + ["run"]


def _service_runs_checkout() -> bool:
    """Whether, from a checkout, the login service would run this checkout.

    Only launchd and systemd are handed _service_command(). The Windows
    Scheduled Task runs LAUNCHER_VBS, which names ~\\.blink\\bin\\blink.exe
    outright, and a Linux machine without systemctl registers nothing; a
    checkout install on either promised a service running from the folder
    (review, 2026-10-25). Each backend answers for itself, next to its own
    install().
    """
    return not _frozen() and backend().runs_checkout()


def _argv_ending_in_run(args):
    """`args` minus its trailing `run`, if it is the shape _service_command()
    writes; None for anything else, so a hand-edited service file is never
    mistaken for ours."""
    if (not isinstance(args, list) or len(args) < 2 or args[-1] != "run"
            or not all(isinstance(a, str) for a in args)):
        return None
    return args[:-1]


def service_program():
    """The program the REGISTERED login service runs, minus `run`, or None.

    None when no service is registered, when its file is not one we wrote,
    and when any path in it is missing from disk: a plist naming a checkout
    that was since deleted, or a launcher whose blink.exe was never copied,
    runs nothing, and a hint naming it sends the reader to a file that is
    not there. One rule for every backend; only Windows used to check
    (review, 2026-10-25).

    This, not _frozen(), is what decides anything about the service. Fleet
    desks run the checkout's CLI from .venv-test beside a frozen service,
    and about nine decisions keyed on which program was RUNNING -- update,
    provision, the repair hints -- did the wrong thing to the service that
    was registered. Never raises: an odd file only means "none".
    """
    try:
        argv = backend().registered_argv()
        if argv and all(os.path.exists(a) for a in argv):
            return argv
    except Exception:
        pass
    return None


def _repair_command() -> str:
    """The command a hint names to REPAIR an existing install.

    Not command_to_type(), which is the program printing the hint. Fleet desks
    run the checkout's CLI from .venv-test beside a live frozen install, and
    after a failed burn or restore status told them to run the checkout's
    `install` -- which re-registers the service against the checkout and
    silently retires the frozen one (review, 2026-10-25). So when a service
    is registered, the hint names what that service runs; with none, there
    is nothing to protect and it is this program.
    """
    argv = service_program()
    return _quote_line(argv) if argv else command_to_type()


def _confirm_running(probe, tries=4) -> bool:
    """Keep asking a service manager whether something is REALLY running.

    All three backends had the same hole and only one of them had learned:
    they read the exit code of the command that started the service and
    called that health. An exit code says a request was accepted. Whether a
    process exists is a different question, and only the supervisor can
    answer it -- which is the lesson recorded above _service_command() and
    measured again on 2026-09-04, when five bootstraps out of five returned
    zero with nothing running.

    So this is the part that is the same everywhere -- ask, and if the answer
    is no, ask again for a few seconds -- and `probe` is the part that is not.
    It is a callable rather than an argv because the QUESTION differs per
    platform and, on Windows, so does its subject: the pid file has to be
    re-read on every attempt, because a daemon that has not started yet has
    not written one.

    Four tries over five seconds: long enough for a start that is merely
    slow, short enough that nobody watching `blink update` thinks it hung.

    Returns False on anything it cannot recognise. That lean is deliberate
    and it is the whole point -- the one thing this must never do again is
    print a claim it cannot see.
    """
    for attempt in range(tries):
        if probe():
            return True
        time.sleep(0.5 * (attempt + 1))
    return False


class _Backend:
    """One platform's answer to "start this at login, and keep it running".

    There used to be five sys.platform ladders: one each for install,
    restart, remove, the line the installer prints about what it creates,
    and status. Nothing held them together, so a case could be handled in
    four of them and forgotten in the fifth -- which is not hypothetical. A
    Linux box with no systemd got a considered answer from remove ("no
    systemd here; stop it yourself if you started it") and a shrug from
    status ("unknown on linux"), for the same machine in the same state.

    One object per platform now, chosen once by backend(). That also makes
    this code reachable from a unit test for the first time: a backend can
    be built on any machine and asked what it would run, where before the
    argv handed to launchctl, schtasks and systemctl could only be observed
    by installing for real on that platform. Three Windows defects in this
    file were found by CI rather than by a test for exactly that reason.

    The base class is not abstract -- it IS the behaviour for a platform
    with no supervisor we know how to drive, and every subclass overrides
    only the parts it can do better.
    """

    def creates(self):
        """What installing will put on disk, for the disclosure block.

        None when there is nothing to name.
        """
        return None

    def checkout_refusal(self):
        """Why install() will register nothing for this process, or None.
        Said in the disclosure before anything is written, and returned by
        install() itself."""
        return None

    def runs_checkout(self) -> bool:
        """Whether install(), run from a checkout, starts the checkout."""
        return False

    def service_claude_config_dir(self):
        """CLAUDE_CONFIG_DIR as the installed service will see it, or None.

        None here, for launchd and systemd: the plist and the unit hand the
        daemon PYTHONUNBUFFERED and nothing else, and neither starts it from
        the shell that exported anything. (A desk that put the variable in
        `launchctl setenv` or ~/.config/environment.d would be seen; that is
        not where anyone is told to put it, and Claude Code's own docs say
        to export it in the shell.)"""
        return None

    def registered_argv(self):
        """What the registered service runs, minus `run`, as its file says;
        None if nothing is registered or the file is not one we wrote.
        Whether those paths exist is service_program()'s question, asked
        the same way for every backend."""
        return None

    def install(self) -> str:
        return (f"not supported on {sys.platform}; run it yourself: "
                f"{command_to_type()} run")

    def restart(self) -> str:
        return "not running under a supervisor; restart it yourself"

    def halt(self) -> None:
        """Stop the running daemon, leaving the service registered.

        Only Windows needs this, and only right before the update swap: it is
        the one platform that refuses to rename <bin> while the daemon holds
        bin\\blink.exe open. Elsewhere a rename does not care what is open, and
        stopping a daemon that launchd or systemd would restart a second later
        buys a gap on the board and nothing else. So: a no-op by default, on
        purpose.

        Not stop() below, which is deliberately a different thing: stop() is
        half of a pair a test run must be able to undo on every platform, and
        it reports what happened. This is best effort, silent, and only where
        an open file handle would otherwise fail a rename.
        """
        return None

    def stop(self, runner=None) -> str:
        """Stop the installed service, without uninstalling it.

        Restart() is what `blink update` needs; this pair is what a test run
        needs. The fleet suite drives the real daemon against a real board, so
        it has to take the serial port off the service that owns it and hand
        it back afterwards -- on a machine the user works on, where anything
        that touched the plist, the unit file or the Scheduled Task itself
        would rewrite their own installation on the way past. So: stop and
        start only, and a service that is not installed is an answer rather
        than an error, because start() is called from a finally block where
        an exception would replace the failure the run was there to find.

        runner is subprocess.run unless a caller substitutes one. Resolved at
        call time rather than bound as a default argument, so a test that
        stubs subprocess.run is still in charge of what this can execute.
        """
        return "not running under a supervisor; stop it yourself"

    def start(self, runner=None) -> str:
        """Start the installed service again. See stop() for the pair.

        Not a restart: this assumes stop() ran, and does not go looking for
        anything else to kill. A caller that wants both is asking for
        restart().
        """
        return "not running under a supervisor; start it yourself"

    def remove(self) -> str:
        return "nothing to remove"

    def status(self) -> str:
        return f"unknown on {sys.platform}"


# launchctl exits with an errno. ESRCH is what booting out a job launchd is
# not running gives back ("Boot-out failed: 3: No such process"), which is an
# answer rather than a failure -- see _LaunchdBackend.stop.
_ESRCH = 3


def _plist_text(argv) -> str:
    """The launchd plist for a service running `argv`. The one writer, used
    by install() and by the tests that check registered_argv() reads it
    back, so the reader is tested against what is really written."""
    args = "".join(f"    <string>{_xml_escape(a)}</string>\n" for a in argv)
    return _PLIST_TEMPLATE.format(label=LABEL, args=args,
                                  log=_xml_escape(log_path()),
                                  grace=STOP_GRACE_S)


class _LaunchdBackend(_Backend):
    def creates(self):
        return plist_path()

    def runs_checkout(self) -> bool:
        return True

    def registered_argv(self):
        if not os.path.exists(plist_path()):
            return None
        import plistlib          # only status and provision reach this
        with open(plist_path(), "rb") as f:
            return _argv_ending_in_run(plistlib.load(f).get("ProgramArguments"))

    def install(self) -> str:
        os.makedirs(os.path.dirname(plist_path()), exist_ok=True)
        with open(plist_path(), "w", encoding="utf-8") as f:
            f.write(_plist_text(_service_command()))
        uid = os.getuid()
        # bootout first so a rerun replaces the running agent rather than
        # failing with "service already loaded".
        out = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                             capture_output=True, **update.ota.NO_WINDOW)
        # Then wait for the old instance to be GONE, as long as its own stop
        # grace. A daemon inside a firmware write holds its SIGTERM for up
        # to STOP_GRACE_S, and launchd keeps the job -- `print` still
        # answers -- until it exits, so a reinstall started mid-OTA used to
        # spend its ~5 s of bootstrap retries against a job still leaving
        # and report "installed, but could not be started" (review of
        # fp/fix-tools, NICE 3; tools/lib_launchd.sh is the same wait for
        # burn.sh and dev.sh). On ESRCH there is nothing to wait for. Any
        # other non-zero code is read with launchd's state rather than on
        # its own, as blink_bootout does: an older macOS is reported to
        # answer a stop under way with "36: Operation now in progress", and
        # only "state = running" means the old job is not leaving.
        leaving = out.returncode == 0 or (
            out.returncode != _ESRCH and not self._is_running(uid))
        if leaving and not _noting_a_slow_stop(
                lambda: self._wait_gone(uid)):
            return ("installed, but the running bridge is still stopping -- "
                    "once it has, start it with: "
                    f"launchctl bootstrap gui/{uid} {plist_path()} && "
                    f"launchctl kickstart gui/{uid}/{LABEL}")
        # ...and then retry, because bootout is asynchronous. The bootstrap
        # immediately after it can fail while launchd is still tearing the old
        # job down, which left a reinstall with no service running at all --
        # observed on the second install of the day, silently.
        #
        # That retry is still worth having, but it was only ever half of this
        # story: it guards a bootstrap that FAILS. A bootstrap that succeeds
        # also starts nothing. Measured on a live machine, with this very
        # plist (RunAtLoad is set in _PLIST_TEMPLATE):
        #
        #   launchctl bootout   gui/501/com.blink.bridge   -> rc 0
        #   launchctl bootstrap gui/501 <plist>            -> rc 0
        #   launchctl print     gui/501/com.blink.bridge   -> "not running",
        #                                                     runs = 0
        #                          (still not running six seconds later)
        #   launchctl kickstart gui/501/com.blink.bridge   -> running, runs = 1
        #
        # So bootstrap registers the job and kickstart is what runs it. Until
        # this line existed the customer was told "running (launchd)" about a
        # job that was registered and idle, and that would not have actually
        # started before their next login.
        for attempt in range(4):
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist_path()],
                               capture_output=True, **update.ota.NO_WINDOW)
            if r.returncode == 0:
                break
            time.sleep(0.5 * (attempt + 1))
        if r.returncode != 0:
            return ("installed, but could not be started: "
                    f"launchctl bootstrap gui/{uid} {plist_path()}")

        # BOOTSTRAP REGISTERS; IT DOES NOT START. A successful bootstrap means
        # launchd has accepted the job, not that anything is running.
        #
        # And this is not a race that sometimes loses. Measured 2026-09-04 by
        # running the old sequence -- bootout, bootstrap, no kickstart -- five
        # times in a row against this very plist: `bootstrap` returned 0 every
        # time and launchd reported "not running" every time. FIVE OUT OF FIVE.
        # RunAtLoad is honoured at LOGIN, not on a bootstrap, so an install left
        # the daemon dead until the user next logged in -- while the installer
        # said it was running. That is why it stayed hidden: anyone who rebooted
        # between installing and looking saw a working service.
        #
        # The observation that started it, straight after an install that had
        # just printed "running (launchd)":
        #
        #     active count = 0
        #     state = not running
        #     last exit code = (never exited)
        #
        # The board went quiet and dozed while the installer said it was fine.
        # This is the same defect tools/burn.sh has in its restore path, and
        # the same lesson _service_command() already records one screen up:
        # registration is not health.
        #
        # It depends on the macOS release. Re-measured 2026-10-28 on macOS
        # 27.0.1 with a throwaway agent of this plist's shape (RunAtLoad,
        # KeepAlive, ExitTimeOut 180): bootstrap started the job by itself
        # five times out of five, running within a second. A plain kickstart
        # after it left the same pid; `kickstart -k` killed it and started a
        # second one (runs = 2). So the kickstart stays, for the releases
        # that only register, and it is plain, for the ones that start.
        #
        # Plain kickstart, not -k. It starts the job just bootstrapped all
        # the same (start() below), and -k would kill an instance that is
        # already running -- on macOS 27 the one this bootstrap just
        # started, and possibly a firmware write it has begun.
        subprocess.run(["launchctl", "kickstart", f"gui/{uid}/{LABEL}"],
                       capture_output=True, **update.ota.NO_WINDOW)

        # And then ASK, rather than assume. Everything above is a command that
        # returned zero; none of it is evidence that a process exists. launchd
        # is the only thing that knows, so the claim we print is its answer and
        # not our hope.
        if _confirm_running(lambda: self._is_running(uid)):
            return _RUNNING_LAUNCHD
        return ("registered with launchd, but it is not running -- "
                f"start it with: launchctl kickstart gui/{uid}/{LABEL}")

    @staticmethod
    def _wait_gone(uid) -> bool:
        """True once launchd no longer knows the job, polled every quarter
        second for up to the stop grace plus 10 s for launchd's own SIGKILL
        to land. Counted in polls rather than read off a clock, so a test
        with time.sleep stubbed cannot spin for three real minutes."""
        for _ in range((STOP_GRACE_S + 10) * 4):
            r = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"],
                               capture_output=True, **update.ota.NO_WINDOW)
            if r.returncode != 0:
                return True
            time.sleep(0.25)
        return False

    def _is_running(self, uid) -> bool:
        """Does launchd say a process actually exists? Its answer, not ours.

        Asked by install() and restart() because they were making the same
        claim from different evidence, and only one of them had learned. An
        exit code says a command was accepted; `launchctl print` is the only
        thing that says a process is running, so both ask it.

        One ask. The retrying belongs to _confirm_running(), which the other
        two backends need for the same reason: a kickstart hands the job to
        launchd and returns, so the exec can still be in flight when the first
        print lands.

        Parsed the same defensive way status() is -- launchctl print is a
        human-readable dump, not a contract.
        """
        pr = subprocess.run(["launchctl", "print", f"gui/{uid}/{LABEL}"],
                            capture_output=True, text=True,
                            **update.ota.NO_WINDOW)
        return pr.returncode == 0 and "state = running" in (pr.stdout or "")

    def restart(self) -> str:
        """Bounce the agent, then check that it came back.

        The old body returned "restarted" whenever kickstart exited zero,
        which is the exact defect install() was measured making on 2026-09-04:
        five bootstraps out of five returned zero with nothing running. A
        kickstart is a request to launchd, and a request that was accepted is
        not a process that exists.

        It matters most on the path that calls this. `blink update` swaps the
        binary underneath the agent and then bounces it; if the new binary
        cannot start -- a bad download, a missing dylib -- kickstart still
        succeeds and launchd's respawns still fail. Saying "restarted" there
        would send the user away from the one file that explains it.
        """
        uid = os.getuid()
        r = subprocess.run(["launchctl", "kickstart", "-k",
                            f"gui/{uid}/{LABEL}"], capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            return "could not restart it"
        if _confirm_running(lambda: self._is_running(uid)):
            return "restarted"
        # Not a command to paste this time: the command that would have been
        # printed is the one that just ran and left nothing running. What is
        # left to do is read why it died.
        return ("restarted, but launchd reports it is not running -- "
                f"see {log_path()}")

    def stop(self, runner=None) -> str:
        """bootout, not `launchctl stop`.

        The plist sets KeepAlive, so a stopped job is one launchd starts
        straight back up -- the process would be gone for about a second and
        the serial port would be held again before anything else could take
        it. bootout unloads the job; the plist stays exactly where it is, and
        start() below bootstraps that same file back in.

        A job that is not loaded is stopped, not a failure. launchctl exits
        with an errno, and booting out something launchd has never heard of
        gives ESRCH -- "Boot-out failed: 3: No such process". That is an
        ordinary state: a plist on disk whose agent was already booted out,
        by a previous run or by hand. Reported as a failure it aborts the
        fleet agent with "the serial port is probably still held", about a
        port that is in fact free.
        """
        if not os.path.exists(plist_path()):
            return "not installed"
        uid = os.getuid()
        r = (runner or subprocess.run)(
            ["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
            capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "stopped"
        if r.returncode == _ESRCH:
            return "stopped (it was not loaded)"
        return f"could not stop it: launchctl bootout gui/{uid}/{LABEL}"

    def start(self, runner=None) -> str:
        """bootstrap, and then kickstart, because bootstrap starts nothing.

        `launchctl bootstrap` registers the job with launchd and exits 0
        without running it, RunAtLoad in the plist notwithstanding. Measured
        on a live machine -- see the block in install(), which had the same
        gap -- a bootstrapped job sits at "not running / runs = 0" for as
        long as you care to watch it, and one kickstart turns it into
        "running / runs = 1" with a pid.

        This is the fleet suite's restore path, called from a finally block
        on a machine someone works at. Answering "started" for a registered
        but idle job is the worst answer available here: the run reports
        success, the daemon never retakes the serial port, and the desk goes
        dark with nothing pointing at the cause. So a kickstart that fails
        is a failed start, even though the job is registered -- what the
        caller asked for is a process holding the port again.
        """
        if not os.path.exists(plist_path()):
            return "not installed"
        uid = os.getuid()
        run = runner or subprocess.run
        r = run(["launchctl", "bootstrap", f"gui/{uid}", plist_path()],
                capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            # No job to kick, and a second failing command would only bury the
            # one line the operator needs to paste.
            return f"could not start it: launchctl bootstrap gui/{uid} {plist_path()}"
        k = run(["launchctl", "kickstart", f"gui/{uid}/{LABEL}"],
                capture_output=True, **update.ota.NO_WINDOW)
        if k.returncode == 0:
            return "started"
        return f"could not start it: launchctl kickstart gui/{uid}/{LABEL}"

    def remove(self) -> str:
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True, **update.ota.NO_WINDOW)
        _rm(plist_path())
        return "removed"

    def status(self) -> str:
        """Registered is not the same as working, and the difference is the
        whole point of this line.

        This used to answer "registered with launchd" for a job that had
        crash-looped 59 times, which is how a dead daemon went unnoticed on
        the author's own machine while every other line of `blink status`
        looked healthy. launchctl already knows -- it reports the run count
        and the last exit code -- so ask it.

        Parsed defensively: launchctl print is a human-readable dump and not a
        contract, so anything unrecognised falls back to the old answer rather
        than claiming a fault that may not exist.
        """
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                           capture_output=True, text=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            # launchctl has never heard of this label, and that is TWO states
            # sharing one sentence. A plist on disk that is merely not loaded
            # is an install that exists and needs starting; no plist at all is
            # an install that never happened. Answering "not installed" to
            # both sends someone to reinstall a service that is already there.
            #
            # It also hides the ordinary aftermath of a burn or a fleet run.
            # Both boot the agent out and are expected to put it back, and if
            # one does not, this line is where a person looks first -- so the
            # state it must name most clearly is exactly the one it was
            # collapsing away.
            #
            # stop() and start() have always checked for the plist before
            # answering. status() did not, which is why status() is the one
            # that lied. Found 2026-09-09 by booting the agent out by hand,
            # burning a board, and then asking.
            if os.path.exists(plist_path()):
                return ("installed but not loaded -- run "
                        f"`{_repair_command()} install` to start it")
            return "not installed"

        fields = {}
        for line in (r.stdout or "").splitlines():
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k in ("pid", "last exit code", "runs") and k not in fields:
                fields[k] = v

        if fields.get("pid", "").isdigit():
            return f"running (launchd, pid {fields['pid']})"

        exit_code = fields.get("last exit code")
        if exit_code and exit_code.isdigit() and int(exit_code) != 0:
            runs = fields.get("runs", "?")
            return (f"registered but FAILING -- exits with code {exit_code},"
                    f" {runs} attempts so far. See {log_path()}")
        return "registered with launchd"


# The autostart Windows never refuses: the user's own Run key.
#
# HKCU is the account's own hive, so writing here needs no administrator by
# definition. That is the whole reason it is the fallback -- see
# _SchtasksBackend._install_without_a_task for what it is a fallback FROM.
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = TASK_NAME
# Where the machine-wide environment variables live, under HKLM. The
# account's own are HKCU\Environment.
SYSTEM_ENV_KEY = (r"SYSTEM\CurrentControlSet\Control\Session Manager"
                  r"\Environment")


def _winreg():
    """The winreg module, or None where there is no registry.

    None rather than an ImportError because the backends are selected by
    platform at run time but exercised on every platform by the tests: a bare
    `import winreg` at the top of a Windows-only helper takes the whole suite
    down on macOS.
    """
    try:
        import winreg
        return winreg
    except ImportError:
        return None


def _autostart_command() -> str:
    """The same hidden-window launcher the Scheduled Task would have run."""
    return f'wscript.exe //B //Nologo "{launcher_path()}"'


def _autostart_set() -> str:
    """Start the bridge at logon from the user's Run key. "" when it worked."""
    winreg = _winreg()
    if winreg is None:
        return "no registry on this system"
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ,
                              _autostart_command())
        return ""
    except OSError as e:
        return str(e)[:120]


def _autostart_clear() -> None:
    """Remove it. Absent is the ordinary case -- most installs use the task."""
    winreg = _winreg()
    if winreg is None:
        return
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, RUN_VALUE)
    except OSError:
        pass


def _autostart_present() -> bool:
    winreg = _winreg()
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, RUN_VALUE)
        return bool(value)
    except OSError:
        return False


def _start_launcher() -> None:
    """Start the bridge now, with no Scheduled Task to trigger.

    Detached and window-less, the same way _schedule_windows_cleanup starts
    its own child: this must outlive the installer that spawned it, and it
    must not put a console window on the customer's desktop.
    """
    DETACHED_NO_WINDOW = 0x00000008 | 0x08000000
    try:
        subprocess.Popen(["wscript.exe", "//B", "//Nologo", launcher_path()],
                         creationflags=DETACHED_NO_WINDOW, close_fds=True)
    except OSError:
        pass


class _SchtasksBackend(_Backend):
    def checkout_refusal(self):
        # LAUNCHER_VBS hard-codes ~\.blink\bin\blink.exe, and a checkout
        # never puts anything there. Registered anyway, the task failed on a
        # missing file at every logon while install recommended
        # `schtasks /run` (review, 2026-10-25). So a checkout gets no task,
        # only the line to start the daemon by hand.
        if not _frozen():
            return ("a checkout on Windows gets no login service; start it"
                    f" by hand with: {command_to_type()} run")
        return None

    def service_claude_config_dir(self):
        # Task Scheduler starts the task, and Explorer the Run key's entry,
        # with the account's environment as the registry holds it -- the
        # user's variable over the machine's, as Windows itself resolves
        # them -- and never with the window `blink install` was typed in.
        # So a `setx CLAUDE_CONFIG_DIR` is seen and a `set` or `$env:` is
        # not.
        winreg = _winreg()
        if winreg is None:
            return None
        for hive, path in ((winreg.HKEY_CURRENT_USER, "Environment"),
                           (winreg.HKEY_LOCAL_MACHINE, SYSTEM_ENV_KEY)):
            try:
                with winreg.OpenKey(hive, path) as key:
                    value, kind = winreg.QueryValueEx(key,
                                                      "CLAUDE_CONFIG_DIR")
            except OSError:
                continue
            if kind == winreg.REG_EXPAND_SZ:
                value = winreg.ExpandEnvironmentStrings(value)
            return value or None
        return None

    def registered_argv(self):
        # The launcher runs blink.exe whatever wrote it (LAUNCHER_VBS). An
        # old checkout install wrote the launcher and no blink.exe;
        # service_program() is where that reads as "none".
        if os.path.exists(launcher_path()):
            return [installed_bin()]
        return None

    def _clear_stale_checkout_registration(self):
        """Take down what a checkout install registered before the refusal
        existed: a task, a Run value and a launcher, all pointing at a
        blink.exe that was never copied. Only when that binary really is
        missing -- a fleet desk's frozen install sits under the same names,
        and running install from a checkout there must not touch it."""
        if not os.path.exists(launcher_path()) or os.path.exists(installed_bin()):
            return
        subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK_NAME],
                       capture_output=True, **update.ota.NO_WINDOW)
        _autostart_clear()
        _rm(launcher_path())

    def creates(self):
        if self.checkout_refusal():
            return None
        return (f'a Scheduled Task named "{TASK_NAME}", and {launcher_path()}\n'
                "             the one-line script it runs, so the bridge starts\n"
                "             with no window. If Windows will not register the\n"
                "             task without administrator, an entry named\n"
                f'             "{RUN_VALUE}" under your own account\'s Run key\n'
                "             is used instead, which starts it the same way")

    def install(self) -> str:
        refusal = self.checkout_refusal()
        if refusal:
            self._clear_stale_checkout_registration()
            return refusal
        # The task runs the launcher, not the program: see launcher_path().
        # The bridge's output goes to bridge.log by its own hand (--log),
        # since a hidden window is nowhere for it to go.
        try:
            os.makedirs(blink_home(), exist_ok=True)
            with open(launcher_path(), "w", encoding="ascii", newline="\r\n") as f:
                f.write(LAUNCHER_VBS)
        except OSError as e:
            return f"could not write {launcher_path()}: {e}"
        # /f overwrites a task from an earlier install rather than failing.
        #
        # This is tried FIRST because a Scheduled Task is the better of the
        # two: it can be ended and re-run by name, which is what `blink
        # update` restarts through. But it is not always allowed -- see
        # _install_without_a_task, which is where a refusal goes.
        r = subprocess.run(
            ["schtasks", "/create", "/f", "/tn", TASK_NAME, "/sc", "onlogon",
             "/tr", f'wscript.exe //B //Nologo "{launcher_path()}"'],
            capture_output=True, text=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            return self._install_without_a_task(r)
        # A daemon from an earlier install is still running, holding the
        # serial port and the single-instance lock, and /create does nothing
        # to it: on the first reinstall over a live 1.0.2 the new task's
        # daemon started, found the lock held, and left -- the old one, with
        # its window, carried on (2026-08-29). Stop it the way restart() does.
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, **update.ota.NO_WINDOW)
        _kill_recorded_daemon()
        # /sc onlogon does not start it now, only at the next logon.
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True, **update.ota.NO_WINDOW)
        # And then ASK Windows, rather than assume. /run returning zero means
        # the task was triggered, not that a daemon exists -- the same claim
        # from the same kind of evidence that left a Mac with no bridge and
        # an installer saying "running (launchd)" on 2026-09-04.
        #
        # Six tries, not four: this is the first run of a freshly copied .exe,
        # and Windows may hold it while a virus scanner reads all of it.
        if _confirm_running(self._is_running, tries=6):
            return _RUNNING_TASK
        return ("registered as a Scheduled Task, but it is not running -- "
                f'start it with: schtasks /run /tn "{TASK_NAME}"')

    def _install_without_a_task(self, refused) -> str:
        """Windows would not register the task. Start at logon the other way.

        A logon TRIGGER needs administrator. Measured on the Windows 10
        release desk (19045), 2026-09-06, from a genuinely non-elevated
        process, on a machine with stock Task Scheduler ACLs
        (`Authenticated Users:(CI)(W,Rc)`) and no policy set:

            schtasks /create /sc once     ... SUCCESS
            schtasks /create /sc onlogon  ... ERROR: Access is denied.
            schtasks /create /sc onlogon /ru <user> ... Access is denied.

        The comment that used to sit above the /create call said "/sc onlogon
        needs no admin rights; a Windows service would". The first half was
        wrong, and believing it was expensive because the failure was silent
        and complete: install printed one line about a task it had not
        registered, `blink status` said "Bridge not installed", no daemon
        ever ran, and the panel sat on its standalone setup screen -- with
        the board plugged in, driver installed and working. A customer
        reported exactly that.

        HKCU\\...\\Run is the account's own key, so no administrator is
        involved, and it is what user-level programs have always started
        from. It is the poorer mechanism -- there is no name to /end or /run,
        so restart() has to start the launcher itself -- which is why it is
        second rather than first.
        """
        _autostart_clear()
        err = _autostart_set()
        if err:
            why = (refused.stderr or "").strip()[:60]
            return (f"could not register a Scheduled Task ({why}), and could"
                    f" not set it to start at logon either: {err}")
        # Same reasoning as the task path: a daemon from an earlier install
        # still holds the serial port and the single-instance lock, and
        # nothing above has stopped it.
        _kill_recorded_daemon()
        _start_launcher()
        if _confirm_running(self._is_running, tries=6):
            return _RUNNING_RUN_KEY
        return ("set to start when you log in, but it is not running yet -- "
                f"see {log_path()}")

    def _is_running(self) -> bool:
        """Is the bridge daemon alive? Asked of Windows, by pid.

        NOT `schtasks /query`. The task's status answers a different question:
        its action is wscript running LAUNCHER_VBS, which starts the bridge
        with `, 0, False` -- do not wait -- and exits at once. A perfectly
        healthy Blink therefore shows the task as Ready within a second of
        /run, so a check on the task's state would have called every working
        install dead. Registration is not health, and neither is the
        launcher's own exit.

        Nor could it be read from the word schtasks prints: that word is
        translated on a localised Windows, and the owner's PC is one.

        So the question is the one _kill_recorded_daemon already asks -- the
        pid the daemon wrote for itself -- with the image name as a filter so
        a recycled pid belonging to something else cannot answer for it. Only
        ~/.blink/bridge.pid is read, not the pre-1.1.0 copies that function
        also sweeps: a daemon still running out of bin.old is the thing an
        install was trying to replace, and counting it as health would report
        exactly the failure of 2026-08-29 as a success.
        """
        try:
            with open(pid_path(), encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            return False
        r = subprocess.run(
            ["tasklist", "/fi", f"PID eq {pid}",
             "/fi", "IMAGENAME eq " + os.path.basename(installed_bin()),
             "/nh", "/fo", "csv"],
            capture_output=True, text=True, **update.ota.NO_WINDOW)
        # tasklist exits ZERO whether or not anything matched; with no match
        # it prints "INFO: No tasks are running which match ...". The pid
        # appearing in a row it printed is the only answer that means
        # anything -- and that row is digits either way, in any language.
        return str(pid) in (r.stdout or "")

    def restart(self) -> str:
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME],
                       capture_output=True, **update.ota.NO_WINDOW)
        # /end only reaches the instance the TASK launched. A daemon that
        # replaced itself started its successor detached (see
        # update.restart_from_daemon), and that one is not the task's -- so
        # without this, /run below would add a second daemon competing for the
        # same serial port.
        _kill_recorded_daemon()
        r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                           capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            # No task to trigger. If this install starts from the Run key
            # instead, there is nothing wrong -- start the launcher directly,
            # which is what that key would have done at the next logon.
            # Without this, `blink update` on such an install replaces the
            # binary and then leaves nothing running.
            if not _autostart_present():
                return "could not restart it"
            _start_launcher()
        if _confirm_running(self._is_running, tries=6):
            return "restarted"
        # This is the `blink update` path: the .exe under the task was just
        # replaced. If the new one cannot start, /run still succeeds and the
        # log is the only thing that says why -- and unlike install() there is
        # no command worth pasting, because the command that would be printed
        # is the one that just ran and left nothing running.
        return ("restarted, but it is not running -- "
                f"see {log_path()}")

    def halt(self) -> None:
        """Let go of bin\\blink.exe so the update swap can rename the directory.

        Windows is the one platform where halting is not the same as stopping
        a supervised service: it is the one that refuses to rename a directory
        while a process holds a file open inside it. stop() already frees
        exactly what needs freeing here -- /end for the instance the task
        launched, and the recorded pid for a daemon that started its own
        successor detached -- and leaves the task and the Run key alone, which
        is the property that matters: a halt that unregistered the service
        would turn a failed update into a machine that never starts Blink
        again.

        The answer is discarded on purpose. There is nothing a caller in the
        middle of an update can do with "not installed", and update._replace()
        waits for the handle regardless of whether this took.
        """
        self.stop()

    def _registered(self, runner=None) -> bool:
        r = (runner or subprocess.run)(["schtasks", "/query", "/tn", TASK_NAME],
                                       capture_output=True, **update.ota.NO_WINDOW)
        return r.returncode == 0

    def stop(self, runner=None) -> str:
        """/end, and then the daemon /end cannot reach.

        Same trap as restart(): /end stops the instance the TASK launched,
        while a daemon that replaced itself started its successor detached
        (see update.restart_from_daemon). That successor keeps the serial
        port, and a second process asking for the same COM port is refused
        with "Access is denied" -- which looks exactly like a board fault. So
        the kill happens even when there is no task registered at all: the
        port is the thing being freed here, not the task, and an unregistered
        task with a live detached daemon says so rather than reporting the
        bare "not installed" that would deny the port was ever held.
        """
        run = runner or subprocess.run
        registered = self._registered(run)
        r = None
        if registered:
            r = run(["schtasks", "/end", "/tn", TASK_NAME],
                    capture_output=True, **update.ota.NO_WINDOW)
        killed = _kill_recorded_daemon(runner=run)
        if not registered and _autostart_present():
            # Installed through the Run key, because Windows would not
            # register the task. Stopped, so start() brings it back: as
            # "not installed" provision left it down (review, 2026-10-25).
            return "stopped (started from the Run key)"
        if not registered:
            return ("not installed; killed a detached daemon" if killed
                    else "not installed")
        if r.returncode == 0:
            return "stopped"
        return f'could not stop it: schtasks /end /tn "{TASK_NAME}"'

    def start(self, runner=None) -> str:
        run = runner or subprocess.run
        if not self._registered(run):
            # The Run-key install restart() already knows about: start the
            # launcher the key would have started at the next logon.
            if _autostart_present():
                _start_launcher()
                return "started"
            return "not installed"
        r = run(["schtasks", "/run", "/tn", TASK_NAME],
                capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "started"
        return f'could not start it: schtasks /run /tn "{TASK_NAME}"'

    def remove(self) -> str:
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True, **update.ota.NO_WINDOW)
        subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK_NAME],
                       capture_output=True, **update.ota.NO_WINDOW)
        # Unconditionally, and without asking first: an install may have used
        # either mechanism, uninstall does not know which, and a leftover Run
        # entry would try to start a program that is about to be deleted on
        # every logon for the rest of the machine's life.
        _autostart_clear()
        _kill_recorded_daemon()
        _kill_by_path()
        return "removed"

    def status(self) -> str:
        r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                           capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "registered as a Scheduled Task"
        # Not "not installed" until BOTH have been asked. Reporting the
        # absence of the task as the absence of the bridge is what made this
        # failure look like a missing install rather than a working one.
        if _autostart_present():
            return "registered to start when you log in"
        return "not installed"


# How long a systemd stop may take before `blink update` or `uninstall`
# says why it is waiting. An idle daemon exits within a second of SIGTERM;
# one inside a firmware write or an app swap holds the stop until the work
# returns, which with TimeoutStopSec=STOP_GRACE_S can be three minutes of a
# command printing nothing (review of the caps change, 2026-10-27).
SLOW_STOP_NOTE_S = 4.0


def _noting_a_slow_stop(call):
    """call(), with one line on stderr if it is still running after
    SLOW_STOP_NOTE_S. systemctl gives no sign of what it is waiting on."""
    import threading
    note = threading.Timer(
        SLOW_STOP_NOTE_S,
        lambda: print("  Waiting for the bridge to finish a firmware write or"
                      f" update before it stops (up to"
                      f" {STOP_GRACE_S // 60} minutes)...",
                      file=sys.stderr, flush=True))
    note.daemon = True
    note.start()
    try:
        return call()
    finally:
        note.cancel()


class _SystemdBackend(_Backend):
    """Linux, where the supervisor may simply not be there.

    systemd is overwhelmingly the default but it is not guaranteed, and a
    machine without it is not a broken machine -- so every method below has
    a real answer for that case rather than an error.
    """

    @staticmethod
    def _has_systemctl() -> bool:
        return bool(shutil.which("systemctl"))

    def creates(self):
        return unit_path()

    @staticmethod
    def _booted() -> bool:
        """systemd is PID 1 here. WSL ships systemctl without it, so
        _has_systemctl() alone predicted a running service there."""
        return os.path.isdir("/run/systemd/system")

    def runs_checkout(self) -> bool:
        # Without systemctl the unit is written but nothing runs it.
        return self._has_systemctl() and self._booted()

    def registered_argv(self):
        if not os.path.exists(unit_path()):
            return None
        with open(unit_path(), encoding="utf-8") as f:
            lines = [l for l in f.read().splitlines()
                     if l.startswith("ExecStart=")]
        if not lines:
            return None
        # systemd's double-quote escaping (see _systemd_exec) is a subset of
        # what shlex undoes in POSIX mode; the doubled % and $ are undone by
        # hand, in one pass so "%%%%" comes back as "%%".
        words = shlex.split(lines[0][len("ExecStart="):])
        return _argv_ending_in_run([_undouble(w) for w in words])

    def install(self) -> str:
        # The unit file is written either way. It costs nothing, and it means
        # a machine that gains systemd later already has the right file.
        argv = _service_command()
        os.makedirs(os.path.dirname(unit_path()), exist_ok=True)
        with open(unit_path(), "w", encoding="utf-8") as f:
            f.write(_unit_text(argv))
        if not self._has_systemctl():
            # The program the unit names, which is what a person should
            # start by hand in its place.
            return f"no systemd here; run it yourself: {_quote_line(argv[:-1])} run"
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True, **update.ota.NO_WINDOW)
        r = subprocess.run(["systemctl", "--user", "enable", "--now",
                            "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode != 0:
            return "installed, but could not be started: systemctl --user enable --now blink-bridge"
        # `enable --now` RETURNED ZERO. That is systemctl accepting the
        # request; it is not a running unit, and this line has already been
        # wrong once in a way nobody caught: see _systemd_exec(), where an
        # unquoted ExecStart with a space in it failed to locate the
        # executable "while `blink install` still printed running (systemd)".
        # Ask systemd instead.
        if _confirm_running(self._is_running):
            return _RUNNING_SYSTEMD
        return ("enabled with systemd, but it is not running -- "
                "see: systemctl --user status blink-bridge.service")

    def _is_running(self) -> bool:
        """Does systemd say the unit is active? Its word, not an exit code.

        `is-active` WITHOUT --quiet prints the state -- active, activating,
        inactive, failed -- and that word is the answer. status() may use the
        exit code because there the command asked and the fact reported are
        the same thing; here the temptation is to reuse the exit code of
        `enable --now`, which is a different command reporting a different
        fact, and that is precisely the defect.

        Anything unrecognised reads as not running: an empty dump means we
        could not see it, and not-seen must never print as healthy.

        Worth the retry even though Restart=always makes systemd patient: a
        unit that has been asked to start reads "activating" until its exec
        is up, and answering the first ask would call that dead.
        """
        r = subprocess.run(["systemctl", "--user", "is-active",
                            "blink-bridge.service"],
                           capture_output=True, text=True,
                           **update.ota.NO_WINDOW)
        lines = (r.stdout or "").split()
        return bool(lines) and lines[0] == "active"

    def restart(self) -> str:
        if not self._has_systemctl():
            return super().restart()
        r = _noting_a_slow_stop(lambda: subprocess.run(
            ["systemctl", "--user", "restart", "blink-bridge.service"],
            capture_output=True, **update.ota.NO_WINDOW))
        if r.returncode != 0:
            return "could not restart it"
        if _confirm_running(self._is_running):
            return "restarted"
        return ("restarted, but systemd reports it is not running -- "
                "see: systemctl --user status blink-bridge.service")

    def stop(self, runner=None) -> str:
        if not self._has_systemctl():
            return super().stop(runner)
        if not os.path.exists(unit_path()):
            return "not installed"
        r = (runner or subprocess.run)(
            ["systemctl", "--user", "stop", "blink-bridge.service"],
            capture_output=True, **update.ota.NO_WINDOW)
        # An explicit stop is not undone by Restart=always, so unlike launchd
        # this needs nothing stronger than the obvious command.
        if r.returncode == 0:
            return "stopped"
        return "could not stop it: systemctl --user stop blink-bridge.service"

    def start(self, runner=None) -> str:
        if not self._has_systemctl():
            return super().start(runner)
        if not os.path.exists(unit_path()):
            return "not installed"
        r = (runner or subprocess.run)(
            ["systemctl", "--user", "start", "blink-bridge.service"],
            capture_output=True, **update.ota.NO_WINDOW)
        if r.returncode == 0:
            return "started"
        return "could not start it: systemctl --user start blink-bridge.service"

    def remove(self) -> str:
        if not self._has_systemctl():
            # Install said "no systemd here; run it yourself", so whatever is
            # running was started by hand and nothing here can stop it. Saying
            # "removed" would be followed a line later by "Nothing of Blink's
            # is left running", which would not be true.
            _rm(unit_path())
            return "no systemd here; stop it yourself if you started it"
        _noting_a_slow_stop(lambda: subprocess.run(
            ["systemctl", "--user", "disable", "--now",
             "blink-bridge.service"],
            capture_output=True, **update.ota.NO_WINDOW))
        _rm(unit_path())
        subprocess.run(["systemctl", "--user", "daemon-reload"],
                       capture_output=True, **update.ota.NO_WINDOW)
        return "removed"

    def status(self) -> str:
        if not self._has_systemctl():
            # Was "unknown on linux", which is the drift described on
            # _Backend: remove() knew exactly what this machine's situation
            # was and status() claimed not to.
            return "no systemd here; not something this can check"
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                            "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
        return "running" if r.returncode == 0 else "not running"


def backend() -> _Backend:
    """The login-service backend for this machine.

    Built fresh each call rather than cached at import: every path it uses is
    resolved from HOME at call time (see the note above blink_home), and the
    tests move HOME between calls.
    """
    if sys.platform == "darwin":
        return _LaunchdBackend()
    if sys.platform == "win32":
        return _SchtasksBackend()
    if sys.platform.startswith("linux"):
        return _SystemdBackend()
    return _Backend()


# The service files earlier releases wrote, kept verbatim so the daemon can
# tell one of ours from one somebody has edited (refresh_service_file).
# Every release from v0.7.0, the first named Blink, to v1.3.4 wrote exactly
# these two templates; v0.6.0 was com.clauge.bridge, at other paths, and is
# nothing this could find. tests/pc/test_stop_grace.py compares them with
# `git show <tag>:pc/cli.py` wherever the tags are in the clone. Append a
# copy here, never edit one, whenever _PLIST_TEMPLATE or _UNIT_TEMPLATE
# changes in a release.
_PLIST_TEMPLATE_V134 = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD plist_path() 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label}</string>
  <key>ProgramArguments</key>
  <array>
{args}  </array>
  <!-- Frozen Python buffers stdout when it is a file, so without this the
       log lags minutes behind the daemon and reads as a hang. -->
  <key>EnvironmentVariables</key>
  <dict><key>PYTHONUNBUFFERED</key><string>1</string></dict>
  <key>RunAtLoad</key><true/>
  <!-- The bridge waits for a board rather than exiting, so KeepAlive is a
       backstop for a crash, not the normal path. ThrottleInterval keeps a
       repeatable crash from becoming a spin. -->
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>10</integer>
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

_UNIT_TEMPLATE_V134 = """[Unit]
Description=Blink USB bridge

[Service]
ExecStart={command}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
"""


def _plist_text_v134(argv) -> str:
    """The plist v0.7.0 to v1.3.4 wrote for `argv`, rendered their way."""
    args = "".join(f"    <string>{_xml_escape(a)}</string>\n" for a in argv)
    return _PLIST_TEMPLATE_V134.format(label=LABEL, args=args,
                                       log=_xml_escape(log_path()))


def _unit_text_v134(argv, exec_line=None) -> str:
    """The unit v0.7.0 to v1.3.4 wrote. Their ExecStart quoted each word but
    did not yet double % and $ (that came after 1.3.4, see _systemd_exec)."""
    if exec_line is None:
        exec_line = " ".join('"%s"' % a.replace("\\", "\\\\")
                             .replace('"', '\\"') for a in argv)
    return _UNIT_TEMPLATE_V134.format(command=exec_line)


def _earlier_service_texts(argv) -> set:
    """Every file a release of ours could have left at this path for argv."""
    if sys.platform == "darwin":
        return {_plist_text_v134(argv)}
    # And the one develop wrote between 1.3.4 and the stop grace: the same
    # template, ExecStart quoted the newer way.
    return {_unit_text_v134(argv),
            _unit_text_v134(argv, exec_line=_systemd_exec(argv))}


def _is_this_program(argv) -> bool:
    """Whether this process is the program `argv` names. Frozen, argv names
    installed_bin() whichever copy is running, so a build unpacked in
    ~/Downloads and started by hand would pass for the installed one; the
    running binary has to be that file (round 2 review, R2-N1)."""
    if not _frozen():
        return True
    try:
        return (os.path.realpath(_self_path())
                == os.path.realpath(installed_bin()))
    except OSError:
        return False


def refresh_service_file():
    """Bring the plist or unit up to this version's, if a release wrote it.

    Called by the daemon at startup. Only `blink install` writes these files,
    and nothing asks anyone to run it again, so a setting added to the
    template -- ExitTimeOut and TimeoutStopSec, the first -- would otherwise
    reach fresh installs only, and every machine updated from 1.3.4 would
    keep launchd's 20 s and systemd's KillMode=control-group under a flash
    (review of the caps change, 2026-10-27). Every update path ends with the
    new daemon starting, so this one place covers all three of them.

    Only a file that is byte for byte what a release of ours wrote for this
    program (_earlier_service_texts) is rewritten. The first version of this
    checked the program line alone and rewrote anything else that differed,
    so an Environment=HTTPS_PROXY= added to the unit, or a BLINK_TAP in the
    plist, was undone on every start without anyone asking (round 2 review,
    R2-S1). `blink install` still overwrites edits, as it always has; it is
    run on purpose.

    Written, never reloaded. A bootout, a bootstrap or a daemon-reload from
    inside the job is the daemon stopping or re-reading itself at a moment
    it did not choose, possibly mid-flash. The file takes effect when the
    supervisor next reads it: launchd at the next login, systemd at the
    next daemon-reload or reboot, either at the next `blink install`. Until
    then the running job keeps the settings it was started with, and
    `blink update` does not change that: launchd's `kickstart -k` and
    systemd's `restart` both restart the job from the definition already
    loaded.

    Left alone unless it names exactly the program this process would
    register (_service_command) and this process is that program: a
    checkout daemon run by hand beside a frozen install must not repoint
    the login service at the checkout, nor a frozen one a checkout's. A
    missing file is never created, and Windows has no file of this kind. A
    symlinked file (stow, chezmoi) is replaced at its target, so the link
    survives; a target that cannot be written is left as it is.

    Races with the commands that also write it are harmless. Uninstall
    stops the job before it removes the file (launchd bootout, systemd
    disable --now), and this runs before the daemon's SIGTERM handler is
    installed, so a daemon starting then dies before it could write; a
    concurrent `blink install` either leaves a half-written file this does
    not recognise, or one it rewrites to the same text install is writing.

    Returns "updated" when the file was rewritten, "edited" when it is ours
    but changed by hand (the daemon then names `blink install`), and None
    when there was nothing to do or nothing it should touch.
    """
    if _skip_service():
        return None
    if sys.platform == "darwin":
        path, render = plist_path(), _plist_text
    elif sys.platform.startswith("linux"):
        path, render = unit_path(), _unit_text
    else:
        return None
    argv = _service_command()
    try:
        if (backend().registered_argv() != argv[:-1]
                or not _is_this_program(argv)):
            return None
        target = os.path.realpath(path)
        with open(target, encoding="utf-8") as f:
            have = f.read()
    except Exception:
        # A plist that does not parse, a file we cannot read: the job it
        # describes still runs, and that is what matters here.
        return None
    want = render(argv)
    if have == want:
        return None
    if have not in _earlier_service_texts(argv):
        return "edited"
    tmp = target + ".tmp"
    replaced = False
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(want)
        os.replace(tmp, target)
        replaced = True
    except OSError:
        return None
    finally:
        # Also on SIGTERM or ^C between the write and the replace: nothing
        # of ours is left beside the real file in ~/Library/LaunchAgents.
        if not replaced:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return "updated"


def _install_service() -> str:
    if _skip_service():
        return f"skipped ({_skip_reason()})"
    return backend().install()


# The exact answers install() gives when the service is up, and nothing
# else. Compared whole, for the reason _RESTART_OK below is: a prefix test on
# "running" would accept any future "running (x), but ..." failure line.
_RUNNING_LAUNCHD = "running (launchd)"
_RUNNING_SYSTEMD = "running (systemd)"
_RUNNING_TASK = "running (Scheduled Task)"
_RUNNING_RUN_KEY = "running (starts when you log in)"
_SERVICE_RUNNING = frozenset({_RUNNING_LAUNCHD, _RUNNING_SYSTEMD,
                              _RUNNING_TASK, _RUNNING_RUN_KEY})


def _service_started(result: str) -> bool:
    """Whether _install_service()'s answer says the service is running.
    Everything else -- skipped, no systemd, a failed bootstrap, WSL with no
    user manager -- is something else, and install must not then say "the
    service runs from" the checkout (review, 2026-10-25)."""
    return result in _SERVICE_RUNNING


def restart_service() -> str:
    """Bounce the login service so it comes up on a freshly replaced binary.

    Not the same as exiting and letting the supervisor notice: this is called
    from `blink update`, which is a separate process from the daemon. The
    daemon's own path is simpler -- on macOS and Linux it exits and KeepAlive /
    Restart=always bring it back. Windows has neither: a Scheduled Task with an
    onlogon trigger does not restart anything, so it is told explicitly.
    """
    if _skip_service():
        return f"skipped ({_skip_reason()})"
    return backend().restart()


# The exact word every backend returns when the service is back up, and
# nothing else. Compared with == and not with startswith(), which is not
# fussiness: three of the four backends report failure as "restarted, but
# launchd reports it is not running -- see ...", and a prefix test reads that
# as success. `4ba9c7d fix: Linux and Windows claimed a running service from
# an exit code` was this same mistake one layer down.
_RESTART_OK = "restarted"

# Nothing was attempted, so there is nothing here to call broken: a test
# harness with BLINK_SKIP_SERVICE=1, and a platform with no supervisor to
# bounce (a developer running out of a checkout).
_RESTART_NOT_ATTEMPTED = ("skipped (", "not running under a supervisor")


def restart_left_it_down(detail: str) -> bool:
    """Did the bounce end with no daemon running?

    `blink update` printed the backend's answer and returned 0 regardless, so
    a service that did not come back was one line in the middle of a
    successful-looking update. The board then sits on "Link the PC daemon"
    forever with nothing on the computer saying why -- which is exactly what
    a customer hit upgrading 1.2.5 -> 1.3.2 on 2026-09-09. Their 1.2.5
    updater could not tell (every check in restart() landed in 1.3.0 and
    1.3.2, after the version doing the upgrading); this one can, and the
    point of saying so is that `blink install` fixes it in one command.
    """
    if detail == _RESTART_OK:
        return False
    return not detail.startswith(_RESTART_NOT_ATTEMPTED)


def _refresh_shims() -> None:
    """Put THIS version's shims on disk, as part of an update.

    `blink update` replaces the program and nothing else, and both shims ship
    inside it: after a swap, ~/.blink/blink-hook.sh is still the copy the
    previous release wrote out. The daemon's DriftWatchdog repairs that
    within five minutes -- but only while a daemon is running, and a daemon
    that did not come back is precisely the case this matters in. Two file
    writes here remove the dependency instead of relying on the half that may
    be down to fix the other half.

    Only shims that are ALREADY there are rewritten. A path that does not
    exist belongs to an install that never happened or was undone, and
    `update` is not the command that sets one up.

    Best effort: an update whose program swap has already succeeded must not
    report failure because a shim could not be rewritten. The watchdog still
    sits behind this.
    """
    for path, name in ((shim_path(), "blink-statusline.sh"),
                       (hook_shim_path(), "blink-hook.sh")):
        if not os.path.exists(path):
            continue
        try:
            _write_shim(path, name)
        except OSError as e:
            print(f"  could not refresh {os.path.basename(path)}: {e}")


def halt_service() -> None:
    """Stop the daemon before replacing the program underneath it.

    Best effort and silent: on the platforms where it does nothing this is
    correct, and on the one where it matters the retry in update._replace()
    still covers a stop that did not take. Nothing here is worth a line of
    output in the middle of an update.
    """
    if _skip_service():
        return
    try:
        backend().halt()
    except Exception:
        pass


def _say_bye():
    """Tell the board the app is leaving for good, so it shows \"connecting\"
    rather than dozing off (firmware 1.2.0 and later). Best effort: the port
    the daemon last used, opened plainly (no DTR/RTS preset -- that would
    reset the board), one line, gone. Any failure is silent; the board falls
    back to what it always did."""
    try:
        import json
        import serial
        from pc import protocol
        with open(os.path.join(blink_home(), "board.json"), encoding="utf-8") as f:
            port = json.load(f).get("port")
        if not port:
            return
        # encode_checked, as every production writer is (CLAUDE.md); a bye
        # the board dropped would be no bye at all.
        raw, _why = protocol.encode_checked(protocol.bye())
        if raw is None:
            return
        with serial.Serial(port, 115200, timeout=0.2, write_timeout=1) as s:
            s.write(raw)
            s.flush()
    except Exception:
        pass


def _remove_service() -> str:
    if _skip_service():
        return f"skipped ({_skip_reason()})"
    return backend().remove()


# One definition, in pc/update.py. This module already imports it.
_rm = update._rm


def _kill_recorded_daemon(runner=None) -> int:
    """Stop the bridge by the pid it wrote for itself, not by its name.

    Returns how many pids it signalled, which stop() reports: on a machine
    with no Scheduled Task registered, a daemon killed here is the difference
    between a freed serial port and one still held.

    Ending the Scheduled Task ends the process the task launched. PyInstaller's
    onefile bootloader re-executes the same .exe as a child, and that child
    keeps running the bridge loop and keeps blink.exe open, which is enough for
    Windows to refuse every attempt to delete it -- including the detached
    rmdir scheduled for after we exit.

    By pid, with the image name only as a FILTER: `taskkill /im blink.exe`
    matches the uninstaller too, and killing ourselves mid-uninstall is exactly
    what the previous attempt did. /t takes the bootloader's child with it.
    """
    if sys.platform != "win32":
        return 0
    killed = 0
    # Every place a daemon may have left its pid: ~/.blink since 1.1.0, and
    # beside the program before that -- which, after 1.1.0's install has
    # rotated the directory, means bin.old. The first 1.0.4 -> 1.1.0
    # install on a real PC left the old daemon running with the serial port
    # because only the new location was read (2026-08-29).
    for path in (pid_path(),
                 os.path.join(bin_dir(), "bridge.pid"),
                 os.path.join(bin_dir() + ".old", "bridge.pid")):
        try:
            with open(path, encoding="utf-8") as f:
                pid = int(f.read().strip())
        except (OSError, ValueError):
            continue
        if pid == os.getpid():
            continue              # somehow ours; nothing to stop
        # runner, so _Backend.stop() can be handed one and have every command
        # it causes go the same way. None is subprocess.run, resolved here
        # rather than as a default argument so the existing callers -- and the
        # tests that stub subprocess.run for them -- are unaffected.
        (runner or subprocess.run)(
            ["taskkill", "/f", "/t", "/pid", str(pid),
             "/fi", "IMAGENAME eq " + os.path.basename(installed_bin())],
            capture_output=True, **update.ota.NO_WINDOW)
        killed += 1
    return killed


def _kill_by_path():
    """Last resort: anything running the exact binary we are deleting.

    The pid file is the good path and this is what happens when it is missing
    -- an install from before it existed, a daemon killed before it could write
    one, a profile that moved. Matched on the full executable PATH and not on
    the image name, so the uninstaller (same name, different file, or the very
    same file) is never in scope, and our own pid is excluded outright.
    """
    if sys.platform != "win32":
        return
    target = installed_bin().replace("'", "''")     # PowerShell string escape
    script = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.ExecutablePath -eq '{target}' "
        f"-and $_.ProcessId -ne {os.getpid()} }} | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive",
                    "-Command", script], capture_output=True, **update.ota.NO_WINDOW)


def _remove_bin_dir(attempts=6):
    """Delete ~/.blink/bin. Returns (done, message).

    Straightforward everywhere but Windows, which will not delete a running
    executable -- and here the executable is usually this one. The undo hint we
    print says `~/.blink/bin/blink.exe uninstall`, so a customer following it
    is asking a program to delete the file it is running from. The daemon can
    be holding the same file too: `schtasks /end` returns before the process
    has actually exited.

    So: try, wait, try again, and if Windows still says no, hand the job to
    something that will outlive us.

    This used to end with `taskkill /f /im blink.exe`, which is worse than the
    problem it was for -- the uninstaller has that image name, so it killed
    itself, mid-uninstall, having already removed the Scheduled Task and the
    status line. Every Windows scenario in CI exited non-zero with no output at
    all, which is exactly what being terminated looks like.
    """
    for attempt in range(attempts):
        for d in (bin_dir(), bin_dir() + ".old", bin_dir() + ".new"):
            shutil.rmtree(d, ignore_errors=True)
        if not any(os.path.exists(d) for d in
                   (bin_dir(), bin_dir() + ".old", bin_dir() + ".new")):
            return True, "removed"
        time.sleep(0.4 * (attempt + 1))
    if sys.platform == "win32":
        return _schedule_windows_cleanup()
    return False, "could not be removed -- something is still running from it"


def _schedule_windows_cleanup():
    """Let Windows delete the directory once nothing is running inside it.

    A detached cmd that waits a few seconds and then removes the tree. By the
    time it fires, this process has exited and released its own hold, and the
    service stopped a moment ago has finished going away.

    `ping -n 4 127.0.0.1` rather than `timeout /t`, which wants a console and
    fails outright in a detached process that has no window.
    """
    script = "ping -n 4 127.0.0.1 >nul" + "".join(
        f' & rmdir /s /q "{d}"' for d in (bin_dir(), bin_dir() + ".old",
                                          bin_dir() + ".new"))
    DETACHED_NO_WINDOW = 0x00000008 | 0x08000000
    try:
        subprocess.Popen(["cmd", "/c", script],
                         creationflags=DETACHED_NO_WINDOW, close_fds=True)
    except Exception as e:
        return False, f"could not be removed ({e})"
    return True, "will be gone a moment after this window closes"


# -------------------------------------------------------------------- install


def _announce():
    """Say what is about to change, before changing it.

    Install is deliberately unattended -- it asks nothing, because plugging the
    board in is meant to be the whole setup. That makes disclosure the only
    thing standing between us and silently editing a file the customer owns,
    so it is not optional and it runs before the first write.
    """
    print("Blink setup. Here is everything it is about to do, before it does any of it.")
    print()
    if _frozen():
        print(f"  Creates    {installed_bin()}")
        print("             a copy of this program and its support files, so the")
        print("             folder you downloaded can be deleted when this finishes.")
    else:
        # Nothing is copied from a checkout (the Program step skips it).
        # The frozen paragraph would announce a file that never appears and
        # suggest deleting the one folder every printed command depends on.
        # This runs before any step, so what it says about the service is a
        # forecast from the backend; the summary at the end of install
        # reports what the service step really did.
        pad = " " * 13
        print(f"  Runs from  {_checkout_dir()}")
        print(pad + "where it already is; a checkout is not copied.")
        if _service_runs_checkout() and not _skip_service():
            print(pad + "The login service starts blink_main.py from")
            print(pad + "this folder, so moving or deleting the folder")
            print(pad + "stops it.")
        else:
            print(pad + "Every command printed below starts from this")
            print(pad + "folder, so leave it where it is.")
    print(f"  Creates    {shim_path()}")
    print("             the small script Claude Code runs to hand over the")
    # What the daemon reads out of that payload, said field by field. This
    # said "Only the usage figures are read out of it" for the whole of
    # feat/ultra-burn while pc/providers/claude_cli read session_id and
    # cost.total_cost_usd on every poll, to tell a reply that came back
    # without a five-hour window (pc/session_verdict) -- the /code-review max
    # of 2026-11-01 found it. Both stay in the daemon's memory, 64 sessions
    # at most, and are never logged, saved or sent.
    print("             usage figures. It keeps the last status line payload")
    print("             Claude Code sent, which also names the session, the")
    print("             working directory and the transcript file. The bridge")
    print("             reads three things from it: the usage figures, the")
    print("             session id and the session's running cost. The last")
    print("             two only tell it whether Claude Code's replies keep")
    print("             coming back without a five-hour limit, and are never")
    print("             logged or saved.")
    print("             Nothing in it leaves this machine, and it is readable")
    print("             by you alone.")
    print(f"  Creates    {hook_shim_path()}")
    print("             a second small script, run when Claude Code starts and")
    print("             finishes work, so the panel can show whether it is busy.")
    print("             It records the event name, the time, and the session and")
    print("             agent ids Claude Code generates -- used to tell concurrent")
    print("             sessions apart, and for nothing else. No prompt, no tool")
    print("             arguments, no file paths, no message text.")
    print(f"  Creates    {os.path.join(blink_home(), 'state')}")
    print("             one small file per open session, deleted when it ends.")
    print(f"  Changes    {settings_path()}")
    # This list has to stay exactly true. Install asks nothing, so the
    # disclosure is the only thing standing between us and silently editing a
    # file the customer owns -- and it said "statusLine.command, and nothing
    # else in the file" for one release after the hooks key started being
    # written too. A disclosure that is merely mostly right is worse than none,
    # because it is the thing people rely on instead of reading the diff.
    # Counted, not spelled. This said "six" while the code wrote ten: the
    # number drifted when StopFailure, SubagentStart and SubagentStop were
    # added, and nothing failed. The comment above says this list has to stay
    # exactly true because it is the thing people rely on instead of reading
    # the diff -- so it now reads itself off the list it describes, and a test
    # pins the two together.
    print("             two keys: statusLine.command, and an entry under hooks")
    print(f"             for each of {len(install_hooks.HOOK_EVENTS)} Claude Code"
          " events. Nothing else in")
    print("             the file is touched, and your own hooks are left in place.")
    prev_cmd = install_statusline._current_command(
        install_statusline._load(settings_path()))
    if prev_cmd:
        print(f"             Your current status line is kept and still runs:")
        print(f"               {prev_cmd}")
    created = backend().creates()
    if created:
        print(f"  Creates    {created}")
        print("             so the bridge starts when you log in.")
    refusal = backend().checkout_refusal()
    if refusal:
        # No closing period: the line ends in a command, and a period
        # pasted with it made it `run.` (review, 2026-10-25).
        print(f"  {win_driver.as_sentence(refusal)}")
    print()
    # Said to match the setting, not as a blanket promise: a reinstall over
    # an install with status-page alerts on keeps them on, and "sends
    # nothing anywhere" would then be false on the screen that says it.
    print("  It reads no credential and no token, and sends nothing of yours"
          " anywhere.")
    if status_page.enabled(blink_home()) is True:
        print("  Status-page alerts are on: it reads status.claude.com, and")
        print("  status.openai.com for a Codex page, every"
              f" {status_page.POLL_S // 60} minutes.")
    else:
        print("  Status-page alerts are off (blink config status-page on).")
    # Every source the daemon reads usage from, and only those. Local
    # Storage was missing until the review follow-up of 2026-11-01, though
    # pc/desktop_local_storage.py has read the five-hour figure and its
    # countdown out of it since Desktop started keeping one there.
    print("  The usage figures come from Claude Code, Claude Desktop's usage")
    print("  cache and Local Storage, or the Codex session log -- each of")
    # Exactly what pc/providers/codex_cli reads, said in full since the
    # /code-review max of 2026-11-01: the rate limits, session_meta's cwd
    # (the project name on the panel) and the task_*/turn_aborted events
    # (the activity light). The token counts are read only on a weekly-only
    # account, and are said below with that condition on them.
    print("  which has already worked them out. From the Codex log it reads")
    print("  the rate limits, the folder each session runs in (to name the")
    print("  project), and when each turn starts and ends, never the")
    print("  conversation.")
    # The burn view's source (pc/burn_reader), missing from this note until
    # the /code-review max of 2026-11-01: on a desk whose account has no
    # five-hour window, the daemon reads Claude Code's own transcripts --
    # the customer's whole working sessions -- so the one place that tells
    # them what is read has to say so. Conditional in fact, not in print:
    # install cannot know the verdict yet, so it says when it happens.
    #
    # The folder is the one the daemon will read, which is not always the
    # one this shell would: the reader honours CLAUDE_CONFIG_DIR, but a
    # login service does not start from this shell. launchd and systemd
    # hand it none of the variable, and the Windows task gets the account's
    # registry environment (service_claude_config_dir). Printed as this
    # shell resolved it, the note named a folder the daemon never reads on
    # any desk that exports the variable in its shell profile (2026-11-01
    # re-review). Where no service is registered, the daemon is started by
    # hand from a shell, and the shell's folder is the true one.
    from pc import burn_reader
    shell_root = burn_reader.transcripts_root()
    root = shell_root
    if backend().creates() and not _skip_service():
        env = {"CLAUDE_CONFIG_DIR":
               backend().service_claude_config_dir() or ""}
        root = burn_reader.transcripts_root(env)
    # On a line of its own, like the paths above: a home folder's length is
    # unbounded, and inline it ran the paragraph off its margin.
    print("  On an account with no five-hour limit, it also reads the")
    print("  output-token count of each reply from Claude Code's transcripts:")
    print(f"    {root}")
    print("  It holds those counts in memory and sends the board only a total")
    print("  per quarter-hour, never the conversation.")
    if os.environ.get("CLAUDE_CONFIG_DIR") and root != shell_root:
        print("  CLAUDE_CONFIG_DIR is set in this shell, but the background")
        print("  service does not see it, so it reads the folder above.")
    # The same for Codex (CodexBurnReader): an account whose Codex log
    # reports a weekly limit and no five-hour one -- Codex Pro on the
    # owner's desk, 2026-11-01 -- has its page fed from the same rollouts
    # the gauges come from. It reads every token figure of a turn, the
    # input and cache ones too, because together they are what recognises
    # a turn written twice (burn_reader, "WHY last_token_usage"); only the
    # output tokens are added up.
    from pc.providers import codex_cli
    print("  If Codex reports no five-hour limit, it also reads the token")
    print("  counts of each Codex turn from its session log, and adds up the")
    print("  output tokens:")
    print(f"    {codex_cli.sessions_root()}")
    print("  It holds those counts in memory and sends the board only a total")
    print("  per quarter-hour, never the conversation.")
    print()
    print(f"  To undo all of it:  {command_to_type()} uninstall")
    print()


def _install_steps() -> int:
    """How many numbered steps `blink install` prints on this platform."""
    return 6 if sys.platform == "win32" else 5


def _stepper(total):
    """A closure that prints "[n/total] Label ... " and counts for you.

    The count used to be written into each string by hand, which was fine
    while every platform had the same five. It stopped being fine the moment
    Windows earned a sixth: five literals would each have needed a
    conditional, and the failure mode of getting one wrong is an installer
    that counts to 5 and then prints step 6 of 5.
    """
    state = {"n": 0}

    def step(label):
        state["n"] += 1
        print(f"[{state['n']}/{total}] {label} ... ", end="", flush=True)

    return step


def _install_driver_step() -> str:
    """The USB-driver line of `blink install`. Windows only.

    Staging the driver here, rather than waiting for a customer to plug the
    board in and hit the yellow triangle, is the whole point: `pnputil
    /add-driver /install` puts it in Windows' driver store, and a board
    plugged in afterwards binds to it silently. Plug and play, once.

    It is also the one step that can raise a Windows permission prompt, and
    it only does so when there is genuinely something to install -- see
    win_driver.ensure_driver. Under BLINK_SKIP_SERVICE this is skipped
    entirely, for the same reason the login service is: the fleet test suite
    runs `install` for real on the Windows desk, and a modal permission
    prompt on a machine nobody is sitting at hangs the run until it times out.
    """
    if _skip_service():
        return f"not checked ({_skip_reason()})"
    try:
        _, line = win_driver.ensure_driver()
        return line
    except Exception as e:
        # The board is the product, but a driver that would not install is
        # not a reason to abandon an install that has otherwise worked --
        # the customer can still run `blink driver` afterwards.
        return f"skipped ({e})"


def cmd_driver(_args) -> int:
    """`blink driver` -- install the USB driver, and say what happened.

    Exists as its own command for two audiences: a customer being walked
    through a support conversation ("run blink driver"), and anyone whose
    install ran before this version, or who declined the permission prompt
    the first time.
    """
    if sys.platform != "win32":
        print("The board's USB-serial chip needs no driver on this system --")
        print("macOS and Linux both carry one in the kernel.")
        return 0

    undriven = win_driver.undriven_boards()
    if undriven:
        print(win_driver.as_sentence(win_driver.summary(undriven)) + ".")
    print("USB driver ... ", end="", flush=True)
    # Forced only when a board is sitting there undriven. Being told "already
    # installed" is the correct answer on a healthy machine, and the wrong
    # one for somebody looking at a yellow triangle: whatever the store
    # holds, it is evidently not reaching their board, so install it again.
    status, line = win_driver.ensure_driver(force=bool(undriven))
    print(line)

    still = win_driver.undriven_boards()
    if not still:
        if undriven:
            print()
            print("The board is set up. Check it with:")
            print(f"  {command_to_type()} status")
        return 0

    # Installed, or claimed to be, and Windows still will not use the device.
    # Repeating "run blink driver" here would be a loop, so this is where the
    # advice stops being something this program can do for them.
    print()
    print(f"Windows still cannot use the board: {win_driver.summary(still)}.")
    if status in ("installed", "ok"):
        print("  Unplug the board, plug it back in, and run this again.")
        print("  If it stays, the machine may need a restart.")
    else:
        print(f"  Install the driver by hand from {win_driver.DRIVER_PAGE}")
    return 1


def cmd_install(_args) -> int:
    # Before anything is written, and before the disclosure: if the file we are
    # about to edit does not parse, the honest move is to change nothing at all
    # and say why. The alternative -- treating it as empty -- writes a fresh
    # settings.json over whatever the customer was halfway through editing.
    try:
        install_statusline._load(settings_path())
    except install_statusline.SettingsUnreadable as e:
        print(f"Blink setup stopped. {e}")
        print()
        print("Nothing was changed. Fix the file, or move it aside, and run")
        print("this again.")
        return 1

    _announce()

    # Windows gets one extra step -- the USB driver -- and the numbering has
    # to agree with it, so it is counted rather than written out. macOS and
    # Linux drive the CH340 out of the kernel and would only be reading a
    # line that said "not needed on this system".
    steps = _install_steps()
    step = _stepper(steps)

    step("Program")
    if _frozen():
        # Only here: a checkout copies nothing, and creating the directory
        # anyway left an empty ~/.blink/bin the disclosure never mentioned.
        os.makedirs(bin_dir(), exist_ok=True)
        # The program is a directory -- the executable and its _internal/
        # support files -- so it is staged whole and rotated in with the
        # same swap the updater uses: <bin>.new is copied, self-tested, and
        # then renamed over <bin> while the previous one goes to <bin>.old.
        # That is what makes re-running the installer over a live install
        # work on every platform: a running program can be renamed but not
        # overwritten, and its directory likewise. Not when it IS the
        # installed copy: a re-run from ~/.blink/bin has nothing to copy.
        src = os.path.dirname(_self_path())
        if os.path.abspath(src) != os.path.abspath(bin_dir()):
            staged = bin_dir() + ".new"
            shutil.rmtree(staged, ignore_errors=True)
            shutil.copytree(src, staged, symlinks=True)
            # Re-running the installer over a live install hits the same
            # Windows rename refusal that `blink update` did; the service is
            # (re)installed and started further down either way.
            halt_service()
            ok, message = update.swap_in(staged, installed_bin(),
                                         RELEASE_VERSION)
            if not ok:
                print(message)
                print()
                print("Nothing else was changed.")
                return 1
        print(installed_bin())
    else:
        # From a checkout there is nothing to copy; the service points at this
        # interpreter instead. Customers never take this path.
        print("running from a checkout, nothing to copy")

    step("Status line")
    os.makedirs(blink_home(), exist_ok=True)
    # Private to the user. The shims write the status line payload -- which
    # names the working directory and the session -- and the per-session
    # state files in here. Both shims create with umask 077, but a directory
    # from an earlier install was made at the default umask, so this
    # tightens it once rather than leaving it to the next mkdir.
    for d in (blink_home(), os.path.join(blink_home(), "state")):
        try:
            os.chmod(d, 0o700)
        except OSError:
            pass          # absent (state/ appears on the first hook), or Windows
    _write_shim(shim_path(), "blink-statusline.sh")
    install_statusline._announce(settings_path(), shim_path(),
                                 undo_hint=f"{command_to_type()} uninstall")
    print("      " + install_statusline.install(settings_path(), shim_path()))

    step("Activity hooks")
    _write_shim(hook_shim_path(), "blink-hook.sh")
    try:
        print(install_hooks.install(settings_path(), hook_shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # The status line is the product; the activity light is a nicety. A
        # hooks section we cannot safely edit costs the user a pulsing dot,
        # and is not worth failing an install that has otherwise worked.
        print(f"skipped ({e})")

    # The disclosure for this step is here rather than up in _announce(),
    # because it is the only step that is conditional on what is already on
    # the machine: on a Codex-free machine there is nothing to disclose, and a
    # paragraph about another vendor's config file would be noise at best and
    # alarming at worst.
    step("Codex hooks")
    if codex_present():
        print()
        _announce_codex_hooks()
        print("      " + _install_codex_hooks())
    else:
        print(_install_codex_hooks())

    if steps == 6:
        step("USB driver")
        print(_install_driver_step())

    step("Background service")
    service = _install_service()
    print(service)

    if _home_refusal():
        _say_home_refused(
            "installed",
            "The files above were written, but nothing starts them.")
        return 1

    print()
    print("Done. Plug the board in over USB -- it picks it up on its own.")
    print(f"  Log:     {log_path()}")
    print(f"  Check:   {command_to_type()} status")
    print(f"  Undo:    {command_to_type()} uninstall")
    print()
    if _frozen():
        print("  You can delete the folder you downloaded.")
    else:
        # Decided by the string the service step handed back, because the
        # platform alone overpromises: WSL without a user manager, a
        # bootstrap that failed, and BLINK_SKIP_SERVICE each end with no
        # service running out of the folder.
        started = _service_runs_checkout() and _service_started(service)
        what = ("the service starts" if started
                else "the commands above start")
        print(f"  Leave {_checkout_dir()} in place: {what} from there.")
    # Absent first: it is a bigger fact than out-of-date, and the two are
    # mutually exclusive (claude_version() gives no version for an absent
    # install, which is exactly what _warn_if_claude_too_old returns on).
    # One read for both: each used to run `claude --version` itself, and a
    # hanging `claude` cost its 30 s timeout twice after "Done." (review,
    # 2026-10-19).
    desk = read_desk()
    _note_if_no_claude_code(desk)
    _warn_if_claude_too_old(desk)
    return 0


def cmd_uninstall(_args) -> int:
    print("Blink uninstall.")
    print()
    print("[1/5] Background service ... ", end="", flush=True)
    print(_remove_service())
    refused = _home_refusal()
    _say_bye()

    print("[2/5] Claude Code setting:")
    try:
        print("      " + install_statusline.uninstall(settings_path(), shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # Keep going. The login service is already gone by this point, so
        # stopping here would leave the machine half-undone -- and the one
        # thing we must not do is "repair" the file by writing a fresh one
        # over whatever the customer was in the middle of editing.
        print(f"      Left alone: {e}")
        print("      Remove the statusLine.command line by hand once it parses.")

    print("[3/5] Activity hooks:")
    try:
        print("      " + install_hooks.uninstall(settings_path(),
                                                 hook_shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # Same reasoning as the step above: keep going, and never "repair" a
        # file we cannot parse by writing a fresh one over it.
        print(f"      Left alone: {e}")

    print("[4/5] Codex hooks:")
    _uninstall_codex_hooks()

    print("[5/5] Files ... ", end="", flush=True)
    # Only what install created. NOT ~/.blink itself: it also holds the two
    # signing keys, which cannot be regenerated -- every board flashed with the
    # first one's public half, and every app carrying the second one's, would
    # stop accepting updates.
    for p in (shim_path(), hook_shim_path(), launcher_path(),
              # Spelled once, in statusline_source.payload_path, and asked
              # for here rather than rebuilt from blink_home().
              statusline_source.payload_path(),
              statusline_source.payload_path() + ".tmp",
              # Pid-scoped temp names, from a render interrupted mid-write.
              *glob.glob(statusline_source.payload_path() + ".*.tmp"),
              os.path.join(blink_home(), "state.json"),
              os.path.join(blink_home(), "state.json.tmp"),
              os.path.join(blink_home(), "pending_fw.json"),
              # `blink config`'s settings, and any temp file a write left.
              # Not install's, but the status-page opt-in is a choice about
              # the network, and a reinstall must not find it already on.
              user_config.path(blink_home()),
              *install_statusline.temp_siblings(
                  user_config.path(blink_home())),
              # The theme's files (pc/theme.FILES): the saved theme, the
              # "seen" record, a preview the builder left, the board's last
              # confirmed theme. Kept, a reinstall sent the old theme to the
              # board on its first connect, which is not what starting over
              # means (review S7, 2026-10-28).
              *_theme_files(blink_home()),
              # Whether the account has a five-hour window
              # (pc/session_verdict), and the temp names its writer,
              # theme._write_json, leaves after a killed write; `.tmp` is the
              # fixed name the writer used before the /code-review max of
              # 2026-11-01. Owner's decision that day: a reinstall starts
              # fresh. Kept, an old "no window" verdict -- a misread one
              # included -- put the next install straight into the burn view
              # before it had seen any evidence of its own.
              *_verdict_files()):
        _rm(p)
    # The per-session state directory, and everything under it. state.json
    # above is the single-slot file this replaced; it is still on the list so
    # an install that predates the directory leaves nothing behind.
    _rm_state_dir()
    done, message = _remove_bin_dir()
    print(message)
    if refused:
        _say_home_refused(
            "stopped or removed",
            "Its files are gone, so a service that is registered still points"
            " at a program that is not there.")
        return 1
    print()
    if done:
        print("Done. Nothing of Blink's is left running.")
        return 0
    print("Everything else is undone, but that file is still there. Log out and")
    print("back in, then delete it by hand:")
    print(f"  {bin_dir()}")
    return 1


def _theme_files(home: str) -> list:
    """Every file the theme feature may have left in `home`, temp files
    included: the names come from pc/theme.FILES and the temp siblings from
    install_statusline.temp_siblings, which is what theme._write_json writes
    through, so a file added there is removed here without anyone
    remembering to. `.theme-*.tmp` is the mkstemp name the first
    development builds used, before 1.4.0 shipped."""
    out = []
    for name in theme_mod.FILES:
        p = os.path.join(home, name)
        out.append(p)
        out.extend(install_statusline.temp_siblings(p))
    out.extend(glob.glob(os.path.join(glob.escape(home), ".theme-*.tmp")))
    return out


def _verdict_files() -> list:
    """The session-window verdict and every temp file its writer may have
    left. The path is session_verdict's, resolved now, never at import
    (tests/test_sandbox.py)."""
    from pc import session_verdict
    p = session_verdict.verdict_path()
    return [p, p + ".tmp", *install_statusline.temp_siblings(p)]


def _uninstall_codex_hooks() -> None:
    """Take the Codex registration back out, and say what is deliberately kept.

    Prints rather than returning, because it has two things to say, and never
    stops on a bad file: the caller has already removed the login service by
    the time this runs, so stopping here would leave the machine half-undone.
    The one thing we must not do is "repair" a file we cannot parse by writing
    a fresh one over it.

    The trust record in Codex's own config.toml is left alone, and the second
    half of this function exists to say so out loud. Removing it is the
    tidier-looking choice and it is the wrong one: under `codex exec` a
    distrusted hook is skipped silently, with no prompt and no output, so a
    reinstall on a headless machine would produce a hook that is installed,
    correct, registered and permanently invisible. Working software that
    cannot be seen to be broken is the worst failure shape this product has.
    The full argument is in docs/research/codex-hook-contract.md under "RULING:
    uninstall leaves the trust record alone"; silence about it here would be
    the same defect one level up -- a decision the user cannot see.
    """
    # No try around this, unlike the status line and activity hook steps of
    # cmd_install (numbered there, so not named by number here at all --
    # Windows has a sixth step and the numbering moves). install_codex_hooks
    # .uninstall() catches its own SettingsUnreadable at all three points that
    # can raise one -- the read, the shape check, and now the write -- and
    # returns a "left it alone" sentence with the marker kept, so every failure
    # already arrives here as a line of report rather than an exception. A
    # handler for an exception that cannot arrive is a guard nobody can
    # falsify, and this file has been carrying several.
    print("      " + install_codex_hooks.uninstall(
        install_codex_hooks.hooks_file(), hook_shim_path()))

    config = os.path.join(install_codex_hooks.codex_home(), "config.toml")
    # Only when the file is really there. On the many machines with no Codex
    # at all, a paragraph about a config file that does not exist is noise
    # about a record that was never written.
    if os.path.exists(config):
        # normpath for the reader only. codex_home() is expanduser("~/.codex"),
        # which on Windows keeps the forward slash: measured on a Windows 11
        # VM, 2026-09-30, the customer was shown
        # C:\Users\tester/.codex\config.toml. The file operations do not care;
        # a person copying the path into Explorer does.
        print("      Left in place: your Codex trust records in "
              f"{os.path.normpath(config)}.")
        print("      If you approved this hook in Codex, that approval stays")
        print("      on file, so installing Blink again works without asking")
        print("      you a second time. Remove its [hooks.state] entry by hand")
        print("      if you would rather Codex asked you afresh.")


def _live_sessions() -> int:
    """How many sessions the hooks are currently tracking.

    The most useful support answer after "are the hooks installed": whether
    they are actually firing. A count of zero on a machine where Claude Code
    is open says the hooks are configured and not running, which is a
    different problem from not being configured at all.
    """
    from pc.providers import claude_state
    try:
        counts, _, _ = claude_state.ClaudeStateProvider(
            path=os.path.join(blink_home(), "state"), sweep=False
        ).scan(time.time())
    except Exception:
        return 0
    return sum(counts.values())


def _rm_tree(root):
    """Remove a flat directory of per-session files."""
    try:
        for name in os.listdir(root):
            _rm(os.path.join(root, name))
        os.rmdir(root)
    except OSError:
        pass


def _rm_state_dir():
    """Remove ~/.blink/state and ~/.blink/state-codex, and their subdirectories.

    Both directories, because the Codex slots deliberately live apart from the
    Claude ones (pc/providers/codex_state explains why) -- and a slot
    directory left behind by an uninstall is not litter, it is a lie: the
    daemon would go on counting sessions of a tool it no longer hooks.

    Two levels deep and no deeper, by construction: the shim only ever creates
    <session>.state files and <session>/<agent> files, in whichever of the two
    directories its second argument chose. Walking rather than shutil.rmtree
    because this runs against a path under the customer's home and a bounded
    loop cannot be talked into deleting more than it was told.
    """
    for sub in ("state", "state-codex"):
        root = os.path.join(blink_home(), sub)
        try:
            names = os.listdir(root)
        except OSError:
            # Absent, which is the normal case for state-codex on a machine
            # that never ran Codex. `continue`, not `return`: the old single
            # directory could give up here, but giving up on the first of two
            # would leave the second untouched on exactly those machines.
            continue
        for name in names:
            p = os.path.join(root, name)
            if os.path.isdir(p):
                for inner in (os.listdir(p) if os.path.isdir(p) else []):
                    _rm(os.path.join(p, inner))
                try:
                    os.rmdir(p)
                except OSError:
                    pass
            else:
                _rm(p)
        try:
            os.rmdir(root)
        except OSError:
            pass


def hook_shim_status_note():
    """A one-line warning when the installed hook shim cannot do its job.

    `blink status` reports the activity hooks by comparing settings.json's
    command strings against the shim path. That check passed throughout the
    period when the feature was dead: the path existed, the hook ran, and the
    file it ran was simply older than the daemon reading its output. The
    daemon can repair this within five minutes of starting -- whether it
    actually will is a separate question the caller answers with
    `_shim_repair_is_live`, because this function only knows the file is
    wrong, not whether anything is left to fix it.

    Two faults, two sentences, because a reader does two different things
    about them. A shim that is ABSENT is not a shim that is stale: the hooks
    in settings.json are still wired to that path, so Claude Code runs a file
    that is not there, silently, on every event -- the pip never lights at
    all, where a stale shim lights it with the wrong field. Saying "out of
    date" about a missing file sends someone looking for a version mismatch
    in a file they will not find.

    This asks about existence FIRST because shim_is_current answers False for
    a file it cannot open, which is how the two faults came to share a
    sentence: absent and stale are the same value to it, and only one of them
    is what it means.
    """
    if not os.path.exists(hook_shim_path()):
        return "the activity hook shim is missing, so the hooks run nothing"
    if shim_is_current(hook_shim_path(), "blink-hook.sh"):
        return None
    return "the activity hook shim is out of date"


def statusline_shim_status_note():
    """The same check as `hook_shim_status_note`, for the other shim.

    `blink status` has reported "Status line installed at ..." from the
    file's mere presence since before DriftWatchdog existed -- the identical
    blind spot the Activity row had until shim_content_check was added to
    repair this shim too. Silent when the shim was never installed: a
    missing file already prints "not installed" two lines up, and asking
    whether a nonexistent file's contents are current is not a question
    worth another line.
    """
    if not os.path.exists(shim_path()):
        return None
    if shim_is_current(shim_path(), "blink-statusline.sh"):
        return None
    return "the status line shim is out of date"


def _shim_repair_is_live(bridge_running: bool) -> bool:
    """Will DriftWatchdog's next tick actually rewrite a stale shim?

    Three ways "the daemon replaces it within five minutes" can be false even
    though the shim really is stale: BLINK_NO_WATCHDOG turns self-healing off
    entirely (install_statusline.drift_check and shim_content_check both
    check it first); shim_content_check no-ops with no install marker, the
    same "never installed, or deliberately uninstalled" rule drift_check
    documents; and with no bridge running there is no poll loop to tick at
    all -- `cmd_status` already knows that from the Bridge row two lines
    above and passes it in here rather than this function re-deriving it.
    Printing the promise when none of these hold is the same species of
    confidently-wrong copy this whole fix exists to end.
    """
    if os.environ.get(install_statusline.WATCHDOG_DISABLE_ENV):
        return False
    if not install_statusline._read_marker():
        return False
    return bridge_running


def _shim_repair_line(note: str, bridge_running: bool) -> str:
    """Format a stale-shim note, promising repair only when it will happen."""
    if _shim_repair_is_live(bridge_running):
        return f"{note} -- the daemon replaces it within five minutes of starting"
    return f"{note} -- run `{_repair_command()} install` to fix it"


def cmd_status(args) -> int:
    # Whether a daemon is actually ticking, for the stale-shim notes below:
    # neither drift_check nor shim_content_check runs on its own, so a shim
    # note that promises "the daemon replaces it" is a lie with no bridge
    # behind it. Every status() string that means "up" starts with "running"
    # (see backend().status()'s docstring); under BLINK_SKIP_SERVICE the
    # question was never asked, and "cannot confirm" gets the same false as
    # "confirmed not running" -- the honest default when the promise cannot
    # be checked.
    if _skip_service():
        # The launchd label and systemd unit name are global while everything
        # else is scoped to $HOME, so querying them under a test HOME reports
        # the real user's agent -- which read as "installed" for an install
        # that never happened.
        print(f"Bridge      not checked ({_skip_reason()})")
        bridge_running = False
    else:
        bridge_status = backend().status()
        print("Bridge      " + bridge_status)
        bridge_running = bridge_status.startswith("running")

    print(f"App         {RELEASE_VERSION}")
    for line in _board_lines():
        print(line)

    desk = read_desk()
    text, ver = desk.claude_text, desk.claude_ver
    if desk.cli_silent:
        # claude_version() fails the same way for "absent" and "present but
        # not answering"; only the second is on PATH. The `elif ver is None`
        # branch below was unreachable for it.
        also = [n for n, on in (("Claude Desktop", desk.desktop),
                                ("Codex", desk.codex)) if on]
        print("Claude Code on PATH, but `claude --version` did not answer"
              + (f" (the panel also reads {' and '.join(also)})"
                 if also else ""))
    elif text is None:
        # The consequence, not just the fact. "Not found" alone left someone
        # with missing countdowns and no way to connect the two. Built from
        # the same Desk as _note_if_no_claude_code, so the two cannot
        # disagree -- and the light from the same ClaudeHooks as the
        # Activity row below.
        if desk.desktop:
            gaps = []
            if not desk.weekly:
                # Not "yet": a chat-only desk may never learn one.
                gaps.append("no weekly countdown")
            if desk.hooks.light:
                gaps.append("activity light from the Code tab"
                            + (" and Codex" if desk.codex else ""))
            elif desk.codex:
                gaps.append("activity light from Codex only: Claude hooks"
                            " not working")
            else:
                gaps.append("no activity light: Claude hooks not working")
            who = ("Claude Desktop and Codex" if desk.codex
                   else "Claude Desktop alone")
            print(f"Claude Code not found -- running on {who}"
                  f" ({'; '.join(gaps)}){desk.desktop_caveat}")
        elif desk.codex:
            # Codex sessions light the pip from their own session log.
            if desk.unsigned_desktop:
                why = " (sign in to Claude Desktop for the Claude page)"
            elif desk.desktop_waiting:
                why = (" (Claude Desktop hasn't written a reading yet; it"
                       " does within a few minutes)")
            else:
                why = " (no Claude page)"
            print("Claude Code not found -- running on Codex alone" + why)
        elif desk.desktop_waiting:
            print("Claude Code not found -- Claude Desktop is signed in but"
                  " hasn't written a reading yet; it does within a few"
                  " minutes")
        elif desk.unsigned_desktop:
            print("Claude Code not found -- Claude Desktop is installed but"
                  " has recorded no usage yet; open it and sign in")
        else:
            print("Claude Code not found -- and no Claude Desktop cache or"
                  " Codex log either, so nothing is reporting usage")
    elif ver is None:
        # It ran and said something we could not read as a version. Saying
        # "not found" would send someone to reinstall a working install.
        print(f"Claude Code {text} -- could not read a version from that")
    elif ver < MIN_CLAUDE:
        m = ".".join(str(n) for n in MIN_CLAUDE)
        print(f"Claude Code {text} -- TOO OLD, needs {m}+ (panel will stay blank)")
    else:
        print(f"Claude Code {text}")

    print("Status line " + (f"installed at {shim_path()}" if os.path.exists(shim_path())
                            else "not installed"))
    # Same blind spot the Activity row had: the path existing does not mean
    # its contents are current, and this shim gets the identical repair from
    # DriftWatchdog (see claude_usage_bridge.py's shims= list), so it earns
    # the identical disclosure.
    stale_statusline = statusline_shim_status_note()
    if stale_statusline:
        print(f"            {_shim_repair_line(stale_statusline, bridge_running)}")

    # Install writes two things into settings.json, so status has to report
    # two. Reporting only the status line is the same omission the setup
    # disclosure had: someone whose activity pip never lights needs a way to
    # see whether the hooks are actually there, and "Status line installed"
    # answers a different question.
    hooks = desk.hooks
    if not hooks.readable:
        print("Activity    unknown -- settings.json does not parse")
    else:
        ours, total = hooks.wired, hooks.total
        if hooks.disabled and ours:
            print(f"Activity    hooks installed ({ours}/{total} events), but"
                  " settings.json sets disableAllHooks -- Claude Code runs"
                  " none of them")
        elif ours == total:
            live = _live_sessions()
            note = f", {live} live session{'s' if live != 1 else ''}" if live else ""
            print(f"Activity    hooks installed ({ours}/{total} events{note})")
        elif ours:
            print(f"Activity    PARTIAL -- {ours}/{total} hooks present;"
                  f" run `{_repair_command()} install` to restore them")
        else:
            print("Activity    hooks not installed -- the busy/idle pip will"
                  " stay dark")
        # Under the row rather than instead of it: every count above can be
        # right while the file those hooks run is older than the daemon
        # reading its output, which is the whole of this fault.
        #
        # Only when `ours` is nonzero: with zero hooks wired into
        # settings.json at all, whether the shim they would have called is
        # stale is not the actionable fact -- "hooks not installed" above
        # already covers it, and a note about a file nothing points at is
        # noise, not disclosure.
        if ours:
            stale = hook_shim_status_note()
            if stale:
                print(f"            {_shim_repair_line(stale, bridge_running)}")

    # The most useful support answer: is fresh data actually arriving?
    #
    # A FRESH payload wins on any desk: something is rendering the status
    # line right now, whatever `which claude` says -- a Claude Code outside
    # PATH, an IDE's copy. Saying "from Claude Desktop's own files" there
    # hid the one live source, and CI's all-sources scenario caught it the
    # first time it ran (2026-10-19).
    #
    # A STALE one on a desk with no `claude` on PATH is a leftover from an
    # earlier Claude Code, and that desk is fed from Desktop's files: "open
    # Claude Code to refresh it" under "running on Claude Desktop alone"
    # sent people to a program they do not have. With a `claude` that does
    # exist, Desktop does not make the status line moot -- that desk still
    # needs "open Claude Code once" (review, 2026-10-19).
    payload = statusline_source.payload_path()
    age = (int(time.time() - os.path.getmtime(payload))
           if os.path.exists(payload) else None)
    # The same bound the panel uses (statusline_source.STALE_AFTER_S): this
    # said "old" at two minutes for a reading the board was still showing
    # as live.
    if age is not None and age < statusline_source.STALE_AFTER_S:
        print(f"Usage data  fresh ({age}s old)")
    elif desk.desktop and not desk.on_path:
        # "Open Claude Code once so it renders its status line" can never
        # clear on this desk: Desktop's Code tab runs Claude Code headless and
        # never renders one (measured 2026-09-26), and the board is already
        # being fed from Desktop's files.
        print("Usage data  from Claude Desktop's own files (no Claude Code"
              " status line on this machine)" + desk.desktop_caveat)
    elif age is not None:
        print(f"Usage data  {age}s old -- open Claude Code to refresh it")
    elif desk.codex:
        print("Usage data  from the Codex session log (no Claude Code status"
              " line yet)")
    elif desk.desktop_waiting and not desk.on_path:
        print("Usage data  none yet -- Claude Desktop hasn't written a"
              " reading; it does within a few minutes")
    elif desk.unsigned_desktop and not desk.on_path:
        print("Usage data  none yet -- Claude Desktop is installed; open it"
              " and sign in")
    else:
        print("Usage data  none yet -- open Claude Code once so it renders "
              "its status line")
    codex = _source_lines(desk)
    if getattr(args, "wire", False):
        # The Session rows come out of the same poll as the wire line.
        _wire_line()
    else:
        from pc import normalizer
        _session_window_line(*_claude_page(desk))
        _codex_session_line(normalizer.merge(codex) if codex else None)
    return 0


def _session_window_line(page, verdict):
    """Say so when the claude page is being treated as having no five-hour
    window, and what proved it.

    The mark rests on evidence (pc/session_verdict), and evidence can be
    misread: a vendor rename of five_hour, a Desktop history whose `fh` key
    went away for some other reason. This line is how support tells a real
    Ultra account from that, and it is what support asks for first.

    `page` is the merged claude frame, so the row says what the merge
    decided, never what one provider thought: it used to poll a provider of
    its own, and printed "no five-hour window" above a wire line carrying a
    session figure (review, 2026-10-31). `verdict` is the SessionVerdict that
    marked it, the persisted one the daemon writes, so a fresh `blink
    status` says what the running daemon says (finding 5).

    No page at all is a different case: no source has a reading, so there
    is no figure the row could contradict, and the saved verdict alone
    decides. A Claude Code-only Ultra desk in a session before its first
    reply has a status line with no rate_limits; the daemon, which
    remembers its last reading, is marked and sending the burn line, and
    the row went missing here because a fresh process has nothing to
    merge (/code-review max, 2026-11-01).
    """
    from pc import session_verdict
    current = verdict.current() if verdict is not None else None
    if page is None:
        if current is None or current[0]:
            return
    elif not page.no_session_window:
        return
    why = session_verdict.DESCRIBE.get(current[1]) if current else None
    print("Session     no five-hour window"
          + (f" ({why})" if why else ""))


def _codex_session_line(page):
    """The Claude row's wording (_session_window_line), for the Codex page:
    said when the merged codex frame is marked -- its newest reading had a
    weekly limit and no five-hour one (codex_cli._no_short_window) -- which
    is when the daemon sends that page the burn view.

    `page` is a merged frame, as the Claude row's is: under `--wire` the
    bus's own page("codex"), so the row is never printed for a Codex
    reading that select_pair left off the board; for a plain status the
    merge of the Codex provider's frames, the rows above it."""
    if page is not None and page.no_session_window:
        print("Session     no five-hour window on Codex (Codex reported a"
              " weekly limit only)")


def _claude_page(desk):
    """(merged claude frame, its SessionVerdict) for plain `blink status`;
    the frame is None when no source has a reading.

    The merge the daemon runs, over the sources that can say anything about
    a session window: the status line and Claude Desktop's usage cache
    (desk's one reading of it, the same one the Desktop row prints from),
    weighed by the verdict the daemon persists. Read-only: a status command
    reports the verdict and never writes it. Claude Desktop's Local Storage,
    whose LevelDB this used to parse on every run, is read only when the
    account is marked, because the only thing it can prove is a window
    (pc/providers/claude_desktop_ls), which can only lift a mark. Not a whole
    IngestionBus poll, which `--wire` does: that also writes the weekly
    anchor and, on a desk that has none, seeds it by walking Claude
    Desktop's IndexedDB, which holds the customer's conversations -- not
    something a plain status should do to print one row. The sources left
    out carry no session figure (activity state, Codex, the weekly
    projection), so they cannot change the answer.
    """
    from pc import normalizer, session_verdict
    from pc.providers import claude_desktop
    from pc.providers.claude_cli import ClaudeCliProvider
    from pc.providers.claude_desktop_ls import (
        ClaudeDesktopLocalStorageProvider)
    now = time.time()
    verdict = session_verdict.SessionVerdict(persist=False)

    def polled(provider):
        try:
            return list(provider.poll(now) or [])
        except Exception:
            # As the bus contains a provider: a source that cannot be read
            # costs its own say, not the status report.
            return []

    frames = polled(ClaudeCliProvider())
    reading = desk.desktop_reading if desk is not None else None
    if reading is not None and reading[0] == claude_desktop.VERDICT_FRAME:
        frames.append(reading[1])
    if verdict.observe(frames, now):
        frames.extend(polled(ClaudeDesktopLocalStorageProvider()))
    verdict.mark(frames, now)
    return (normalizer.merge(frames) if frames else None), verdict


def _wire_line():
    """The Session row and the usage message the bridge would put on the
    cable right now, from one poll.

    The last thing that can be checked without a board: every source read,
    merged and encoded exactly as the daemon does it (pc/ingest -> normalizer
    -> protocol). CI feeds the installed binary the files a customer's machine
    would have and asserts on this line; a person can paste it into a report.
    """
    import json as _json
    from pc import ingest, session_verdict
    # The persisted verdict, read and never written: the daemon writes it.
    bus = ingest.IngestionBus(
        verdict=session_verdict.SessionVerdict(persist=False))
    msg = bus.poll()
    _session_window_line(bus.claude_page(), bus.verdict())
    from pc.providers import codex_cli
    _codex_session_line(bus.page(codex_cli.PROVIDER_ID))
    print()
    if msg is None:
        print("Wire        nothing to send -- no source has a reading")
        return
    print("Wire        the next usage message, exactly as the board gets it:")
    print(_json.dumps(msg, separators=(",", ":")))


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def board_lines(known, ports, undriven=(), blink_cmd="blink",
                can_update=True):
    """The Board lines of `blink status`, from what the app remembers and
    what is plugged in right now.

    `known` is board.json ({port, board_id, fw}); `ports` is [(device,
    chip)] for every port that could be a board. A developer with other
    CH340 hardware on the desk needs to see that the app knows the
    difference: the board it talks to, named; everything else listed as
    looked at once and left alone.

    `undriven` is pc.win_driver.undriven_boards() -- boards Windows can see
    but cannot use, which appear on no serial port and so are invisible in
    `ports`. They are reported in every branch rather than only in the empty
    one: a desk can hold a working board AND a second one Windows has not
    driven, and the working board's line would otherwise hide it.
    """
    port = known.get("port")
    here = [d for d, _ in ports]
    chip = dict(ports)
    out = []
    if not ports and undriven:
        # The case that made this function wrong. No serial port exists, so
        # every branch below reads the desk as empty and says "not plugged
        # in" -- to a customer looking straight at the board.
        out.append(f"Board       {win_driver.summary(undriven)}")
        out += [f"            {line}"
                for line in win_driver.advice(undriven, blink_cmd)]
        if port:
            out.append(f"            last seen on {port}")
        return out
    if port and port in here:
        ident = []
        if known.get("board_id"):
            ident.append(f"id {known['board_id']}")
        if known.get("fw"):
            ident.append(f"firmware {known['fw']}")
        tail = f" -- {', '.join(ident)}" if ident else ""
        out.append(f"Board       {port} ({chip[port]}){tail}")
    elif here:
        seen = ", ".join(f"{d} ({c})" for d, c in ports)
        out.append(f"Board       none identified yet -- looking at {seen}")
        if port:
            out.append(f"            last seen on {port}")
    else:
        out.append("Board       not plugged in"
                   + (f" (last seen on {port})" if port else ""))
    others = [(d, c) for d, c in ports if d != port]
    if port and port in here and others:
        names = ", ".join(f"{d} ({c})" for d, c in others)
        out.append(f"            other serial devices: {names}"
                   " -- asked once, not BLINK, left alone")
    # Reached only when at least one port DID show up: a working board on one
    # socket and an undriven one on the next. The line above lists the serial
    # devices that were left alone deliberately; this one is a device that
    # never became a serial device at all, and saying nothing would read as
    # the app ignoring a board sitting right there.
    if undriven:
        out.append(f"            also: {win_driver.summary(undriven)}")
        out += [f"            {line}"
                for line in win_driver.advice(undriven, blink_cmd)]
    # The board running a LATER release than this app. The two ship from one
    # tag and install together, so this means something split them -- a
    # hand-flash, or an app update that failed after the firmware went on.
    # Whichever, the half that is behind is the one the panel cannot reach:
    # the board's update row says the app is old and no tap can fix it.
    # Naming the command is the whole point of the line.
    if ota.is_newer(known.get("fw") or "", RELEASE_VERSION):
        out.append(f"            the board is on {known['fw']} and this app is"
                   f" {RELEASE_VERSION} -- they ship together")
        if not can_update:
            # A checkout: `update` refuses there (see cmd_update), so naming
            # it would send the reader into a dead end.
            out.append("            this is a checkout: bring it up to date"
                       " with git")
            return out
        out.append(f"            run: {blink_cmd} update")
        # ...which can find nothing, and used not to admit it.
        #
        # This compares the board against THIS APP, so it fires whenever the
        # board is ahead -- including when the app is already the newest thing
        # published, which is every developer's desk from the first local build
        # onwards and any unit hand-flashed from an unreleased tree. There the
        # command above is a no-op and the reader is left rerunning it.
        #
        # Answering it properly means asking the feed, and `blink status` does
        # not touch the network on purpose: it has to work on a plane, and it
        # is the first thing anyone runs when nothing works. So it names the
        # other possibility instead of guessing between them. The panel has
        # the same question and no room for this sentence --
        # docs/open-bugs/app-is-old-is-a-guess.md.
        out.append("            (if that finds nothing, this board is ahead"
                   " of the published release)")
    return out


def _board_lines():
    try:
        from claude_usage_bridge import describe_ports, remembered_board
        return board_lines(remembered_board(blink_home()), describe_ports(),
                           win_driver.undriven_boards(), command_to_type(),
                           can_update=_update_reaches_the_service())
    except Exception as e:
        return [f"Board       could not list serial ports: {e}"]


def _codex_hook_status():
    """The Codex hook lines of `blink status`.

    Three states worth telling apart, because the repairs are different:
    registered and firing, registered and silent, not registered at all.

    The middle one is the reason this function exists. Under `codex exec` a
    hook the user has not trusted is skipped silently -- no prompt, no
    warning, no output whatsoever (docs/research/codex-hook-contract.md, F5) --
    so from out here, a hook that is registered and has never written a slot
    is exactly what declining Codex's trust prompt looks like. It is the one
    support question this feature has that no other part of the product does,
    and without this line the symptom is a panel that simply never mentions a
    Codex session waiting on its human, with nothing anywhere saying why.

    Read off our own marker rather than off Codex's hooks.json, unlike the
    Activity row's count: that file belongs to another vendor, and parsing it
    a second time here would be a second copy of install_codex_hooks' reader,
    free to drift from the first.
    """
    if not install_codex_hooks._read_marker():
        return [f"Codex hook  not installed -- run `{_repair_command()} install`"
                " on a machine with Codex"]
    try:
        # sweep=False, for the reason codex_state.scan gives at length: a
        # diagnostic that deletes what it is diagnosing destroys the evidence
        # somebody ran it to see. Here that is not an abstraction -- the
        # never-written case above IS the diagnosis, and a sweep is what would
        # manufacture it.
        states, _agents = codex_state.scan(time.time(), sweep=False)
    except Exception as e:
        # Not folded into the empty case below. "It has never written
        # anything" would send someone to Codex's trust prompt over a
        # directory we merely failed to read, which is the wrong repair for a
        # different fault.
        return [f"Codex hook  registered, but its slots could not be read ({e})"]
    if not states:
        return [
            "Codex hook  registered, but it has never written anything",
            "            Codex asks once whether to trust a hook. If that",
            "            prompt was declined the hook stays in the file and",
            "            never runs -- open Codex and accept it.",
        ]
    n = len(states)
    return [f"Codex hook  firing, {n} live session{'s' if n != 1 else ''}"]


def _source_lines(desk=None):
    """One line each for the two sources CI cannot exercise.

    Claude Desktop is a closed GUI app and Codex will not write a rollout
    without an account, so neither format change can be caught on a runner.
    It can only be caught here, on the machine where the file is -- and the
    daemon already knows (it logs once and goes quiet). This is where a
    person finds out, and what they paste into a bug report.

    The Desktop row prints from `desk`'s one reading of the cache, the same
    one every other Desktop line of status came from; without a desk (a
    caller that wants these rows alone) it takes its own.
    """
    from pc import desktop_paths
    now = time.time()

    # One folder answer for the whole Desktop row: desk's, when the reading
    # came from there, so the row names the folder the reading came from.
    # Asked separately, the source and the path were two resolves, and a
    # cache rewritten between them could print "moved by the Store build"
    # beside the Roaming path (review, 2026-10-30).
    held = desk.desktop_paths if desk is not None else None
    with desktop_paths.one_answer(held) as folder:
        _source_lines_desktop(desk, folder, now)
    return _source_lines_codex(now)


def _source_lines_desktop(desk, folder, now):
    """_source_lines' Desktop row, under the folder answer `folder`."""
    from pc.providers import claude_desktop
    desktop = claude_desktop.ClaudeDesktopProvider()
    reading = desk.desktop_reading if desk is not None else desktop_reading()
    if reading is not None:
        # verdict(), not poll(): one read decides the row, and nothing lands
        # on stderr. poll() and a second look at the file could see two
        # different files -- the app rewrites it every few minutes -- and
        # poll()'s daemon-log complaint printed "did not parse" right above a
        # row saying the opposite (review, 2026-10-25).
        verdict, frame = reading
        if verdict == claude_desktop.VERDICT_FRAME:
            age = now - frame.observed_at
            print(f"Desktop     usage cache parsed, reading"
                  f" {_age(age)} old")
            for line in desktop_staleness_note(age):
                print(f"            {line}")
        elif verdict == claude_desktop.VERDICT_UNFILLED:
            # The app's own empty shape, before its first sample, is not a
            # changed format. It used to share the "please report it" line
            # below, and people reported a fault that was not there
            # (destaben noticed, 2026-10-19). Only the exact empty shapes
            # land here; see _is_unfilled_doc. The last line keeps a report
            # open for an app that never fills it.
            print("Desktop     usage cache present but holds no usage"
                  " figures yet")
            print("            Claude Desktop fills it in after its first"
                  " reading; if it stays empty while")
            print("            the app shows usage, please report it")
        elif verdict == claude_desktop.VERDICT_UNREADABLE:
            # Permissions, a Windows sharing lock, a file deleted between the
            # exists() above and the read. None of those is a format change,
            # and calling them one sent a report about the wrong thing.
            print("Desktop     usage cache present but could not be read")
            print(f"            looked at {desktop.path()}")
        else:
            # VERDICT_NOT_OURS and VERDICT_GARBLED: JSON in a layout we do not
            # know, or not JSON at all. Either may be the app changing its
            # file, which is exactly what this row exists to catch.
            print("Desktop     usage cache present but did not parse --"
                  " the app's file format may have changed; please report it")
    else:
        # Name the path. Whether Claude Desktop keeps this file where we
        # think it does has been seen on macOS and is a convention on the
        # other two; the first person to run this beside a signed-in Desktop
        # on Windows or Linux is the check, and this line is what they need.
        if desk is not None and desk.desktop_installed:
            # Its Local Storage says the app has run here, so "not
            # installed" is already ruled out.
            print("Desktop     no usage cache (Claude Desktop installed but"
                  " never signed in)")
        else:
            print("Desktop     no usage cache (Claude Desktop not installed,"
                  " or never signed in)")
        print(f"            looked at {desktop.path()}")
        if folder.dangling:
            # The app left a note saying where its data went, and that
            # folder is gone. Reading Roaming instead is the right fallback,
            # and this line is what a Store-build report needs to say why.
            print("            Store build marker found but its folder"
                  " is gone")
    if folder.moved:
        # Windows only. On the Store build measured 2026-10-29 Roaming held
        # nothing but the marker, so a report that does not name the folder
        # actually read leaves the reader guessing which of two layouts
        # this desk has. See pc/desktop_paths.
        print(f"            data moved by the Store build: {folder.path}")
    elif folder.ignored:
        # A marker and its folder are there, but Roaming was written more
        # recently -- the exe build back in use. Without this line the
        # report reads like a plain install, and "why is it reading Roaming
        # when I have Claude-Data?" has no answer in it.
        print("            Store build marker ignored, Roaming is newer")


def _source_lines_codex(now):
    """_source_lines' Codex rows; returns the Codex frames they were printed
    from, for the Session row (_codex_session_line), or [] for none."""
    from pc.providers import codex_cli
    logs = codex_cli.recent_rollouts()
    if logs:
        # sweep=False, matching _live_sessions above and for the same reason:
        # the provider's poll() scans the Codex hook slots, and the scan
        # DELETES abandoned ones. `blink status` must not mutate what it
        # reports -- asking "is the Codex hook working?" would otherwise
        # delete the evidence that it is, and leave the next run reading
        # differently because of this one.
        frames = codex_cli.CodexCliProvider(sweep=False).poll(now)
        if frames:
            print(f"Codex       session log parsed, reading"
                  f" {_age(now - frames[0].observed_at)} old")
        else:
            print(f"Codex       {len(logs)} recent session log(s), none with a"
                  " rate-limit line -- finish one turn in Codex; if that does"
                  " not help, its log format may have changed; please report it")
    else:
        frames = []
        print("Codex       no session logs (Codex not installed, or never run)")
        print(f"            looked under {codex_cli.sessions_root()}")

    # Under the Codex rows, and only where there is something to answer. These
    # lines answer "why is my Codex session not showing as waiting"; on a
    # machine with neither a Codex log nor a registration of ours, that is a
    # question nobody asked, and telling them to install a hook for a tool
    # they do not have is worse than saying nothing.
    if logs or install_codex_hooks._read_marker():
        for line in _codex_hook_status():
            print(line)
    return list(frames or [])


def _update_reaches_the_service() -> bool:
    """Whether `blink update` -- a frozen build unpacked into ~/.blink/bin,
    then a restart -- changes the program that runs.

    By what the SERVICE runs, not by which program is asking. Fleet desks
    run the checkout's CLI beside a frozen service, and an earlier version
    of this refused there, taking `update` away from the desks that use it
    (review, 2026-10-25). So: yes when the service runs the installed
    binary; no when it runs a checkout, whose service would go on running
    the checkout after a download nobody can see; and with no service, yes
    for the frozen binary (as before this check existed) and no for a
    checkout.
    """
    prog = service_program()
    if prog is not None:
        return prog == [installed_bin()]
    return bool(_frozen())


def cmd_update(_args) -> int:
    """Fetch and install a newer daemon, then restart the service.

    Manual by design for now. The machinery for doing it unattended is all
    here, but it is gated on `daemon.auto` in the signed manifest so that the
    first release to switch it on is a decision someone makes, not a side
    effect of shipping this command.
    """
    if not _update_reaches_the_service():
        print("Running from a checkout: update with git.")
        return 1
    print(f"Blink update. This app is {RELEASE_VERSION}.")
    print()
    manifest = update.fetch_signed_manifest()
    if manifest is None:
        print("Could not read the release feed, or it is not properly signed.")
        print("Nothing was changed.")
        return 1
    found = update.available(manifest)
    if not found:
        key = update.platform_key()
        if key is None:
            print(f"There is no published build for {sys.platform}"
                  f" {os.uname().machine if hasattr(os, 'uname') else ''}.")
            return 1
        print("Already up to date.")
        return 0
    version, artifact = found
    print(f"Downloading {version} ...")
    try:
        blob = update.download(update.platform_key(), artifact)
    except Exception as e:
        print(f"Download failed: {e}")
        print("Nothing was changed.")
        return 1
    # Let go of the program before replacing it. Windows will not rename <bin>
    # while the daemon holds bin\blink.exe open, and the daemon is restarted a
    # few lines below regardless -- so this only moves that bounce earlier.
    halt_service()
    ok, message = update.apply(blob, installed_bin(), version)
    print(message)
    if not ok:
        return 1
    # Before the service comes back, so the daemon that starts is reading the
    # shims this release ships rather than last release's.
    _refresh_shims()
    detail = restart_service()
    print("Background service ... " + detail)
    if _home_refusal():
        _say_home_refused(
            "restarted",
            "The new program is in place, and the old one may still be"
            " running.")
        return 1
    if restart_left_it_down(detail):
        # The program on disk is the new one; what is missing is a process.
        # Say which half worked, because "update failed" would send someone
        # to download it again -- and downloading it again would not help.
        print()
        print("The new version is installed, but the background service is")
        print("not running, so the board will stay on \"Link the PC daemon\".")
        print(f"Run `{_repair_command()} install` to finish.")
        return 1
    return 0


def cmd_provision(args) -> int:
    """Stamp this unit's edition. A factory step, run ONCE after programming.

    Deliberately its own command rather than a flag on `install`: it is not
    part of setting a customer's machine up, it is part of building a board,
    and the two happen in different places by different people.

    Once is enforced by the BOARD, not by this. The user installs this same
    binary, so a check here would be a check on the honour system -- the
    firmware latches the record on the first successful stamp and refuses
    every later one. All this can do is report that clearly.

    The daemon owns the serial port whenever it is running, so this stops the
    login service, talks to the board, and starts it again. That is heavier
    than it looks like it needs to be and it is the honest sequence: two
    processes cannot hold one tty, and a provisioning step that silently did
    nothing because the port was busy is exactly the failure this product
    cannot afford at the point a board is being boxed.
    """
    import serial                      # only this path needs it installed

    from claude_usage_bridge import autodetect_port

    port = args.port or autodetect_port()
    if not port:
        print("No board found. Plug one in, or pass --port.", file=sys.stderr)
        return 1

    from pc import service_ctl

    stopped = False
    if not _skip_service():
        # Stop by pid first: the login service may not be registered on a
        # bench machine, and the pid file is the thing that is true either way.
        _kill_recorded_daemon()
        # Then stop the service itself, and later start it, with the pair
        # the fleet suite uses: neither writes, moves or deletes the plist,
        # the unit or the task. This used to remove() the service and
        # install() it again, which put back the program RUNNING provision
        # rather than the one the service ran -- and on Windows, from a
        # checkout, install() refuses, so the frozen task was deleted and
        # never came back (review, 2026-10-25). Before that it decided
        # whether to reinstall by whether ~/.blink/bin/blink existed, and
        # left a never-installed bench laptop with a login item for a
        # missing binary. stop() answering "stopped" is the fact both
        # needed: there was a service, and start() puts that same one back.
        stopped = service_ctl.stop_service().detail.startswith("stopped")
    # One try, opened the moment the service is down, so every way out --
    # a dead port, no hello, a refusal, Ctrl-C during the settle sleep --
    # starts it again exactly once. The restart used to sit at each return
    # and in the port's own finally, which the sleeps ran ahead of: an
    # interrupt there left the desk on HOST LOST (review, 2026-10-25).
    try:
        if not _skip_service():
            # stop() reaches the task's instance and the recorded pid. A
            # Windows daemon with no pid file -- from before the file
            # existed, or one that died before writing it -- kept the COM
            # port, and the open below failed with "Access is denied"
            # (review, 2026-10-25). Matched on installed_bin()'s full path,
            # as uninstall does, and a no-op off Windows. Here and not in
            # stop(), which the fleet suite and halt() share.
            _kill_by_path()
            time.sleep(1.0)
        return _stamp_edition(serial, port, args)
    finally:
        if stopped:
            print(f"Background service ... {service_ctl.start_service()}")


def _stamp_edition(serial, port, args) -> int:
    """cmd_provision's half that holds the port: open it, wait for hello,
    send the edition, read back the board's answer."""
    try:
        ser = serial.Serial()
        ser.port = port
        ser.baudrate = args.baud
        ser.timeout = 0.2
        # Same open dance as the daemon: leave DTR/RTS alone, then pulse RTS,
        # so the board comes up in run mode rather than the ROM loader. See
        # the long note in claude_usage_bridge.main.
        ser.dtr = False
        ser.rts = False
        ser.open()
        ser.dtr = False
        ser.rts = True
        time.sleep(0.15)
        ser.rts = False
        time.sleep(0.4)
        ser.reset_input_buffer()
    except Exception as e:
        print(f"Could not open {port}: {e}", file=sys.stderr)
        return 1

    try:
        # Wait for the board to say hello before sending anything.
        #
        # Opening the port resets it (the RTS pulse above is deliberate --
        # see the daemon), so for the first second or two the thing on the
        # other end is a bootloader, not the firmware. The first attempt at
        # this wrote immediately after a 0.4 s sleep and the message went
        # into the void: no error, no confirmation, indistinguishable from a
        # board too old to understand it. `hello` is the board saying it is
        # listening, and it is the only honest thing to wait for.
        heard = []
        ready = False
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            chunk = ser.readline()
            if not chunk:
                continue
            text = chunk.decode("utf-8", "replace").strip()
            if text:
                heard.append(text)
            if '"t":"hello"' in text.replace(" ", ""):
                ready = True
                break
        if not ready:
            # The finally below closes the port, and cmd_provision's starts
            # the service. Both used to be done here as well, so this path
            # started the service twice.
            print(f"No hello from the board on {port} within 10 s.",
                  file=sys.stderr)
            return 1

        # encode_checked, as every production writer is (CLAUDE.md): a line
        # over the board's limit is dropped whole and silently.
        raw, why = protocol.encode_checked(protocol.edition(args.edition))
        if raw is None:
            print(f"Not sent: {why}", file=sys.stderr)
            return 1
        ser.write(raw)
        ser.flush()
        # Read back what the board says about it. The firmware prints one
        # line either way, and that line is the whole confirmation: a board
        # too old to know the message answers nothing, which is a different
        # outcome from a board that stored it.
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            chunk = ser.readline()
            if not chunk:
                continue
            text = chunk.decode("utf-8", "replace").strip()
            if text:
                heard.append(text)
            if "[cfg] edition" in text:
                break
    finally:
        ser.close()

    said = [h for h in heard if "[cfg] edition" in h]
    if said:
        line = said[-1].split("[cfg] ", 1)[-1]
        print(line)
        # A refusal is not a success. The edition latches on the first stamp
        # and the board declines every one after it (cfg_set_edition), so a
        # unit that comes back down the line for a second pass has to fail
        # here loudly rather than be boxed as whatever the label said.
        if "refusing" in line:
            print("This unit was stamped already. Clearing it means erasing"
                  " the config partition\nover USB with the board in"
                  " bootloader mode -- a factory operation, on purpose.",
                  file=sys.stderr)
            rc = 1
        else:
            rc = 0
    else:
        print(f"Sent edition={args.edition}, but the board did not confirm it."
              "\nA board running firmware older than this feature ignores the"
              " message.", file=sys.stderr)
        rc = 1

    return rc


# `blink config <setting> [on|off]`. One table, so the listing, the parser's
# choices and the setter cannot disagree about which settings exist.
#
# A `config` verb rather than a top-level `blink status-page on`: `blink
# status` already means "is the panel getting data?", and a sibling called
# status-page beside it reads as more of that diagnosis rather than as a
# switch. It also gives the settings that come after this one a home instead
# of a new verb each.
class _Setting(NamedTuple):
    key: str            # in <blink_home>/config.json
    enabled: object     # callable(blink_home) -> True / False / None
    what: str           # one line, for the listing
    said_on: tuple      # printed after turning it on
    said_off: tuple     # printed after turning it off


# Said out loud, because it is the one setting that adds to who this computer
# talks to -- otherwise only github.com, once a day, for updates. No restart
# is needed: the daemon re-reads the file every few seconds
# (status_page.TICK_S). The firmware line is there because a board that does
# not list "svc" in its caps is not sent the message this turns on
# (protocol.BOARD_CAP_SVC), and "I switched it on and nothing happened"
# should not need a support email. It names 1.3.4 rather than the release
# that brings the feature: 1.3.4 is the last firmware without it, a fact that
# cannot go stale the way a guess at the next version number can.
_CONFIG = {
    "status-page": _Setting(
        status_page.SETTING, status_page.enabled,
        "show incidents from status.claude.com and status.openai.com"
        " on the panel",
        (f"The bridge will read each provider's public status page every"
         f" {status_page.POLL_S // 60} minutes,",
         "for the pages your board shows. No account, token or usage is"
         " sent.",
         "Board firmware 1.3.4 and earlier does not show it; update the"
         " board to see it."),
        ("The bridge makes no status-page requests.",)),
}


def cmd_config(args) -> int:
    name = getattr(args, "setting", None)
    value = getattr(args, "value", None)
    home = blink_home()

    def state(setting):
        on = setting.enabled(home)
        return "unreadable" if on is None else ("on" if on else "off")

    if name is None:
        for n, setting in _CONFIG.items():
            # 12 wide: "unreadable" is ten, and ran into the description.
            print(f"{n:<14}{state(setting):<12}{setting.what}")
        return 0
    setting = _CONFIG[name]
    if value is not None:
        try:
            user_config.put(home, setting.key, value == "on")
        except user_config.Unreadable as e:
            # Refused rather than replaced: a file that does not parse is
            # usually one somebody is in the middle of editing. _load's own
            # words, which already name the file and say which of "not valid
            # JSON" or "valid JSON but not an object" it is.
            print(f"{e}; it was left as it is.")
            print("Fix it or delete it, then run this again.")
            return 1
        except OSError as e:
            print(f"Could not save the setting: {e}")
            return 1
    print(f"{name}: {state(setting)}")
    if value is not None:
        for line in (setting.said_on if value == "on" else setting.said_off):
            print(line)
    return 0


def _theme_board_note(home):
    """A line when the board this computer last saw cannot take a theme.

    From the caps the board listed, not its version number (see
    theme.board_takes_themes). Silent when they are not known."""
    takes, fw = theme_mod.board_takes_themes(home)
    if takes is False:
        on = f"firmware {fw}" if fw else "firmware"
        return (f"Your board runs {on}, which does not take themes."
                " It will be sent once the board is updated.")
    return None


def _theme_kept_note(home):
    """A line when the board's own choice was kept over a theme set here.

    The contract lets the board win when both sides changed while the app
    was off (Addendum 4), and the only trace of it was a line in
    bridge.log, so a `blink theme set` that did not stick looked like a
    fault (review of fp/fix-pc, N6). Shown while theme.json is still the
    board's choice; setting a theme again makes it moot."""
    kept = theme_mod.board_kept(home)
    if kept is None:
        return None
    lost = kept[1]
    line = ("Picked on the board while the app was off, so it was kept over"
            " the theme set here then")
    if lost["id"] == theme_mod.CUSTOM:
        return line + " (a custom one)."
    name = theme_mod.display_name(lost["id"])
    shown = theme_mod.board_name(lost["id"])
    return line + f" ({shown}). blink theme set {name} sends that one."


def _theme_lines(t):
    """How `blink theme show` and `set` describe a theme."""
    if t is None:
        return ["Theme: the board's own choice. Nothing is set from this"
                " computer."]
    # Named as its tile on the board names it; the command keeps the typed
    # spelling, which is what works in every shell without quotes.
    name = theme_mod.display_name(t["id"])
    shown = theme_mod.board_name(t["id"])
    if t["id"] != theme_mod.CUSTOM:
        return [f"Theme: {shown}",
                f"On another desk: blink theme set {name}"]
    return [f"Theme: {shown}",
            "As a command:   blink theme set " + " ".join(
                theme_mod.as_args(t)),
            f"Share code:     {theme_mod.to_share_code(t)}"]


@contextlib.contextmanager
def _signals_cancel():
    """While `blink theme` runs, a closed terminal or a kill ends it the way
    Ctrl-C does, so the cancel is written.

    SIGHUP (the terminal window closed) and SIGTERM killed the process on
    the spot, and the preview stayed on the board until its own 15 minute
    backstop; with no bridge running it stayed in theme_preview.json for the
    next start to send (review of 1.4.0, builder N10). The daemon makes the
    same turn for SIGTERM (exit_on_sigterm). SIGBREAK is Ctrl-Break on
    Windows, which the C runtime also raises when a console window is
    closed; whether that leaves time to write the cancel was not measured.
    The handlers are put back afterwards, and nothing is installed off the
    main thread, where signal.signal refuses."""
    def stop(signum, frame):
        raise KeyboardInterrupt
    old = {}
    for name in ("SIGTERM", "SIGHUP", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            old[sig] = signal.signal(sig, stop)
        except (ValueError, OSError):
            pass
    try:
        yield
    finally:
        for sig, handler in old.items():
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def _theme_builder(home) -> int:
    """`blink theme`: the builder, in the foreground, until it finishes."""
    # One builder to a home. Two write the same preview file, and the end of
    # one (a cancel, a closed tab, ten minutes idle) took the other's live
    # preview off the board while its tab still said "Preview sent" (review
    # of 1.4.0, builder N1). The app's own builder, opened from the board,
    # counts, and so does another `blink theme`. The refusal once said Edit
    # on computer would reopen the app's tab; with the held button the board
    # refuses that tap for exactly as long as this refusal can be printed
    # (final review of 1.4.0, SHOULD 1), so it now says what the board shows.
    if theme_server.running_builder(home) is not None:
        print("A theme builder is already open on this computer, from the"
              " app or from another blink theme. Use its tab, or close it"
              " first; it stops by itself after ten minutes with no change."
              " If it is the app's, the board shows Editing on your"
              " computer while it is open, and Edit on computer comes back"
              " once its tab is closed.")
        return 1
    try:
        server = theme_server.BuilderServer(home)
    except FileNotFoundError:
        print("The theme builder page is missing from this install;"
              " reinstall BLINK to get it back.")
        return 1
    except OSError as e:
        print(f"The theme builder could not start: {e}")
        return 1
    server.start()
    url = server.new_link()
    # The link is printed only here, to the person's own terminal: it is the
    # one-time key to the server, and the daemon never prints it.
    print("Opening the theme builder in your browser.")
    print(f"If it does not open, go to this link on this computer: {url}")
    print("Save or cancel there. Closing the tab cancels, and so does Ctrl-C"
          " here.")
    # Said now and not only after Save: the page's previews all answer 200
    # whatever the board takes, so with a 1.3.4 board nothing here told the
    # owner that none of it would show (review of 1.4.0, builder S3).
    note = _theme_board_note(home)
    if note:
        print(note)
    try:
        with _signals_cancel():
            if not theme_server.open_url(url):
                print("No browser opened here, so use the link above.")
            # In slices: see wait_interruptibly, or Ctrl-C does nothing on
            # Windows.
            outcome = server.wait_interruptibly()
    except KeyboardInterrupt:
        server.finish("cancelled")
        outcome = server.wait(5)
    if outcome == "saved":
        print("Saved. The bridge sends it to the board within a few seconds.")
        for line in _theme_lines(theme_mod.load_saved(home)):
            print(line)
        note = _theme_board_note(home)
        if note:
            print(note)
    elif outcome == "idle":
        print("Ten minutes with no change, so the builder stopped. The board"
              " is back on its saved theme.")
    else:
        print("Cancelled. The board is back on its saved theme.")
    return 0


def cmd_theme(args) -> int:
    home = blink_home()
    action = getattr(args, "action", None)
    values = list(getattr(args, "values", None) or [])
    if action is None:
        return _theme_builder(home)
    if action != "set" and values:
        # `reset ocean` set Dark and dropped the word, and `show ocean` and
        # `list light` printed as if it had not been typed (review of 1.4.0,
        # builder N6). Only `set` takes values.
        print(f"blink theme {action} takes nothing after it."
              " To change the theme: blink theme set ocean, for example.")
        return 2
    if action == "list":
        # Three columns: the number and the typed name, which `set` both
        # takes, and the name the board's tile shows, so the list can be
        # matched against Settings > Theme. The number used to be printed
        # and then refused by `set` (audit, 2026-10-28).
        cur = theme_mod.load_saved(home)
        print("Set one with blink theme set <name> or <number>.")
        for i in range(len(theme_mod.NAMES)):
            mark = "*" if cur is not None and cur["id"] == i else " "
            extra = "  (set with key=value, or the builder)" \
                if i == theme_mod.CUSTOM else ""
            print(f" {mark} {i:>2}  {theme_mod.display_name(i):<12}"
                  f"{theme_mod.board_name(i)}{extra}")
        return 0
    if action == "show":
        # A theme.json that is there and cannot be read is not "nothing set":
        # set and reset refuse to write over it, and show said the board
        # chose for itself, so the refusal that followed had no explanation
        # (review of 1.4.0, builder N7).
        try:
            theme_mod.read_saved_file(home)
        except theme_mod.Unreadable as e:
            print("Theme: the saved theme file could not be read.")
            print(f"{e}.")
            return 1
        for line in _theme_lines(theme_mod.load_saved(home)):
            print(line)
        note = _theme_kept_note(home)
        if note:
            print(note)
        return 0
    if action == "reset":
        t = {"id": 0}
    else:   # set
        try:
            t = theme_mod.from_args(values, theme_mod.saved_custom(home))
        except theme_mod.ThemeError as e:
            print(f"Not set: {e}.")
            print("For example: blink theme set ocean, blink theme set"
                  " bg=000000 text=ffffff, or blink theme set BT1.<code>")
            return 2
    try:
        keep = theme_mod.saved_custom(home) \
            if t["id"] != theme_mod.CUSTOM else None
        theme_mod.save(home, t, keep_custom=keep)
        # A cancel in place of any preview a builder left: that preview
        # would otherwise reach the board after this and show something
        # other than what was set, and one already on the board stays there
        # 15 minutes when this sets the theme already stored, since no save
        # goes out then to end it (review NICE-4). ThemeSync.outgoing drops
        # the cancel when a save is owed.
        theme_mod.write_cancel(home)
    except OSError as e:
        print(f"Could not save the theme: {e}.")
        return 1
    for line in _theme_lines(theme_mod.load_saved(home)):
        print(line)
    print("The bridge sends it to the board within a few seconds.")
    note = _theme_board_note(home)
    if note:
        print(note)
    return 0


def cmd_run(args) -> int:
    """The daemon. This is what the login service starts.

    Its arguments are handed over explicitly. Calling main() with none left it
    parsing sys.argv itself, which in the packaged binary begins with the
    subcommand name "run" -- rejected on the spot, restarted by the service ten
    seconds later, forever.
    """
    import claude_usage_bridge
    log = getattr(args, "log", None)
    if log:
        # The Windows service runs with no window: this is where its output
        # goes instead. launchd and systemd redirect for us; wscript does not.
        f = open(log, "a", buffering=1, encoding="utf-8", errors="replace")
        sys.stdout = sys.stderr = f
    forwarded = []
    if getattr(args, "port", None):
        forwarded += ["--port", args.port]
    if getattr(args, "baud", None):
        forwarded += ["--baud", str(args.baud)]
    claude_usage_bridge.main(forwarded)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="blink", description="Blink desk gauge: setup and bridge.")
    # Also the self-test in pc/update.py: a replacement binary has to run and
    # say what it is before it is allowed to become the login service's target.
    parser.add_argument("--version", action="version",
                        version=f"blink {RELEASE_VERSION}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("install", help="Set everything up (this is the default)")
    sub.add_parser("uninstall", help="Put it all back")
    status_p = sub.add_parser("status", help="Is the panel getting data?")
    status_p.add_argument(
        "--wire", action="store_true",
        help="Also print the usage message the bridge would send the board"
             " right now, as one JSON line")
    sub.add_parser("update", help="Fetch a newer version of this app")
    sub.add_parser("driver",
                   help="Install the board's USB driver (Windows only)")
    prov_p = sub.add_parser(
        "provision",
        help="Stamp this unit's edition (factory step, run once)")
    prov_p.add_argument("--edition", required=True,
                        choices=list(protocol.EDITIONS),
                        help="Which boot clip this board plays")
    prov_p.add_argument("--port", default=None,
                        help="Serial port (default: find the board)")
    prov_p.add_argument("--baud", type=int, default=115200)
    config_p = sub.add_parser(
        "config",
        help="Show or change a setting (blink config status-page on)")
    config_p.add_argument("setting", nargs="?", choices=list(_CONFIG),
                          help="Which setting; omit to list them all")
    config_p.add_argument("value", nargs="?", choices=["on", "off"],
                          help="Turn it on or off; omit to show it")
    theme_p = sub.add_parser(
        "theme",
        help="Change the board's colours (blink theme opens the builder)")
    theme_p.add_argument("action", nargs="?",
                         choices=["set", "reset", "list", "show"],
                         help="Omit to open the theme builder in a browser")
    theme_p.add_argument("values", nargs="*",
                         help="For set: a theme name or its number in"
                              " blink theme list, a BT1. share code, or"
                              " key=value pairs (bg text gauge1 gauge2 low"
                              " mid high width ends dot arcs)")
    run_p = sub.add_parser("run", help="Run the bridge in the foreground")
    run_p.add_argument("--port", default=None,
                       help="Serial port (default: find the board)")
    run_p.add_argument("--baud", type=int, default=115200)
    run_p.add_argument("--log", default=None,
                       help="Append output to this file (the Windows service)")
    args = parser.parse_args(argv)

    # Bare `./blink` installs. Someone who just downloaded a file and
    # double-clicked it meant "set this up", and making them discover a
    # subcommand first is the opposite of the point.
    return {
        None: cmd_install,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "update": cmd_update,
        "driver": cmd_driver,
        "provision": cmd_provision,
        "config": cmd_config,
        "theme": cmd_theme,
        "run": cmd_run,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
