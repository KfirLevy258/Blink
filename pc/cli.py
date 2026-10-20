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
import glob
import os
import shutil
import subprocess
import sys
import threading
import time
from typing import NamedTuple

from pc import (install_codex_hooks, install_hooks, install_opencode_plugin,
                install_statusline, ota, protocol, statusline_source, update,
                win_driver)
# Eagerly, unlike the other providers, which are imported inside the functions
# that use them to keep the frozen binary's start-up cheap. This one costs
# nothing to import (os, and claude_state, which is json/os/sys/time) and it
# has to be a module attribute rather than a local: `blink status` reads it,
# and a test has to be able to replace it without a real slot directory in
# reach -- the scan behind it deletes files.
from pc.providers import codex_state
# Eagerly too, and for the same reason: `blink status` and the install steps
# read it, and a test has to be able to replace its fetch_once -- the one call
# in this file that makes a network request with a customer's key -- without
# a real auth.json or a real Z.ai anywhere in reach. Importing it costs ssl
# and urllib, which pc/ota already pulls in for every command.
from pc.providers import opencode
from pc.providers import opencode_budget
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
def _skip_service():
    return os.environ.get("BLINK_SKIP_SERVICE") == "1"


def _frozen() -> bool:
    return getattr(sys, "frozen", False)


def _self_path() -> str:
    """The binary to copy into place. sys.executable is the frozen binary."""
    return sys.executable if _frozen() else os.path.abspath(sys.argv[0])


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
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(here, "tools", name), encoding="utf-8").read()


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


def desktop_app_present() -> bool:
    """Has Claude Desktop ever written its usage cache on this machine?

    The file, not the application bundle. An installed app that has never run
    has nothing for us to read, and a machine where the app was removed but
    the cache remains still has a (stale, and reported as stale) reading. The
    file is the thing this product actually depends on, so the file is what
    gets asked about.
    """
    from pc.providers import claude_desktop
    try:
        return os.path.exists(claude_desktop.cache_path())
    except Exception:
        return False


def desktop_app_installed() -> bool:
    """Has Claude Desktop ever run here, even if it has recorded no usage yet?

    desktop_app_present() asks for the usage cache, which Desktop writes only
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

    The logs, not the binary, for the reason desktop_app_present() gives: the
    file the daemon reads is the thing that matters.
    """
    from pc.providers import codex_cli
    try:
        return bool(codex_cli.recent_rollouts(limit=1))
    except Exception:
        return False


def opencode_present() -> bool:
    """Has OpenCode run on this machine?

    Its database, not its binary, for the reason codex_present gives: the file
    is what proves the tool is used here (pc/providers/opencode.present).
    """
    try:
        return opencode.present()
    except Exception:
        return False


def opencode_plan_key() -> bool:
    """Does OpenCode hold a GLM Coding Plan key Blink would read quota with?

    A yes/no and nothing more: quota_source() returns which plan and which
    host, never the key, so nothing that asks this is handed a secret it
    could print. Reads OpenCode 2.0's credential table in opencode.db,
    read-only, and auth.json; makes no request.
    """
    try:
        return opencode.quota_source() is not None
    except Exception:
        return False


def opencode_plugin_ours() -> bool:
    """Is OUR plugin at the path OpenCode loads it from?

    Ours by its marker line, not by its name: a blink.js somebody else wrote
    lights nothing, and calling it Blink's would promise a light it cannot
    give (install_opencode_plugin explains the ownership rule).
    """
    try:
        return install_opencode_plugin.is_ours(
            install_opencode_plugin.plugin_path(opencode.config_dir()))
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
    codex: bool
    hooks: ClaudeHooks
    weekly: bool
    # OpenCode, in three facts rather than one, because each changes a
    # different sentence. `opencode`: it has run here (its database exists).
    # `opencode_plan`: it holds a GLM Coding Plan key, so there is a quota to
    # show -- without one an OpenCode desk gets a light and no numbers, and
    # promising numbers there is the confidently-wrong copy this tuple was
    # written to end. `opencode_plugin`: our plugin is the one at the path
    # OpenCode loads, so its sessions can light the pip at all; a blink.js
    # of somebody else's is left in place by install and lights nothing.
    # Defaulted, so a test that builds a Desk by hand for a Claude-only
    # question does not have to know OpenCode exists.
    opencode: bool = False
    opencode_plan: bool = False
    opencode_plugin: bool = False
    # The providers, OpenCode aside, that would put a NUMBER on the cable
    # right now -- numbered_sources(), read off the same look-only bus
    # `status --wire` builds. Not inferred from which tools are installed:
    # see numbered_sources for the two desks that inference got wrong.
    numbers: frozenset = frozenset()

    @property
    def others_report(self) -> bool:
        """Whether anything but OpenCode puts a usage message on the cable.

        The one predicate the install note and `blink status` both ask:
        without such a message, and without a GLM plan key, the daemon sends
        nothing at all and OpenCode's light has no frame to ride on.
        """
        return bool(self.numbers)

    @property
    def cli_answers(self) -> bool:
        return self.claude_text is not None

    @property
    def tools(self) -> list:
        """The other vendors' tools on this desk, by name, in panel order."""
        return [n for n, on in (("Codex", self.codex),
                                ("OpenCode", self.opencode)) if on]

    @property
    def tool_lights(self) -> list:
        """The tools whose sessions can light the pip on this desk.

        Codex from its own session log, whatever its hooks are doing;
        OpenCode only through our plugin: 1.x keeps session state in memory
        and nowhere on disk (docs/plans/opencode.md), and 2.0, which does
        write run state to its database, still holds permission prompts --
        the state the light exists for -- in memory only
        (docs/plans/opencode-2-light.md).
        """
        return [n for n, on in (("Codex", self.codex),
                                ("OpenCode",
                                 self.opencode and self.opencode_plugin))
                if on]

    @property
    def cli_silent(self) -> bool:
        """On PATH, but --version said nothing. Not "absent": telling that
        person Claude Code is not installed sends them to reinstall the
        wrong thing."""
        return self.claude_text is None and self.on_path

    @property
    def unsigned_desktop(self) -> bool:
        return self.desktop_installed and not self.desktop


def numbered_sources(now=None) -> frozenset:
    """The providers other than OpenCode with a usage figure to send now.

    Third review, 2026-10-19: the install note asked `desk.codex or
    desk.opencode_plan` and `blink status` asked "a statusline.json ever
    written, or Desktop, or Codex" -- two different guesses at one question,
    and both wrong for real desks. A Codex whose rollouts carry no usable
    rate_limits (an API-key login writes none) sends no frame, and a
    statusline.json weeks old, whose windows have since reset, sends
    nothing either; on each, with a keyless OpenCode beside it, the daemon
    sends no usage message and the board sits on its waiting screen while
    both commands promised OpenCode's light.

    So the question is put to the providers themselves: the look-only bus
    `status --wire` builds (ingest.default_providers(look_only=True)), each
    polled once, and normalizer.rank -- the step that decides which
    providers get a ring -- says who has a number. OpenCode is left out:
    its quota is the one source that needs the network, and on a desk with
    a plan key that key is the number source anyway (desk.opencode_plan).
    Polled directly rather than through IngestionBus.poll, which would also
    write the weekly anchor and may walk Desktop's stores to seed it: the
    install has no business changing ~/.blink to describe it. look_only
    sweeps nothing.
    """
    from pc import ingest, normalizer
    now = time.time() if now is None else now
    frames = []
    for p in ingest.default_providers(look_only=True):
        if isinstance(p, opencode.OpenCodeProvider):
            continue
        try:
            frames.extend(p.poll(now) or [])
        except Exception:
            # The bus contains a failing provider the same way, and a
            # provider the daemon would skip has no number to send.
            continue
    return frozenset(m.provider for m in normalizer.rank(frames))


