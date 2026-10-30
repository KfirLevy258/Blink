"""Run the fleet agent on every desk in an inventory, and add it up.

    python tools/fleet/run.py                       # the whole fleet
    python tools/fleet/run.py --only galit-win10    # one desk
    python tools/fleet/run.py --scenarios overage   # one scenario, everywhere
    python tools/fleet/run.py --inventory tools/fleet/fleet-vm.toml  # the VMs

Each desk gets a snapshot of THIS checkout at HEAD (`git archive` piped into
`tar -x` over ssh), runs tests/fleet/agent.py against its own board, and hands
back a result.json. The three run at the same time -- one board per desk, no
shared state -- and the verdicts land in .fleet/last_run.json, which the
release gate reads.

The inventory is fleet.toml next to this file. It names an interpreter per
desk rather than assuming `python3`, because the agent spawns the daemon and
the daemon imports serial at module scope: on this Mac only .venv-test has
pyserial, and a system python would fail as a board fault.

Everything here is written around one question -- can this print PASS when it
did not actually prove anything? -- and the four answers it has to keep shut:

  - A desk that never ran must never read as a desk that passed. An
    unreachable machine, a missing interpreter, a hung ssh and a truncated
    result all return {"ok": False, "problems": [...]}; run_host raises
    nothing at all. A host that produced no result is still a key in the
    output, holding a sentence, because a missing key is invisible and an
    honest failure is not.

  - The workdir survives between runs, so result.json from LAST week is
    sitting on the remote before this run starts. If the agent dies before
    writing, pulling that file back would report last week's green against
    today's commit. It is deleted on the remote, in the same command that
    unpacks the snapshot, before the agent is started.

  - .fleet/last_run.json is removed before the inventory is even read, and
    written atomically at the end from a finally. A run that dies halfway --
    or that gives up on a typo in --only -- leaves no file rather than the
    previous green one, and the gate cannot read a half-written one. The
    report is printed from a try of its own, because a fault in the table
    must not be the last word over a verdict already on disk.

  - An ssh that hangs -- a desk asleep, a key that needs a passphrase, a
    machine mid-reboot -- would otherwise hold the gate open forever. Every
    call has a timeout, and BatchMode=yes turns a password prompt into an
    immediate error instead of a wait.

On the Windows desk the remote shell is cmd.exe, which costs three things.
%USERPROFILE% there is non-ASCII, so it is never expanded here: the command
line says %USERPROFILE% and cmd expands it, and no Hebrew ever crosses an ssh
command line whose encoding nothing in the chain agrees on. Windows sshd runs
the command as `cmd.exe /c <line>`, and cmd strips the outer quotes of a line
that BEGINS with one, so every line built here begins with a bare verb (cd,
mkdir). And %errorlevel% inside a one-liner is expanded when the line is
parsed, not when it runs, so it always reads 0: the exit code that is
believed is the one ssh itself returns, which is the last command's.

The VM fleet (fleet-vm.toml, `--inventory tools/fleet/fleet-vm.toml`) is
the same run with one board instead of three. This Mac goes first, then each
UTM VM in turn: its snapshot is pushed while the board is still here, the
board is handed to it (`utmctl usb connect`, or the operator from the VM
window's USB menu when utmctl cannot), the agent is started only once the
guest can see the board, and the board is handed back from a finally. This
Mac's installed service is held stopped for that whole phase, because a port
somebody has open is not a device UTM can take. The run records that it was
a VM run (`fleet.kind`), and the release gate refuses to read it as the desk
fleet. Two VM-only wrinkles in the transport: each VM needs its own ssh key,
so a host may carry `ssh_options`, and the Windows VM's sshd lands in
PowerShell, which hands stdin to native programs as text -- so for a host
with shell = "powershell" nothing is piped at all, and the snapshot and each
cmd.exe line travel as files by scp.
"""
import argparse
import concurrent.futures
import ipaddress
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INVENTORY = Path(__file__).resolve().parent / "fleet.toml"
DEFAULT_STATE = REPO_ROOT / ".fleet" / "last_run.json"

# The agent writes here, relative to the workdir it is started in. Relative on
# purpose: an absolute path would have to carry the Windows desk's non-ASCII
# profile name through the ssh command line.
RESULT_NAME = "result.json"

REQUIRED_KEYS = ("ssh", "os", "board", "python", "workdir",
                 "has_claude_desktop")
KNOWN_OS = ("darwin", "linux", "windows")
KNOWN_SHELLS = ("cmd", "powershell")

# What kind of fleet an inventory describes. The desks are real machines a
# customer could own; "vm" is this Mac and its UTM guests sharing one board.
# Recorded in the result so the gate can refuse to read one as the other.
KNOWN_KINDS = ("desks", "vm")
DEFAULT_KIND = "desks"

# The placeholder a VM host's ssh line carries, filled in at run time: UTM's
# shared network renumbers its guests, so an address written down here works
# until it does not, and then fails as a dead desk.
ADDRESS = "{address}"

# Where macOS's own DHCP server (bootpd, which UTM's shared network uses)
# writes its leases. The fallback for a guest whose agent does not answer:
# utmctl ip-address answered OSStatus -1712 (an AppleEvent timeout) for a VM
# that was booting, on 2026-10-30.
DHCP_LEASES = Path("/var/db/dhcpd_leases")

# utmctl drives UTM by AppleEvent, and an AppleEvent to a busy UTM times out
# at about two minutes (-1712) rather than failing fast. Past that it is not
# coming back.
UTM_TIMEOUT_S = 150.0

# Seconds for the guest to enumerate the board after it is handed over, and
# for this Mac to see it again after it is handed back. USB redirection plus
# a driver bind is seconds, not minutes; ninety is room for a Windows guest
# installing the device for the first time.
BOARD_WAIT_S = 90.0

# The files a PowerShell host is sent instead of a pipe, relative to its
# home directory -- which is where both scp and Windows sshd start.
PS_STEP = "blink-fleet-step.cmd"
PS_SNAPSHOT = "blink-fleet-snapshot.tar"

# BatchMode turns "please enter a passphrase" into an error rather than a
# machine sitting at a prompt until somebody notices the release stalled.
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=20"]

PUSH_TIMEOUT_S = 600.0
PULL_TIMEOUT_S = 300.0
GIT_TIMEOUT_S = 60.0
# Five scenarios with their silences, plus two customer-path scenarios that
# unpack 50 MB and run an installer that self-tests the copy it made.
#
# It has to sit OUTSIDE the agent's own worst case, not on top of it. The
# customer path runs first and allows BUNDLE_TIMEOUT_S per program call for
# BUNDLE_CALLS calls (tests/fleet/agent.py) -- 4200 s -- which used to be
# exactly this constant: a slow update_path could then eat the entire desk
# budget, the four board scenarios would never run, and this end would report
# a timeout instead of a result. The two are pinned against each other by a
# test. What is left over here is ~50 minutes for the board passes, which
# take five to eight. Generous on purpose: a timeout is reported as a failed
# release, and slow is not broken.
AGENT_TIMEOUT_S = 7200.0

# How much of a failing command's own output is worth repeating. Enough for
# the sentence that names the cause, short enough that the table stays a
# table.
TAIL_CHARS = 600


# ---------------------------------------------------------------------------
# The inventory
# ---------------------------------------------------------------------------

