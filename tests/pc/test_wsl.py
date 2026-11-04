"""pc/wsl.py with fakes: a Python script stands in for wsl.exe, a dict for the
registry. The real wsl.exe and claude are never started (conftest refuses)."""
import os
import shutil
import subprocess
import sys

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
    assert wsl.policy_blocks(FakeReg({p: {"AllowInboxWSL": 0}}))


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


def _linux(tmp_path, *, passwd=b"bob:x:1000:1000:,,,:/home/bob:/bin/zsh\n",
           env=b"\nPATH=/n/bin:/usr/bin\0CLAUDE_CONFIG_DIR=/c\0",
           found=b"/home/bob/.nvm/bin/claude\n", executable=()):
    """A fake distribution answering the four probe steps."""
    def ans(argv):
        tail = argv[argv.index("-k") + 3:]
        if tail[0] == "sh" and "getent" in tail[2]:
            return 0, passwd, b""
        if tail[1:] == ["-lic", "exec env -0"]:
            return 0, b"Welcome to a banner\nline 2\n" + env, b""
        if tail[0] == "sh" and "command -v" in tail[2]:
            assert "PATH=/n/bin:/usr/bin" in argv
            return (0, found, b"") if found else (1, b"", b"")
        if tail[0] == "test":
            return (0 if tail[2] in executable else 1), b"", b""
        raise AssertionError(argv)
    return Rec(ans)


def test_probe_ignores_banner_noise(tmp_path):
    p = wsl.probe("U", exe=EXE, runner=_linux(tmp_path))
    assert p == wsl.Probe("bob", "/home/bob", "/bin/zsh", "/c",
                          "/home/bob/.nvm/bin/claude")


def test_probe_default_config_dir(tmp_path):
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


def test_probe_rejects_windows_claude(tmp_path):
    r = _linux(tmp_path, found=b"/mnt/c/Users/b/AppData/Roaming/npm/claude\n")
    p = wsl.probe("U", exe=EXE, runner=r)
    assert p.claude_exe is None


def test_probe_falls_back_to_local_bin(tmp_path):
    r = _linux(tmp_path, found=b"", executable=("/home/bob/.local/bin/claude",))
    assert wsl.probe("U", exe=EXE, runner=r).claude_exe == \
        "/home/bob/.local/bin/claude"
    r = _linux(tmp_path, found=b"", executable=("/home/bob/.claude/local/claude",))
    assert wsl.probe("U", exe=EXE, runner=r).claude_exe == \
        "/home/bob/.claude/local/claude"


def test_probe_none_when_unreadable(tmp_path):
    assert wsl.probe("U", exe=EXE, runner=Rec(lambda a: (1, b"", b""))) is None


@pytest.mark.skipif(os.name != "posix", reason="POSIX shells")
@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
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
    (home / ".config" / "fish" / "config.fish").write_text(
        f"{banner}\nset -gx PATH $HOME/.nvm/bin $PATH\n"
        "set -gx CLAUDE_CONFIG_DIR /cfg\n")
    env = {"PATH": os.environ["PATH"], "HOME": str(home),
           "ZDOTDIR": str(home), "XDG_CONFIG_HOME": str(home / ".config")}
    out = subprocess.run([exe, "-lic", "exec env -0"], env=env,
                         stdin=subprocess.DEVNULL, capture_output=True,
                         timeout=20)
    got = wsl._parse_env0(out.stdout)
    assert got["CLAUDE_CONFIG_DIR"] == "/cfg"
    assert got["PATH"].startswith(f"{home}/.nvm/bin")
