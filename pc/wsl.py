"""Windows Subsystem for Linux: list distributions, check them, run in them,
and probe one for where Claude Code lives (docs/plans/enterprise-spend.md,
section 4).

Nothing here runs unless a caller asks, and the callers only ask when the
user named a distribution with `blink install --wsl <name>`.

Facts this module is built on, each checked against the microsoft/WSL source:

* wsl.exe is called by its absolute path under %SystemRoot%\\System32, never
  found through PATH, with ota.NO_WINDOW (a console window flashing on a
  desk is a defect), binary pipes, all three standard handles and a timeout.
* WSL_UTF8 is set explicitly in the child's environment. "1" makes wsl.exe's
  own messages UTF-8; without it they are UTF-16LE with no BOM, and the
  default depends on the Windows build. `running` asks for UTF-16LE on
  purpose and decodes it; everything that goes into Linux sets "1".
* Commands into Linux are `-d <name> [-u <user>] -e <argv...>`, never `--`.
  `-e` splits the rest of the command line with CommandLineToArgvW, which
  is what Python's list2cmdline quotes for, so an argv list round-trips.
* A distribution is identified by its registry GUID (HKCU\\...\\Lxss), not
  by `wsl -l -q`, whose names carry no GUID and can be renamed.
* `wsl -l --running -q` prints nothing and exits 0 when none is running.

Nothing the probe reads from a distribution -- the environment above all, which
can hold tokens and proxy credentials -- is printed or logged, and no
proxy or certificate variable is kept: Probe holds five strings and the
captured PATH is used once and dropped.

`winreg` is imported inside the default registry reader, so importing this
module does nothing and works on macOS and Linux (tests/test_import_is_inert.py
imports every module there). Registry access goes through a reader object
(`values(hive, path)`, `subkeys(hive, path)`, both raising OSError) so the
tests run on every platform.
"""
import os
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from pc.ota import NO_WINDOW

HKCU = "HKCU"
HKLM = "HKLM"
LXSS = r"Software\Microsoft\Windows\CurrentVersion\Lxss"
POLICY = r"Software\Policies\WSL"

_STATE_INSTALLED = 1

# What a failed or timed-out call returns; callers test rc != 0.
FAILED = (-1, b"", b"")


def wsl_exe():
    """The argv prefix that starts wsl.exe: a tuple, so a test can hand in
    (sys.executable, "fake_wsl.py"). Resolved at call time, not import."""
    root = os.environ.get("SystemRoot") or os.environ.get("windir") \
        or r"C:\Windows"
    return (os.path.join(root, "System32", "wsl.exe"),)


@dataclass(frozen=True)
class Distro:
    guid: str
    name: str
    version: int
    default: bool


@dataclass(frozen=True)
class Probe:
    user: str
    home: str
    shell: str
    config_dir: str
    claude_exe: Optional[str]


class _WinReg:
    """The real reader. winreg is imported here, not at the top."""

    @staticmethod
    def _open(hive, path):
        import winreg
        root = {HKCU: winreg.HKEY_CURRENT_USER,
                HKLM: winreg.HKEY_LOCAL_MACHINE}[hive]
        return winreg, winreg.OpenKey(root, path)

    def values(self, hive, path):
        winreg, key = self._open(hive, path)
        out = {}
        with key:
            i = 0
            while True:
                try:
                    name, value, _ = winreg.EnumValue(key, i)
                except OSError:
                    return out
                out[name] = value
                i += 1

    def subkeys(self, hive, path):
        winreg, key = self._open(hive, path)
        out = []
        with key:
            i = 0
            while True:
                try:
                    out.append(winreg.EnumKey(key, i))
                except OSError:
                    return out
                i += 1


def policy_blocks(reg=None):
    """Does group policy forbid WSL? AllowWSL or AllowInboxWSL equal to 0.
    Missing means allowed; so does an unreadable registry, because the
    policy key is absent on almost every machine."""
    reg = reg or _WinReg()
    try:
        values = reg.values(HKLM, POLICY)
    except OSError:
        return False
    return any(values.get(k) == 0 for k in ("AllowWSL", "AllowInboxWSL"))


def distros(reg=None):
    """Installed distributions (State == 1; a half-installed one is skipped),
    from the registry. On any registry error: [] -- the caller says to check
    WSL. No fallback to `wsl -l -q`, whose names carry no GUID."""
    reg = reg or _WinReg()
    try:
        default = str(reg.values(HKCU, LXSS).get("DefaultDistribution", ""))
        found = []
        for sub in reg.subkeys(HKCU, LXSS):
            if not sub.startswith("{"):
                continue
            v = reg.values(HKCU, LXSS + "\\" + sub)
            name = v.get("DistributionName")
            if v.get("State") != _STATE_INSTALLED or not name:
                continue
            found.append(Distro(guid=sub, name=str(name),
                                version=int(v.get("Version", 0) or 0),
                                default=sub.lower() == default.lower()))
        return found
    except (OSError, ValueError, TypeError):
        return []


def _call(argv, wsl_utf8, stdin, timeout, runner):
    """One wsl.exe launch. Returns (rc, out, err) or FAILED on a timeout or
    when wsl.exe cannot start."""
    env = dict(os.environ)
    env["WSL_UTF8"] = wsl_utf8
    kwargs = dict(stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                  env=env, timeout=timeout, **NO_WINDOW)
    if stdin is None:
        kwargs["stdin"] = subprocess.DEVNULL
    else:
        kwargs["input"] = stdin
    try:
        done = runner(list(argv), **kwargs)
    except (subprocess.TimeoutExpired, OSError):
        return FAILED
    return done.returncode, done.stdout or b"", done.stderr or b""