def load_inventory(path=DEFAULT_INVENTORY):
    """Read fleet.toml. Every complaint names the host and the missing key.

    A malformed inventory is refused here rather than halfway through a run:
    the alternative is stopping two desks' services, discovering the third
    has no `python` line, and leaving somebody's board dark over a typo.

    Two entries naming the same machine are refused for the same reason and
    then some. main() submits every desk to the thread pool at once, so a
    duplicated `ssh` -- or two entries with none, which both mean THIS machine
    -- puts two agents on one desk: both stopping and starting the same login
    service, both unpacking into the same workdir, and both asking the daemon
    for the one serial port. The second one's failures then read as a board
    fault on a board that was busy answering the first.
    """
    path = Path(path)
    doc = _read_inventory(path)
    hosts = doc.get("hosts") or {}
    if not hosts:
        raise ValueError(f"The fleet inventory at {path} names no hosts."
                         f" It needs a [hosts.<name>] table per desk.")
    settings = _settings(doc, path)
    machines = {}
    for name, cfg in hosts.items():
        if not isinstance(cfg, dict):
            raise ValueError(f"Host {name} in {path} is not a table.")
        missing = [key for key in REQUIRED_KEYS if key not in cfg]
        if missing:
            raise ValueError(
                f"Host {name} in {path} is missing"
                f" {', '.join(missing)}. Every desk needs all of"
                f" {', '.join(REQUIRED_KEYS)}.")
        if cfg["os"] not in KNOWN_OS:
            raise ValueError(
                f"Host {name} in {path} has os = {cfg['os']!r}, which this"
                f" orchestrator has no shell for. Use one of"
                f" {', '.join(KNOWN_OS)}.")
        _check_vm_fields(name, cfg, settings, path)
        where = str(cfg["ssh"]).strip()
        if is_vm(cfg):
            # Every VM's ssh line is the same placeholder, so the machine is
            # the VM itself.
            where = f"{where} utm:{cfg['vm']['utm']}"
        if where in machines:
            first = machines[where]
            which = (f"ssh = {where!r}" if where else
                     "no ssh line, which means this machine")
            raise ValueError(
                f"Hosts {first} and {name} in {path} both have {which}, so"
                f" they are one desk written down twice. Both would be run at"
                f" the same time: two agents stopping and starting the same"
                f" login service, unpacking into the same workdir and"
                f" competing for one serial port, with the loser's failures"
                f" reading as a dead board. Give each desk one entry.")
        machines[where] = name
    return hosts


def _read_inventory(path):
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        raise ValueError(f"The fleet inventory could not be read: {e}."
                         f" It should be a TOML file at {path}.") from e
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"The fleet inventory at {path} is not valid"
                         f" TOML: {e}") from e


def load_settings(path=DEFAULT_INVENTORY):
    """The inventory's [fleet] table, with its defaults filled in.

    fleet.toml has none, and reads as a fleet of real desks run in parallel
    -- which is what it always was.
    """
    return _settings(_read_inventory(path), Path(path))


def _settings(doc, path):
    fleet = doc.get("fleet", {})
    if not isinstance(fleet, dict):
        raise ValueError(f"[fleet] in {path} is not a table.")
    kind = fleet.get("kind", DEFAULT_KIND)
    if kind not in KNOWN_KINDS:
        raise ValueError(
            f"[fleet] in {path} has kind = {kind!r}. It is one of"
            f" {', '.join(KNOWN_KINDS)}: the release gate reads it, so a"
            f" kind nothing knows would be a claim nothing checks.")
    sequential = fleet.get("sequential", False)
    if not isinstance(sequential, bool):
        raise ValueError(f"[fleet] in {path} has sequential ="
                         f" {sequential!r}; it is true or false.")
    usb = fleet.get("board_usb")
    if usb is not None and not _usb_id(usb):
        raise ValueError(
            f"[fleet] in {path} has board_usb = {usb!r}. It is the board's"
            f" USB id as VID:PID in hex, e.g. \"1A86:7523\".")
    host_python = fleet.get("host_python")
    if host_python is not None and not isinstance(host_python, str):
        raise ValueError(f"[fleet] in {path} has host_python ="
                         f" {host_python!r}; it is a path.")
    return {"kind": kind, "sequential": sequential, "board_usb": usb,
            "host_python": host_python}


def _usb_id(text):
    """(vid, pid) from "1A86:7523", or None for anything else."""
    if not isinstance(text, str):
        return None
    parts = text.split(":")
    if len(parts) != 2 or not all(len(p) == 4 for p in parts):
        return None
    try:
        return int(parts[0], 16), int(parts[1], 16)
    except ValueError:
        return None


def _check_vm_fields(name, cfg, settings, path):
    """The optional fields only the VM fleet uses, refused if malformed.

    Refused here and not at the handoff for the reason load_inventory gives:
    by the handoff, this Mac's desk has already been run and its service
    stopped, and a typo is not worth an hour.
    """
    options = cfg.get("ssh_options", [])
    if not (isinstance(options, list)
            and all(isinstance(o, str) for o in options)):
        raise ValueError(f"Host {name} in {path} has ssh_options that are"
                         f" not a list of strings.")
    shell = cfg.get("shell", "cmd")
    if shell not in KNOWN_SHELLS:
        raise ValueError(f"Host {name} in {path} has shell = {shell!r}. It"
                         f" is one of {', '.join(KNOWN_SHELLS)}.")
    if shell != "cmd" and cfg["os"] != "windows":
        raise ValueError(f"Host {name} in {path} names shell = {shell!r},"
                         f" which only means something on windows.")
    vm = cfg.get("vm")
    placeholder = ADDRESS in str(cfg["ssh"])
    if vm is None:
        if placeholder:
            raise ValueError(
                f"Host {name} in {path} has {ADDRESS} in its ssh line but no"
                f" [hosts.{name}.vm] table to find the address from.")
        return
    if not isinstance(vm, dict) or not isinstance(vm.get("utm"), str) \
            or not vm["utm"].strip():
        raise ValueError(
            f"Host {name} in {path} has a vm table without utm, the VM's"
            f" UUID from `utmctl list`. A name is not enough: UTM will"
            f" happily register one bundle three times under one name.")
    if "hostname" in vm and not isinstance(vm["hostname"], str):
        raise ValueError(f"Host {name} in {path} has a vm hostname that is"
                         f" not a string.")
    if not cfg["ssh"]:
        raise ValueError(f"Host {name} in {path} is a VM with no ssh line.")
    if not settings["sequential"]:
        raise ValueError(
            f"Host {name} in {path} is a VM, and VMs share this Mac's board,"
            f" so the inventory needs sequential = true under [fleet]. Run"
            f" side by side, two desks would be handed the same board.")
    if not settings["board_usb"] or not settings["host_python"]:
        raise ValueError(
            f"Host {name} in {path} is a VM, so [fleet] needs board_usb (the"
            f" id the board is handed over by) and host_python (an"
            f" interpreter here with pyserial, to see it come back).")
    if settings["kind"] != "vm":
        # The kind is what the release gate reads, and here it is known for
        # certain rather than declared: an inventory with a VM in it that
        # called itself the desks would record a VM run as a desk run.
        raise ValueError(
            f"Host {name} in {path} is a VM, so the inventory is a VM fleet"
            f" and needs kind = \"vm\" under [fleet]. The release gate reads"
            f" that kind, and a VM run recorded as the desks would be"
            f" accepted as them.")


def is_local(cfg):
    """A desk with no ssh line is this machine, and gets no ssh."""
    return not cfg.get("ssh")


def is_vm(cfg):
    """A desk that is a UTM guest on this Mac, borrowing its board."""
    return isinstance(cfg.get("vm"), dict)


def _powershell(cfg):
    return cfg["os"] == "windows" and cfg.get("shell", "cmd") == "powershell"


def _ssh(cfg):
    return ["ssh", *SSH_OPTS, *cfg.get("ssh_options", []), cfg["ssh"]]


def local_workdir(cfg):
    return Path(os.path.expanduser(cfg["workdir"]))


# ---------------------------------------------------------------------------
# Command lines
# ---------------------------------------------------------------------------

def _win_workdir(cfg):
    """cmd.exe's spelling of the workdir, with the variable left unexpanded.

    A workdir written $USERPROFILE\\... is accepted and rewritten: that is
    the POSIX habit, and being strict about it here would only produce a
    remote directory literally named $USERPROFILE.
    """
    raw = cfg["workdir"].replace("$USERPROFILE", "%USERPROFILE%")
    return raw.rstrip("\\")


