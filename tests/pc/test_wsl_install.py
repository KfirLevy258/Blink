"""Installing the status line and hooks inside a WSL distribution.

The daemon stays on Windows with the board; Claude Code runs inside Linux
(docs/plans/enterprise-spend.md, section 4). So the settings are merged on
Windows with the same code a native install uses, then written back inside
the distribution as the user, through a script that follows a symlink and
refuses a file that changed since it was read; the shims there write into a
Windows folder the daemon owns, named in $HOME/.blink/out.

No real wsl.exe is started (tests/conftest.py refuses it). The `wsl_run`
these tests hand in runs the same argv under this machine's real `sh`
against a tmp tree, which stands in for the Linux home -- so the scripts
that go into the distribution are exercised for real, POSIX only.
"""
import functools
import hashlib
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys

import pytest

from pc import (claude_home, cli, install_hooks, install_statusline, wsl,
                wsl_watch)

GUID = "{5C3D2B1A-0000-4000-8000-00000000ABCD}"
REPO = os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))))

posix_only = pytest.mark.skipif(sys.platform == "win32",
                                reason="runs the WSL scripts under real sh")


@pytest.fixture(autouse=True)
def _no_recapture_wait(monkeypatch):
    """uninstall_from waits HOOK_RECAPTURE_S between taking the hooks out
    and removing the shims they call; the tests that care say so."""
    monkeypatch.setattr(install_statusline, "HOOK_RECAPTURE_S", 0)


def _home(lin, name="Ubuntu"):
    """A WSL home whose Linux home is the tmp folder `lin`. Its config_dir
    is the UNC spelling of lin/.claude, exactly as install would record it;
    install_into turns that back into the Linux path."""
    lin = str(lin)
    return claude_home.wsl(distro_guid=GUID, distro_name=name,
                           linux_user="zoe", linux_home=lin,
                           config_dir=wsl.unc(name, lin + "/.claude"),
                           config_dir_set=False)


def _fake_bin(tmp_path, wslpath_body=None):
    """A PATH folder holding a fake wslpath. On this machine the drop
    folder is already a POSIX path, so the fake prints it back."""
    b = tmp_path / "fakebin"
    b.mkdir(exist_ok=True)
    body = wslpath_body or '[ "$1" = -u ] && shift\nprintf \'%s\\n\' "$1"\n'
    p = b / "wslpath"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(0o755)
    return b


def _runner(lin, bin_dir, calls=None, before=None, shell="sh"):
    """A wsl_run: argv run here, under real sh, with HOME the Linux home.
    `before(argv)` runs first, to change the tree between read and write.
    `shell` stands in for /bin/sh: dash is what Debian and Ubuntu run as sh,
    and macOS's sh is bash, which forgives more. Looked up here, before a
    test pretends to be Windows, where shutil.which reads PATHEXT."""
    shell_path = shutil.which(shell)

    def run(argv, stdin=None):
        if calls is not None:
            calls.append(list(argv))
        if before is not None:
            before(list(argv))
        if argv and argv[0] == "sh":
            argv = [shell_path] + list(argv[1:])
        env = {"HOME": str(lin), "LC_ALL": "C",
               "PATH": f"{bin_dir}:/usr/bin:/bin:/usr/sbin:/sbin"}
        done = subprocess.run(list(argv), input=stdin, capture_output=True,
                              env=env, timeout=30)
        return done.returncode, done.stdout, done.stderr
    return run


def _sl_cmd(lin):
    return "sh " + shlex.quote(str(lin) + "/.blink/blink-statusline.sh")


def _hook_cmd(lin, event):
    return "sh " + shlex.quote(str(lin) + "/.blink/blink-hook.sh") + " " + event


def _all_commands(doc):
    out = []
    sl = doc.get("statusLine")
    if isinstance(sl, dict):
        out.append(sl.get("command", ""))
    for groups in (doc.get("hooks") or {}).values():
        for g in groups:
            for h in g.get("hooks", []):
                out.append(h.get("command", ""))
    return out


# --------------------------------------------------------------- the shims

@posix_only
def test_shim_reads_its_out_folder(tmp_path):
    """With $HOME/.blink/out present, both shims write into the folder it
    names -- a Windows folder, through the automount, whose path may hold a
    space and a letter outside ASCII -- and the chain file stays where it
    was, inside the distribution."""
    home = tmp_path / "home"
    (home / ".blink").mkdir(parents=True)
    out = tmp_path / "x" / "Zoë Smith" / ".blink" / ("wsl-" + GUID.strip("{}").lower())
    out.mkdir(parents=True)
    (home / ".blink" / "out").write_bytes((str(out) + "\n").encode("utf-8"))
    (home / ".blink" / "statusline-chain").write_text("printf CHAINED\n")
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}

    payload = b'{"session_id":"abc","rate_limits":{}}'
    done = subprocess.run(["sh", os.path.join(REPO, "tools", "blink-statusline.sh")],
                          input=payload, capture_output=True, env=env,
                          timeout=30)
    assert done.returncode == 0
    assert done.stdout == b"CHAINED"
    assert (out / "statusline.json").read_bytes() == payload
    assert not (home / ".blink" / "statusline.json").exists()

    done = subprocess.run(["sh", os.path.join(REPO, "tools", "blink-hook.sh"),
                           "UserPromptSubmit"],
                          input=b'{"session_id":"abc","cwd":"/w/proj"}',
                          capture_output=True, env=env, timeout=30)
    assert done.returncode == 0
    assert (out / "state" / "abc.state").exists()
    assert not (home / ".blink" / "state").exists()