def running(name, exe=None, timeout=5, runner=subprocess.run):
    """Is the named distribution running right now? Never starts it."""
    rc, out, _ = _call([*(exe or wsl_exe()), "-l", "--running", "-q"],
                       "0", None, timeout, runner)
    if rc != 0:
        return False
    text = out.decode("utf-16-le", "replace").lstrip("\ufeff")
    return any(line.strip().casefold() == name.casefold()
               for line in text.splitlines())


def run(name, argv, user=None, env=None, exe=None, stdin=None, timeout=30,
        runner=subprocess.run):
    """Run argv inside the distribution as `user`: (rc, stdout, stderr), or
    (-1, b"", b"") on a timeout.

    The command is wrapped in `timeout -k 5 <t>` inside Linux so a hung
    program dies there and not only the wsl.exe that carries it; the Windows
    side waits ten seconds longer so the Linux side ends first. `env` is
    passed as K=V words to env(1), not through the Windows environment, which
    wsl.exe does not forward."""
    secs = max(1, int(timeout))
    cmd = [*(exe or wsl_exe()), "-d", name]
    if user:
        cmd += ["-u", user]
    cmd += ["-e", "env"]
    cmd += [f"{k}={v}" for k, v in (env or {}).items()]
    cmd += ["timeout", "-k", "5", str(secs), *argv]
    return _call(cmd, "1", stdin, secs + 10, runner)


def unc(name, linux_path, long_paths=True):
    """The Windows path of a file inside the distribution."""
    rest = linux_path.lstrip("/").replace("/", "\\")
    if long_paths:
        base = "\\\\?\\UNC\\wsl.localhost\\" + name
    else:
        base = "\\\\wsl$\\" + name
    return base + ("\\" + rest if rest else "")


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_DRIVE_MOUNT = re.compile(r"^/mnt/[A-Za-z]/")


def _parse_env0(raw):
    """NUL-separated environment out of an interactive login shell. An rc
    file may print a banner before `exec env -0` runs; it lands in front of
    the first entry, so the first entry is cut at its last newline. Entries
    that still do not look like NAME=value are dropped."""
    entries = raw.decode("utf-8", "replace").split("\0")
    env = {}
    for i, entry in enumerate(entries):
        if i == 0 and not _ENV_NAME.match(entry) and "\n" in entry:
            entry = entry.rsplit("\n", 1)[1]
        if _ENV_NAME.match(entry):
            k, v = entry.split("=", 1)
            env[k] = v
    return env


def _passwd(out):
    for line in reversed(out.decode("utf-8", "replace").splitlines()):
        f = line.strip().split(":")
        if len(f) >= 7 and f[0] and f[5].startswith("/"):
            return f[0], f[5], f[6].strip() or "/bin/sh"
    return None


def _usable(path):
    """A claude inside Linux, not the Windows one reached through the
    automount root (/mnt/c/...) or a .exe/.cmd."""
    return (path.startswith("/") and not _DRIVE_MOUNT.match(path)
            and not path.lower().endswith((".exe", ".cmd", ".bat")))


def probe(name, exe=None, runner=subprocess.run):
    """Find the Linux user, home, shell, Claude config folder and claude
    binary of a distribution, or None if it cannot be read. Starts the
    distribution; callers only reach this after the user named it."""
    kw = dict(exe=exe, runner=runner)
    rc, out, _ = run(name, ["sh", "-c", 'getent passwd "$(id -un)"'], **kw)
    pw = _passwd(out) if rc == 0 else None
    if not pw:
        return None
    user, home, shell = pw

    # The user's own login shell, interactive so ~/.zshrc and nvm-style
    # additions to PATH count; stdin is closed so nothing waits for a tty.
    # `exec env -0` works in bash, zsh and fish, and zsh has no `type -P`.
    rc, out, _ = run(name, [shell, "-lic", "exec env -0"],
                     user=user, timeout=20, **kw)
    captured = _parse_env0(out) if rc == 0 else {}
    path = captured.get("PATH")
    if not path:
        return None
    config = captured.get("CLAUDE_CONFIG_DIR") or home + "/.claude"
    # A Windows claude on the mounted drive, early in PATH, would hide a real
    # Linux one later in it; drop those entries before looking.
    path = ":".join(d for d in path.split(":") if not _DRIVE_MOUNT.match(d + "/"))

    # Non-login sh with the captured PATH: the rc files already did their work.
    claude = None
    rc, out, _ = run(name, ["sh", "-c", "command -v claude"],
                     user=user, env={"PATH": path}, **kw)
    if rc == 0:
        lines = [l.strip() for l in out.decode("utf-8", "replace").splitlines()]
        found = [l for l in lines if l.startswith("/")]
        if found and _usable(found[-1]):
            claude = found[-1]
    if claude is None:
        for cand in (home + "/.local/bin/claude", home + "/.claude/local/claude"):
            if run(name, ["test", "-x", cand], user=user, **kw)[0] == 0:
                claude = cand
                break
    return Probe(user=user, home=home, shell=shell, config_dir=config,
                 claude_exe=claude)