def shell_path(cfg, *parts):
    """The workdir, plus optional components, as one token for that shell.

    The tilde is the subtlety: '~/blink-fleet' quoted is a directory NAMED
    tilde, because sh expands the tilde before it expands quotes and never
    inside them. "$HOME" survives double quotes, so that is what is sent.
    """
    if cfg["os"] == "windows":
        return '"' + "\\".join([_win_workdir(cfg), *parts]) + '"'
    workdir = cfg["workdir"]
    if workdir == "~" or workdir.startswith("~/"):
        tail = "/".join([p for p in (workdir[2:], *parts) if p])
        return '"$HOME"' + ("/" + shlex.quote(tail) if tail else "")
    return shlex.quote("/".join([workdir.rstrip("/"), *parts]))


def _quote(cfg, word):
    if cfg["os"] != "windows":
        return shlex.quote(word)
    # cmd.exe has no escape for a quote inside a quoted string, so this only
    # protects against the characters that actually turn up in these lines:
    # spaces in a bundle path, and cmd's own separators.
    if word and not any(ch in word for ch in ' \t&|<>^()"'):
        return word
    return '"' + word.replace('"', "") + '"'


def shell_line(cfg, argv):
    return " ".join(_quote(cfg, word) for word in argv)


def remote_cmd(cfg, argv):
    """What to hand subprocess to run argv in the desk's workdir.

    Local desks get argv back untouched -- run_host passes the workdir as the
    cwd instead, which keeps this machine off ssh entirely. Remote desks get
    a single shell line, because that is all sshd will accept.
    """
    if is_local(cfg):
        return list(argv)
    if _powershell(cfg):
        return [*_ssh(cfg), PS_INVOKE]
    return [*_ssh(cfg), remote_line(cfg, argv)]


def remote_line(cfg, argv):
    """The shell line that runs argv in the desk's workdir.

    For a PowerShell host this is what goes into the step file, and the ssh
    command line carries only PS_INVOKE.
    """
    line = shell_line(cfg, argv)
    if cfg["os"] == "windows":
        # /d because the workdir may be on another drive than cmd starts on.
        return f"cd /d {shell_path(cfg)} && {line}"
    return f"cd {shell_path(cfg)} && {line}"


# What a PowerShell host is asked to run: the step file, under cmd.exe, with
# its exit code carried out. `powershell -c` otherwise exits 0 or 1 by
# whether the last command "succeeded", which turns the agent's 1, cmd's 9009
# for a missing python and everything else into one indistinguishable 1.
# $env:USERPROFILE is expanded by PowerShell on the far side, so no profile
# name crosses this end.
PS_INVOKE = (f"cmd.exe /d /c \"$env:USERPROFILE\\{PS_STEP}\";"
             f" exit $LASTEXITCODE")


def step_file_text(line):
    """A cmd.exe line as a batch file. CRLF, because that is cmd's."""
    return f"@echo off\r\n{line}\r\n"


def push_cmd(cfg):
    """Make the workdir, drop the previous result, unpack the new snapshot.

    The delete is the point of doing these three as one command: it happens
    on the remote, immediately before the agent runs, so there is no window
    in which an agent that dies early leaves an older result.json to be
    pulled back and believed.

    Windows joins with `&` rather than `&&` because cmd's mkdir fails when
    the directory exists, which is the normal case. The exit code that
    reaches ssh is tar's -- the last command's -- which is the one worth
    having: if mkdir really failed, tar fails too.
    """
    if is_local(cfg):
        return ["tar", "-x", "-f", "-", "-C", str(local_workdir(cfg))]
    if _powershell(cfg):
        return [*_ssh(cfg), PS_INVOKE]
    return [*_ssh(cfg), push_line(cfg)]


def push_line(cfg):
    directory = shell_path(cfg)
    result = shell_path(cfg, RESULT_NAME)
    if _powershell(cfg):
        # The archive was sent ahead by scp, so tar reads a file, not stdin.
        # Windows' own bsdtar by full path: Git for Windows puts a GNU tar
        # on some PATHs, and GNU tar reads "C:..." as a remote host.
        return (f"mkdir {directory} 2>nul"
                f" & del /q {result} 2>nul"
                f" & %SystemRoot%\\System32\\tar.exe -x"
                f" -f \"%USERPROFILE%\\{PS_SNAPSHOT}\" -C {directory}")
    if cfg["os"] == "windows":
        return (f"mkdir {directory} 2>nul"
                f" & del /q {result} 2>nul"
                f" & tar -x -f - -C {directory}")
    return (f"mkdir -p {directory}"
            f" && rm -f {result}"
            f" && tar -x -f - -C {directory}")


def send_file_cmd(cfg, local, remote_name):
    """scp one file to the desk's home directory."""
    return ["scp", *SSH_OPTS, *cfg.get("ssh_options", []), str(local),
            f"{cfg['ssh']}:{remote_name}"]


def _send_text(cfg, runner, text_or_bytes, remote_name, what):
    """Write a file here and scp it there. Returns problems, or []."""
    data = (text_or_bytes.encode("utf-8") if isinstance(text_or_bytes, str)
            else text_or_bytes)
    with tempfile.TemporaryDirectory(prefix="blink-fleet-send-") as tmp:
        local = Path(tmp) / remote_name
        local.write_bytes(data)
        done, problems = _run(send_file_cmd(cfg, local, remote_name), runner,
                              what, timeout=PUSH_TIMEOUT_S)
    if problems:
        return problems
    if done.returncode != 0:
        return [f"{what} failed (exit {done.returncode}): {_tail(done)}"]
    return []


def run_remote_line(cfg, line, runner, what, **kwargs):
    """Run one shell line on a remote desk. (completed or None, problems).

    The one place the PowerShell host differs: its line goes as a step file
    and only PS_INVOKE crosses the ssh command line, because PowerShell
    rewrites quotes and percent signs on the way to a native program and has
    no && in the Windows PowerShell 5.1 that ships with the OS.
    """
    if _powershell(cfg):
        problems = _send_text(cfg, runner, step_file_text(line), PS_STEP,
                              f"Sending the next command to {cfg['ssh']}")
        if problems:
            return None, problems
        return _run([*_ssh(cfg), PS_INVOKE], runner, what, **kwargs)
    return _run([*_ssh(cfg), line], runner, what, **kwargs)


def pull_cmd(cfg, dest):
    """scp the result back, addressed relative to the remote's home.

    Home-relative rather than ~/ or %USERPROFILE%\\: scp's remote path is
    resolved by sftp against the login directory, so the plainest spelling is
    the one that needs no shell and no variable on the far side.
    """
    workdir = cfg["workdir"]
    for prefix in ("~/", "%USERPROFILE%\\", "%USERPROFILE%/", "$USERPROFILE\\",
                   "$USERPROFILE/"):
        if workdir.startswith(prefix):
            workdir = workdir[len(prefix):]
            break
    workdir = workdir.replace("\\", "/").rstrip("/")
    return ["scp", *SSH_OPTS, *cfg.get("ssh_options", []),
            f"{cfg['ssh']}:{workdir}/{RESULT_NAME}", str(dest)]


def _python(cfg):
    """The interpreter to start the agent with.

    A relative path on the LOCAL desk is resolved against this checkout, not
    against the workdir: .venv-test lives in the source tree, is gitignored,
    and is therefore not inside the snapshot that gets unpacked. It is also
    the only interpreter on that Mac with pyserial, so no default can stand
    in for it.
    """
    interpreter = cfg["python"]
    if is_local(cfg):
        interpreter = os.path.expanduser(interpreter)
        if not os.path.isabs(interpreter):
            interpreter = str(REPO_ROOT / interpreter)
    return interpreter