@posix_only
def test_shim_without_an_out_file_writes_where_it_always_did(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run(["sh", os.path.join(REPO, "tools", "blink-statusline.sh")],
                   input=b"{}", capture_output=True, env=env, timeout=30)
    assert (home / ".blink" / "statusline.json").read_bytes() == b"{}"


@posix_only
def test_an_out_file_without_a_newline_still_counts(tmp_path):
    """`read` returns non-zero on a last line with no newline but has set
    the variable; a hand-written file has none."""
    home = tmp_path / "home"
    (home / ".blink").mkdir(parents=True)
    out = tmp_path / "drop"
    out.mkdir()
    (home / ".blink" / "out").write_text(str(out))
    env = {"HOME": str(home), "PATH": "/usr/bin:/bin"}
    subprocess.run(["sh", os.path.join(REPO, "tools", "blink-statusline.sh")],
                   input=b"{}", capture_output=True, env=env, timeout=30)
    assert (out / "statusline.json").read_bytes() == b"{}"


# ------------------------------------------------------ the merge functions

def _write_json(path, doc):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2)
        f.write("\n")


@pytest.mark.parametrize("start", [
    {},
    {"theme": "dark"},
    {"statusLine": {"type": "command", "command": "my-bar --x"}},
    {"statusLine": "not-an-object",
     "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "beep"}]}]}},
])
def test_merge_functions_match_native_install(tmp_path, start):
    """The pure merges give exactly what install() and uninstall() write,
    so a WSL install is the native one applied to another file."""
    settings = str(tmp_path / "s.json")
    shim = str(tmp_path / "blink-statusline.sh")
    hook = str(tmp_path / "blink-hook.sh")
    _write_json(settings, start)

    # Native, through the files.
    install_statusline.install(settings, shim)
    install_hooks.install(settings, hook)
    with open(settings) as f:
        native_doc = json.load(f)
    native_marker = install_statusline._read_marker()
    native_hooks_marker = install_hooks._read_marker()
    chain = install_statusline._chain_path()
    native_chain = open(chain).read() if os.path.exists(chain) else None

    # Pure, on the same starting document.
    doc = json.loads(json.dumps(start))
    doc, marker, action, msg = install_statusline.merge_install(
        doc, "", None, install_statusline.statusline_command(shim))
    doc, hmarker, added, repointed = install_hooks.merge_install(
        doc, set(), functools.partial(install_hooks.hook_command, hook))
    assert doc == native_doc
    assert marker == native_marker
    assert hmarker == native_hooks_marker
    assert added == len(install_hooks.HOOK_EVENTS) and repointed == 0
    if native_chain is None:
        assert action in (None, ("delete",))
    else:
        assert action == ("write", native_chain)
    assert msg.startswith("Blink statusline installed")

    # And back out.
    install_statusline.uninstall(settings, shim)
    install_hooks.uninstall(settings, hook)
    with open(settings) as f:
        native_back = json.load(f)
    doc2, m2, action2, _ = install_statusline.merge_uninstall(
        doc, marker, native_chain, install_statusline.statusline_command(shim))
    doc2, removed = install_hooks.merge_uninstall(
        doc2, hmarker, functools.partial(install_hooks.hook_command, hook))
    assert doc2 == native_back
    assert m2 == ""
    assert action2 == ("delete",)
    assert removed == len(install_hooks.HOOK_EVENTS)


def test_merge_uninstall_leaves_a_foreign_command_alone():
    doc = {"statusLine": {"type": "command", "command": "theirs"}}
    out, marker, action, msg = install_statusline.merge_uninstall(
        dict(doc), "sh /old/blink-statusline.sh", "mine\n", "sh /x")
    assert out is None and marker == "sh /old/blink-statusline.sh"
    assert action is None and "leaving it alone" in msg


# --------------------------------------------------------------- install_into

def _tree(tmp_path, settings_doc=None, link=True):
    """A Linux home in tmp: lin/.claude/settings.json, a symlink into a
    dotfiles folder when `link`."""
    lin = tmp_path / "lin"
    (lin / ".claude").mkdir(parents=True)
    target = lin / ".claude" / "settings.json"
    if settings_doc is not None:
        if link:
            (lin / "dotfiles").mkdir()
            real = lin / "dotfiles" / "settings.json"
            _write_json(str(real), settings_doc)
            real.chmod(0o640)
            target.symlink_to(real)
        else:
            _write_json(str(target), settings_doc)
    return lin


SHELLS = [pytest.param(sh, marks=pytest.mark.skipif(
    shutil.which(sh) is None, reason=f"no {sh} here")) for sh in ("sh", "dash")]


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_install_into_keeps_a_symlink(tmp_path, monkeypatch, shell):
    """Review focus 4: settings.json is a symlink into a dotfiles repo.
    Install and uninstall both write the file it points at and leave the
    link a link. On "Windows", so the command form is proved to be the
    distribution's (sh), not the one Windows' own Claude Code needs."""
    lin = _tree(tmp_path, {"theme": "dark",
                           "statusLine": {"type": "command",
                                          "command": "my-bar"}})
    home = _home(lin)
    run = _runner(lin, _fake_bin(tmp_path), shell=shell)
    monkeypatch.setattr(sys, "platform", "win32")

    msg = install_statusline.install_into(home, run)
    assert "installed" in msg

    link = lin / ".claude" / "settings.json"
    real = lin / "dotfiles" / "settings.json"
    assert link.is_symlink()
    doc = json.loads(real.read_text())
    assert doc["theme"] == "dark"
    assert doc["statusLine"] == {"type": "command", "command": _sl_cmd(lin)}
    for ev, _ in install_hooks.HOOK_EVENTS:
        assert any(h["command"] == _hook_cmd(lin, ev)
                   for g in doc["hooks"][ev] for h in g["hooks"])
    assert not any(c.startswith("bash ") or "USERPROFILE" in c
                   for c in _all_commands(doc))
    # The mode a customer chose is kept.
    assert stat.S_IMODE(real.stat().st_mode) == 0o640
    # No temp file left beside it.
    assert sorted(os.listdir(lin / "dotfiles")) == ["settings.json"]

    # The chain file is inside the distribution, where its shim reads it.
    assert (lin / ".blink" / "statusline-chain").read_text() == "my-bar\n"
    # The out file names the drop folder, which Windows made first.
    assert os.path.isdir(home.drop_dir)
    assert os.path.isdir(home.state_dir)
    assert (lin / ".blink" / "out").read_bytes() == \
        (home.drop_dir + "\n").encode("utf-8")
    # The shims, byte for byte, executable.
    for name in ("blink-statusline.sh", "blink-hook.sh"):
        p = lin / ".blink" / name
        assert p.read_bytes() == cli._shim_source(name).encode("utf-8")
        assert os.access(p, os.X_OK)
    # The markers, on Windows, in this home's own folder.
    assert install_statusline._read_marker(home) == _sl_cmd(lin)
    assert install_hooks._read_marker(home) == {
        _hook_cmd(lin, ev) for ev, _ in install_hooks.HOOK_EVENTS}
    assert install_statusline._read_marker(claude_home.native()) == ""

    # A second install changes nothing and chains nothing.
    install_statusline.install_into(home, run)
    assert (lin / ".blink" / "statusline-chain").read_text() == "my-bar\n"
    assert json.loads(real.read_text()) == doc

    msg = install_statusline.uninstall_from(home, run)
    assert link.is_symlink()
    back = json.loads(real.read_text())
    assert back == {"theme": "dark",
                    "statusLine": {"type": "command", "command": "my-bar"}}
    assert not (lin / ".blink" / "statusline-chain").exists()
    assert install_statusline._read_marker(home) == ""
    assert install_hooks._read_marker(home) == set()


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_install_into_creates_a_missing_settings_file(tmp_path, shell):
    lin = _tree(tmp_path)
    home = _home(lin)
    install_statusline.install_into(
        home, _runner(lin, _fake_bin(tmp_path), shell=shell))
    p = lin / ".claude" / "settings.json"
    assert json.loads(p.read_text())["statusLine"]["command"] == _sl_cmd(lin)
    # A new file is the user's alone, as a native one is.
    assert stat.S_IMODE(p.stat().st_mode) == 0o600


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_install_into_refuses_a_changed_file(tmp_path, shell):
    """Claude Code -- or the user -- wrote settings.json between our read
    and our write. The write script sees a different SHA-256 and writes
    nothing, and the error names no path inside the distribution."""
    lin = _tree(tmp_path, {"theme": "dark"}, link=False)
    target = lin / ".claude" / "settings.json"
    home = _home(lin)

    def before(argv):
        # The settings write is the one call whose `want` is a SHA-256
        # (every write carries the SHA-256 of what it writes, after it).
        if (argv[2] == install_statusline._WRITE and len(argv[5]) == 64
                and all(c in "0123456789abcdef" for c in argv[5])):
            _write_json(str(target), {"theme": "light"})

    run = _runner(lin, _fake_bin(tmp_path), before=before, shell=shell)
    with pytest.raises(install_statusline.WslInstallError) as e:
        install_statusline.install_into(home, run)
    assert "changed" in str(e.value)
    assert str(lin) not in str(e.value)
    assert json.loads(target.read_text()) == {"theme": "light"}
    assert not [n for n in os.listdir(lin / ".claude") if "tmp" in n]
    # Nothing recorded for a write that did not happen.
    assert install_statusline._read_marker(home) == ""


@posix_only
def test_install_into_says_when_automount_is_off(tmp_path):
    lin = _tree(tmp_path, {})
    home = _home(lin)
    bad = _fake_bin(tmp_path, "echo 'wslpath: no such file' >&2\nexit 1\n")
    with pytest.raises(install_statusline.WslInstallError) as e:
        install_statusline.install_into(home, _runner(lin, bad))
    assert "automount" in str(e.value)
    assert str(lin) not in str(e.value) and home.drop_dir not in str(e.value)
    assert json.loads((lin / ".claude" / "settings.json").read_text()) == {}


# ------------------------------------------------------------- the watchdog

@posix_only
def test_watchdog_never_writes_the_windows_form_into_wsl(tmp_path, monkeypatch):
    """On Windows the native command is `bash "$USERPROFILE/..."`, which
    inside Linux names nothing. A WSL home's drift repair writes the
    distribution's own `sh '<linux_home>/...'` and nothing else."""
    lin = _tree(tmp_path, {"theme": "dark"}, link=False)
    target = lin / ".claude" / "settings.json"
    home = _home(lin)
    run = _runner(lin, _fake_bin(tmp_path))
    monkeypatch.setattr(sys, "platform", "win32")
    install_statusline.install_into(home, run)

    # Something wiped the status line.
    doc = json.loads(target.read_text())
    del doc["statusLine"]
    _write_json(str(target), doc)

    dog = install_statusline.DriftWatchdog(
        cli.settings_path(home), cli.shim_path(home),
        check=functools.partial(install_statusline.drift_check, wsl_run=run),
        home=home)
    msg = dog.tick()
    assert msg and "restored" in msg
    doc = json.loads(target.read_text())
    assert doc["statusLine"]["command"] == _sl_cmd(lin)
    assert not any(c.startswith("bash ") or "USERPROFILE" in c or "\\" in c
                   for c in _all_commands(doc))
    # Intact now: the next check does nothing.
    assert install_statusline.drift_check(
        cli.settings_path(home), cli.shim_path(home), home, wsl_run=run) is None


def test_watchdog_with_a_wsl_home_never_touches_the_unc_path(tmp_path,
                                                             monkeypatch):
    """The settings path a WSL home's watchdog is built with is a UNC
    string; drift_check must go through wsl_run, never _load or _save on
    it -- those are the native form's writers."""
    home = _home("/home/zoe")
    install_statusline._write_marker(_sl_cmd("/home/zoe"), home)
    touched = []
    monkeypatch.setattr(install_statusline, "_load",
                        lambda p: touched.append(p) or {})
    monkeypatch.setattr(install_statusline, "_save",
                        lambda *a, **k: touched.append(a))
    install_statusline.drift_check(cli.settings_path(home),
                                   cli.shim_path(home), home,
                                   wsl_run=lambda argv, stdin=None: (1, b"", b""))
    assert touched == []


def _drift_messages(tmp_path, current_cmd, fail_write):
    lin = tmp_path / "lin"
    home = _home(lin)
    install_statusline._write_marker(_sl_cmd(lin), home)
    body = json.dumps({"statusLine": {"type": "command",
                                      "command": current_cmd}}).encode()
    target = str(lin) + "/.claude/settings.json"

    def run(argv, stdin=None):
        script = argv[2] if len(argv) > 2 else ""
        if "readlink" in script:
            return 0, (target + "\npresent\n").encode() + body, b""
        if fail_write:
            return 1, b"", ("cannot write " + target).encode()
        return 0, b"", b""
    return home, install_statusline.drift_check(
        cli.settings_path(home), cli.shim_path(home), home, wsl_run=run)


@pytest.mark.parametrize("current", ["", "sh /home/zoe/my-bar.sh",
                                     "sh /old/blink-statusline.sh"])
@pytest.mark.parametrize("fail_write", [False, True])
def test_drift_error_messages_hold_no_wsl_path(tmp_path, current, fail_write):
    """What the drift check returns goes into bridge.log, the file customers
    send us. A Linux path or a UNC path is somebody's user name and project
    folder; the replacement command is somebody's own script."""
    home, msg = _drift_messages(tmp_path, current, fail_write)
    assert msg
    for secret in (str(tmp_path), home.config_dir, home.linux_home,
                   "/home/zoe", "wsl.localhost", "my-bar", "/old/"):
        assert secret not in msg, (secret, msg)


# ----------------------------------------------------------- the temp sweep

def test_tmp_sweep_covers_state(tmp_path):
    """The shims write statusline.json.$$.tmp and $sid.state.$$.tmp. A
    render killed between the write and the mv leaves one behind, on a
    Windows folder nothing else sweeps."""
    drop = tmp_path / "drop"
    (drop / "state").mkdir(parents=True)
    now = 1_000_000.0
    old = [drop / "statusline.json.123.tmp", drop / "state" / "abc.state.456.tmp"]
    fresh = [drop / "statusline.json.124.tmp", drop / "state" / "abc.state.457.tmp"]
    keep = [drop / "statusline.json", drop / "state" / "abc.state",
            drop / "notes.txt"]
    for p in old + fresh + keep:
        p.write_text("x")
    for p in old + keep:
        os.utime(p, (now - 61, now - 61))
    for p in fresh:
        os.utime(p, (now - 5, now - 5))

    wsl_watch._sweep_shim_temps(str(drop), now)
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in fresh + keep)


