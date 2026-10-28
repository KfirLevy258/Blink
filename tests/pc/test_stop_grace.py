"""What stands between a stop and a half-written board or a half-installed app.

The daemon defers SIGTERM until a firmware write or an app swap returns
(claude_usage_bridge.no_exit_while). That guard is only as long as the
supervisor lets it be: launchd SIGKILLs a job 20 s after SIGTERM unless the
plist says otherwise, systemd 90 s after unless the unit does -- and systemd's
default KillMode also sends the SIGTERM to esptool itself, which defeats the
guard at second zero. The final whole-branch review (2026-10-27, SHOULD 1-3)
found these, the daily auto-update running the swap unguarded, and the two
writers that still skipped encode_checked.

No real launchctl, systemctl, serial port or network is touched here.
"""
import ast
import os
import pathlib
import plistlib
import subprocess

import pytest

import claude_usage_bridge as cub
from pc import cli
from pc import protocol

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _Runs:
    def __init__(self):
        self.calls = []

    _gone = False

    def __call__(self, argv, **kw):
        # launchd forgets a job once a bootout it accepted has finished, and
        # `print` then exits 113 until the next bootstrap. install() waits on
        # exactly that before it bootstraps again (the old instance may be
        # inside its stop grace), so the stand-in models it, without taking
        # one of the exit codes a test queued for the commands it lists.
        if len(argv) > 1 and argv[1] == "bootout":
            self._gone = True
        elif len(argv) > 1 and argv[1] == "bootstrap":
            self._gone = False
        elif len(argv) > 1 and argv[1] == "print" and self._gone:
            self.calls.append(list(argv))
            if hasattr(self, "kwargs"):
                self.kwargs.append(kw)
            return subprocess.CompletedProcess(argv, 113, stdout="",
                                               stderr="")
        self.calls.append(list(argv))
        out = ""
        if len(argv) > 1 and argv[1] == "print":
            out = "state = running\n"
        elif "is-active" in argv:
            out = "active\n"
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")


@pytest.fixture
def runs(monkeypatch):
    r = _Runs()
    monkeypatch.setattr(cli.subprocess, "run", r)
    monkeypatch.setattr(cli.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cli.os, "getuid", lambda: 501, raising=False)
    return r


# ------------------------------------------------------------ the grace --


def test_the_grace_covers_a_flash_and_its_retry():
    # pc/ota.flash writes once and retries once, each about 75 s at the
    # 115200 baud the CH340 needs (pc/ota.py), behind an eFuse probe of a
    # few seconds. Both writes have to fit, or a stop during the retry
    # kills the attempt that would have repaired slot 0.
    assert cli.STOP_GRACE_S >= 2 * 75 + 10


def test_the_plist_gives_the_daemon_its_grace():
    d = plistlib.loads(cli._plist_text(["/x/blink", "run"]).encode())
    assert d["ExitTimeOut"] == cli.STOP_GRACE_S


def test_the_unit_gives_the_daemon_its_grace_and_spares_esptool():
    unit = cli._unit_text(["/x/blink", "run"]).splitlines()
    assert f"TimeoutStopSec={cli.STOP_GRACE_S}" in unit
    # control-group, the default, SIGTERMs esptool along with the daemon.
    assert "KillMode=mixed" in unit


_OLD_PLIST = """<?xml version="1.0" encoding="UTF-8"?>
<plist version="1.0"><dict>
  <key>Label</key><string>com.blink.bridge</string>
  <key>ProgramArguments</key><array><string>/x/blink</string>
  <string>run</string></array>
</dict></plist>
"""


def test_reinstalling_rewrites_an_old_plist_and_reloads_it(runs, monkeypatch):
    """`blink install` is the repair every hint names; it must carry the new
    key onto an existing install, and launchd reads a plist only when it is
    bootstrapped, so the job has to go round again too."""
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    os.makedirs(os.path.dirname(cli.plist_path()), exist_ok=True)
    with open(cli.plist_path(), "w", encoding="utf-8") as f:
        f.write(_OLD_PLIST)
    assert cli.backend().install() == "running (launchd)"
    with open(cli.plist_path(), "rb") as f:
        assert plistlib.load(f)["ExitTimeOut"] == cli.STOP_GRACE_S
    verbs = [c[1] for c in runs.calls if c[0] == "launchctl"]
    assert verbs.index("bootout") < verbs.index("bootstrap")