def agent_argv(cfg, scenarios=None, bundle=None, prev_bundle=None,
               ota_dir=None, expect_version=None, real_account=True,
               result_name=RESULT_NAME):
    """The agent's command line for one desk.

    `scenarios` is a filter by NAME and becomes the agent's --only; the
    agent's own --scenarios is a directory, and the default one inside the
    snapshot is always the right answer.

    No --port is passed. The daemon finds the board by USB VID:PID, which is
    what picks COM15 out of the Windows desk's twelve COM ports; naming a
    port here would break the first time a cable moved socket.

    The bundle paths are interpreted ON THE TARGET DESK. Release archives are
    per-platform, so one path cannot mean the same file on three machines --
    putting the right archive on each desk is a step before this one.
    """
    argv = [_python(cfg), "-m", "tests.fleet.agent",
            "--board", cfg["board"], "--out", result_name]
    if scenarios:
        if not isinstance(scenarios, str):
            scenarios = ",".join(scenarios)
        argv += ["--only", scenarios]
    for flag, value in (("--bundle", bundle), ("--prev-bundle", prev_bundle),
                        ("--ota-dir", ota_dir),
                        ("--expect-version", expect_version)):
        if value:
            argv += [flag, str(value)]
    if real_account:
        argv.append("--real-account")
    return argv


# ---------------------------------------------------------------------------
# Running one desk
# ---------------------------------------------------------------------------

def _text(raw):
    """Whatever a command said, as something printable.

    Bytes rather than text=True on the runner: cmd.exe answers in the console
    codepage, which on that machine is not UTF-8, and a decode error in the
    middle of reporting somebody's failure would replace the cause with a
    UnicodeDecodeError.
    """
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def _tail(done):
    """The end of a command's output, for repeating back to a person."""
    said = (_text(getattr(done, "stderr", "")).strip()
            or _text(getattr(done, "stdout", "")).strip())
    said = " ".join(said.split())
    return said[-TAIL_CHARS:] if said else "it said nothing at all"


def _run(cmd, runner, what, **kwargs):
    """Run one command. Returns (completed or None, problems).

    Nothing raises past here: an unreachable host, a missing ssh binary and a
    timeout are all outcomes this program reports, not conditions it dies of.
    """
    kwargs.setdefault("capture_output", True)
    try:
        return runner(cmd, **kwargs), []
    except subprocess.TimeoutExpired as e:
        return None, [f"{what} timed out after {int(e.timeout or 0)} s and"
                      f" was given up on. The desk may be asleep, or the"
                      f" agent may be stuck holding the serial port."]
    except FileNotFoundError as e:
        return None, [f"{what} could not be started: {e}. The command was"
                      f" {cmd[0]!r}."]
    except OSError as e:
        return None, [f"{what} failed: {e}"]
    except Exception as e:  # noqa: BLE001 - a runner is allowed to surprise us
        return None, [f"{what} failed unexpectedly: {e!r}"]


def push_snapshot(cfg, runner, name=None, timeout=PUSH_TIMEOUT_S):
    """Send this checkout at HEAD to the desk. Returns problems, or [].

    HEAD and not the working tree, deliberately: what the fleet proves has to
    be a commit, because .fleet/last_run.json names one and a release gated
    on uncommitted edits is gated on nothing. `git archive` also skips
    everything gitignored, which is why the venv has to be named separately.
    """
    name = name or cfg.get("ssh") or "this machine"
    done, problems = _run(["git", "archive", "--format=tar", "HEAD"], runner,
                          "Taking a snapshot of this checkout",
                          cwd=str(REPO_ROOT), timeout=GIT_TIMEOUT_S)
    if problems:
        return problems
    if done.returncode != 0:
        return [f"This checkout could not be archived: {_tail(done)}"]
    archive = done.stdout
    if not archive:
        return ["This checkout archived to nothing, so there is no snapshot"
                " to send. Is HEAD a real commit?"]
    if isinstance(archive, str):
        archive = archive.encode("utf-8", "replace")

    if is_local(cfg):
        workdir = local_workdir(cfg)
        workdir.mkdir(parents=True, exist_ok=True)
        # The same stale-result delete the remote line does inline.
        (workdir / RESULT_NAME).unlink(missing_ok=True)

    if _powershell(cfg):
        problems = _send_text(cfg, runner, archive, PS_SNAPSHOT,
                              f"Sending the snapshot to {name}")
        if problems:
            return problems
        done, problems = run_remote_line(
            cfg, push_line(cfg), runner, f"Unpacking the snapshot on {name}",
            timeout=timeout)
    else:
        done, problems = _run(push_cmd(cfg), runner,
                              f"Unpacking the snapshot on {name}",
                              input=archive, timeout=timeout)
    if problems:
        return problems
    if done.returncode != 0:
        return [f"The snapshot would not unpack on {name}"
                f" (exit {done.returncode}): {_tail(done)}"]
    return []


def _read_result(cfg, name, runner, workroot):
    """Bring the desk's result.json home and parse it. (result, problems)."""
    if is_local(cfg):
        path = local_workdir(cfg) / RESULT_NAME
    else:
        path = Path(workroot) / f"{name}-{RESULT_NAME}"
        done, problems = _run(pull_cmd(cfg, path), runner,
                              f"Fetching the result from {name}",
                              timeout=PULL_TIMEOUT_S)
        if problems:
            return None, problems
        if done.returncode != 0:
            return None, [f"The result file could not be fetched from"
                          f" {name} (exit {done.returncode}): {_tail(done)}."
                          f" The agent may have died before writing it."]
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as e:
        return None, [f"{name} left no result file to read: {e}"]
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        return None, [f"The result file from {name} is not valid JSON ({e}),"
                      f" so this desk proved nothing. It was probably cut"
                      f" short: {raw[:200]!r}"]
    if not isinstance(parsed, dict) or "ok" not in parsed:
        return None, [f"The result file from {name} is not an agent result:"
                      f" {raw[:200]!r}"]
    return parsed, []


def _failed(name, cfg, problems):
    return {"desk": name, "board": cfg.get("board"), "os": cfg.get("os"),
            "has_claude_desktop": cfg.get("has_claude_desktop"),
            "note": cfg.get("note"),
            "scenarios": {}, "problems": list(problems), "ok": False}