def test_tmp_sweep_of_a_missing_folder_is_quiet(tmp_path):
    wsl_watch._sweep_shim_temps(str(tmp_path / "nope"), 0.0)


# ----------------------------------------- the Task 2 note: markers per home

def test_holds_ours_reads_the_homes_hook_marker(tmp_path):
    """_holds_ours read the hook marker with no home: the native one,
    whatever home it was asked about."""
    h = _home("/home/zoe")
    install_hooks._write_marker({"sh /gone/blink-hook.sh Stop"}, h)
    install_statusline._write_marker("sh /gone/blink-statusline.sh", h)
    p = str(tmp_path / "settings.json")
    _write_json(p, {"hooks": {"Stop": [{"hooks": [
        {"type": "command", "command": "sh /gone/blink-hook.sh Stop"}]}]}})
    assert cli._holds_ours(p, h) is True
    _write_json(p, {"statusLine": {"type": "command",
                                   "command": "sh /gone/blink-statusline.sh"}})
    assert cli._holds_ours(p, h) is True
    assert cli._holds_ours(p, claude_home.native()) is False


# ------------------------------------------------------- review of e000823

@posix_only
def test_uninstall_stops_when_the_chain_file_cannot_be_read(tmp_path):
    """The chain read exits 0 for a missing file, so a failure there is
    wsl.exe failing. Taken for "no chain", uninstall dropped statusLine and
    deleted the customer's only copy of their command."""
    lin = _tree(tmp_path, {"statusLine": {"type": "command",
                                          "command": "my-bar"}}, link=False)
    home = _home(lin)
    real = _runner(lin, _fake_bin(tmp_path))
    install_statusline.install_into(home, real)
    target = lin / ".claude" / "settings.json"
    before = target.read_bytes()
    chain = lin / ".blink" / "statusline-chain"

    def run(argv, stdin=None):
        if len(argv) > 2 and argv[2] == install_statusline._CAT_OR_NOTHING:
            return -1, b"", b""
        return real(argv, stdin)

    with pytest.raises(install_statusline.WslInstallError) as e:
        install_statusline.uninstall_from(home, run)
    assert "chain" in str(e.value) and str(lin) not in str(e.value)
    assert target.read_bytes() == before
    assert chain.read_text() == "my-bar\n"
    assert install_statusline._read_marker(home) == _sl_cmd(lin)


