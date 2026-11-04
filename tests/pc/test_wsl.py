"""pc/wsl.py with fakes: a Python script stands in for wsl.exe, a dict for the
registry. The real wsl.exe and claude are never started (conftest refuses)."""
import os
import shutil
import subprocess

import pytest

from pc import wsl


class FakeReg:
    def __init__(self, tree):
        self.tree = tree

    def values(self, hive, path):
        try:
            return dict(self.tree[(hive, path)])
        except KeyError:
            raise OSError("no key")

    def subkeys(self, hive, path):
        prefix = path + "\\"
        return [p[len(prefix):] for h, p in self.tree
                if h == hive and p.startswith(prefix) and "\\" not in p[len(prefix):]]


L = wsl.LXSS


def test_distros_skip_installing():
    reg = FakeReg({
        ("HKCU", L): {"DefaultDistribution": "{B}"},
        ("HKCU", L + "\\{A}"): {"DistributionName": "Old", "State": 3, "Version": 2},
        ("HKCU", L + "\\{B}"): {"DistributionName": "Ubuntu", "State": 1, "Version": 2},
        ("HKCU", L + "\\{C}"): {"DistributionName": "Deb", "State": 1, "Version": 1},
    })
    got = wsl.distros(reg)
    assert got == [wsl.Distro("{B}", "Ubuntu", 2, True),
                   wsl.Distro("{C}", "Deb", 1, False)]
    assert wsl.distros(FakeReg({})) == []


def test_policy_blocks():
    p = ("HKLM", wsl.POLICY)
    assert not wsl.policy_blocks(FakeReg({}))
    assert not wsl.policy_blocks(FakeReg({p: {"AllowWSL": 1}}))
    assert wsl.policy_blocks(FakeReg({p: {"AllowWSL": 0}}))
    # AllowInboxWSL=0 turns off only the WSL that ships inside Windows; the
    # Store WSL that `wsl --install` gives still runs, and Microsoft's own
    # Intune recommended settings set it. Only AllowWSL=0 is "WSL off".
    assert not wsl.policy_blocks(FakeReg({p: {"AllowInboxWSL": 0}}))
    assert wsl.policy_blocks(FakeReg({p: {"AllowWSL": 0, "AllowInboxWSL": 1}}))


def test_unc_paths():
    assert wsl.unc("Ubuntu", "/home/a/.claude") == \
        "\\\\?\\UNC\\wsl.localhost\\Ubuntu\\home\\a\\.claude"
    assert wsl.unc("Ubuntu", "/home/a", long_paths=False) == \
        "\\\\wsl$\\Ubuntu\\home\\a"
    assert wsl.unc("Ubuntu", "/") == "\\\\?\\UNC\\wsl.localhost\\Ubuntu"


class Rec:
    """A runner that records the call and answers from a function."""
    def __init__(self, answer=lambda argv: (0, b"", b"")):
        self.calls, self.answer = [], answer

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        rc, out, err = self.answer(argv)
        return subprocess.CompletedProcess(argv, rc, out, err)


EXE = ("W",)


def test_run_argv_uses_dash_e():
    r = Rec()
    wsl.run("Ubuntu", ["echo", "a b"], user="bob", env={"PATH": "/x y"},
            exe=EXE, runner=r, timeout=12)
    argv, kw = r.calls[0]
    assert argv == ["W", "-d", "Ubuntu", "-u", "bob", "-e", "env", "PATH=/x y",
                    "timeout", "-k", "5", "12", "echo", "a b"]
    assert "--" not in argv
    assert kw["env"]["WSL_UTF8"] == "1"
    assert kw["stdin"] is subprocess.DEVNULL
    assert kw["timeout"] > 12
    r2 = Rec()
    wsl.run("U", ["id"], exe=EXE, runner=r2, stdin=b"x")
    assert "-u" not in r2.calls[0][0] and r2.calls[0][1]["input"] == b"x"