def run_host(name, cfg, runner=subprocess.run, scenarios=None, bundle=None,
             prev_bundle=None, ota_dir=None, expect_version=None,
             real_account=True, timeout=AGENT_TIMEOUT_S, workroot=None,
             before_agent=None):
    """Prove one desk. Always returns a result; never raises.

    The agent's own verdict is returned as it wrote it, because its problems
    are sentences about that machine's board that nothing here could
    reconstruct -- "the board never answered: PermissionError 13 opening
    /dev/ttyUSB0" is the whole answer for the Ubuntu desk right now, and
    replacing it with "host failed" would throw away the only useful part.

    Two disagreements are refused rather than resolved. A result that says
    every scenario passed while listing no scenarios did not run anything; a
    result that says the same while the agent exited non-zero cannot have
    both halves be true. Both become failures, because a release gate that
    guesses in favour of shipping is not a gate.

    `before_agent(cfg)` runs between the push and the agent and returns
    problems, or []. It is where a VM desk is handed the board: after the
    snapshot is there, because a push does not need the board and a push
    that fails should not have cost anybody theirs.
    """
    try:
        problems = push_snapshot(cfg, runner, name=name)
        if problems:
            return _failed(name, cfg, problems)
        if before_agent is not None:
            problems = before_agent(cfg)
            if problems:
                return _failed(name, cfg, problems)

        argv = agent_argv(cfg, scenarios=scenarios, bundle=bundle,
                          prev_bundle=prev_bundle, ota_dir=ota_dir,
                          expect_version=expect_version,
                          real_account=real_account)
        what = f"The fleet agent on {name}"
        if is_local(cfg):
            done, problems = _run(remote_cmd(cfg, argv), runner, what,
                                  timeout=timeout,
                                  cwd=str(local_workdir(cfg)))
        else:
            done, problems = run_remote_line(cfg, remote_line(cfg, argv),
                                             runner, what, timeout=timeout)
        if done is None:
            # A run cut off from this end says nothing about what it left
            # behind on that end. The agent hands the serial port back from a
            # finally, but a finally does not run in a process that was
            # killed, and the shape of that failure is a desk that looks fine
            # with a dark board on it.
            problems.append(
                f"Nothing was collected from {name}, so this desk has proved"
                f" nothing about this commit. Check it by hand before"
                f" trusting it: a run cut off mid-scenario can leave the"
                f" installed service stopped and that board dark.")
            return _failed(name, cfg, problems)

        with _workroot(workroot) as root:
            result, problems = _read_result(cfg, name, runner, root)
        if result is None:
            if done.returncode != 0:
                problems.append(f"The agent on {name} exited"
                                f" {done.returncode}: {_tail(done)}")
            return _failed(name, cfg, problems)

        result.setdefault("problems", [])
        result.setdefault("scenarios", {})
        if not isinstance(result["problems"], list):
            result["problems"] = [str(result["problems"])]
        if not isinstance(result["scenarios"], dict):
            result["problems"].append("The agent's scenario list came back"
                                      " in a shape nothing can read.")
            result["scenarios"] = {}
        # A scenario entry that is not a verdict is turned into one HERE, at
        # the edge, rather than being carried into the report: format_report
        # asks every entry whether it passed, and an AttributeError there
        # would arrive after the state file had already been written -- a
        # traceback instead of a table, over a file claiming the fleet
        # passed. Unreadable is counted as failed, like everything else this
        # module cannot make sense of.
        for scenario, outcome in list(result["scenarios"].items()):
            if not isinstance(outcome, dict) or "ok" not in outcome:
                result["scenarios"][scenario] = {"ok": False, "problems": [
                    f"The verdict for {scenario} came back as {outcome!r},"
                    f" which is not a verdict. Nothing here can read it, so"
                    f" it counts as a failure."]}
        result["desk"] = name
        result["os"] = cfg.get("os")
        result["has_claude_desktop"] = cfg.get("has_claude_desktop")
        # What this desk actually is, from the inventory, carried into the
        # evidence: "windows" alone would let a Windows 11 ARM VM read as the
        # Windows 10 PC a customer has.
        result["note"] = cfg.get("note")

        # The agent exits 1 when it has failures to report, which it has just
        # reported in its own words. Repeating "exited 1" under every failing
        # desk would bury those words in noise. Any OTHER non-zero code did
        # not come from the agent -- 9009 is cmd.exe saying it found no
        # python, 255 is ssh itself -- and a zero-code run whose result says
        # everything passed while the exit code says otherwise is a
        # contradiction that has to be refused rather than resolved.
        if done.returncode != 0:
            if result.get("ok") is True:
                result["problems"].append(
                    f"The agent on {name} exited {done.returncode}:"
                    f" {_tail(done)}. Its result file says every scenario"
                    f" passed, which cannot also be true, so {name} is"
                    f" counted as failed.")
            elif done.returncode != 1 or not result["problems"]:
                result["problems"].append(
                    f"The agent on {name} exited {done.returncode}:"
                    f" {_tail(done)}")
            result["ok"] = False
        if result.get("ok") is True and not result["scenarios"]:
            result["problems"].append(
                f"{name} reported success without running a single scenario,"
                f" which is not a pass. Check the scenario filter and the"
                f" agent's own output.")
            result["ok"] = False
        # The third contradiction, and the one that would read worst: a
        # PASSED sitting on the same line as "overage FAILED". The agent in
        # this snapshot cannot write it -- its _finish() ands the scenarios
        # together -- but the whole premise here is refusing contradictions
        # rather than picking the half that ships.
        lost = sorted(scenario for scenario, outcome
                      in result["scenarios"].items()
                      if outcome.get("ok") is not True)
        if result.get("ok") is True and lost:
            result["problems"].append(
                f"{name} reported success while {', '.join(lost)} failed on"
                f" it. Those two cannot both be true, so it is counted as"
                f" failed.")
            result["ok"] = False
        result["ok"] = result.get("ok") is True
        return result
    except Exception as e:  # noqa: BLE001 - a desk may not take the run down
        return _failed(name, cfg, [f"Proving {name} stopped on an unexpected"
                                   f" error: {e!r}"])


class _workroot:
    """A directory for pulled results: the caller's, or a temporary one."""

    def __init__(self, given):
        self.given = given
        self.tmp = None

    def __enter__(self):
        if self.given:
            Path(self.given).mkdir(parents=True, exist_ok=True)
            return Path(self.given)
        self.tmp = tempfile.TemporaryDirectory(prefix="blink-fleet-")
        return Path(self.tmp.name)

    def __exit__(self, *exc):
        if self.tmp:
            self.tmp.cleanup()
        return False


# ---------------------------------------------------------------------------
# The VM fleet: one board, handed from desk to desk
# ---------------------------------------------------------------------------

def address_cmd(cfg):
    return ["utmctl", "ip-address", cfg["vm"]["utm"]]


def _first_ipv4(text):
    """The first IPv4 address in utmctl's answer, or None.

    By shape rather than by line: utmctl prints "Error from event: ..." for a
    guest whose agent did not answer and has been seen to exit 0 doing it,
    and it lists IPv6 addresses after the IPv4 one.
    """
    for word in text.split():
        try:
            address = ipaddress.ip_address(word)
        except ValueError:
            continue
        if address.version == 4 and not address.is_loopback:
            return word
    return None


def lease_address(hostname, leases_text):
    """The address macOS's DHCP server most recently gave `hostname`.

    /var/db/dhcpd_leases is a list of {...} blocks of key=value lines. One
    guest can hold more than one block -- the Ubuntu VM had two on
    2026-10-30, .2 and .3, under two client ids -- so the block whose lease
    runs latest wins, since that is the one the guest asked for last.
    """
    best = None
    for block in leases_text.split("{")[1:]:
        fields = {}
        for raw in block.split("}")[0].splitlines():
            key, sep, value = raw.strip().partition("=")
            if sep:
                fields[key] = value
        if fields.get("name") != hostname or "ip_address" not in fields:
            continue
        try:
            expires = int(fields.get("lease", "0"), 16)
        except ValueError:
            expires = 0
        if best is None or expires > best[0]:
            best = (expires, fields["ip_address"])
    return best[1] if best else None


def resolve_address(name, cfg, runner, leases=DHCP_LEASES):
    """Where to reach a VM desk now. (address or None, how, problems)."""
    done, problems = _run(address_cmd(cfg), runner,
                          f"Asking UTM for {name}'s address",
                          timeout=UTM_TIMEOUT_S)
    if done is not None and done.returncode == 0:
        address = _first_ipv4(_text(done.stdout))
        if address:
            return address, "utmctl", []
    said = problems[0] if problems else (
        _tail(done) if done is not None else "no answer")
    hostname = cfg["vm"].get("hostname")
    if hostname:
        try:
            text = Path(leases).read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
        address = lease_address(hostname, text)
        if address:
            return address, "dhcp lease", []
    return None, None, [
        f"{name} has no address. UTM's guest agent did not give one ({said})"
        + (f" and there is no DHCP lease for {hostname} in {leases}"
           if hostname else "")
        + ". Is the VM started and booted? `utmctl list` says which."]


def with_address(cfg, address):
    return dict(cfg, ssh=str(cfg["ssh"]).replace(ADDRESS, address))


def usb_connect_cmd(cfg, usb):
    return ["utmctl", "usb", "connect", cfg["vm"]["utm"], usb]


def usb_disconnect_cmd(usb):
    return ["utmctl", "usb", "disconnect", usb]


def board_wait_argv(python, usb, seconds=BOARD_WAIT_S, gone=False):
    argv = [python, "-m", "tests.fleet.board_present", "--usb", usb,
            "--wait", str(int(seconds))]
    if gone:
        argv.append("--gone")
    return argv


def service_argv(python, verb):
    return [python, "-m", "tests.fleet.service", verb]


def _utm_worked(done):
    """utmctl's own word, read suspiciously.

    It reports AppleEvent failures as "Error from event: ..." and has been
    seen to exit 0 while doing it, so the exit code alone is not believed.
    """
    if done is None or done.returncode != 0:
        return False
    said = (_text(done.stdout) + _text(done.stderr)).lower()
    return "error" not in said and "cannot be found" not in said