def test_reinstalling_rewrites_an_old_unit_and_reloads_it(runs, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    os.makedirs(os.path.dirname(cli.unit_path()), exist_ok=True)
    with open(cli.unit_path(), "w", encoding="utf-8") as f:
        f.write("[Service]\nExecStart=\"/x/blink\" \"run\"\nRestart=always\n")
    assert cli.backend().install() == "running (systemd)"
    with open(cli.unit_path(), encoding="utf-8") as f:
        text = f.read()
    assert f"TimeoutStopSec={cli.STOP_GRACE_S}" in text
    assert ["systemctl", "--user", "daemon-reload"] in runs.calls


# ------------------------------------------------- the daily auto-update --


def test_the_daily_update_swaps_inside_the_guard(monkeypatch, tmp_path):
    seen = {}

    def apply(blob, target, version):
        seen["holding"] = cub._TERM["holding"]
        return False, "kept the current one"

    monkeypatch.setattr(cub.update, "fetch_signed_manifest",
                        lambda: {"daemon": {"auto": True}})
    monkeypatch.setattr(cub.update, "available",
                        lambda m: ("9.9.9", {"name": "x"}))
    monkeypatch.setattr(cub.update, "auto_update_allowed", lambda h: True)
    monkeypatch.setattr(cub.update, "platform_key", lambda: "k")
    monkeypatch.setattr(cub.update, "download", lambda k, a: b"blob")
    monkeypatch.setattr(cub.update, "apply", apply)
    cub._self_update_tick(str(tmp_path / ".blink" / "bin" / "blink"))
    assert seen["holding"], "update.apply ran with no exit guard"
    assert cub._TERM["holding"] is None


def test_every_update_apply_in_the_daemon_is_guarded():
    tree = ast.parse((ROOT / "claude_usage_bridge.py").read_text("utf-8"))
    guarded = set()
    for w in ast.walk(tree):
        if isinstance(w, ast.With) and any(
                isinstance(i.context_expr, ast.Call)
                and getattr(i.context_expr.func, "id", "") == "no_exit_while"
                for i in w.items):
            guarded.update(id(n) for n in ast.walk(w))
    applies = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
               and isinstance(n.func, ast.Attribute) and n.func.attr == "apply"
               and getattr(n.func.value, "id", "") == "update"]
    assert len(applies) >= 2
    assert all(id(n) in guarded for n in applies)


# --------------------------------------------------- the unchecked writers --


class _Port:
    def __init__(self, answer=b'{"t":"pref","v":2,"provider":"claude"}\n'):
        self.written = []
        self._answer = [answer]

    def reset_input_buffer(self):
        pass

    def write(self, b):
        self.written.append(b)

    def read(self, n):
        return self._answer.pop(0) if self._answer else b""


def test_the_probe_writes_the_checked_welcome():
    port = _Port()
    assert cub.probe_is_our_board(port, 0.2) is True
    raw, why = protocol.encode_checked(
        protocol.welcome("blink-bridge", cub.RELEASE_VERSION))
    assert why is None and port.written == [raw]


def test_a_welcome_the_board_would_drop_is_not_sent(monkeypatch, capsys):
    monkeypatch.setattr(protocol, "MAX_LINE_BYTES", 20)
    port = _Port(answer=b"")
    assert cub.probe_is_our_board(port, 0.2) is False
    assert port.written == []
    err = capsys.readouterr().err
    assert "welcome" in err and "blink-bridge" not in err   # reason, no bytes


def test_a_refused_welcome_still_listens(monkeypatch):
    # False means "silent", and for a known port silent means a reset. A
    # board that speaks on its own (its 10 s ping) is still ours.
    monkeypatch.setattr(protocol, "MAX_LINE_BYTES", 20)
    port = _Port(answer=b'{"t":"ping","v":2}\n')
    assert cub.probe_is_our_board(port, 0.2) is True
    assert port.written == []


# ------------------------------------- existing installs get the grace too --
#
# The daemon rewrites its own plist or unit at startup, but only a file that
# is byte for byte what a release of ours wrote: anything else has been
# edited by somebody, and an edit is theirs (round 2 review, R2-S1).


def _old_plist(argv):
    return cli._plist_text_v134(argv)


def _old_unit(argv):
    return cli._unit_text_v134(argv)