@posix_only
def test_uninstall_says_when_the_shims_were_not_removed(tmp_path):
    lin = _tree(tmp_path, {}, link=False)
    home = _home(lin)
    real = _runner(lin, _fake_bin(tmp_path))
    install_statusline.install_into(home, real)

    def run(argv, stdin=None):
        if len(argv) > 2 and argv[2] == install_statusline._RM \
                and any(a.endswith("blink-hook.sh") for a in argv):
            return 1, b"", b""
        return real(argv, stdin)

    msg = install_statusline.uninstall_from(home, run)
    assert "Could not remove Blink's shims" in msg
    assert str(lin) not in msg


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a mode-000 file")
@pytest.mark.parametrize("shim", ["blink-statusline.sh", "blink-hook.sh"])
def test_an_unreadable_out_file_prints_nothing(tmp_path, shim):
    home = tmp_path / "home"
    (home / ".blink").mkdir(parents=True)
    out = home / ".blink" / "out"
    out.write_text("/nowhere\n")
    out.chmod(0o000)
    try:
        done = subprocess.run(["sh", os.path.join(REPO, "tools", shim), "Stop"],
                              input=b'{"session_id":"a"}', capture_output=True,
                              env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                              timeout=30)
    finally:
        out.chmod(0o600)
    assert done.returncode == 0
    assert done.stderr == b""