class Bench:
    """This Mac's end of a VM run: its service, and the board's handoffs.

    Only one thing here is about the product, and it is not in this class:
    the guest's own agent. Everything here is plumbing that makes the one
    board available to it, and every failure in it is reported as that --
    a desk that never got the board -- rather than as a board fault.

    `ask` is input() unless a test says otherwise, and is only called when
    `interactive`: a run with nobody at the keyboard (a CI-like shell, a
    pipe) cannot be waited on, so a handoff utmctl could not do is a failed
    desk there, not a prompt nobody will answer.
    """

    def __init__(self, settings, runner, mode="auto", ask=input,
                 interactive=False):
        self.usb = settings["board_usb"]
        self.python = _python({"ssh": "",
                               "python": settings["host_python"]})
        self.runner = runner
        self.mode = mode
        self.ask = ask
        self.interactive = interactive
        self.held = False
        self.problems = []
        self.handoffs = {}
        # The VM the board may be in right now, set the moment a handoff is
        # believed to have happened rather than when connect() returns: a
        # Ctrl-C during the wait that follows would otherwise skip the
        # handback and leave the board in the VM. Found by a test.
        self.lent_to = None

    # -- this Mac's installed service ------------------------------------

    def _service(self, verb):
        """(problems, skipped) for one service call."""
        done, problems = _run(service_argv(self.python, verb), self.runner,
                              f"Asking this Mac's service to {verb}",
                              cwd=str(REPO_ROOT), timeout=PULL_TIMEOUT_S)
        if problems:
            return problems, False
        said = _tail(done)
        if done.returncode == 3:
            return [f"BLINK_SKIP_SERVICE is set in this shell, so this Mac's"
                    f" service was not {verb}ped and still holds the port"
                    f" UTM would have to take. Unset it and start again."], \
                True
        if done.returncode != 0:
            return [f"This Mac's service would not {verb}: {said}."], False
        return [], False

    def hold(self):
        """Stop this Mac's service for the VM phase. Problems, or [].

        A port somebody has open is not a device UTM can take: the board
        was passed through by hand on 2026-10-19 only with the daemon
        stopped first (docs/windows-check.md).

        Any attempt counts as held, except the BLINK_SKIP_SERVICE no-op: a
        stop that timed out or answered 1 may still have booted the agent
        out, and starting a service that is already running costs nothing,
        while not starting one that was stopped leaves this Mac dark.
        """
        problems, skipped = self._service("stop")
        self.held = not skipped
        self.problems = [f"No VM was handed the board. {p}"
                         for p in problems]
        return self.problems

    def release(self):
        """Start this Mac's service again. Warnings, or []."""
        if not self.held:
            return []
        self.held = False
        problems, _ = self._service("start")
        return [f"{p} The board on this Mac will stay dark until it is"
                f" started (blink install puts it back)." for p in problems]

    # -- the board --------------------------------------------------------

    def _prompt(self, text):
        """Ask the operator; False when there is nobody to answer (EOF).

        Only EOF. A Ctrl-C at this prompt means stop the run, and is let
        through to the finallys that hand the board back and restart this
        Mac's service -- swallowing it used to move straight on to the next
        VM's handoff and its prompt.
        """
        try:
            self.ask(text)
            return True
        except EOFError:
            return False

    def connect(self, name, cfg):
        """Hand the board to a VM and wait for the guest to see it.

        Returns (handed, problems). The board is recorded as lent out
        BEFORE utmctl is asked, not after it answers: utmctl talks to UTM by
        AppleEvent, and an event that timed out at this end (-1712, UTM's
        commonest failure here) or a Ctrl-C during the call can still have
        been carried out at the other. Asking back a board that never left
        costs one failed utmctl call and a wait that succeeds at once;
        not asking back one that did leaves this Mac's board in a guest.
        """
        if self.problems:
            return False, list(self.problems)
        self.lent_to = name
        how, said = None, ""
        if self.mode != "operator":
            done, problems = _run(usb_connect_cmd(cfg, self.usb),
                                  self.runner,
                                  f"Handing the board to {name}",
                                  timeout=UTM_TIMEOUT_S)
            if _utm_worked(done):
                how = "utmctl"
            else:
                said = problems[0] if problems else _tail(done)
        if how is None:
            if self.mode == "utmctl" or not self.interactive:
                return False, [
                    f"The board could not be handed to {name}: utmctl said"
                    f" {said!r}. Run again from a terminal to be asked to"
                    f" do it from the VM window instead (its USB icon ->"
                    f" USB Serial -> Connect)."]
            if not self._prompt(
                    f"\n>>> Hand the board to {name}: in its UTM window,"
                    f" USB icon -> USB Serial -> Connect.\n"
                    f"    Press Enter once it is connected. "):
                return False, [f"The board was not handed to {name}: the"
                               f" prompt was abandoned."]
            how = "operator"
        self.handoffs[name] = how
        argv = board_wait_argv(_python(cfg), self.usb)
        done, problems = run_remote_line(
            cfg, remote_line(cfg, argv), self.runner,
            f"Waiting for {name} to see the board",
            timeout=BOARD_WAIT_S + PULL_TIMEOUT_S)
        if problems:
            return True, problems
        if done.returncode != 0:
            return True, [
                f"{name} never saw the board ({self.usb}) after it was"
                f" handed over by {how}: {_tail(done)}. On Windows, check"
                f" Device Manager for USB-SERIAL CH340; on Linux,"
                f" /dev/ttyUSB0."]
        return True, []

    def _home(self, name):
        """Wait for this Mac to see the board. (home, what was said)."""
        done, problems = _run(board_wait_argv(self.python, self.usb),
                              self.runner,
                              f"Waiting for the board to come back from"
                              f" {name}", cwd=str(REPO_ROOT),
                              timeout=BOARD_WAIT_S + PULL_TIMEOUT_S)
        if problems:
            return False, problems[0]
        return done.returncode == 0, _tail(done)

    def disconnect(self, name, cfg):
        """Take the board back to this Mac. Warnings, or [].

        The test of success is this Mac seeing the board, not utmctl's
        answer: a board that never left (a handoff that failed) is home at
        once, and one utmctl claims to have released may not be.

        A board that does not come home is a warning for THIS desk, whose
        evidence is complete by now -- and a hard stop for every VM after
        it: bench.problems is set, so no later VM is handed a board this
        Mac has not got back, and each one fails saying so.
        """
        self.lent_to = None
        prompt = (f"\n>>> Take the board back from {name}: in its UTM"
                  f" window, USB icon -> USB Serial -> Disconnect.\n"
                  f"    Press Enter once it is disconnected. ")
        if self.mode == "operator":
            if self.interactive:
                self._prompt(prompt)
        else:
            _run(usb_disconnect_cmd(self.usb), self.runner,
                 f"Taking the board back from {name}", timeout=UTM_TIMEOUT_S)
        home, said = self._home(name)
        if not home and self.interactive and self.mode != "operator":
            if self._prompt(prompt):
                home, said = self._home(name)
        if home:
            return []
        self.problems.append(
            f"The board never came back to this Mac from {name} ({said}),"
            f" so no later VM was handed it. Disconnect it from {name}'s USB"
            f" menu and run again.")
        return [f"The board did not come back to this Mac from {name}"
                f" ({said}). Disconnect it from that VM's USB menu."]


def run_vm_host(name, cfg, bench, runner=subprocess.run,
                leases=DHCP_LEASES, **kwargs):
    """Prove one VM desk with the borrowed board. (result, warnings).

    Never raises, like run_host. The board is asked back from a finally
    whenever it may have been handed over, including when the agent failed
    or timed out: a board left in a VM is a dark board on this Mac.
    """
    warnings = []
    try:
        address, found_by, problems = resolve_address(name, cfg, runner,
                                                       leases)
        if address is None:
            result = _failed(name, cfg, problems)
        else:
            cfg = with_address(cfg, address)

            def take(target):
                return bench.connect(name, target)[1]

            try:
                result = run_host(name, cfg, runner=runner,
                                  before_agent=take, **kwargs)
            finally:
                if bench.lent_to == name:
                    warnings += bench.disconnect(name, cfg)
        result["vm"] = {"utm": cfg["vm"]["utm"],
                        "address": address, "address_from": found_by,
                        "handoff": bench.handoffs.get(name)}
        return result, warnings
    except Exception as e:  # noqa: BLE001 - a desk may not take the run down
        return _failed(name, cfg, [f"Proving {name} stopped on an unexpected"
                                   f" error: {e!r}"]), warnings