def read_desk(version=None) -> Desk:
    """Read the machine once. `version` is a claude_version() result the
    caller already has: `blink install` used to ask twice, and a `claude`
    that hangs costs its 30 s timeout per ask (measured 60.0 s, review
    2026-10-19). The weekly question is only asked where a desk can need it
    -- it may read Desktop's IndexedDB, and a CLI desk has no use for it."""
    text, ver = claude_version() if version is None else version
    desktop = desktop_app_present()
    return Desk(claude_text=text, claude_ver=ver,
                on_path=text is not None or claude_on_path(),
                desktop=desktop,
                desktop_installed=desktop or desktop_app_installed(),
                codex=codex_present(),
                hooks=claude_hooks(),
                weekly=text is None and desktop and weekly_countdown_known(),
                opencode=opencode_present(),
                opencode_plan=opencode_plan_key(),
                opencode_plugin=opencode_plugin_ours(),
                numbers=numbered_sources())


def _and(names) -> str:
    """"A", "A and B", "A, B and C" -- for the lists of sources above."""
    names = list(names)
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


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


def _announce_opencode_quota() -> None:
    """Say, before it happens, that Blink will send OpenCode's key somewhere.

    The first time this product makes a request with a customer's
    credential (pc/providers/opencode.py, WHY A NETWORK REQUEST AT ALL), and
    install asks nothing -- so this paragraph is the consent, such as it is,
    and every clause in it is a rule that module enforces in code: the host
    the key was issued for and no other, read-only, the cadence, nothing
    logged. Change one there, change it here.

    Said whether or not a key is there today. The daemon looks for the key
    at every fetch (OpenCode 2.0's credential table, else auth.json), so a
    key added next week starts the requests with no install in between; disclosing it only when one is present now would
    make this the only notice that person never gets. The last line says
    which of the two this desk is, so nobody has to guess whether anything
    is being sent yet.

    Wrapped continuation lines start lower case, as in
    _announce_codex_hooks: a line that continues a sentence is not a new one.
    """
    print("  Quota    If OpenCode holds a GLM Coding Plan key -- in its")
    print("           auth.json, or in its database from OpenCode 2.0 on --")
    print("           Blink reads that key and sends it to the quota")
    print("           endpoint of whichever service issued it -- Z.ai,")
    print("           or BigModel for a mainland plan -- and nowhere else.")
    print("           The request only reads your plan's usage. It is made")
    print("           about once a minute while OpenCode is in use, every 10")
    print("           minutes otherwise, and not at all after a week idle.")
    print("           The key is never logged or stored by Blink. Where")
    print("           this machine uses an HTTPS proxy the request goes")
    print("           through it like any other, the key inside the")
    print("           encrypted connection.")
    print("  Stop     Uninstalling Blink stops it, and so does removing the")
    print("           plan key from OpenCode:")
    print(f"             {installed_bin()} uninstall")
    if opencode_plan_key():
        print("  Now      A plan key is there, so the first read follows")
        print("           within a minute of the service starting.")
    else:
        print("  Now      No plan key is there, so nothing is sent.")
    print()


def _opencode_plugin_text() -> str:
    """The plugin's text, from the bundle when frozen, the tree when not.

    _shim_source, not a second resolver: tools/blink-opencode.js ships beside
    blink-hook.sh (--add-data in tools/build_binary.sh), and one function
    deciding where the bundle is means a frozen build cannot read one file
    from _MEIPASS and the other from somewhere else.
    """
    return _shim_source(install_opencode_plugin.SOURCE_NAME)


def _install_opencode_plugin() -> str:
    """Put our plugin where OpenCode loads plugins, if there is an OpenCode.

    Detected, like the Codex step, and absent OpenCode NOTHING is written:
    not the plugin, not the plugin directory, not ~/.config/opencode. A
    directory created for a tool that is not installed is a stranger's
    configuration left on the machine -- and this one is worse than most,
    because OpenCode treats every config directory it finds as somewhere to
    run `npm install` (install_opencode_plugin, OPENCODE'S OWN SIDE EFFECT).
    """
    if not opencode_present():
        return "no OpenCode on this machine, nothing to do"
    # Private from the first write, for the reason _install_codex_hooks gives:
    # the slots name the projects someone has open.
    #
    # And made HERE, because the plugin no longer makes it: it writes only
    # into a state directory that already exists, so that an OpenCode still
    # running after `blink uninstall` -- which keeps the plugin loaded once
    # its file is gone -- cannot put the directory back (found in review,
    # 2026-10-19; tools/blink-opencode.js, THE DIRECTORY IS THE INSTALLER'S).
    # Without this line the plugin would install and never light anything.
    state_dir = opencode.state_dir()
    os.makedirs(state_dir, exist_ok=True)
    try:
        os.chmod(state_dir, 0o700)
    except OSError:
        pass                 # Windows, where the mode does not apply
    try:
        text = _opencode_plugin_text()
    except (OSError, UnicodeDecodeError) as e:
        # A build that forgot the --add-data line. The status line is the
        # product, and an install that has otherwise worked is not failed
        # over the light of a third tool.
        return f"skipped (the plugin is missing from this build: {e})"
    return install_opencode_plugin.install(opencode.config_dir(), text)


def _record_opencode_dirs():
    """Hand the service the OpenCode directories this shell sees.

    The service runs without this shell's XDG_DATA_HOME / XDG_CONFIG_HOME
    (launchd, systemd and the Task Scheduler each start it with an
    environment of their own), so where those move OpenCode off its
    defaults the daemon would look in the wrong place and show nothing --
    found in review, 2026-10-19; opencode.PATHS_RECORD has the whole story.
    A line to print, or None. Never raises: an unwritable record costs the
    gauges on an unusual desk, and is not worth failing the install over.
    """
    try:
        return opencode.record_dirs()
    except OSError as e:
        return f"could not record OpenCode's directories for the service ({e})"


def refresh_opencode_plugin():
    """Rewrite OUR plugin if it is there and stale. None when nothing to do.

    For `blink update` and DriftWatchdog: the plugin ships inside the program
    exactly as the shims do, so an update leaves the previous release's copy
    behind. install_opencode_plugin.refresh holds the two rules -- never
    create one where none was, never touch a blink.js that is not ours --
    and this only supplies the text. Never raises: it runs in the daemon's
    poll loop.
    """
    try:
        text = _opencode_plugin_text()
    except (OSError, UnicodeDecodeError):
        # Nothing to compare against: the same judgement shim_is_current
        # makes about an unreadable bundle. Rewriting from nothing is
        # impossible, and logging it every tick would be noise.
        return None
    try:
        return install_opencode_plugin.refresh(opencode.config_dir(), text)
    except Exception as e:
        return f"OpenCode plugin could not be refreshed ({e})"


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
    # OpenCode is a third source with a third mechanism: its plugin, the
    # only place its session state is ever written (docs/plans/opencode.md).
    # So it is listed only when that plugin is ours and in place, and a desk
    # whose blink.js belongs to somebody else hears why it has no light.
    tools = desk.tool_lights
    sessions = f"{_and(tools)} sessions" if tools else ""
    if claude and tools:
        sep = ", and " if len(tools) > 1 else " and "
        shown.append(f"the activity light, for {sessions}{sep}Desktop's"
                     " Code tab")
    elif claude:
        shown.append("the activity light, while you work in Desktop's"
                     " Code tab")
    elif tools:
        shown.append(f"the activity light, for {sessions}")
    if desk.desktop and not desk.hooks.light:
        missing.append("the activity light for Desktop's Code tab -- "
                       + _hooks_gap(desk.hooks))
    if desk.opencode and not desk.opencode_plugin:
        missing.append("the activity light for OpenCode sessions -- its"
                       " plugin is not installed (see the OpenCode plugin"
                       " step above)")
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
        if desk.desktop or desk.tools:
            source = _and((["Claude Desktop"] if desk.desktop else [])
                          + desk.tools)
            print(f"     Meanwhile the panel also reads {source}.")
        return

    if not desk.desktop and desk.opencode:
        _note_tools_alone(desk)
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
    if desk.opencode_plan and desk.opencode:
        # Only with a plan key: without one OpenCode has no number to put on
        # a page, and its light is already listed above.
        shown.append("OpenCode's GLM plan quota, on its own page")
    missing += light_missing
    missing.append("an up-to-the-minute weekly percentage: it follows"
                   " Desktop's own refresh")

    who = (f"{_and(['Claude Desktop'] + desk.tools)}" if desk.tools
           else "Claude Desktop alone")
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