def test_run_passes_no_window_via_runner(monkeypatch):
    monkeypatch.setattr(wsl, "NO_WINDOW", {"creationflags": 0x08000000})
    r = Rec()
    wsl.run("U", ["id"], exe=EXE, runner=r)
    kw = r.calls[0][1]
    assert kw["creationflags"] == 0x08000000
    assert kw["stdout"] == subprocess.PIPE and kw["stderr"] == subprocess.PIPE


def test_run_timeout():
    def boom(argv, **kw):
        raise subprocess.TimeoutExpired(argv, 1)
    assert wsl.run("U", ["id"], exe=EXE, runner=boom) == (-1, b"", b"")


def test_running_decodes_utf16():
    def ans(names):
        return lambda argv: (0, "\r\n".join(names).encode("utf-16-le"), b"")
    r = Rec(ans(["Ubuntu", "Debian"]))
    assert wsl.running("ubuntu", exe=EXE, runner=r)
    assert r.calls[0][1]["env"]["WSL_UTF8"] == "0"
    assert not wsl.running("Arch", exe=EXE, runner=Rec(ans(["Ubuntu"])))
    assert not wsl.running("Ubuntu", exe=EXE, runner=Rec(ans([])))
    assert not wsl.running("Ubuntu", exe=EXE, runner=Rec(lambda a: (1, b"", b"")))


def _linux(*, passwd=b"bob:x:1000:1000:,,,:/home/bob:/bin/zsh\n",
           env=b"\nPATH=/n/bin:/usr/bin\0CLAUDE_CONFIG_DIR=/c\0",
           found=b"/home/bob/.nvm/bin/claude\n", executable=(),
           want_path="/n/bin:/usr/bin"):
    """A fake distribution answering the four probe steps."""
    def ans(argv):
        tail = argv[argv.index("-k") + 3:]
        if tail[0] == "sh" and "getent" in tail[2]:
            return 0, passwd, b""
        if tail[1:] == ["-lic", "exec env -0"]:
            return 0, b"Welcome to a banner\nline 2\n" + env, b""
        if tail[0] == "sh" and "command -v" in tail[2]:
            assert f"PATH={want_path}" in argv
            return (0, found, b"") if found else (1, b"", b"")
        if tail[0] == "test":
            return (0 if tail[2] in executable else 1), b"", b""
        raise AssertionError(argv)
    return Rec(ans)


def test_probe_ignores_banner_noise():
    p = wsl.probe("U", exe=EXE, runner=_linux())
    assert p == wsl.Probe("bob", "/home/bob", "/bin/zsh", "/c",
                          "/home/bob/.nvm/bin/claude", config_dir_set=True)


def _user_of(argv):
    return argv[argv.index("-u") + 1] if "-u" in argv else None


def test_probe_runs_steps_as_the_user_with_a_timeout():
    r = _linux()
    wsl.probe("U", exe=EXE, runner=r)
    by = {}
    for argv, kw in r.calls:
        tail = argv[argv.index("-k") + 3:]
        by["env" if "-lic" in tail else "which" if "command -v claude" in tail[-1]
           else "other"] = (argv, kw)
    for step in ("env", "which"):
        assert _user_of(by[step][0]) == "bob", step
    # 20 s on the Linux timeout(1), and the Windows side waits longer.
    env_argv = by["env"][0]
    assert env_argv[env_argv.index("-k") + 2] == "20"
    assert by["env"][1]["timeout"] > 20
    assert by["env"][1]["stdin"] is subprocess.DEVNULL


def test_probe_drops_mounted_drive_entries_from_path():
    r = _linux(env=b"PATH=/mnt/c/Users/b/npm:/mnt/d/x:/home/bob/.nvm/bin:/usr/bin\0",
               want_path="/home/bob/.nvm/bin:/usr/bin")
    p = wsl.probe("U", exe=EXE, runner=r)
    assert p.claude_exe == "/home/bob/.nvm/bin/claude"