def run_order(inventory):
    """Desks with their own board first, in file order, then the VMs.

    The board starts on this Mac, and its service is held stopped for as
    long as the board is lent out. A local desk run in the middle of that
    would find its own service already stopped by somebody else.
    """
    return ([name for name, cfg in inventory.items() if not is_vm(cfg)]
            + [name for name, cfg in inventory.items() if is_vm(cfg)])


def _relative(path):
    path = Path(path).resolve()
    try:
        return path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# The verdict
# ---------------------------------------------------------------------------

def aggregate(hosts):
    """Green only if there is something to be green about, and all of it is.

    An empty fleet is a failure, not a vacuous pass: it is what a filter that
    matched nothing, or a run that collected nothing, looks like.
    """
    failed = sorted(name for name, result in hosts.items()
                    if result.get("ok") is not True)
    return {"ok": bool(hosts) and not failed, "hosts": len(hosts),
            "failed": failed}


def format_report(state):
    """The table somebody reads when a release is blocked.

    A desk that ran nothing prints "Nothing ran" rather than an empty cell,
    because an empty cell in a row of ticks reads as fine.
    """
    hosts = state.get("hosts", {})
    took = (state.get("finished_at") or 0) - (state.get("started_at") or 0)
    lines = [""]
    lines.append(f"Fleet run {_short(state.get('sha', 'unknown'))}:"
                 f" {len(hosts)} desk(s) in {took / 60:.1f} min")
    fleet = state.get("fleet") or {}
    if fleet.get("kind") == "vm":
        # Said at the top, where nobody can read the table without it.
        lines.append("A VM fleet run: this Mac and its UTM guests, one board"
                     " handed from desk to desk. Not the desk fleet.")
    lines.append("-" * 72)
    for name in sorted(hosts):
        result = hosts[name]
        verdict = "PASSED" if result.get("ok") is True else "FAILED"
        scenarios = result.get("scenarios") or {}
        if scenarios:
            summary = ", ".join(
                f"{scenario} {'ok' if outcome.get('ok') else 'FAILED'}"
                for scenario, outcome in sorted(scenarios.items()))
        else:
            summary = "Nothing ran"
        lines.append(f"{name:<16}{verdict:<8}{summary}")
        if result.get("note"):
            lines.append(f"{'':<16}({result['note']})")
        for problem in result.get("problems") or []:
            lines.append(f"{'':<16}{problem}")
        for scenario, outcome in sorted(scenarios.items()):
            if outcome.get("ok"):
                continue
            for problem in outcome.get("problems") or []:
                lines.append(f"{'':<16}{scenario}: {problem}")
    lines.append("-" * 72)
    summary = aggregate(hosts)
    # The run's own verdict when it has one, not this function's opinion of
    # the desks. They can differ -- three green desks on a commit that could
    # not be read is still not a release anybody can point at -- and a table
    # that says PASSED under an exit code of 1 is the worst of both.
    passed = state["ok"] is True if "ok" in state else summary["ok"]
    if passed:
        lines.append("The fleet passed.")
    else:
        blocked = ", ".join(summary["failed"]) or "no desk reported at all"
        if summary["ok"]:
            blocked = "nothing on the desks, see below"
        lines.append(f"The fleet did NOT pass. Blocked by: {blocked}.")
    for problem in state.get("problems") or []:
        lines.append(problem)
    for warning in state.get("warnings") or []:
        lines.append(f"Warning: {warning}")
    lines.append("")
    return lines


def _short(sha):
    """Seven characters of a commit, but all seven letters of "unknown"."""
    sha = str(sha)
    return sha[:7] if len(sha) > 12 else sha


def _head_sha(runner):
    """The commit the desks were sent. (sha, problems)."""
    done, problems = _run(["git", "rev-parse", "HEAD"], runner,
                          "Reading this checkout's commit",
                          cwd=str(REPO_ROOT), timeout=GIT_TIMEOUT_S)
    if problems or done.returncode != 0:
        return "unknown", (problems or
                           [f"This checkout's commit could not be read:"
                            f" {_tail(done)}. Without it the result names no"
                            f" commit and cannot gate a release."])
    return _text(done.stdout).strip() or "unknown", []