def _note_tools_alone(desk):
    """The note for a desk with OpenCode and no Claude at all, Codex or not.

    Built from parts rather than written out per combination: Codex alone
    keeps its own hand-set paragraph above, and every desk with OpenCode on
    it lands here, where three independent facts -- a plan key or not, our
    plugin in place or not, Codex beside it or not -- decide what the panel
    can show. Eight paragraphs written in advance is eight chances for one
    to promise something its desk cannot do, which is the failure every
    other branch of _note_if_no_claude_code was rewritten to stop.
    """
    import textwrap
    shown = []
    # Only a Codex whose log has limits in it (numbered_sources): one that
    # writes none -- an API-key login -- shows neither here.
    if desk.codex and "codex" in desk.numbers:
        shown.append("both Codex limits and their countdowns, from the"
                     " session log Codex already writes")
    if desk.opencode_plan:
        shown.append("OpenCode's GLM Coding Plan quota, read from the plan's"
                     " own endpoint with the key OpenCode keeps")
    # The light rides on a usage message, and there is none without a
    # number: the daemon sends nothing at all on a desk where no source has
    # one, and the board stays on its waiting screen. Measured with real
    # OpenCode on the real board (2026-10-19) -- this note promised "only its
    # light" to exactly that desk. Asked of the providers, not of which
    # tools are installed (numbered_sources, third review 2026-10-19).
    #
    # A token budget is a number too (pc/providers/opencode_budget.py): the
    # daemon then sends OpenCode's own page, light and all.
    budgeted = (not desk.opencode_plan
                and opencode_budget.read_budget() is not None)
    numbered = desk.others_report or desk.opencode_plan or budgeted
    if desk.tool_lights and numbered:
        shown.append(f"the activity light for {_and(desk.tool_lights)}"
                     " sessions")
    who = (f"{desk.tools[0]} alone" if len(desk.tools) == 1
           else _and(desk.tools))
    text = f"Claude Code is not installed, so the panel runs on {who}"
    # Commas between the items and ", and" before the last: the items carry
    # "and" inside them ("limits and their countdowns"), and a bare " and "
    # join ran the quota and the light together into one clause.
    if len(shown) > 1:
        shown = [", ".join(shown[:-1]) + ", and " + shown[-1]]
    text += (": " + shown[0] + ".") if shown else "."
    if not desk.opencode_plan:
        # Not a fault to fix, so not phrased as one: OpenCode on any other
        # model has no plan quota anywhere to read (docs/plans/opencode.md).
        if budgeted:
            text += (" OpenCode holds no GLM Coding Plan key, so its rings"
                     " show its token use against the budget you set.")
        elif numbered:
            text += (" OpenCode holds no GLM Coding Plan key, so it has no"
                     " usage figures to show"
                     + (", only its light." if desk.opencode_plugin
                        else "."))
        else:
            text += (" OpenCode holds no GLM Coding Plan key and nothing"
                     " else here reports a usage figure, so the board stays"
                     " on its waiting screen. Add the key with `opencode"
                     " auth login`, provider Z.AI Coding Plan, and OpenCode"
                     " gets gauges and its light.")
    if not desk.opencode_plugin:
        text += (" OpenCode's light needs the plugin the step above could"
                 " not install.")
    if desk.unsigned_desktop:
        text += (" Claude Desktop is installed but not signed in; sign in"
                 " to it and the Claude page fills by itself.")
    else:
        text += (" The Claude page stays empty until you add Claude Code;"
                 " the status line just installed starts working by itself"
                 " when you do.")
    print(textwrap.fill(text, 67, initial_indent="  !  ",
                        subsequent_indent="     ", break_on_hyphens=False))


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
  <key>StandardOutPath</key><string>{log}</string>
  <key>StandardErrorPath</key><string>{log}</string>
</dict>
</plist>
"""

def _systemd_exec() -> str:
    """ExecStart, with every argument quoted.

    systemd splits ExecStart on whitespace, so a bare " ".join() broke on any
    space in the interpreter path, the checkout path or $HOME: with a spaced
    home the unit resolved the executable as the first fragment and failed
    with "Failed to locate executable", while `blink install` still printed
    "running (systemd)". The launchd and schtasks backends already quote
    per-argument; systemd was the only one that did not, and the CI scenario
    that uses a spaced home asserts only that the unit FILE exists.

    systemd's own quoting: double quotes, with \\ and \" escaped inside.
    """
    out = []
    for a in _service_command():
        out.append('"%s"' % a.replace("\\", "\\\\").replace('"', '\\"'))
    return " ".join(out)


_UNIT_TEMPLATE = """[Unit]
Description=Blink USB bridge

[Service]
ExecStart={command}
Environment=PYTHONUNBUFFERED=1
Restart=always
RestartSec=10

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
    if _frozen():
        return [installed_bin(), "run"]
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return [sys.executable, os.path.join(repo, "blink_main.py"), "run"]


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

    def install(self) -> str:
        return (f"not supported on {sys.platform}; run it yourself: "
                f"{installed_bin()} run")

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