def test_probe_empty_shell_defaults_to_sh():
    r = _linux(passwd=b"bob:x:1000:1000::/home/bob:\n")
    p = wsl.probe("U", exe=EXE, runner=r)
    assert p.shell == "/bin/sh"
    assert any("/bin/sh" in a and "-lic" in a for a, _ in r.calls)


def test_probe_default_config_dir():
    # PATH differs from _linux's assertion, so build this case directly.
    def ans(argv):
        tail = argv[argv.index("-k") + 3:]
        if "getent" in tail[-1]:
            return 0, b"bob:x:1:1::/home/bob:/bin/bash\n", b""
        if tail[1:] == ["-lic", "exec env -0"]:
            return 0, b"PATH=/usr/bin\0", b""
        return 1, b"", b""
    p = wsl.probe("U", exe=EXE, runner=Rec(ans))
    assert p.config_dir == "/home/bob/.claude" and p.claude_exe is None
    # Not set: .claude.json is then in the Linux home, not in config_dir.
    assert p.config_dir_set is False


def test_probe_rejects_windows_claude():
    r = _linux(found=b"/mnt/c/Users/b/AppData/Roaming/npm/claude\n")
    p = wsl.probe("U", exe=EXE, runner=r)
    assert p.claude_exe is None


def test_probe_falls_back_to_local_bin():
    r = _linux(found=b"", executable=("/home/bob/.local/bin/claude",))
    assert wsl.probe("U", exe=EXE, runner=r).claude_exe == \
        "/home/bob/.local/bin/claude"
    r = _linux(found=b"", executable=("/home/bob/.claude/local/claude",))
    assert wsl.probe("U", exe=EXE, runner=r).claude_exe == \
        "/home/bob/.claude/local/claude"


def test_probe_none_when_unreadable():
    assert wsl.probe("U", exe=EXE, runner=Rec(lambda a: (1, b"", b""))) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX shells")