# ------------------------------------- the shims after `blink update` (finding 3)

@posix_only
def test_wsl_watchdog_refreshes_stale_shims_and_the_out_file(tmp_path,
                                                            monkeypatch):
    """`blink update` swaps the program and never rewrites a shim; natively
    shim_content_check catches that, and inside WSL nothing did. The WSL
    watchdog now reads each shim through wsl_run, rewrites one that is not
    the bundle's, and puts back an out file that is gone -- saying so with
    no path in it, since this goes into bridge.log."""
    lin = _tree(tmp_path, {}, link=False)
    home = _home(lin)
    run = _runner(lin, _fake_bin(tmp_path))
    install_statusline.install_into(home, run)
    hook = lin / ".blink" / "blink-hook.sh"
    hook.write_text("#!/bin/sh\n# an older release\nexit 0\n")
    (lin / ".blink" / "out").unlink()

    dog = install_statusline.wsl_drift_watchdog(home, run)
    msg = dog.tick()
    assert msg and "blink-hook.sh" in msg and "out file" in msg
    assert "blink-statusline.sh" not in msg
    for secret in (str(tmp_path), home.drop_dir, "wsl.localhost"):
        assert secret not in msg
    assert hook.read_bytes() == cli._shim_source("blink-hook.sh").encode()
    assert os.access(hook, os.X_OK)
    assert (lin / ".blink" / "out").read_bytes() == \
        (home.drop_dir + "\n").encode("utf-8")
    # Current now: a check that is due again finds nothing to do.
    assert install_statusline.wsl_drift_watchdog(home, run).tick() is None