class _LaunchdBackend(_Backend):
    def creates(self):
        return plist_path()

    def install(self) -> str:
        os.makedirs(os.path.dirname(plist_path()), exist_ok=True)
        with open(plist_path(), "w", encoding="utf-8") as f:
            args = "".join(f"    <string>{_xml_escape(a)}</string>\n"
                           for a in _service_command())
            f.write(_PLIST_TEMPLATE.format(label=LABEL, args=args,
                                           log=_xml_escape(log_path())))
        uid = os.getuid()
        # bootout first so a rerun replaces the running agent rather than
        # failing with "service already loaded".
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                       capture_output=True, **update.ota.NO_WINDOW)
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
        subprocess.run(["launchctl", "kickstart", "-k", f"gui/{uid}/{LABEL}"],
                       capture_output=True, **update.ota.NO_WINDOW)

        # And then ASK, rather than assume. Everything above is a command that
        # returned zero; none of it is evidence that a process exists. launchd
        # is the only thing that knows, so the claim we print is its answer and
        # not our hope.
        if _confirm_running(lambda: self._is_running(uid)):
            return "running (launchd)"
        return ("registered with launchd, but it is not running -- "
                f"start it with: launchctl kickstart -k gui/{uid}/{LABEL}")

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
                        f"`{installed_bin()} install` to start it")
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
    def creates(self):
        return (f'a Scheduled Task named "{TASK_NAME}", and {launcher_path()}\n'
                "             the one-line script it runs, so the bridge starts\n"
                "             with no window. If Windows will not register the\n"
                "             task without administrator, an entry named\n"
                f'             "{RUN_VALUE}" under your own account\'s Run key\n'
                "             is used instead, which starts it the same way")

    def install(self) -> str:
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
            return "running (Scheduled Task)"
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
            return "running (starts when you log in)"
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
        if not registered:
            return ("not installed; killed a detached daemon" if killed
                    else "not installed")
        if r.returncode == 0:
            return "stopped"
        return f'could not stop it: schtasks /end /tn "{TASK_NAME}"'

    def start(self, runner=None) -> str:
        run = runner or subprocess.run
        if not self._registered(run):
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

    def install(self) -> str:
        # The unit file is written either way. It costs nothing, and it means
        # a machine that gains systemd later already has the right file.
        os.makedirs(os.path.dirname(unit_path()), exist_ok=True)
        with open(unit_path(), "w", encoding="utf-8") as f:
            f.write(_UNIT_TEMPLATE.format(command=_systemd_exec()))
        if not self._has_systemctl():
            return f"no systemd here; run it yourself: {installed_bin()} run"
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
            return "running (systemd)"
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
        r = subprocess.run(["systemctl", "--user", "restart",
                            "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
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
        subprocess.run(["systemctl", "--user", "disable", "--now",
                        "blink-bridge.service"], capture_output=True, **update.ota.NO_WINDOW)
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


def _install_service() -> str:
    if _skip_service():
        return "skipped (BLINK_SKIP_SERVICE=1)"
    return backend().install()


def restart_service() -> str:
    """Bounce the login service so it comes up on a freshly replaced binary.

    Not the same as exiting and letting the supervisor notice: this is called
    from `blink update`, which is a separate process from the daemon. The
    daemon's own path is simpler -- on macOS and Linux it exits and KeepAlive /
    Restart=always bring it back. Windows has neither: a Scheduled Task with an
    onlogon trigger does not restart anything, so it is told explicitly.
    """
    if _skip_service():
        return "skipped (BLINK_SKIP_SERVICE=1)"
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
    # The OpenCode plugin ships inside the program too, under the same two
    # rules: only a plugin that is already there, and only one that is ours
    # (refresh_opencode_plugin). Silent when it worked, like the shims above;
    # a refusal or a failed write is said, because the plugin left behind is
    # last release's and the light it drives may not match this daemon.
    msg = refresh_opencode_plugin()
    if msg and not msg.startswith(("OpenCode plugin updated",
                                   "OpenCode plugin already")):
        print(f"  {msg}")


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
        with serial.Serial(port, 115200, timeout=0.2, write_timeout=1) as s:
            s.write((json.dumps(protocol.bye(), separators=(",", ":")) + "\n").encode())
            s.flush()
    except Exception:
        pass


def _remove_service() -> str:
    if _skip_service():
        return "skipped (BLINK_SKIP_SERVICE=1)"
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
    print(f"  Creates    {installed_bin()}")
    print("             a copy of this program and its support files, so the")
    print("             folder you downloaded can be deleted when this finishes.")
    print(f"  Creates    {shim_path()}")
    print("             the small script Claude Code runs to hand over the")
    print("             usage figures. It keeps the last status line payload")
    print("             Claude Code sent, which also names the session, the")
    print("             working directory and the transcript file. Only the")
    print("             usage figures are read out of it, and nothing in it")
    print("             leaves this machine. It is readable by you alone.")
    print(f"  Creates    {hook_shim_path()}")
    print("             a second small script, run when Claude Code starts and")
    print("             finishes work, so the panel can show whether it is busy.")
    print("             It records the event name, the time, and the session and")
    print("             agent ids Claude Code generates -- used to tell concurrent")
    print("             sessions apart, and for nothing else. No prompt, no tool")
    print("             arguments, no file paths, no message text.")
    print(f"  Creates    {os.path.join(blink_home(), 'state')}")
    print("             one small file per open session, deleted when it ends.")
    # Named here only when it will really be written: the step below writes
    # nothing on a desk with no OpenCode, and a "Creates" line for a file
    # that never appears is the disclosure being wrong the other way.
    oc = opencode_present()
    if oc:
        print("  Creates    "
              + install_opencode_plugin.plugin_path(opencode.config_dir()))
        print("             a plugin OpenCode loads, so the panel can show")
        print("             whether an OpenCode session is busy or waiting on")
        print("             you. The OpenCode plugin step below says what it")
        print("             records, and what Blink sends on OpenCode's behalf.")
        print(f"  Creates    {opencode.state_dir()}")
        # "Recent", not "open": under OpenCode 2.0 the sessions live in one
        # background service that outlives every window, and closing a
        # window ends nothing, so a slot stays until the session is deleted,
        # the service stops, or the hour-long sweep takes it.
        print("             one small file per recent OpenCode session.")
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
    print()
    # This sentence was unconditionally true until OpenCode: there is no
    # usage figure for a GLM plan anywhere on disk, so Blink asks the plan's
    # own endpoint with the key OpenCode holds (pc/providers/opencode.py).
    # "Reads no credential, sends nothing" printed above a step that does
    # exactly that would be the disclosure contradicting itself. And on a
    # desk with no OpenCode today it is still said, briefly: the daemon reads
    # auth.json whenever one appears, with no install in between.
    if oc:
        print("  It reads no Claude or Codex credential. The one thing it")
        print("  sends anywhere is OpenCode's GLM Coding Plan key, if OpenCode")
        print("  holds one, to that plan's own quota endpoint -- the OpenCode")
        print("  plugin step below says exactly when.")
    else:
        print("  It reads no credential and no token, and sends nothing anywhere.")
        print("  (One exception, should you add OpenCode on Z.ai's GLM Coding")
        print("  Plan later: Blink then reads that plan's quota with the key")
        print("  OpenCode keeps, from the plan's own endpoint and no other.)")
    print("  The usage figures come from Claude Code, Claude Desktop's usage")
    print("  cache, or the Codex session log -- each of which has already")
    print("  worked them out. From the Codex log it keeps only the rate-limit")
    print("  line, never the conversation.")
    print()
    print(f"  To undo all of it:  {installed_bin()} uninstall")
    print()


def _install_steps() -> int:
    """How many numbered steps `blink install` prints on this platform.

    Counted on every desk, OpenCode or not: the OpenCode step prints its
    "nothing to do" line where there is none, as the Codex step does, so the
    numbering is the same everywhere and docs/windows-check.md can name it.
    """
    return 7 if sys.platform == "win32" else 6


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
        return "not checked (BLINK_SKIP_SERVICE=1)"
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
            print(f"  {installed_bin()} status")
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
    os.makedirs(bin_dir(), exist_ok=True)
    if _frozen():
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
                                 undo_hint=f"{installed_bin()} uninstall")
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
    # because it is conditional on what is already on the machine: on a
    # Codex-free machine there is nothing to disclose, and a paragraph about
    # another vendor's config file would be noise at best and alarming at
    # worst. The OpenCode step below is the second such step, and for the
    # same reason carries its own.
    step("Codex hooks")
    if codex_present():
        print()
        _announce_codex_hooks()
        print("      " + _install_codex_hooks())
    else:
        print(_install_codex_hooks())

    # Disclosure, then the write, as above -- and this one has two halves,
    # because OpenCode is where Blink first sends a credential anywhere: the
    # plugin it is about to put on disk, and the quota request the daemon
    # will make with OpenCode's key.
    step("OpenCode plugin")
    if opencode_present():
        print()
        print(install_opencode_plugin.announce(opencode.config_dir()))
        _announce_opencode_quota()
        note = _record_opencode_dirs()
        if note:
            print("      " + note)
        print("      " + _install_opencode_plugin())
    else:
        print(_install_opencode_plugin())

    if sys.platform == "win32":
        step("USB driver")
        print(_install_driver_step())

    step("Background service")
    print(_install_service())

    print()
    print("Done. Plug the board in over USB -- it picks it up on its own.")
    print(f"  Log:     {log_path()}")
    print(f"  Check:   {installed_bin()} status")
    print(f"  Undo:    {installed_bin()} uninstall")
    print()
    print("  You can delete the folder you downloaded.")
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
    print("[1/6] Background service ... ", end="", flush=True)
    print(_remove_service())
    _say_bye()

    print("[2/6] Claude Code setting:")
    try:
        print("      " + install_statusline.uninstall(settings_path(), shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # Keep going. The login service is already gone by this point, so
        # stopping here would leave the machine half-undone -- and the one
        # thing we must not do is "repair" the file by writing a fresh one
        # over whatever the customer was in the middle of editing.
        print(f"      Left alone: {e}")
        print("      Remove the statusLine.command line by hand once it parses.")

    print("[3/6] Activity hooks:")
    try:
        print("      " + install_hooks.uninstall(settings_path(),
                                                 hook_shim_path()))
    except install_statusline.SettingsUnreadable as e:
        # Same reasoning as the step above: keep going, and never "repair" a
        # file we cannot parse by writing a fresh one over it.
        print(f"      Left alone: {e}")

    print("[4/6] Codex hooks:")
    _uninstall_codex_hooks()

    # Ours only: a blink.js without our marker line is somebody else's, and
    # stays (install_opencode_plugin.uninstall). Nothing else of OpenCode's
    # is touched -- not its config, not auth.json, not the directory, which
    # OpenCode populates itself. Its slot directory goes with the others in
    # the Files step below.
    print("[5/6] OpenCode plugin:")
    print("      " + install_opencode_plugin.uninstall(opencode.config_dir()))

    print("[6/6] Files ... ", end="", flush=True)
    # Only what install created. NOT ~/.blink itself: it also holds the two
    # signing keys, which cannot be regenerated -- every board flashed with the
    # first one's public half, and every app carrying the second one's, would
    # stop accepting updates.
    for p in (shim_path(), hook_shim_path(), launcher_path(),
              os.path.join(blink_home(), "statusline.json"),
              os.path.join(blink_home(), "statusline.json.tmp"),
              # Pid-scoped temp names, from a render interrupted mid-write.
              *glob.glob(os.path.join(blink_home(), "statusline.json.*.tmp")),
              os.path.join(blink_home(), "state.json"),
              os.path.join(blink_home(), "state.json.tmp"),
              os.path.join(blink_home(), "pending_fw.json"),
              # Written by install only on a desk whose XDG variables move
              # OpenCode (opencode.PATHS_RECORD); absent everywhere else.
              opencode.paths_record_path()):
        _rm(p)
    # The per-session state directory, and everything under it. state.json
    # above is the single-slot file this replaced; it is still on the list so
    # an install that predates the directory leaves nothing behind.
    _rm_state_dir()
    done, message = _remove_bin_dir()
    print(message)
    print()
    if done:
        print("Done. Nothing of Blink's is left running.")
        return 0
    print("Everything else is undone, but that file is still there. Log out and")
    print("back in, then delete it by hand:")
    print(f"  {bin_dir()}")
    return 1


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
    """Remove ~/.blink/state, state-codex and state-opencode, and what is in them.

    All three, because the Codex and OpenCode slots deliberately live apart
    from the Claude ones (pc/providers/codex_state explains why) -- and a slot
    directory left behind by an uninstall is not litter, it is a lie: the
    daemon would go on counting sessions of a tool it no longer hooks.

    Two levels deep and no deeper, by construction: the shim only ever creates
    <session>.state files and <session>/<agent> files, in whichever of the two
    directories its second argument chose. Walking rather than shutil.rmtree
    because this runs against a path under the customer's home and a bounded
    loop cannot be talked into deleting more than it was told.
    """
    # The OpenCode plugin writes the hook shim's exact layout -- <session>.state,
    # <session>.waiting, <session>/<agent> -- so the same two levels hold
    # (tools/blink-opencode.js, docs/plans/opencode.md).
    for sub in ("state", "state-codex", "state-opencode"):
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
    return f"{note} -- run `{installed_bin()} install` to fix it"


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
        print("Bridge      not checked (BLINK_SKIP_SERVICE=1)")
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
        also = (["Claude Desktop"] if desk.desktop else []) + desk.tools
        print("Claude Code on PATH, but `claude --version` did not answer"
              + (f" (the panel also reads {_and(also)})"
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
            lights = desk.tool_lights
            if desk.hooks.light:
                gaps.append("activity light from the Code tab"
                            + (f" and {_and(lights)}" if lights else ""))
            elif lights:
                gaps.append(f"activity light from {_and(lights)} only:"
                            " Claude hooks not working")
            else:
                gaps.append("no activity light: Claude hooks not working")
            who = (_and(["Claude Desktop"] + desk.tools) if desk.tools
                   else "Claude Desktop alone")
            print(f"Claude Code not found -- running on {who}"
                  f" ({'; '.join(gaps)})")
        elif desk.tools:
            # Codex sessions light the pip from their own session log;
            # OpenCode's from our plugin, and its gauges need a plan key --
            # each said on its own OpenCode row below, not crammed in here.
            who = (f"{desk.tools[0]} alone" if len(desk.tools) == 1
                   else _and(desk.tools))
            print(f"Claude Code not found -- running on {who}"
                  + (" (sign in to Claude Desktop for the Claude page)"
                     if desk.unsigned_desktop else " (no Claude page)"))
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
                  f" run `{installed_bin()} install` to restore them")
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
    payload = os.path.join(blink_home(), "statusline.json")
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
              " status line on this machine)")
    elif age is not None:
        print(f"Usage data  {age}s old -- open Claude Code to refresh it")
    elif desk.codex and "codex" in desk.numbers:
        print("Usage data  from the Codex session log (no Claude Code status"
              " line yet)")
    elif desk.opencode and desk.opencode_plan:
        print("Usage data  from OpenCode's GLM plan quota (no Claude Code"
              " status line yet)")
    elif desk.opencode and opencode_budget.read_budget() is not None:
        print("Usage data  from OpenCode's token counts, against the budget"
              " you set (no Claude Code status line yet)")
    elif desk.opencode and not desk.on_path and not desk.unsigned_desktop:
        # OpenCode is the only tool here and has no plan key. Found end to
        # end with real OpenCode 1.19.3, 2026-10-19: this desk fell through
        # to "open Claude Code once" -- printed straight under "Claude Code
        # not found -- running on OpenCode alone", sending the person to a
        # program they do not have, for a number it would not have given
        # their OpenCode anyway. The OpenCode rows below say what would.
        print("Usage data  none -- nothing on this desk reports a usage"
              " figure (see the OpenCode rows below)")
    elif desk.codex and not desk.on_path and not desk.unsigned_desktop:
        # Codex alone, and its log has no limits in it -- an API-key login
        # writes none. It used to say "from the Codex session log" here
        # while the daemon sent nothing (third review, 2026-10-19), and
        # "open Claude Code once" is no better for a desk without it.
        print("Usage data  none -- Codex's session log has no usage limits"
              " in it yet")
    elif desk.unsigned_desktop and not desk.on_path:
        print("Usage data  none yet -- Claude Desktop is installed; open it"
              " and sign in")
    else:
        print("Usage data  none yet -- open Claude Code once so it renders "
              "its status line")
    # The one network request `blink status` makes, and only on a desk where
    # OpenCode holds a GLM plan key: there is no other place a GLM figure
    # exists to look at (see _opencode_lines). Its answer is handed to the
    # wire line below rather than fetched a second time there.
    # Whether anything but OpenCode puts a number on the cable. Without one,
    # OpenCode's light has no frame to ride on (see _opencode_lines). The
    # Desk's answer, which the install note shares: this used to be its own
    # guess -- "a status line ever written, Desktop, or Codex" -- and
    # disagreed with the note's (numbered_sources, third review 2026-10-19).
    oc_quota = _source_lines(bridge_running,
                             others_report=desk.others_report)
    if getattr(args, "wire", False):
        _wire_line(oc_quota)
    return 0


def _wire_line(oc_quota=None):
    """The usage message the bridge would put on the cable right now.

    The last thing that can be checked without a board: every source read,
    merged and encoded exactly as the daemon does it (pc/ingest -> normalizer
    -> protocol). CI feeds the installed binary the files a customer's machine
    would have and asserts on this line; a person can paste it into a report.
    """
    import json as _json
    from pc import ingest
    # look_only: the bus the daemon builds would start OpenCode's quota thread
    # on this first poll, which makes a request with the customer's key the
    # moment it is due -- that is, at once. A one-shot command must make no
    # request it did not decide to make, and must not sweep the slots it is
    # reporting on (_codex_hook_status gives that rule). So the OpenCode
    # provider here fetches nothing; the reading status already took, if
    # any, is handed to it instead, so the line still shows what the daemon
    # would send.
    providers = ingest.default_providers(look_only=True)
    if oc_quota is not None:
        quota, fetched_at = oc_quota
        for p in providers:
            if isinstance(p, opencode.OpenCodeProvider):
                p.remember(quota, fetched_at)
    bus = ingest.IngestionBus(providers=providers)
    msg = bus.poll()
    print()
    if msg is None:
        print("Wire        nothing to send -- no source has a reading")
        return
    print("Wire        the next usage message, exactly as the board gets it:")
    print(_json.dumps(msg, separators=(",", ":")))
    # Only on a desk with three providers, so every line above is what it
    # always was and anything that asserts on it is undisturbed. A board on
    # firmware with a third page is sent these two instead of the line above
    # (bridge._read_usage), and a report that showed only the line above
    # would hide the page the person is asking about.
    three = bus.paged()
    if three is not None:
        fw = ".".join(str(n) for n in protocol.FW_SHOWS_PAGE3)
        print(f"            and on a board with a third page (firmware {fw}"
              " or later):")
        for m in three:
            print(_json.dumps(m, separators=(",", ":")))


def _age(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 90:
        return f"{seconds}s"
    if seconds < 5400:
        return f"{seconds // 60} min"
    if seconds < 172800:
        return f"{seconds // 3600} h"
    return f"{seconds // 86400} d"


def board_lines(known, ports, undriven=(), blink_cmd="blink"):
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
        # not touch the network for this on purpose: it has to work on a
        # plane, and it is the first thing anyone runs when nothing works.
        # (Its one request is OpenCode's quota, which has no offline answer at
        # all, made only on a desk holding a plan key and bounded at
        # opencode.STATUS_FETCH_TIMEOUT_S -- see _opencode_lines.) So it names the
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
                           win_driver.undriven_boards(), installed_bin())
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
        return [f"Codex hook  not installed -- run `{installed_bin()} install`"
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


def _source_lines(bridge_running=False, others_report=True):
    """One line each for the sources CI cannot exercise.

    Claude Desktop is a closed GUI app and Codex will not write a rollout
    without an account, so neither format change can be caught on a runner.
    It can only be caught here, on the machine where the file is -- and the
    daemon already knows (it logs once and goes quiet). This is where a
    person finds out, and what they paste into a bug report. OpenCode's
    quota endpoint is the same story with a network in the way.

    Returns OpenCode's (Quota, fetched_at) when this run read one, for the
    wire line, or None.
    """
    from pc.providers import claude_desktop, codex_cli
    now = time.time()

    desktop = claude_desktop.ClaudeDesktopProvider()
    if os.path.exists(desktop.path()):
        frames = desktop.poll(now)
        if frames:
            age = now - frames[0].observed_at
            print(f"Desktop     usage cache parsed, reading"
                  f" {_age(age)} old")
            for line in desktop_staleness_note(age):
                print(f"            {line}")
        else:
            print("Desktop     usage cache present but did not parse --"
                  " the app's file format may have changed; please report it")
    else:
        # Name the path. Whether Claude Desktop keeps this file where we
        # think it does has been seen on macOS and is a convention on the
        # other two; the first person to run this beside a signed-in Desktop
        # on Windows or Linux is the check, and this line is what they need.
        print("Desktop     no usage cache (Claude Desktop not installed, or"
              " never signed in)")
        print(f"            looked at {desktop.path()}")

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

    lines, oc_quota = _opencode_lines(bridge_running,
                                      others_report=others_report)
    for line in lines:
        print(line)
    return oc_quota


def _opencode_dirs_mismatch():
    """Lines warning that the service will look somewhere else, or [].

    This shell resolves OpenCode's directories through its own XDG
    variables; the service has none and goes by the record install wrote,
    else the defaults (opencode.service_dirs). When the two disagree, every
    other OpenCode line `blink status` prints describes a directory the
    daemon never reads -- the very misreport that hid the gauges on an XDG
    desk until review caught it, 2026-10-19. Running install again from
    this shell rewrites the record.
    """
    try:
        shell = (opencode.data_dir(), opencode.config_dir())
        service = opencode.service_dirs()
    except Exception:
        return []
    out = []
    for what, mine, theirs in (("data", shell[0], service[0]),
                               ("config", shell[1], service[1])):
        if os.path.normcase(os.path.abspath(mine)) != os.path.normcase(
                os.path.abspath(theirs)):
            out.append(f"OpenCode    the service reads OpenCode's {what} from"
                       f" {theirs}, not {mine}")
    if out:
        out.append(f"            Run `{installed_bin()} install` again from"
                   " this shell to point it here.")
    return out


def _status_fetch(now):
    """opencode.fetch_once for `blink status`, bounded by the clock.

    urllib's timeout covers the connection and each read, NOT the name
    lookup: getaddrinfo runs before the socket exists and blocks for as long
    as the system resolver's own timeouts and retries take, which are not
    ours to set. Found in review, 2026-10-19: on a network whose DNS server
    has stopped answering, the four seconds STATUS_FETCH_TIMEOUT_S promises
    became however long the resolver held on, in front of someone waiting
    to read a status. So the fetch runs on a daemon thread and this waits for it
    STATUS_FETCH_TIMEOUT_S and no longer; a fetch still running then is
    reported as unreachable -- which is what it is -- and left to finish or
    die with the process. Daemon, so it cannot hold `blink status` open.

    A BaseException from the thread is re-raised here rather than turned
    into a category: fetch_once already turns every Exception into one, so
    anything else is a KeyboardInterrupt, or the test suite's guard against
    a real request (tests/conftest.py), and both must stay loud.
    """
    box = []

    def work():
        try:
            box.append(opencode.fetch_once(
                now, timeout=opencode.STATUS_FETCH_TIMEOUT_S))
        except BaseException as e:            # see the docstring
            box.append(e)

    t = threading.Thread(target=work, name="blink-status-quota", daemon=True)
    t.start()
    t.join(opencode.STATUS_FETCH_TIMEOUT_S)
    if not box:
        return None, opencode.ERR_UNREACHABLE
    if isinstance(box[0], BaseException):
        raise box[0]
    return box[0]


def _opencode_lines(bridge_running=False, now=None, others_report=True):
    """The OpenCode lines of `blink status`, and the quota read for them.

    Three questions, one row each, because each has a different repair:
    has OpenCode run here, is our plugin the one it loads, and can the GLM
    plan quota be read. ([lines], (Quota, fetched_at) or None).

    The quota is the one network request `blink status` makes, and it is
    made deliberately: there is no GLM usage figure anywhere on disk to
    look at instead (docs/plans/opencode.md), so "is my quota being read?"
    has no offline answer. One synchronous fetch_once(), bounded by its
    timeout, and only where OpenCode holds a plan key -- quota_source()
    says so without handing over the key, so nothing here can print it.
    Its answer is a category ("unreachable", "rejected the key"), never an
    exception's text.

    `others_report` says whether any other source on this desk puts a
    usage figure on the cable. Without one, and without a plan key, the
    daemon sends nothing at all and the board keeps its waiting screen,
    which the no-key line has to say rather than promise a light.
    """
    now = time.time() if now is None else now
    out = []
    here = opencode_present()
    target = install_opencode_plugin.plugin_path(opencode.config_dir())
    mine = install_opencode_plugin.is_ours(target)
    if not here and not mine:
        # Same shape as the Codex row: say where we looked, because the
        # person reading this may well have OpenCode under an XDG_DATA_HOME
        # the daemon's login environment does not share.
        out.append("OpenCode    no database (OpenCode not installed, or never"
                   " run)")
        out.append(f"            looked in {opencode.data_dir()}")
        return out, None
    if here:
        out.append(f"OpenCode    database found in {opencode.data_dir()}")
    out.extend(_opencode_dirs_mismatch())

    if mine:
        try:
            # fetch=False and sweep=False: no thread, no request, and no
            # collecting the slots being counted (_codex_hook_status gives
            # the sweep rule; the fetch rule is _wire_line's).
            frames = opencode.OpenCodeProvider(fetch=False,
                                               sweep=False).poll(now)
            n = sum(f.n_sessions() for f in frames
                    if f.src == opencode.STATE_SRC_ID)
        except Exception:
            n = 0
        try:
            current = install_opencode_plugin.is_current(
                opencode.config_dir(), _opencode_plugin_text())
        except (OSError, UnicodeDecodeError):
            # The bundle is unreadable: nothing to compare against, so say
            # nothing about staleness, as shim_is_current does.
            current = True
        if not current:
            out.append("OpenCode    "
                       + _shim_repair_line("plugin is out of date",
                                           bridge_running))
        else:
            out.append("OpenCode    plugin installed, "
                       + (f"{n} live session{'s' if n != 1 else ''}"
                          if n else "no live sessions"))
            # Nothing written by the plugin in the last hour -- not a slot,
            # not a tombstone -- yet OpenCode's own database says it was
            # used: the plugin is installed and not running.
            # That is how OpenCode 2.0 left the light dark for five weeks --
            # it skips a plugin it cannot load with a log warning only, and
            # this line said "no live sessions" throughout. A restart is
            # the one thing the person can do; a service started before the
            # file existed is the likeliest cause we cannot rule out.
            if (not n and not opencode.plugin_wrote_since(now - 3600)
                    and opencode.recent_v2_sessions(now, within_s=3600)):
                out.append("            but OpenCode was used in the last"
                           " hour and the plugin reported nothing --")
                out.append("            restart OpenCode (`opencode service"
                           " restart`) so it loads the plugin.")
    elif os.path.lexists(target):
        out.append(f"OpenCode    plugin not installed: {target} is not"
                   " Blink's, so it was left alone")
        out.append("            Rename it and run install again to add"
                   " Blink's.")
    else:
        out.append(f"OpenCode    plugin not installed -- run"
                   f" `{installed_bin()} install` to add it")

    if not here:
        return out, None
    # "Its light works" only where it does: our plugin is the one OpenCode
    # loads. Found in review, 2026-10-19: this said so unconditionally, so a
    # desk whose blink.js was somebody else's -- reported two lines above
    # as "plugin not installed" -- was then told its light works.
    #
    # And only where something else puts a number on the cable. Found end to
    # end with real OpenCode 1.19.3 on the real board, 2026-10-19: on a desk
    # with OpenCode alone and no plan key the daemon sent NOTHING -- no usage
    # message, so no session message either, because the state frame rides
    # on a usage frame and no source had one. The board stayed on its
    # waiting screen through a whole permission prompt while this line said
    # the light worked. The fix that is in the person's hands is the key, in
    # OpenCode's own words: `opencode auth login` lists the provider as
    # "Z.AI Coding Plan" ("Zhipu AI Coding Plan" on the mainland host), read
    # from OpenCode 1.19.3's provider list.
    if not others_report:
        no_key = ["OpenCode    no GLM Coding Plan key saved in OpenCode"
                  " -- nothing on this desk reports a usage figure, so the"
                  " board stays on its waiting screen",
                  "            Add the key with `opencode auth login`,"
                  " provider Z.AI Coding Plan, and OpenCode gets gauges."]
    else:
        no_key = ["OpenCode    no GLM Coding Plan key saved in OpenCode"
                  " -- "
                  + ("its light works, there are no usage figures to show"
                     if mine else "there are no usage figures to show")]
    # A token budget turns a keyless desk into one with numbers, so where
    # one is set the no-key lines lose their "nothing to show" half and the
    # budget's own lines say what the rings carry instead.
    #
    # On a desk WITH a plan key the budget is only mentioned when one is
    # set: the plan's quota is that desk's number, and a hint to set up a
    # second one beside it is noise.
    budget_lines = _opencode_budget_lines(now)
    budgeted = opencode_budget.read_budget() is not None
    plan_budget = (budget_lines + [
        "OpenCode    the GLM plan quota is on the rings whenever it can be"
        " read, the budget only when it cannot"] if budgeted else [])
    if budgeted:
        no_key = ["OpenCode    no GLM Coding Plan key saved in OpenCode --"
                  " the rings show its token use against your budget"]
    if opencode.quota_source() is None:
        out.extend(no_key)
        out.extend(budget_lines)
        return out, None
    quota, err = _status_fetch(now)
    if quota is not None:
        out.append(f"OpenCode    {opencode.describe(quota)}")
        out.extend(plan_budget)
        return out, (quota, now)
    if err is None:
        # The key went between the two reads. Not an error to report.
        out.extend(no_key)
        out.extend(budget_lines)
        return out, None
    out.append(f"OpenCode    GLM plan quota could not be read: {err}")
    out.extend(plan_budget)
    return out, None


def _budget_text(budget):
    """"2M per 5 hours, 20M per 7 days" -- the windows that have one."""
    parts = []
    if budget.session_tokens:
        parts.append(f"{opencode_budget.format_tokens(budget.session_tokens)}"
                     " per 5 hours")
    if budget.weekly_tokens:
        parts.append(f"{opencode_budget.format_tokens(budget.weekly_tokens)}"
                     " per 7 days")
    return ", ".join(parts)


def _use_text(used, budget):
    if not budget:
        return f"{opencode_budget.format_tokens(used)} (no budget)"
    pct = opencode_budget.percent(used, budget)
    return (f"{opencode_budget.format_tokens(used)} of"
            f" {opencode_budget.format_tokens(budget)} ({pct:.0f}%)")


def _clock_at(at, now):
    """"at 14:05" today, "Thu 14:05" within the week -- a 7-day window can
    put it days out."""
    t = time.localtime(at)
    if time.strftime("%Y%m%d", t) == time.strftime("%Y%m%d",
                                                   time.localtime(now)):
        return "at " + time.strftime("%H:%M", t)
    return "on " + time.strftime("%a %H:%M", t)


def _opencode_budget_lines(now=None):
    """The token budget's lines for `blink status` and `blink
    opencode-budget`: what is set, what OpenCode has used against it, and
    -- the one case a person cannot see for themselves -- a server that
    never reports usage, so OpenCode writes 0 for every reply and the
    rings stay blank. Measured with real OpenCode 1.19.3 against a server
    whose replies leave `usage` out: every token field of every assistant
    message was 0.

    The database is opened only when a budget is set, and the lines say
    counts and percentages, never anything from a message.
    """
    now = time.time() if now is None else now
    budget = opencode_budget.read_budget()
    if budget is None:
        return ["OpenCode    no token budget set -- `blink opencode-budget"
                " 5h=2M 7d=20M` shows its token use on the rings"]
    out = [f"OpenCode    token budget {_budget_text(budget)}"]
    try:
        _b, use = opencode.OpenCodeProvider(
            fetch=False, sweep=False,
            tokens=opencode_budget.TokenCounter(report=False)
        ).token_use(now)
    except Exception:
        use = None
    if use is None:
        out.append("OpenCode    token use could not be read from its"
                   " database")
        return out
    session = _use_text(use.session_tokens, budget.session_tokens)
    weekly = _use_text(use.weekly_tokens, budget.weekly_tokens)
    out.append(f"OpenCode    used {session} in the last 5 hours,"
               f" {weekly} in the last 7 days")
    # Over budget, the one thing the dial's countdown cannot say on its
    # own: the clock time. Local time, because it is read off a desk.
    for label, at in (("5-hour", use.session_clear_at),
                      ("7-day", use.weekly_clear_at)):
        if at is not None:
            out.append(f"OpenCode    over the {label} budget -- back at it"
                       f" {_clock_at(at, now)} if nothing more is used")
    if opencode_budget.no_counts(use):
        out.append("OpenCode    OpenCode recorded no token counts -- your model"
                   " server may not return usage, so the rings stay blank")
    return out


def cmd_opencode_budget(args) -> int:
    """`blink opencode-budget [5h=N] [7d=N] | off`: the budget the rings
    measure OpenCode's own token use against, on a desk whose model has no
    plan quota (pc/providers/opencode_budget.py).

    Each window given is set and the other is kept, so `5h=3M` alone
    changes one ring; `5h=off` drops one window and `off` the whole budget.
    Written atomically 0600 to ~/.blink/opencode-budget.json, which the
    running daemon re-reads when it changes -- no restart.
    """
    settings = list(getattr(args, "settings", None) or [])
    path = opencode_budget.budget_path()
    if settings == ["off"]:
        removed = opencode_budget.clear_budget(path)
        print("OpenCode token budget cleared." if removed
              else "No OpenCode token budget was set.")
        return 0
    if settings:
        current = opencode_budget.read_budget(path) or opencode_budget.Budget(
            None, None)
        fields = current._asdict()
        names = {"5h": "session_tokens", "7d": "weekly_tokens",
                 "week": "weekly_tokens"}
        for item in settings:
            key, sep, value = item.partition("=")
            field = names.get(key.strip().lower())
            if not sep or field is None:
                print(f"blink: {item!r} is not a setting -- use 5h=2M, 7d=20M"
                      " or off", file=sys.stderr)
                return 2
            if value.strip().lower() == "off":
                fields[field] = None
                continue
            try:
                fields[field] = opencode_budget.parse_amount(value)
            except ValueError as e:
                print(f"blink: {e}", file=sys.stderr)
                return 2
        budget = opencode_budget.Budget(**fields)
        try:
            opencode_budget.write_budget(budget, path)
        except OSError as e:
            print(f"blink: could not write {path} ({e.strerror or e})",
                  file=sys.stderr)
            return 1
        if not (budget.session_tokens or budget.weekly_tokens):
            print("OpenCode token budget cleared.")
            return 0
        print(f"OpenCode token budget set: {_budget_text(budget)}."
              " The panel picks it up within a few seconds.")
    else:
        budget = opencode_budget.read_budget(path)
        if budget is None:
            print("No OpenCode token budget set. Set one with"
                  " `blink opencode-budget 5h=2M 7d=20M`.")
            return 0
        print(f"OpenCode token budget: {_budget_text(budget)}.")
    if opencode_present():
        for line in _opencode_budget_lines()[1:]:
            print(line)
    return 0


def cmd_update(_args) -> int:
    """Fetch and install a newer daemon, then restart the service.

    Manual by design for now. The machinery for doing it unattended is all
    here, but it is gated on `daemon.auto` in the signed manifest so that the
    first release to switch it on is a decision someone makes, not a side
    effect of shipping this command.
    """
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
    if restart_left_it_down(detail):
        # The program on disk is the new one; what is missing is a process.
        # Say which half worked, because "update failed" would send someone
        # to download it again -- and downloading it again would not help.
        print()
        print("The new version is installed, but the background service is")
        print("not running, so the board will stay on \"Link the PC daemon\".")
        print(f"Run `{installed_bin()} install` to finish.")
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

    stopped = False
    if not _skip_service():
        # Stop by pid first: the login service may not be registered on a
        # bench machine, and the pid file is the thing that is true either way.
        _kill_recorded_daemon()
        try:
            backend().remove()
            # Put a service back afterwards only where one belongs: on a
            # machine Blink is INSTALLED on. remove() reports "removed"
            # whether or not anything was registered, and treating that as
            # proof of a service left a bench laptop -- running the
            # downloaded binary, never installed -- with a login item
            # pointing at ~/.blink/bin/blink, which does not exist there,
            # respawning on every throttle interval forever.
            stopped = os.path.exists(installed_bin())
        except Exception:
            pass
        time.sleep(1.0)

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
        if stopped:
            print("Background service ... " + _install_service())
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
            print(f"No hello from the board on {port} within 10 s.",
                  file=sys.stderr)
            ser.close()
            if stopped:
                print("Background service ... " + _install_service())
            return 1

        ser.write(protocol.encode(protocol.edition(args.edition)))
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
        # The service goes back in the SAME finally that closes the port.
        #
        # It used to be restored only on the happy path and the two early
        # returns, so a nudged cable, a CH340 re-enumerating, or Ctrl-C during
        # the ten-second hello wait propagated straight past it: the launchd
        # plist / systemd unit had already been deleted, nothing restarted the
        # daemon at next login, and nothing said so. On an assembly line that
        # is a unit boxed with a dead bridge.
        #
        # Idempotent by construction -- `stopped` is cleared here, so the tail
        # of the function cannot install it twice.
        if stopped:
            print("Background service ... " + _install_service())
            stopped = False

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

    # Normally already done by the finally above; kept for the paths that
    # never reached it.
    if stopped:
        print("Background service ... " + _install_service())
    return rc


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
    budget_p = sub.add_parser(
        "opencode-budget",
        help="Show OpenCode's token use against a budget you set")
    budget_p.add_argument(
        "settings", nargs="*", metavar="SETTING",
        help="5h=2M and/or 7d=20M (K and M allowed), 5h=off, or off;"
             " none to show the budget")
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
        "opencode-budget": cmd_opencode_budget,
        "run": cmd_run,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