@pytest.mark.parametrize("shell", ["bash", "zsh", "fish", "tcsh", "csh"])
def test_probe_env_capture_under_real_shells(shell, tmp_path):
    """The step-2 command, `<shell> -lic 'exec env -0'`, under the real shell
    with a fake HOME whose rc files print a banner and add an nvm-like entry
    to PATH. Local processes only: no wsl, no claude."""
    exe = shutil.which(shell)
    if not exe:
        pytest.skip(f"{shell} not installed")
    home = tmp_path / "home"
    (home / ".config" / "fish").mkdir(parents=True)
    banner = "echo 'banner: hello'"
    (home / ".bashrc").write_text(
        f"{banner}\nexport PATH=\"$HOME/.nvm/bin:$PATH\"\n"
        "export CLAUDE_CONFIG_DIR=/cfg\n")
    (home / ".bash_profile").write_text(". ~/.bashrc\n")
    (home / ".zshrc").write_text((home / ".bashrc").read_text())
    (home / ".cshrc").write_text(
        f"{banner}\nsetenv PATH \"$HOME/.nvm/bin:$PATH\"\n"
        "setenv CLAUDE_CONFIG_DIR /cfg\n")
    (home / ".config" / "fish" / "config.fish").write_text(
        f"{banner}\nset -gx PATH $HOME/.nvm/bin $PATH\n"
        "set -gx CLAUDE_CONFIG_DIR /cfg\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(home),
           "ZDOTDIR": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    out = subprocess.run([exe, *wsl.env_capture_args(exe)], env=env,
                         stdin=subprocess.DEVNULL, capture_output=True,
                         timeout=20)
    got = wsl._parse_env0(out.stdout)
    assert got["CLAUDE_CONFIG_DIR"] == "/cfg"
    assert got["PATH"].startswith(f"{home}/.nvm/bin")


# --- the final branch review, finding 1 -----------------------------------

class _OneBadKey(FakeReg):
    """A registry whose key for one GUID raises on read."""
    def __init__(self, tree, bad, exc=OSError):
        super().__init__(tree)
        self.bad, self.exc = bad, exc

    def values(self, hive, path):
        if path.endswith("\\" + self.bad):
            raise self.exc("denied")
        return super().values(hive, path)


def test_one_bad_subkey_costs_that_subkey_alone():
    tree = {("HKCU", L): {"DefaultDistribution": "{B}"},
            ("HKCU", L + "\\{A}"): {"DistributionName": "Old", "State": 1},
            ("HKCU", L + "\\{B}"): {"DistributionName": "Ubuntu", "State": 1,
                                    "Version": 2}}
    for exc in (OSError, ValueError, TypeError):
        reg = _OneBadKey(tree, "{A}", exc)
        assert wsl.distros(reg) == [wsl.Distro("{B}", "Ubuntu", 2, True)]
        found, unsure = wsl.scan(reg)
        assert [d.name for d in found] == ["Ubuntu"] and unsure == {"{a}"}


def test_scan_tells_unreadable_from_none():
    class Broken(FakeReg):
        def subkeys(self, hive, path):
            raise PermissionError("denied")
    with pytest.raises(wsl.Unreadable):
        wsl.scan(Broken({("HKCU", L): {}}))

    class Absent(FakeReg):
        def values(self, hive, path):
            raise FileNotFoundError("no Lxss key")
    # No Lxss key at all is WSL with nothing installed, not "cannot say".
    assert wsl.scan(Absent({})) == ([], set())
    # A key that is there but not installed (converting, being removed)
    # is not proof the distribution is gone.
    reg = FakeReg({("HKCU", L): {},
                   ("HKCU", L + "\\{C}"): {"DistributionName": "X",
                                           "State": 4}})
    assert wsl.scan(reg) == ([], {"{c}"})


def test_wsl_exe_falls_back(monkeypatch):
    monkeypatch.setenv("SystemRoot", "S:\\Win")
    monkeypatch.setenv("windir", "W:\\Other")
    assert wsl.wsl_exe() == (os.path.join("S:\\Win", "System32", "wsl.exe"),)
    monkeypatch.delenv("SystemRoot")
    assert wsl.wsl_exe() == (os.path.join("W:\\Other", "System32",
                                          "wsl.exe"),)
    monkeypatch.delenv("windir")
    assert wsl.wsl_exe() == (os.path.join("C:\\Windows", "System32",
                                          "wsl.exe"),)


def test_policy_unreadable_means_allowed():
    class Denied(FakeReg):
        def values(self, hive, path):
            raise PermissionError("denied")
    assert not wsl.policy_blocks(Denied({}))


@pytest.mark.parametrize("path,ok", [
    ("/home/bob/.local/bin/claude", True),
    ("/usr/bin/claude", True),
    ("/mnt/c/Users/bob/AppData/Roaming/npm/claude", False),
    ("/home/bob/bin/claude.exe", False),
    ("/home/bob/bin/claude.CMD", False),
    ("/opt/claude.bat", False),
    ("claude", False),
])
def test_usable_rejects_windows_claudes(path, ok):
    assert wsl._usable(path) is ok


# --- review of feat/enterprise-spend, W3 ----------------------------------

import sys
import time


def test_default_runner_is_the_bounded_one():
    """subprocess.run under CPython 3.11 on Windows, after a timeout, kills
    wsl.exe and then waits on communicate() with no timeout at all, while a
    grandchild can still hold the pipes; that wedges WslWatch for good. Every
    entry point must default to the runner that bounds the whole call."""
    import inspect
    for fn in (wsl.running, wsl.run, wsl.probe):
        assert inspect.signature(fn).parameters["runner"].default \
            is wsl._bounded_run, fn.__name__


def test_bounded_run_returns_while_a_grandchild_holds_the_pipes(monkeypatch):
    # The child starts a grandchild that inherits stdout and outlives it:
    # the shape of wsl.exe killed while its relay still holds the handle.
    monkeypatch.setattr(wsl, "_REAP_SECONDS", 0.3)
    code = ("import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(4)'])\n"
            "time.sleep(4)\n")
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        wsl._bounded_run([sys.executable, "-c", code],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         stdin=subprocess.DEVNULL, timeout=0.5)
    assert time.monotonic() - t0 < 3


def test_bounded_run_returns_when_the_child_never_reads_its_input(monkeypatch):
    # 3.11's Windows communicate() writes input on the calling thread before
    # its timeout starts; a child that never reads blocks that write once
    # the pipe buffer is full. Far more than any pipe buffer here.
    monkeypatch.setattr(wsl, "_REAP_SECONDS", 0.3)
    t0 = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        wsl._bounded_run([sys.executable, "-c", "import time; time.sleep(4)"],
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         input=b"x" * (4 << 20), timeout=0.5)
    assert time.monotonic() - t0 < 3


def test_bounded_run_round_trips():
    done = wsl._bounded_run(
        [sys.executable, "-c",
         "import sys; d = sys.stdin.buffer.read(); "
         "sys.stdout.buffer.write(d[::-1]); sys.stderr.write('e'); "
         "sys.exit(3)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, input=b"abc",
        timeout=20)
    assert (done.returncode, done.stdout, done.stderr) == (3, b"cba", b"e")
    done = wsl._bounded_run([sys.executable, "-c", "print(1)"],
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            stdin=subprocess.DEVNULL, timeout=20)
    assert done.returncode == 0 and done.stdout.strip() == b"1"


# --- review of feat/enterprise-spend, W5 ----------------------------------

@pytest.mark.parametrize("shell,flags", [
    ("/bin/bash", "-lic"), ("/usr/bin/zsh", "-lic"), ("/usr/bin/fish", "-lic"),
    ("/bin/sh", "-lic"), ("/bin/tcsh", "-ic"), ("/usr/bin/csh", "-ic"),
    ("/usr/local/bin/tcsh", "-ic"),
])
def test_the_csh_family_is_asked_without_l(shell, flags):
    """tcsh and csh take -l only as the sole flag, so `tcsh -lic` exits 1
    and every `install --wsl` for a csh user failed with "could not read
    the distribution". -ic reads ~/.cshrc, where they set PATH."""
    assert wsl.env_capture_args(shell) == [flags, "exec env -0"]


def test_probe_works_for_a_tcsh_user():
    def ans(argv):
        tail = argv[argv.index("-k") + 3:]
        if tail[0] == "sh" and "getent" in tail[2]:
            return 0, b"bob:x:1:1::/home/bob:/bin/tcsh\n", b""
        if tail == ["/bin/tcsh", "-ic", "exec env -0"]:
            return 0, b"PATH=/home/bob/.nvm/bin:/usr/bin\0", b""
        if tail[0] == "/bin/tcsh":
            return 1, b"", b"Unknown option"
        if tail[0] == "sh" and "command -v" in tail[2]:
            return 0, b"/home/bob/.nvm/bin/claude\n", b""
        return 1, b"", b""
    p = wsl.probe("U", exe=EXE, runner=Rec(ans))
    assert p is not None and p.claude_exe == "/home/bob/.nvm/bin/claude"


# --- review of feat/enterprise-spend, U34 ---------------------------------

@pytest.mark.parametrize("linux,want", [
    ("/home/a//.claude", "\\home\\a\\.claude"),
    ("/home/a/x/../.claude", "\\home\\a\\.claude"),
    ("/home/a/./.claude/", "\\home\\a\\.claude"),
    ("//home/a/.claude", "\\home\\a\\.claude"),
    ("/..", ""),
])
def test_unc_normalises_what_a_long_path_will_not(linux, want):
    """A \\\\?\\ path is handed to the filesystem as it is: Windows does
    not collapse `..`, `.` or an empty component in one, so a
    CLAUDE_CONFIG_DIR of /home/a//.claude or /home/a/x/../.claude, which
    Linux reads happily, named nothing from Windows."""
    assert wsl.unc("U", linux) == "\\\\?\\UNC\\wsl.localhost\\U" + want
    assert wsl.unc("U", linux, long_paths=False) == "\\\\wsl$\\U" + want
