"""One binary: the setup, the status check, and the bridge itself.

A customer downloads a single file and runs it. There is no Python to install,
no virtualenv to build, no repository to clone, and nothing left behind that
they have to keep in place -- `clauge install` copies the binary into
~/.clauge/bin and points the login service at that copy, so the download is
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
import os
import shutil
import subprocess
import sys
import time

from pc import install_statusline, update
from pc.version import RELEASE_VERSION

# Resolved per call, not at import. These used to be module constants, which
# meant every path was fixed by whatever HOME happened to be when the module
# loaded -- untestable without a subprocess, and quietly wrong for any caller
# that changes HOME.
LABEL = "com.clauge.bridge"


def _home():
    return os.path.expanduser("~")


def clauge_home():
    return os.path.join(_home(), ".clauge")


def bin_dir():
    return os.path.join(clauge_home(), "bin")


def installed_bin():
    # .exe on Windows: without the extension the copy is not executable, and
    # the Scheduled Task would register a path Windows refuses to launch.
    name = "clauge.exe" if sys.platform == "win32" else "clauge"
    return os.path.join(bin_dir(), name)


def shim_path():
    return os.path.join(clauge_home(), "clauge-statusline.sh")


def log_path():
    return os.path.join(clauge_home(), "bridge.log")


def settings_path():
    return os.path.join(_home(), ".claude", "settings.json")


def plist_path():
    return os.path.join(_home(), "Library", "LaunchAgents", LABEL + ".plist")


def unit_path():
    return os.path.join(_home(), ".config", "systemd", "user",
                        "clauge-bridge.service")


# Windows has no launchd and no systemd. A Scheduled Task with an at-logon
# trigger is the equivalent that needs no admin rights and no service wrapper.
TASK_NAME = "Clauge bridge"

# The oldest Claude Code that carries usage figures in its status line payload.
# 2.1.0 does not carry rate_limits at all; 2.1.100 does. Below this every step
# of the install succeeds and the panel stays blank forever.
MIN_CLAUDE = (2, 1, 100)

# Set by the tests to skip registering a real login service. The launchd label
# and the systemd unit name are constants while everything else is scoped to
# $HOME, so without this a test under a temporary HOME still boots out the
# real agent of whoever is logged in.
def _skip_service():
    return os.environ.get("CLAUGE_SKIP_SERVICE") == "1"


def _frozen() -> bool:
    return getattr(sys, "frozen", False)


def _self_path() -> str:
    """The binary to copy into place. sys.executable is the frozen binary."""
    return sys.executable if _frozen() else os.path.abspath(sys.argv[0])


def _shim_source() -> str:
    """The shim's text, from the bundle when frozen, the tree when not.

    One source of truth either way -- tools/clauge-statusline.sh is what the
    build embeds, so the shipped shim and the one in the repository cannot
    drift apart.
    """
    if _frozen():
        base = getattr(sys, "_MEIPASS", os.path.dirname(_self_path()))
        return open(os.path.join(base, "clauge-statusline.sh")).read()
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return open(os.path.join(here, "tools", "clauge-statusline.sh")).read()


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
    parts = []
    for piece in head.split(".")[:3]:
        parts.append(int(piece) if piece.isdigit() else 0)
    while len(parts) < 3:
        parts.append(0)
    return (out or None), tuple(parts)


def _warn_if_claude_too_old():
    text, ver = claude_version()
    if ver is None or ver >= MIN_CLAUDE:
        return
    # A warning, not a refusal. Everything installed is correct and stays
    # correct, so the moment they update it starts working with nothing to
    # redo; refusing would make them run this again for no reason.
    m = ".".join(str(n) for n in MIN_CLAUDE)
    print()
    print(f"  !! Your Claude Code is {text}, and Clauge needs {m} or newer.")
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

_UNIT_TEMPLATE = """[Unit]
Description=Clauge USB bridge

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

    Frozen, that is the copy in ~/.clauge/bin. From a checkout it is this
    interpreter and module -- there is no single-file binary to point at, and
    a developer running from source should still get a working service rather
    than one aimed at a path that does not exist.
    """
    if _frozen():
        return [installed_bin(), "run"]
    return [sys.executable, "-m", "pc.cli", "run"]


def _install_service() -> str:
    if _skip_service():
        return "skipped (CLAUGE_SKIP_SERVICE=1)"
    if sys.platform == "darwin":
        os.makedirs(os.path.dirname(plist_path()), exist_ok=True)
        with open(plist_path(), "w") as f:
            args = "".join(f"    <string>{_xml_escape(a)}</string>\n"
                           for a in _service_command())
            f.write(_PLIST_TEMPLATE.format(label=LABEL, args=args,
                                           log=_xml_escape(log_path())))
        uid = os.getuid()
        # bootout first so a rerun replaces the running agent rather than
        # failing with "service already loaded".
        subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                       capture_output=True)
        # ...and then retry, because bootout is asynchronous. The bootstrap
        # immediately after it can fail while launchd is still tearing the old
        # job down, which left a reinstall with no service running at all --
        # observed on the second install of the day, silently.
        for attempt in range(4):
            r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", plist_path()],
                               capture_output=True)
            if r.returncode == 0:
                break
            time.sleep(0.5 * (attempt + 1))
        if r.returncode == 0:
            return "running (launchd)"
        return f"installed, but could not be started: launchctl bootstrap gui/{uid} {plist_path()}"
    if sys.platform == "win32":
        # /f overwrites a task from an earlier install rather than failing.
        # /sc onlogon needs no admin rights; a Windows service would.
        r = subprocess.run(
            ["schtasks", "/create", "/f", "/tn", TASK_NAME, "/sc", "onlogon",
             "/tr", f'"{installed_bin()}" run'],
            capture_output=True, text=True)
        if r.returncode != 0:
            return f"could not register a Scheduled Task: {r.stderr.strip()[:120]}"
        # /sc onlogon does not start it now, only at the next logon.
        subprocess.run(["schtasks", "/run", "/tn", TASK_NAME], capture_output=True)
        return "running (Scheduled Task)"
    if sys.platform.startswith("linux"):
        os.makedirs(os.path.dirname(unit_path()), exist_ok=True)
        with open(unit_path(), "w") as f:
            f.write(_UNIT_TEMPLATE.format(
                command=" ".join(_service_command())))
        if not shutil.which("systemctl"):
            return f"no systemd here; run it yourself: {installed_bin()} run"
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now",
                            "clauge-bridge.service"], capture_output=True)
        if r.returncode == 0:
            return "running (systemd)"
        return "installed, but could not be started: systemctl --user enable --now clauge-bridge"
    return f"not supported on {sys.platform}; run it yourself: {installed_bin()} run"


def restart_service() -> str:
    """Bounce the login service so it comes up on a freshly replaced binary.

    Not the same as exiting and letting the supervisor notice: this is called
    from `clauge update`, which is a separate process from the daemon. The
    daemon's own path is simpler -- on macOS and Linux it exits and KeepAlive /
    Restart=always bring it back. Windows has neither: a Scheduled Task with an
    onlogon trigger does not restart anything, so it is told explicitly.
    """
    if _skip_service():
        return "skipped (CLAUGE_SKIP_SERVICE=1)"
    if sys.platform == "darwin":
        r = subprocess.run(["launchctl", "kickstart", "-k",
                            f"gui/{os.getuid()}/{LABEL}"], capture_output=True)
        return "restarted" if r.returncode == 0 else "could not restart it"
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME],
                       capture_output=True)
        r = subprocess.run(["schtasks", "/run", "/tn", TASK_NAME],
                           capture_output=True)
        return "restarted" if r.returncode == 0 else "could not restart it"
    if sys.platform.startswith("linux") and shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "restart",
                            "clauge-bridge.service"], capture_output=True)
        return "restarted" if r.returncode == 0 else "could not restart it"
    return "not running under a supervisor; restart it yourself"


def _remove_service() -> str:
    if _skip_service():
        return "skipped (CLAUGE_SKIP_SERVICE=1)"
    if sys.platform == "darwin":
        subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}/{LABEL}"],
                       capture_output=True)
        _rm(plist_path())
        return "removed"
    if sys.platform == "win32":
        subprocess.run(["schtasks", "/end", "/tn", TASK_NAME], capture_output=True)
        subprocess.run(["schtasks", "/delete", "/f", "/tn", TASK_NAME],
                       capture_output=True)
        return "removed"
    if sys.platform.startswith("linux"):
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "disable", "--now",
                            "clauge-bridge.service"], capture_output=True)
        _rm(unit_path())
        if shutil.which("systemctl"):
            subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        return "removed"
    return "nothing to remove"


def _rm(path):
    try:
        os.remove(path)
    except OSError:
        pass


# -------------------------------------------------------------------- install


def _make_way_for_copy():
    """Windows will not let us overwrite an executable that is running.

    And by the second install it IS running: the first one registered a
    Scheduled Task and started it, so ~/.clauge/bin/clauge.exe is locked and
    shutil.copy2 raises PermissionError. That is not an edge case -- it is
    what happens to every customer who re-runs the installer to upgrade.

    Windows does allow a running executable to be RENAMED, so move it aside
    and copy into the freed name. The leftover is deleted on the next run,
    once nothing has it open any more; uninstall takes the whole directory.
    """
    if sys.platform != "win32":
        return
    target = installed_bin()
    stale = target + ".old"
    try:
        os.remove(stale)
    except OSError:
        pass          # still locked from a previous upgrade; harmless
    if os.path.exists(target):
        try:
            os.replace(target, stale)
        except OSError:
            pass      # nothing running holds it; the copy will just overwrite


def _announce():
    """Say what is about to change, before changing it.

    Install is deliberately unattended -- it asks nothing, because plugging the
    board in is meant to be the whole setup. That makes disclosure the only
    thing standing between us and silently editing a file the customer owns,
    so it is not optional and it runs before the first write.
    """
    print("Clauge setup. Here is everything it is about to do, before it does any of it.")
    print()
    print(f"  Creates    {installed_bin()}")
    print("             a copy of this program, so the file you downloaded")
    print("             can be deleted when this finishes.")
    print(f"  Creates    {shim_path()}")
    print("             the small script Claude Code runs to hand over the")
    print("             usage figures.")
    print(f"  Changes    {settings_path()}")
    print("             the statusLine.command key, and nothing else in the file.")
    previous = install_statusline._load(settings_path()).get("statusLine") or {}
    prev_cmd = previous.get("command", "")
    if prev_cmd:
        print(f"             Your current command is kept and still runs:")
        print(f"               {prev_cmd}")
    if sys.platform == "darwin":
        print(f"  Creates    {plist_path()}")
    elif sys.platform.startswith("linux"):
        print(f"  Creates    {unit_path()}")
    elif sys.platform == "win32":
        print(f"  Creates    a Scheduled Task named \"{TASK_NAME}\"")
    print("             so the bridge starts when you log in.")
    print()
    print("  It reads or stores nothing else -- no credential, no token, no")
    print("  account data. The usage figures come from Claude Code, which has")
    print("  already worked them out.")
    print()
    print(f"  To undo all of it:  {installed_bin()} uninstall")
    print()


def cmd_install(_args) -> int:
    _announce()

    print("[1/3] Program ... ", end="", flush=True)
    os.makedirs(bin_dir(), exist_ok=True)
    if _frozen():
        src = _self_path()
        # Copying onto a running binary is fine on macOS and Linux -- the old
        # inode stays alive for whoever has it open -- but only when it is not
        # literally the same path, or a re-run of the installed copy would
        # truncate itself mid-execution.
        if os.path.abspath(src) != os.path.abspath(installed_bin()):
            _make_way_for_copy()
            shutil.copy2(src, installed_bin())
            os.chmod(installed_bin(), 0o755)
        print(installed_bin())
    else:
        # From a checkout there is nothing to copy; the service points at this
        # interpreter instead. Customers never take this path.
        print("running from a checkout, nothing to copy")

    print("[2/3] Status line ... ", end="", flush=True)
    os.makedirs(clauge_home(), exist_ok=True)
    with open(shim_path(), "w") as f:
        f.write(_shim_source())
    os.chmod(shim_path(), 0o755)
    install_statusline._announce(settings_path(), shim_path(),
                                 undo_hint=f"{installed_bin()} uninstall")
    print("      " + install_statusline.install(settings_path(), shim_path()))

    print("[3/3] Background service ... ", end="", flush=True)
    print(_install_service())

    print()
    print("Done. Plug the board in over USB -- it picks it up on its own.")
    print(f"  Log:     {log_path()}")
    print(f"  Check:   {installed_bin()} status")
    print(f"  Undo:    {installed_bin()} uninstall")
    print()
    print("  You can delete the file you downloaded.")
    _warn_if_claude_too_old()
    return 0


def cmd_uninstall(_args) -> int:
    print("Clauge uninstall.")
    print()
    print("[1/3] Background service ... ", end="", flush=True)
    print(_remove_service())

    print("[2/3] Claude Code setting:")
    print("      " + install_statusline.uninstall(settings_path(), shim_path()))

    print("[3/3] Files ... ", end="", flush=True)
    # Only what install created. NOT ~/.clauge itself: it also holds the OTA
    # signing key, which cannot be regenerated -- every board already flashed
    # with its public half would stop accepting updates.
    shutil.rmtree(bin_dir(), ignore_errors=True)
    for p in (shim_path(), os.path.join(clauge_home(), "statusline.json"),
              os.path.join(clauge_home(), "statusline.json.tmp")):
        _rm(p)
    print("removed")
    print()
    print("Done. Nothing of Clauge's is left running.")
    return 0


def cmd_status(_args) -> int:
    if _skip_service():
        # The launchd label and systemd unit name are global while everything
        # else is scoped to $HOME, so querying them under a test HOME reports
        # the real user's agent -- which read as "installed" for an install
        # that never happened.
        print("Bridge      not checked (CLAUGE_SKIP_SERVICE=1)")
    elif sys.platform == "darwin":
        r = subprocess.run(["launchctl", "print", f"gui/{os.getuid()}/{LABEL}"],
                           capture_output=True)
        print("Bridge      " + ("registered with launchd" if r.returncode == 0
                                else "not installed"))
    elif sys.platform == "win32":
        r = subprocess.run(["schtasks", "/query", "/tn", TASK_NAME],
                           capture_output=True)
        print("Bridge      " + ("registered as a Scheduled Task"
                                if r.returncode == 0 else "not installed"))
    elif sys.platform.startswith("linux") and shutil.which("systemctl"):
        r = subprocess.run(["systemctl", "--user", "is-active", "--quiet",
                            "clauge-bridge.service"], capture_output=True)
        print("Bridge      " + ("running" if r.returncode == 0 else "not running"))
    else:
        print(f"Bridge      unknown on {sys.platform}")

    print(f"App         {RELEASE_VERSION}")

    text, ver = claude_version()
    if ver is None:
        print("Claude Code not found on PATH")
    elif ver < MIN_CLAUDE:
        m = ".".join(str(n) for n in MIN_CLAUDE)
        print(f"Claude Code {text} -- TOO OLD, needs {m}+ (panel will stay blank)")
    else:
        print(f"Claude Code {text}")

    print("Status line " + (f"installed at {shim_path()}" if os.path.exists(shim_path())
                            else "not installed"))

    # The most useful support answer: is fresh data actually arriving?
    payload = os.path.join(clauge_home(), "statusline.json")
    if os.path.exists(payload):
        age = int(time.time() - os.path.getmtime(payload))
        if age < 120:
            print(f"Usage data  fresh ({age}s old)")
        else:
            print(f"Usage data  {age}s old -- open Claude Code to refresh it")
    else:
        print("Usage data  none yet -- open Claude Code once so it renders "
              "its status line")
    return 0


def cmd_update(_args) -> int:
    """Fetch and install a newer daemon, then restart the service.

    Manual by design for now. The machinery for doing it unattended is all
    here, but it is gated on `daemon.auto` in the signed manifest so that the
    first release to switch it on is a decision someone makes, not a side
    effect of shipping this command.
    """
    print(f"Clauge update. This app is {RELEASE_VERSION}.")
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
    ok, message = update.apply(blob, installed_bin(), version)
    print(message)
    if not ok:
        return 1
    print("Background service ... " + restart_service())
    return 0


def cmd_run(args) -> int:
    """The daemon. This is what the login service starts.

    Its arguments are handed over explicitly. Calling main() with none left it
    parsing sys.argv itself, which in the packaged binary begins with the
    subcommand name "run" -- rejected on the spot, restarted by the service ten
    seconds later, forever.
    """
    import claude_usage_bridge
    forwarded = []
    if getattr(args, "port", None):
        forwarded += ["--port", args.port]
    if getattr(args, "baud", None):
        forwarded += ["--baud", str(args.baud)]
    claude_usage_bridge.main(forwarded)
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="clauge", description="Clauge desk gauge: setup and bridge.")
    # Also the self-test in pc/update.py: a replacement binary has to run and
    # say what it is before it is allowed to become the login service's target.
    parser.add_argument("--version", action="version",
                        version=f"clauge {RELEASE_VERSION}")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("install", help="set everything up (default)")
    sub.add_parser("uninstall", help="put it all back")
    sub.add_parser("status", help="is the panel getting data?")
    sub.add_parser("update", help="fetch a newer version of this app")
    run_p = sub.add_parser("run", help="run the bridge in the foreground")
    run_p.add_argument("--port", default=None,
                       help="serial port (default: find the board)")
    run_p.add_argument("--baud", type=int, default=115200)
    args = parser.parse_args(argv)

    # Bare `./clauge` installs. Someone who just downloaded a file and
    # double-clicked it meant "set this up", and making them discover a
    # subcommand first is the opposite of the point.
    return {
        None: cmd_install,
        "install": cmd_install,
        "uninstall": cmd_uninstall,
        "status": cmd_status,
        "update": cmd_update,
        "run": cmd_run,
    }[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