@posix_only
def test_wsl_shim_refresh_respects_an_uninstall(tmp_path):
    """No marker means the user uninstalled: nothing is put back."""
    lin = _tree(tmp_path, {}, link=False)
    home = _home(lin)
    calls = []
    run = _runner(lin, _fake_bin(tmp_path), calls=calls)
    assert install_statusline.wsl_drift_watchdog(home, run).tick() is None
    assert calls == []
    assert not (lin / ".blink" / "blink-hook.sh").exists()


def test_wsl_shim_refresh_does_not_rewrite_what_it_could_not_read(tmp_path):
    """wsl.exe failing is "cannot tell", not "stale": no write follows."""
    home = _home("/home/zoe")
    install_statusline._write_marker(_sl_cmd("/home/zoe"), home)
    calls = []

    def run(argv, stdin=None):
        calls.append(argv)
        return -1, b"", b""
    assert install_statusline.wsl_shim_check(home, run) is None
    assert all(stdin_less[2] != install_statusline._WRITE
               for stdin_less in calls)


# ------------------------------- review of feat/enterprise-spend, U30 (b, c)

@posix_only
@pytest.mark.parametrize("bad", ["crlf", "stale", "unreadable"])
def test_wsl_watchdog_rewrites_an_out_file_that_names_the_wrong_folder(
        tmp_path, bad):
    """The repair put back only an empty out file. One saved with a CR
    (an editor on the Windows side) names a folder that ends in \\r, and
    one left from an older Windows profile names a folder the daemon never
    reads: either way the panel stops with no error. Both are rewritten,
    and so is one the user cannot read, which the shim treats as absent."""
    lin = _tree(tmp_path, {}, link=False)
    home = _home(lin)
    run = _runner(lin, _fake_bin(tmp_path))
    install_statusline.install_into(home, run)
    out = lin / ".blink" / "out"
    good = out.read_bytes()
    if bad == "crlf":
        out.write_bytes(good.rstrip(b"\n") + b"\r\n")
    elif bad == "stale":
        out.write_bytes(b"/mnt/c/Users/zoe-old-profile/.blink/wsl-x\n")
    else:
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            pytest.skip("root reads a mode-000 file")
        out.chmod(0o000)
    msg = install_statusline.wsl_shim_check(home, run)
    assert msg and "out file" in msg
    for secret in (str(tmp_path), home.drop_dir, "zoe-old-profile"):
        assert secret not in msg
    assert out.read_bytes() == good
    assert install_statusline.wsl_shim_check(home, run) is None


@posix_only
def test_wsl_watchdog_leaves_an_out_file_it_cannot_check(tmp_path):
    """wslpath failing (automount turned off since) is "cannot tell" for
    an out file that is there: it is left as it is, and nothing is said."""
    lin = _tree(tmp_path, {}, link=False)
    home = _home(lin)
    install_statusline.install_into(home, _runner(lin, _fake_bin(tmp_path)))
    out = lin / ".blink" / "out"
    out.write_bytes(b"/somewhere/else\n")
    broken = tmp_path / "broken"
    broken.mkdir()
    run = _runner(lin, _fake_bin(broken, "exit 1\n"))
    assert install_statusline.wsl_shim_check(home, run) is None
    assert out.read_bytes() == b"/somewhere/else\n"


@posix_only
@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root reads a mode-000 file")
@pytest.mark.parametrize("shim", ["blink-statusline.sh", "blink-hook.sh"])
def test_an_unreadable_out_file_ignores_an_exported_out_dir(tmp_path, shim):
    """U30(c): `read` fails on an unreadable out file and leaves out_dir as
    it was, so an `out_dir` exported in the user's environment became the
    folder the payload was written into. The shims clear it first."""
    home = tmp_path / "home"
    (home / ".blink").mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    out = home / ".blink" / "out"
    out.write_text("/nowhere\n")
    out.chmod(0o000)
    try:
        subprocess.run(["sh", os.path.join(REPO, "tools", shim),
                        "UserPromptSubmit"],
                       input=b'{"session_id":"a","cwd":"/p"}',
                       capture_output=True, timeout=30,
                       env={"HOME": str(home), "PATH": "/usr/bin:/bin",
                            "out_dir": str(elsewhere)})
    finally:
        out.chmod(0o600)
    assert list(elsewhere.iterdir()) == []


# ------------------------------------ review of feat/enterprise-spend, U32