def _tag_source(tag):
    r = subprocess.run(["git", "-C", str(ROOT), "show", f"{tag}:pc/cli.py"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        pytest.skip(f"{tag} is not in this clone (shallow checkout)")
    return r.stdout


def _template(src, name):
    import re
    return re.search(name + r' = """(.*?)"""', src, re.S).group(1)


@pytest.mark.parametrize("tag", ["v0.7.0", "v1.0.0", "v1.2.0", "v1.3.4"])
def test_the_frozen_copies_are_the_released_templates(tag):
    # Every release from v0.7.0 (the first named Blink) to v1.3.4 wrote the
    # same two templates; v0.6.0 was com.clauge.bridge, at other paths.
    src = _tag_source(tag)
    assert _template(src, "_PLIST_TEMPLATE") == cli._PLIST_TEMPLATE_V134
    assert _template(src, "_UNIT_TEMPLATE") == cli._UNIT_TEMPLATE_V134
    assert 'out.append(\'"%s"\' % a.replace("\\\\", "\\\\\\\\")' \
        '.replace(\'"\', \'\\\\"\'))' in src
    assert 'args = "".join(f"    <string>{_xml_escape(a)}</string>\\n"' in src


@pytest.fixture
def service_on(monkeypatch, runs):
    # conftest sets BLINK_SKIP_SERVICE=1 for every test; these are about the
    # file alone, with subprocess still faked by `runs`.
    monkeypatch.delenv("BLINK_SKIP_SERVICE", raising=False)
    return runs


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def test_the_daemon_brings_a_1_3_4_plist_up_to_date(service_on, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    _write(cli.plist_path(), _old_plist(cli._service_command()))
    assert cli.refresh_service_file() == "updated"
    with open(cli.plist_path(), "rb") as f:
        assert plistlib.load(f)["ExitTimeOut"] == cli.STOP_GRACE_S
    # Written, never reloaded: a bootout from inside the job would stop the
    # daemon doing it, possibly mid-flash. launchd reads it at next login.
    assert service_on.calls == []
    assert os.listdir(os.path.dirname(cli.plist_path())) == [
        "com.blink.bridge.plist"]


def test_the_daemon_brings_a_1_3_4_unit_up_to_date(service_on, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    _write(cli.unit_path(), _old_unit(cli._service_command()))
    assert cli.refresh_service_file() == "updated"
    assert "KillMode=mixed" in _read(cli.unit_path())
    assert service_on.calls == []      # no daemon-reload from inside it


def test_a_develop_unit_with_the_newer_quoting_is_ours_too(service_on,
                                                            monkeypatch):
    # Between 1.3.4 and this change `blink install` doubled % and $ in
    # ExecStart; a unit written then is the current one minus the grace.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    argv = cli._service_command()
    text = "\n".join(l for l in cli._unit_text(argv).splitlines()
                     if not l.startswith(("TimeoutStopSec", "KillMode", "#"))
                     ) + "\n"
    _write(cli.unit_path(), text)
    assert cli.refresh_service_file() == "updated"


def test_an_added_environment_line_survives(service_on, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    edited = _old_unit(cli._service_command()).replace(
        "Environment=PYTHONUNBUFFERED=1\n",
        "Environment=PYTHONUNBUFFERED=1\n"
        "Environment=HTTPS_PROXY=http://proxy.example:3128\n")
    _write(cli.unit_path(), edited)
    assert cli.refresh_service_file() == "edited"
    assert _read(cli.unit_path()) == edited


def test_an_added_plist_key_survives(service_on, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    edited = _old_plist(cli._service_command()).replace(
        "<key>PYTHONUNBUFFERED</key><string>1</string>",
        "<key>PYTHONUNBUFFERED</key><string>1</string>"
        "<key>BLINK_TAP</key><string>1</string>")
    _write(cli.plist_path(), edited)
    assert cli.refresh_service_file() == "edited"
    assert _read(cli.plist_path()) == edited


def test_the_daemon_hints_at_blink_install_for_an_edited_file():
    src = (ROOT / "claude_usage_bridge.py").read_text("utf-8")
    i = src.index("refresh_service_file()")
    assert "blink install" in src[i:i + 1200]


def test_a_current_file_is_left_untouched(service_on, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    _write(cli.plist_path(), cli._plist_text(cli._service_command()))
    before = os.stat(cli.plist_path()).st_mtime_ns
    assert cli.refresh_service_file() is None
    assert os.stat(cli.plist_path()).st_mtime_ns == before


def test_a_file_for_another_program_is_left_alone(service_on, monkeypatch):
    # A checkout daemon run by hand beside a frozen install must not point
    # the login service at the checkout, nor the other way round.
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    other = _old_plist(["/somewhere/else/blink", "run"])
    _write(cli.plist_path(), other)
    assert cli.refresh_service_file() is None
    assert _read(cli.plist_path()) == other


def test_a_frozen_build_run_from_elsewhere_is_not_the_installed_one(
        service_on, monkeypatch, tmp_path):
    # An archive unpacked in ~/Downloads and started with ./blink run names
    # installed_bin() in its _service_command, but is not that program.
    from tests.support.process import as_frozen
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    as_frozen(monkeypatch)
    old = _old_plist(cli._service_command())
    _write(cli.plist_path(), old)
    monkeypatch.setattr(cli, "_self_path",
                        lambda: str(tmp_path / "Downloads" / "blink"))
    assert cli.refresh_service_file() is None
    assert _read(cli.plist_path()) == old
    # ...and the installed one itself does get it.
    os.makedirs(os.path.dirname(cli.installed_bin()), exist_ok=True)
    open(cli.installed_bin(), "w").close()
    monkeypatch.setattr(cli, "_self_path", lambda: cli.installed_bin())
    assert cli.refresh_service_file() == "updated"


def test_a_symlinked_unit_is_replaced_through_the_link(service_on,
                                                       monkeypatch, tmp_path):
    # stow and chezmoi keep the real file elsewhere and link it in.
    monkeypatch.setattr(cli.sys, "platform", "linux")
    real = tmp_path / "dotfiles" / "blink-bridge.service"
    _write(str(real), _old_unit(cli._service_command()))
    os.makedirs(os.path.dirname(cli.unit_path()), exist_ok=True)
    os.symlink(real, cli.unit_path())
    assert cli.refresh_service_file() == "updated"
    assert os.path.islink(cli.unit_path())
    assert "KillMode=mixed" in _read(str(real))
    assert os.listdir(real.parent) == ["blink-bridge.service"]


def test_an_interrupted_write_leaves_no_tmp(service_on, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    _write(cli.unit_path(), _old_unit(cli._service_command()))

    def interrupted(src, dst):
        raise KeyboardInterrupt
    monkeypatch.setattr(cli.os, "replace", interrupted)
    with pytest.raises(KeyboardInterrupt):
        cli.refresh_service_file()
    assert os.listdir(os.path.dirname(cli.unit_path())) == [
        "blink-bridge.service"]


def test_a_file_we_did_not_write_is_left_alone(service_on, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    _write(cli.unit_path(), "[Service]\nExecStart=/usr/bin/true\n")
    assert cli.refresh_service_file() is None
    assert _read(cli.unit_path()) == "[Service]\nExecStart=/usr/bin/true\n"


def test_no_service_file_means_none_is_created(service_on, monkeypatch):
    for platform in ("darwin", "linux", "win32"):
        monkeypatch.setattr(cli.sys, "platform", platform)
        assert cli.refresh_service_file() is None
    assert not os.path.exists(cli.plist_path())
    assert not os.path.exists(cli.unit_path())


def test_skip_service_skips_the_refresh(runs, monkeypatch):
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    _write(cli.plist_path(), _old_plist(cli._service_command()))
    assert cli.refresh_service_file() is None    # BLINK_SKIP_SERVICE=1


def test_the_daemon_refreshes_its_service_file_at_startup():
    tree = ast.parse((ROOT / "claude_usage_bridge.py").read_text("utf-8"))
    main = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "main")
    assert any(isinstance(n, ast.Attribute) and n.attr ==
               "refresh_service_file" for n in ast.walk(main))


# --------------------------------------------- a stop that waits on a flash --


def test_a_slow_systemd_stop_says_why(service_on, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    monkeypatch.setattr(cli, "SLOW_STOP_NOTE_S", 0.05)
    import threading
    gate = threading.Event()

    def slow(argv, **kw):
        if "restart" in argv or "disable" in argv:
            gate.wait(0.5)
        return service_on(argv, **kw)
    monkeypatch.setattr(cli.subprocess, "run", slow)
    cli.backend().restart()
    assert "firmware write" in capsys.readouterr().err
    cli.backend().remove()
    assert "firmware write" in capsys.readouterr().err


def test_a_quick_systemd_stop_says_nothing(service_on, monkeypatch, capsys):
    monkeypatch.setattr(cli.sys, "platform", "linux")
    monkeypatch.setattr(cli.shutil, "which", lambda _n: "/usr/bin/systemctl")
    cli.backend().restart()
    assert "firmware write" not in capsys.readouterr().err


def test_burn_sh_writes_no_bytecode_into_the_tree():
    src = (ROOT / "tools" / "burn.sh").read_text("utf-8")
    assert 'PYTHONDONTWRITEBYTECODE=1 "$PY" - "$PORT" "$EDITION"' in src


def test_burn_sh_stamps_the_edition_through_encode_checked():
    src = (ROOT / "tools" / "burn.sh").read_text("utf-8")
    assert "encode_checked(protocol.edition(" in src
    assert 'json.dumps({"t": "edition"' not in src