def _write_state(path, doc):
    """Write the verdict atomically, so a reader never sees half of one."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".partial")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        prog="tools/fleet/run.py",
        description="Run the fleet scenarios on every desk and add it up.")
    ap.add_argument("--inventory", default=str(DEFAULT_INVENTORY),
                    help="The fleet.toml naming the desks")
    ap.add_argument("--state", default=str(DEFAULT_STATE),
                    help="Where the run's verdict is written")
    ap.add_argument("--only", default=None,
                    help="Comma-separated host names, instead of all of them")
    ap.add_argument("--scenarios", default=None,
                    help="Comma-separated scenario names, instead of all")
    ap.add_argument("--bundle", default=None,
                    help="Release archive for the fresh-install scenario, as"
                         " named ON EACH DESK -- archives are per-platform")
    ap.add_argument("--prev-bundle", default=None,
                    help="The previous release's archive, on each desk")
    ap.add_argument("--ota-dir", default=None,
                    help="Directory of the candidate's manifest, signature"
                         " and archive, on each desk")
    ap.add_argument("--expect-version", default=None,
                    help="The version both bundle scenarios must end at")
    ap.add_argument("--timeout", type=float, default=AGENT_TIMEOUT_S,
                    help="Seconds to allow one desk's agent")
    ap.add_argument("--no-real-account", action="store_true",
                    help="Skip the pass against each desk's own account")
    ap.add_argument("--dry-run", action="store_true",
                    help="Print the commands each desk would be sent and"
                         " stop, without connecting to anything")
    ap.add_argument("--sequential", action="store_true",
                    help="One desk at a time, in inventory order. The VM"
                         " inventory always runs this way")
    ap.add_argument("--handoff", choices=("auto", "utmctl", "operator"),
                    default="auto",
                    help="How a VM is handed the board: utmctl, the operator"
                         " at the VM window's USB menu, or utmctl and then"
                         " the operator if it fails (the default)")
    return ap.parse_args(argv)


def _print_dry_run(inventory, settings, args):
    """Every command the run would make, in the order it would make them.

    This run stops somebody's daemon on three machines and takes their boards
    for the best part of an hour. Being able to read the exact lines first --
    particularly the cmd.exe one, which cannot be tested anywhere but on that
    desk -- is cheaper than finding out halfway.
    """
    sequential = settings["sequential"] or args.sequential
    names = run_order(inventory) if sequential else sorted(inventory)
    if settings["kind"] == "vm":
        print("A VM fleet: one board, handed from desk to desk.")
    if sequential:
        print(f"One desk at a time: {', '.join(names)}.")
    bench = None
    for name in names:
        cfg = inventory[name]
        argv = agent_argv(cfg, scenarios=args.scenarios, bundle=args.bundle,
                          prev_bundle=args.prev_bundle, ota_dir=args.ota_dir,
                          expect_version=args.expect_version,
                          real_account=not args.no_real_account)
        if is_vm(cfg):
            if bench is None:
                bench = Bench(settings, runner=None)
                print(f"\nthis Mac, before the first VM:\n"
                      f"  hold: {service_argv(bench.python, 'stop')}")
            print(f"\n{name} ({cfg['os']}, UTM {cfg['vm']['utm']}):")
            print(f"  addr: {address_cmd(cfg)}, else the newest DHCP lease"
                  f" for {cfg['vm'].get('hostname') or '(no hostname)'}")
            cfg = with_address(cfg, "<address>")
        else:
            print(f"\n{name} ({cfg['os']},"
                  f" {cfg['ssh'] or 'no ssh, this machine'}):")
        if _powershell(cfg):
            print(f"  send: {send_file_cmd(cfg, '<snapshot>', PS_SNAPSHOT)}")
            print(f"  push: {PS_STEP} = {push_line(cfg)}")
            print(f"        {push_cmd(cfg)}")
        else:
            print(f"  push: {push_cmd(cfg)}")
        if is_vm(cfg):
            if args.handoff != "operator":
                print(f"  give: {usb_connect_cmd(cfg, bench.usb)}")
            if args.handoff == "auto":
                print("        (or, if utmctl cannot, the operator at the VM"
                      " window's USB menu)")
            elif args.handoff == "operator":
                print("  give: the operator, at the VM window's USB menu")
            wait = remote_line(cfg, board_wait_argv(_python(cfg), bench.usb))
            print(f"  wait: {wait}")
        if _powershell(cfg):
            print(f"  run : {PS_STEP} = {remote_line(cfg, argv)}")
            print(f"        {remote_cmd(cfg, argv)}")
        else:
            print(f"  run : {remote_cmd(cfg, argv)}")
        if not is_local(cfg):
            print(f"  pull: {pull_cmd(cfg, '<temporary file>')}")
        if is_vm(cfg):
            if args.handoff != "operator":
                print(f"  back: {usb_disconnect_cmd(bench.usb)}")
            print(f"  here: {board_wait_argv(bench.python, bench.usb)}")
    if bench is not None:
        print(f"\nthis Mac, after the last VM:\n"
              f"  free: {service_argv(bench.python, 'start')}")
    print("\nNothing was run and no desk was contacted.")


def _agent_kwargs(args):
    return {"scenarios": args.scenarios, "bundle": args.bundle,
            "prev_bundle": args.prev_bundle, "ota_dir": args.ota_dir,
            "expect_version": args.expect_version,
            "real_account": not args.no_real_account,
            "timeout": args.timeout}


def _run_side_by_side(inventory, args, runner, doc):
    """Every desk at once: one board each, no shared state."""
    with concurrent.futures.ThreadPoolExecutor(
            max_workers=max(1, len(inventory))) as pool:
        futures = {pool.submit(run_host, name, cfg, runner=runner,
                               **_agent_kwargs(args)): name
                   for name, cfg in inventory.items()}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                doc["hosts"][name] = future.result()
            except BaseException as e:  # noqa: BLE001
                doc["hosts"][name] = _failed(
                    name, inventory[name],
                    [f"Proving {name} died in its own thread: {e!r}"])


def _run_in_turn(inventory, settings, args, runner, doc, ask, interactive,
                 leases):
    """One desk at a time, the VMs last, with this Mac's service held for
    as long as the board is lent out -- and given back from a finally."""
    bench = None
    try:
        for name in run_order(inventory):
            cfg = inventory[name]
            print(f"\n== {name}")
            if is_vm(cfg):
                if bench is None:
                    bench = Bench(settings, runner, mode=args.handoff,
                                  ask=ask, interactive=interactive)
                    bench.hold()
                result, warnings = run_vm_host(name, cfg, bench,
                                               runner=runner, leases=leases,
                                               **_agent_kwargs(args))
                doc["warnings"] += warnings
            else:
                result = run_host(name, cfg, runner=runner,
                                  **_agent_kwargs(args))
            doc["hosts"][name] = result
            print(f"== {name}: {'PASSED' if result.get('ok') else 'FAILED'}")
    finally:
        if bench is not None:
            doc["warnings"] += bench.release()


def _interactive():
    try:
        return sys.stdin.isatty()
    except (AttributeError, ValueError):
        return False


def main(argv=None, runner=subprocess.run, ask=input, interactive=None,
         leases=DHCP_LEASES):
    args = parse_args(argv)
    state = Path(args.state)
    if not args.dry_run:
        # Before the inventory is even read, so that the paths which give up
        # early take the old verdict with them. A typo in --only that left
        # last week's green file behind would hand the release gate a file
        # the operator believes they just refreshed. A dry run is the one
        # exception: it decides nothing, so it disturbs nothing.
        state.parent.mkdir(parents=True, exist_ok=True)
        state.unlink(missing_ok=True)

    try:
        inventory = load_inventory(args.inventory)
        settings = load_settings(args.inventory)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2

    wanted = None
    if args.only:
        wanted = [name.strip() for name in args.only.split(",")
                  if name.strip()]
        unknown = [name for name in wanted if name not in inventory]
        if unknown:
            print(f"The inventory has no desk called"
                  f" {', '.join(unknown)}. It holds:"
                  f" {', '.join(sorted(inventory))}.", file=sys.stderr)
            return 2
        inventory = {name: inventory[name] for name in wanted}

    # A PowerShell desk gets its command line as a batch file, and in a
    # batch file a % that is not a variable is eaten -- on a command line it
    # would have survived. So a path that carries one is refused rather than
    # quietly sent somewhere else.
    if any(_powershell(cfg) for cfg in inventory.values()):
        for flag, value in (("--bundle", args.bundle),
                            ("--prev-bundle", args.prev_bundle),
                            ("--ota-dir", args.ota_dir)):
            if value and "%" in value:
                print(f"{flag} {value!r} has a %, which a PowerShell desk's"
                      f" batch file would eat. Rename the path.",
                      file=sys.stderr)
                return 2

    if args.dry_run:
        _print_dry_run(inventory, settings, args)
        return 0

    sequential = settings["sequential"] or args.sequential
    sha, problems = _head_sha(runner)
    doc = {"sha": sha, "started_at": time.time(), "finished_at": None,
           "ok": False, "hosts": {}, "problems": problems,
           # Recorded so the release gate can tell a full fleet run from a
           # one-desk or one-scenario run that happens to be green.
           "only": wanted, "scenarios": args.scenarios,
           # And so it can tell a VM run from the desk fleet: the gate
           # compares `kind` with the inventory it was asked about, and
           # refuses a VM run offered as the desks.
           "fleet": {"kind": settings["kind"], "sequential": sequential,
                     "inventory": _relative(args.inventory)},
           "real_account": not args.no_real_account,
           "warnings": []}
    print(f"Fleet run of {_short(sha)} on {len(inventory)} desk(s):"
          f" {', '.join(sorted(inventory))}"
          f"{', one at a time' if sequential else ''}.")
    if interactive is None:
        interactive = _interactive()
    try:
        if sequential:
            _run_in_turn(inventory, settings, args, runner, doc, ask,
                         interactive, leases)
        else:
            _run_side_by_side(inventory, args, runner, doc)
    finally:
        doc["finished_at"] = time.time()
        missing = sorted(set(inventory) - set(doc["hosts"]))
        for name in missing:
            doc["hosts"][name] = _failed(
                name, inventory[name],
                ["This desk never reported. The run was cut short before it"
                 " finished, so nothing is known about it."])
        summary = aggregate(doc["hosts"])
        doc["ok"] = (summary["ok"] and not doc["problems"]
                     and doc["sha"] != "unknown")
        _write_state(state, doc)

    # The verdict is already on disk by here. Whatever the table does, it
    # must not be the last thing that happens: a traceback in place of the
    # report, over a .fleet/last_run.json the gate will read as green, is the
    # worst pairing this program has. So the fallback says the two things
    # that cannot be lost -- what was decided, and where it was written.
    try:
        for line in format_report(doc):
            print(line)
    except Exception as e:  # noqa: BLE001
        print(f"\nThe result table could not be printed ({e!r}), so here is"
              f" the verdict without it.", file=sys.stderr)
        print(f"Fleet run {_short(doc['sha'])}:"
              f" {'PASSED' if doc['ok'] else 'did NOT pass'}.")
        for host, result in sorted(doc["hosts"].items()):
            state_word = "PASSED" if result.get("ok") is True else "FAILED"
            print(f"  {host}: {state_word}")
    print(f"Written to {state}")
    return 0 if doc["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