@posix_only
@pytest.mark.parametrize("hooks", [
    "not an object",
    {"UserPromptSubmit": [], "Stop": {"hooks": [{"type": "command",
                                                 "command": "x"}]}},
])
def test_a_hooks_section_it_cannot_edit_still_gets_the_status_line(
        tmp_path, hooks):
    """Natively a hooks section Blink cannot safely edit costs the activity
    light and nothing else (cli: "Activity hooks ... skipped"). Inside WSL
    the same SettingsUnreadable stopped the whole install, status line and
    all. Now it installs the status line, leaves "hooks" exactly as it was
    -- an event merged before the bad one is not kept either -- and says
    the hooks were skipped."""
    lin = _tree(tmp_path, {"statusLine": {"type": "command",
                                          "command": "echo MINE"},
                           "hooks": hooks}, link=False)
    home = _home(lin)
    msg = install_statusline.install_into(home,
                                          _runner(lin, _fake_bin(tmp_path)))
    assert "skipped" in msg
    doc = json.loads((lin / ".claude" / "settings.json").read_text())
    assert doc["statusLine"]["command"] == _sl_cmd(lin)
    assert doc["hooks"] == hooks
    assert install_hooks._read_marker(home) == set()
    assert (lin / ".blink" / "statusline-chain").read_text().strip() == \
        "echo MINE"


# ------------------------------------ review of feat/enterprise-spend, U31

_WSLPATH_WITH_A_DRIVE = r'''
if [ "$1" = -w ]; then
  case $2 in */mnt/c/*) printf 'C:\\Users\\zoe\\.claude\n' ;;
             *) printf '\\\\wsl.localhost\\Ubuntu%s\n' "$2" ;; esac
  exit 0
fi
[ "$1" = -u ] && shift
printf '%s\n' "$1"
'''


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_install_refuses_settings_on_the_windows_drive(tmp_path, shell):
    """(a) ~/.claude inside WSL linked into the Windows profile: the WSL
    install wrote `sh /home/...` commands into the settings Claude Code on
    Windows reads, blanking its status line and breaking its hooks, and
    chained Windows' own `bash "$USERPROFILE/..."` command into Linux. It is
    refused, with no path in the message; uninstall still works there, to
    take out what an earlier install put in."""
    lin = tmp_path / "lin"
    lin.mkdir()
    win = tmp_path / "mnt" / "c" / "Users" / "zoe" / ".claude"
    win.mkdir(parents=True)
    before = {"statusLine": {"type": "command",
                             "command": 'bash "$USERPROFILE/.blink/x.sh"'}}
    _write_json(str(win / "settings.json"), before)
    (lin / ".claude").symlink_to(win)
    home = _home(lin)
    run = _runner(lin, _fake_bin(tmp_path, _WSLPATH_WITH_A_DRIVE),
                  shell=shell)
    with pytest.raises(install_statusline.WslInstallError) as e:
        install_statusline.install_into(home, run)
    assert "Windows" in str(e.value)
    for secret in (str(tmp_path), "zoe"):
        assert secret not in str(e.value)
    assert json.loads((win / "settings.json").read_text()) == before
    # The drift check reads the same way and so does nothing.
    install_statusline._write_marker(_sl_cmd(lin), home)
    assert install_statusline.drift_check("", "", home, wsl_run=run) is None
    assert json.loads((win / "settings.json").read_text()) == before
    # Uninstall is not refused: it only ever takes Blink out.
    doc = {"statusLine": {"type": "command", "command": _sl_cmd(lin)}}
    _write_json(str(win / "settings.json"), doc)
    install_statusline.uninstall_from(home, run)
    assert "statusLine" not in json.loads((win / "settings.json").read_text())


@posix_only
@pytest.mark.parametrize("shell", SHELLS)
def test_a_dangling_link_into_a_missing_folder_stays_a_link(tmp_path, shell):
    """(b) settings.json a link to a dotfiles path whose folder is not there
    yet. GNU readlink -f fails on it, the script fell back to the link
    itself, and the mv replaced the link with a plain file -- the dotfiles
    loss _save's comment describes. Natively _save follows the link and
    makes the folder; so does this now."""
    lin = tmp_path / "lin"
    (lin / ".claude").mkdir(parents=True)
    target = lin / "dotfiles" / "claude" / "settings.json"
    link = lin / ".claude" / "settings.json"
    link.symlink_to(target)
    home = _home(lin)
    run = _runner(lin, _fake_bin(tmp_path), shell=shell)
    real_readlink = shutil.which("readlink")
    # macOS readlink -f resolves a missing parent; GNU's does not. Stand in
    # for GNU: -f fails when the target's folder is absent.
    fake = tmp_path / "fakebin" / "readlink"
    fake.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = -f ]; then shift; [ "$1" = -- ] && shift\n'
        '  t=$(' + real_readlink + ' "$1") || exit 1\n'
        '  [ -d "${t%/*}" ] || exit 1\nfi\n'
        'exec ' + real_readlink + ' "$@"\n')
    fake.chmod(0o755)
    install_statusline.install_into(home, run)
    assert link.is_symlink()
    assert json.loads(target.read_text())["statusLine"]["command"] == \
        _sl_cmd(lin)


# ------------------------------------ review of feat/enterprise-spend, U29

@posix_only
def test_uninstall_waits_for_claude_code_to_drop_the_hooks(tmp_path,
                                                            monkeypatch):
    """Claude Code re-reads its hooks a second or so after settings.json
    changes, and until then runs the old ones. Removing the hook shim at
    once left that second's hook calling a missing file: dash exits 2,
    which Claude Code takes as "block". The shims go after a short wait,
    and only when hooks were taken out."""
    lin = _tree(tmp_path, {}, link=False)
    home = _home(lin)
    order = []
    real = _runner(lin, _fake_bin(tmp_path))

    def run(argv, stdin=None):
        if len(argv) > 2:
            order.append("rm" if argv[2] == install_statusline._RM
                         else "write" if argv[2] == install_statusline._WRITE
                         else "read")
        return real(argv, stdin)
    install_statusline.install_into(home, real)
    monkeypatch.setattr(install_statusline, "HOOK_RECAPTURE_S", 2.0)
    monkeypatch.setattr(install_statusline.time, "sleep",
                        lambda s: order.append(("sleep", s)))
    install_statusline.uninstall_from(home, run)
    assert order.index(("sleep", 2.0)) > order.index("write")
    # The last rm is the shims' (the first may be the chain file's).
    assert order[-1] == "rm" and order[-2] == ("sleep", 2.0)
    # Nothing of ours in settings: nothing to wait for.
    order.clear()
    install_statusline.uninstall_from(home, run)
    assert ("sleep", 2.0) not in order


@posix_only
@pytest.mark.parametrize("theirs", [["sh", "/home/zoe/x.sh"], {"run": "x"}])
def test_a_command_that_is_not_a_string_is_left_alone_inside(tmp_path,
                                                              theirs):
    """A hook inside the distribution whose "command" is a list or object.
    install_into and uninstall_from step over it; both used to raise
    TypeError, and `blink install --wsl` did so after stopping the service
    (review of the WSL work, 2026-11-05)."""
    entry = {"type": "command", "command": theirs}
    lin = _tree(tmp_path, {"hooks": {"Stop": [{"hooks": [entry]}]}},
                link=False)
    home = _home(lin)
    run = _runner(lin, _fake_bin(tmp_path))
    target = lin / ".claude" / "settings.json"

    install_statusline.install_into(home, run)
    doc = json.loads(target.read_text())
    assert {"hooks": [entry]} in doc["hooks"]["Stop"]
    assert _hook_cmd(lin, "Stop") in _all_commands(doc)

    install_statusline.uninstall_from(home, run)
    doc = json.loads(target.read_text())
    assert doc["hooks"] == {"Stop": [{"hooks": [entry]}]}
    assert "statusLine" not in doc


# ------------------------------- review of the WSL work, 2026-11-05: A4-1

@posix_only
@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("keep", [0, 1, 20])
def test_a_write_cut_short_installs_nothing(tmp_path, shell, keep):
    """The write script installed whatever reached its stdin: when the
    process feeding wsl.exe died mid-write, `cat` met an early EOF, exited
    0, and `mv -f` put a prefix of settings.json (or of a shim) in place --
    b'{\\n  "permissions": {' was the reviewer's. Now the length and SHA-256
    of the whole document travel as arguments and the script checks the temp
    file against both before the mv; a short one is deleted, the target is
    left as it was, and the Windows side reports a failure. `keep` is how
    many bytes arrive."""
    lin = _tree(tmp_path, {"theme": "dark"}, link=False)
    target = lin / ".claude" / "settings.json"
    before = target.read_bytes()
    real = _runner(lin, _fake_bin(tmp_path), shell=shell)

    def cut(argv, stdin=None):
        if argv[2] == install_statusline._WRITE and stdin:
            stdin = stdin[:keep]
        return real(argv, stdin)

    new = json.dumps({"permissions": {"allow": ["x"] * 50}},
                     indent=2).encode()
    with pytest.raises(install_statusline.WslInstallError):
        install_statusline._wsl_write(
            cut, str(target), hashlib.sha256(before).hexdigest(), "keep",
            new, "Claude Code's settings")
    assert target.read_bytes() == before
    assert sorted(os.listdir(lin / ".claude")) == ["settings.json"]

    # The same write, whole, goes in.
    install_statusline._wsl_write(
        real, str(target), hashlib.sha256(before).hexdigest(), "keep", new,
        "Claude Code's settings")
    assert target.read_bytes() == new


@posix_only
def test_an_empty_document_still_writes(tmp_path):
    """Length 0 is a real length: an empty chain file is written, not
    refused as a cut-short one."""
    lin = _tree(tmp_path)
    p = lin / ".blink" / "x"
    install_statusline._wsl_write(_runner(lin, _fake_bin(tmp_path)), str(p),
                                  "-", "600", b"", "a file")
    assert p.read_bytes() == b""
