"""The VM fleet: one board handed from this Mac to each UTM guest in turn.

Nothing here runs utmctl, ssh or scp, starts a VM, or touches a board. Every
command is answered by a fake runner that records it, the DHCP lease file is
written by the test into tmp_path, and so is the inventory. What is under
test is the part a real run cannot show cheaply: the ORDER of things around
a borrowed board -- snapshot before the handoff, the guest seeing the board
before the agent starts, the board asked back on every path -- and that the
record of the run says it was a VM run, so the release gate can refuse to
read it as the desk fleet.

The cases worth a sentence each:

  - A handoff utmctl cannot do, with nobody at the keyboard, is a failed
    desk -- not a prompt that waits for ever, and not an agent started
    against a port that is not there. docs/windows-check.md records utmctl
    answering "The device cannot be found" every time on 2026-10-19.

  - The board comes back from a VM whose agent failed, too. A board left in
    a VM is a dark board on this Mac.

  - This Mac's service is held stopped for the whole VM phase and started
    again afterwards, and a BLINK_SKIP_SERVICE that turned the stop into a
    no-op stops the VM phase before any board is handed anywhere.

  - The Windows VM's sshd lands in PowerShell, so nothing is piped to it:
    the snapshot and every command go as files, and only one fixed line
    crosses the ssh command line.
"""
import importlib.util
import json
import pathlib
import subprocess

import pytest

pytest.importorskip("tomllib",
                    reason="tools/fleet/run.py needs Python 3.11 or newer")

ROOT = pathlib.Path(__file__).resolve().parents[2]
_RUN_PY = ROOT / "tools" / "fleet" / "run.py"
VM_INVENTORY = ROOT / "tools" / "fleet" / "fleet-vm.toml"


def _load():
    spec = importlib.util.spec_from_file_location("blink_fleet_run_vm",
                                                  _RUN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


fleet_run = _load()

USB = "1A86:7523"
UBUNTU_UUID = "11111111-2222-3333-4444-555555555555"
WIN_UUID = "66666666-7777-8888-9999-000000000000"
SETTINGS = {"kind": "vm", "sequential": True, "board_usb": USB,
            "host_python": "venv/bin/python"}
UBUNTU_VM = {"ssh": "tester@{address}", "os": "linux", "board": "claude",
             "python": "/usr/bin/python3", "workdir": "~/blink-fleet",
             "has_claude_desktop": False,
             "ssh_options": ["-o", "IdentityFile=~/k"],
             "vm": {"utm": UBUNTU_UUID, "hostname": "blink-ubuntu"}}
WIN_VM = {"ssh": "tester@{address}", "os": "windows", "shell": "powershell",
          "board": "claude", "python": "C:\\Python311-x64\\python.exe",
          "workdir": "%USERPROFILE%\\blink-fleet",
          "has_claude_desktop": False, "note": "Windows 11 ARM64 VM",
          "vm": {"utm": WIN_UUID, "hostname": "blink-win"}}


def _ok_result():
    return {"host": "vm", "board": "claude", "ok": True, "problems": [],
            "scenarios": {"overage": {"ok": True, "problems": []}}}


def _leases(tmp_path, *entries):
    """A dhcpd_leases file in bootpd's own shape, generated here."""
    blocks = "".join(
        "{\n"
        f"\tname={name}\n\tip_address={ip}\n"
        f"\thw_address=1,aa:bb:cc:dd:ee:{i:02x}\n"
        f"\tidentifier=1,aa:bb:cc:dd:ee:{i:02x}\n"
        f"\tlease={lease}\n"
        "}\n" for i, (name, ip, lease) in enumerate(entries))
    path = tmp_path / "dhcpd_leases"
    path.write_text(blocks, encoding="utf-8")
    return path


class VmRunner:
    """Answers utmctl, ssh, scp, git and the local helpers, and records.

    `fail` maps a kind of command to a CompletedProcess (or exception) to
    answer it with instead of success. Kinds: address, connect, disconnect,
    guest_wait, here_wait, agent, hold, release, push.

    For a PowerShell host the command that runs is only ever PS_INVOKE; what
    it MEANS is whatever step file was scp'd last, so that is what is read.
    """

    def __init__(self, fail=None, result=None, local_workdir=None):
        self.fail = fail or {}
        self.result = result or _ok_result()
        self.local_workdir = local_workdir
        self.calls = []
        self.kinds = []
        self.step = ""
        self.inputs = []

    def __call__(self, cmd, input=None, cwd=None, timeout=None,
                 capture_output=True, **kw):
        cmd = list(cmd)
        self.calls.append(cmd)
        if input is not None:
            self.inputs.append(cmd)
        kind = self._kind(cmd)
        self.kinds.append(kind)
        answer = self.fail.get(kind)
        if isinstance(answer, list):
            # One answer per call, then success.
            answer = answer.pop(0) if answer else None
        if answer is not None:
            if isinstance(answer, BaseException):
                raise answer
            return answer
        if kind == "git":
            return subprocess.CompletedProcess(cmd, 0, b"TAR", b"")
        if kind == "address":
            return subprocess.CompletedProcess(
                cmd, 0, b"192.168.64.9\nfd59::1\n", b"")
        if kind == "pull":
            dest = pathlib.Path(cmd[-1])
            dest.write_text(json.dumps(self.result), encoding="utf-8")
        if kind == "agent" and cmd[0] != "ssh" and self.local_workdir:
            out = pathlib.Path(self.local_workdir) / "result.json"
            out.write_text(json.dumps(self.result), encoding="utf-8")
        return subprocess.CompletedProcess(cmd, 0, b"", b"")

    def _kind(self, cmd):
        if cmd[0] == "git":
            return "git"
        if cmd[:2] == ["utmctl", "ip-address"]:
            return "address"
        if cmd[:3] == ["utmctl", "usb", "connect"]:
            return "connect"
        if cmd[:3] == ["utmctl", "usb", "disconnect"]:
            return "disconnect"
        if cmd[0] == "scp":
            if ":" in cmd[-1] and not pathlib.Path(cmd[-1]).is_absolute():
                local = pathlib.Path(cmd[-2])
                if local.name == fleet_run.PS_STEP:
                    self.step = local.read_text(encoding="utf-8")
                return "send"
            return "pull"
        line = " ".join(cmd)
        if cmd[0] == "ssh" and fleet_run.PS_INVOKE in cmd:
            line = self.step
        if "tests.fleet.service" in line:
            return "hold" if line.endswith("stop") else "release"
        if "tests.fleet.board_present" in line:
            return "guest_wait" if cmd[0] == "ssh" else "here_wait"
        if "tests.fleet.agent" in line:
            return "agent"
        if "tar" in line:
            return "push"
        return "other"


def _bench(runner, **kw):
    return fleet_run.Bench(SETTINGS, runner, **kw)


def _done(code=0, out=b"", err=b""):
    return subprocess.CompletedProcess(["x"], code, out, err)


# --------------------------------------------------------------------------
# The shipped VM inventory
# --------------------------------------------------------------------------

def test_the_shipped_vm_inventory_loads_and_says_what_it_is():
    hosts = fleet_run.load_inventory(VM_INVENTORY)
    settings = fleet_run.load_settings(VM_INVENTORY)
    assert settings["kind"] == "vm" and settings["sequential"] is True
    assert settings["board_usb"] == USB
    assert list(hosts) == ["local-mac", "ubuntu-vm", "windows-vm"]
    assert hosts["local-mac"]["ssh"] == ""
    assert fleet_run.is_vm(hosts["ubuntu-vm"])
    assert fleet_run.is_vm(hosts["windows-vm"])
    assert hosts["windows-vm"]["shell"] == "powershell"
    # The difference from the desk it stands in for is written where the
    # result carries it, not only in the docs.
    note = hosts["windows-vm"]["note"]
    assert "ARM64" in note and "NOT Windows 10" in note
    assert not any("port" in cfg for cfg in hosts.values())


def test_the_shipped_vm_inventory_is_one_board_on_every_desk():
    """board = must agree across desks: it is literally the same board."""
    hosts = fleet_run.load_inventory(VM_INVENTORY)
    assert {cfg["board"] for cfg in hosts.values()} == {"claude"}


def test_the_desk_inventory_is_still_the_desks_run_side_by_side():
    settings = fleet_run.load_settings(fleet_run.DEFAULT_INVENTORY)
    assert settings["kind"] == "desks" and settings["sequential"] is False


# --------------------------------------------------------------------------
# What the loader refuses
# --------------------------------------------------------------------------

def _write(tmp_path, text):
    path = tmp_path / "inv.toml"
    path.write_text(text, encoding="utf-8")
    return path


_VM_HOST = ('[hosts.vm1]\nssh = "tester@{address}"\nos = "linux"\n'
            'board = "claude"\npython = "python3"\nworkdir = "~/w"\n'
            'has_claude_desktop = false\n'
            '[hosts.vm1.vm]\nutm = "AAAA"\n')
_FLEET = ('[fleet]\nkind = "vm"\nsequential = true\n'
          'board_usb = "1A86:7523"\nhost_python = "venv/bin/python"\n')


def test_a_vm_in_a_parallel_inventory_is_refused(tmp_path):
    """Run side by side, two desks would be handed one board."""
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, _VM_HOST))
    assert "sequential" in str(e.value)


def test_a_vm_needs_its_uuid(tmp_path):
    text = _FLEET + _VM_HOST.replace('utm = "AAAA"\n', 'hostname = "x"\n')
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, text))
    assert "UUID" in str(e.value)


def test_an_address_placeholder_with_no_vm_is_refused(tmp_path):
    text = _FLEET + _VM_HOST.split("[hosts.vm1.vm]")[0]
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, text))
    assert "{address}" in str(e.value)


def test_a_vm_needs_the_board_id_and_a_python_here(tmp_path):
    text = '[fleet]\nkind = "vm"\nsequential = true\n' + _VM_HOST
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, text))
    assert "board_usb" in str(e.value) and "host_python" in str(e.value)


@pytest.mark.parametrize("fleet, word", [
    ('[fleet]\nkind = "vms"\n', "kind"),
    ('[fleet]\nsequential = "yes"\n', "sequential"),
    ('[fleet]\nboard_usb = "1a86-7523"\n', "board_usb"),
])
def test_a_malformed_fleet_table_is_refused(tmp_path, fleet, word):
    text = fleet + ('[hosts.a]\nssh = ""\nos = "darwin"\nboard = "claude"\n'
                    'python = "p"\nworkdir = "~/w"\n'
                    'has_claude_desktop = true\n')
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, text))
    assert word in str(e.value)


def test_an_inventory_with_a_vm_has_to_say_it_is_a_vm_fleet(tmp_path):
    """The kind is what the gate reads, and a VM in the inventory decides it.
    (Review of 7c6b2a3, S2: a VM inventory with no kind recorded "desks".)"""
    text = _FLEET.replace('kind = "vm"\n', '') + _VM_HOST
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, text))
    assert 'kind = "vm"' in str(e.value)


def test_powershell_is_only_a_windows_shell(tmp_path):
    text = ('[hosts.a]\nssh = "u@h"\nos = "linux"\nshell = "powershell"\n'
            'board = "claude"\npython = "p"\nworkdir = "~/w"\n'
            'has_claude_desktop = false\n')
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, text))
    assert "windows" in str(e.value)


def test_two_vms_with_the_same_placeholder_are_two_machines(tmp_path):
    """Every VM's ssh line reads tester@{address}; the UUID tells them apart
    -- and the same UUID twice is still one machine written down twice."""
    second = _VM_HOST.replace("vm1", "vm2")
    fleet_run.load_inventory(
        _write(tmp_path, _FLEET + _VM_HOST + second.replace("AAAA", "BBBB")))
    with pytest.raises(ValueError) as e:
        fleet_run.load_inventory(_write(tmp_path, _FLEET + _VM_HOST + second))
    assert "vm1" in str(e.value) and "vm2" in str(e.value)


# --------------------------------------------------------------------------
# Finding a VM
# --------------------------------------------------------------------------

def test_the_newest_lease_for_the_hostname_wins(tmp_path):
    """The Ubuntu VM held two leases on 2026-10-30 under two client ids."""
    text = _leases(tmp_path, ("blink-ubuntu", "192.168.64.2", "6ae3e78c"),
                   ("blink-win", "192.168.64.4", "6ad6847f"),
                   ("blink-ubuntu", "192.168.64.3", "6ae3e78f")).read_text()
    assert fleet_run.lease_address("blink-ubuntu", text) == "192.168.64.3"
    assert fleet_run.lease_address("blink-win", text) == "192.168.64.4"
    assert fleet_run.lease_address("nobody", text) is None


def test_the_guest_agent_is_asked_first(tmp_path):
    runner = VmRunner()
    address, how, problems = fleet_run.resolve_address(
        "ubuntu-vm", UBUNTU_VM, runner, leases=tmp_path / "none")
    assert (address, how, problems) == ("192.168.64.9", "utmctl", [])


def test_an_apple_event_error_falls_back_to_the_lease(tmp_path):
    """utmctl printed this, for a booting VM, and exited 0 doing it."""
    said = "Error from event: The operation couldn't be completed. (-1712.)"
    runner = VmRunner(fail={"address": _done(0, said.encode())})
    leases = _leases(tmp_path, ("blink-ubuntu", "192.168.64.3", "6ae3e78f"))
    address, how, _ = fleet_run.resolve_address("ubuntu-vm", UBUNTU_VM,
                                                runner, leases=leases)
    assert (address, how) == ("192.168.64.3", "dhcp lease")


def test_a_vm_with_no_address_is_a_failed_desk_that_says_why(tmp_path):
    runner = VmRunner(fail={"address": _done(1, b"", b"no such VM")})
    result, warnings = fleet_run.run_vm_host(
        "ubuntu-vm", UBUNTU_VM, _bench(runner), runner=runner,
        leases=tmp_path / "none")
    assert result["ok"] is False
    assert "no address" in result["problems"][0]
    assert "connect" not in runner.kinds and warnings == []


# --------------------------------------------------------------------------
# The handoff, in order
# --------------------------------------------------------------------------

def _run_vm(cfg, runner, tmp_path, **bench_kw):
    bench = _bench(runner, **bench_kw)
    return fleet_run.run_vm_host("vm", cfg, bench, runner=runner,
                                 leases=tmp_path / "none")


def test_a_vm_desk_is_pushed_then_handed_the_board_then_run(tmp_path):
    runner = VmRunner()
    result, warnings = _run_vm(UBUNTU_VM, runner, tmp_path)
    assert result["ok"] is True, result["problems"]
    order = [k for k in runner.kinds
             if k in ("push", "connect", "guest_wait", "agent", "pull",
                      "disconnect", "here_wait")]
    assert order == ["push", "connect", "guest_wait", "agent", "pull",
                     "disconnect", "here_wait"]
    assert result["vm"] == {"utm": UBUNTU_UUID, "address": "192.168.64.9",
                            "address_from": "utmctl", "handoff": "utmctl"}
    assert warnings == []
    # The address went into the ssh line, the placeholder did not.
    flat = " ".join(" ".join(c) for c in runner.calls)
    assert "tester@192.168.64.9" in flat and "{address}" not in flat
    assert "IdentityFile=~/k" in flat


def test_the_board_comes_back_from_a_vm_whose_agent_failed(tmp_path):
    runner = VmRunner(fail={"agent": subprocess.TimeoutExpired("ssh", 5)})
    result, _ = _run_vm(UBUNTU_VM, runner, tmp_path)
    assert result["ok"] is False
    assert "disconnect" in runner.kinds and "here_wait" in runner.kinds


def test_the_agent_never_starts_before_the_guest_sees_the_board(tmp_path):
    runner = VmRunner(fail={"guest_wait": _done(1, b"no board 1A86:7523")})
    result, _ = _run_vm(UBUNTU_VM, runner, tmp_path)
    assert result["ok"] is False
    assert "never saw the board" in result["problems"][0]
    assert "agent" not in runner.kinds
    # It may be in the VM all the same, so it is asked back.
    assert "disconnect" in runner.kinds


def test_a_handoff_nobody_can_finish_is_a_failed_desk_not_a_wait(tmp_path):
    said = b"Error from event: The device cannot be found."
    runner = VmRunner(fail={"connect": _done(0, said)})

    def ask(prompt):
        raise AssertionError("prompted with nobody at the keyboard")

    result, _ = _run_vm(UBUNTU_VM, runner, tmp_path, ask=ask,
                        interactive=False)
    assert result["ok"] is False
    assert "could not be handed" in result["problems"][0]
    assert "USB Serial -> Connect" in result["problems"][0]
    assert "agent" not in runner.kinds
    # Asked back all the same -- utmctl's word is not trusted either way --
    # and found home at once, so nothing is held against the next VM.
    assert "disconnect" in runner.kinds and "here_wait" in runner.kinds


def test_an_operator_finishes_what_utmctl_could_not(tmp_path):
    runner = VmRunner(fail={"connect": _done(0, b"Error from event: x"),
                            "disconnect": _done(1, b"", b"nope")})
    asked = []
    result, warnings = _run_vm(UBUNTU_VM, runner, tmp_path,
                               ask=asked.append, interactive=True)
    assert result["ok"] is True, result["problems"]
    assert result["vm"]["handoff"] == "operator"
    # utmctl's disconnect failed, but the board was seen home, which is the
    # test -- so nobody is asked to do what has already happened.
    assert len(asked) == 1 and "Connect" in asked[0]
    assert warnings == []


def test_the_operator_is_asked_back_only_when_the_board_is_not_home(
        tmp_path):
    runner = VmRunner(fail={"disconnect": _done(1, b"", b"nope"),
                            "here_wait": [_done(1, b"no board")]})
    asked = []
    result, warnings = _run_vm(UBUNTU_VM, runner, tmp_path,
                               ask=asked.append, interactive=True)
    assert result["ok"] is True
    assert len(asked) == 1 and "Disconnect" in asked[0]
    assert runner.kinds.count("here_wait") == 2 and warnings == []


def test_operator_mode_never_runs_utmctl_usb(tmp_path):
    runner = VmRunner()
    result, _ = _run_vm(UBUNTU_VM, runner, tmp_path, mode="operator",
                        ask=lambda prompt: None, interactive=True)
    assert result["ok"] is True
    assert "connect" not in runner.kinds and "disconnect" not in runner.kinds


def test_a_board_that_does_not_come_home_stops_every_later_vm(tmp_path):
    """A warning on the desk that had it -- its evidence is complete -- and
    a hard stop for the next one, which is never handed a board this Mac
    has not got back. (Review of 7c6b2a3, M2: the next VM was handed it.)"""
    runner = VmRunner(fail={"here_wait": _done(1, b"no board")})
    bench = _bench(runner)
    result, warnings = fleet_run.run_vm_host("u", UBUNTU_VM, bench,
                                             runner=runner,
                                             leases=tmp_path / "none")
    assert result["ok"] is True
    assert warnings and "did not come back" in warnings[0]
    connects = runner.kinds.count("connect")
    second, _ = fleet_run.run_vm_host("w", WIN_VM, bench, runner=runner,
                                      leases=tmp_path / "none")
    assert runner.kinds.count("connect") == connects
    assert second["ok"] is False
    assert "never came back to this Mac from u" in second["problems"][0]


def test_a_utmctl_timeout_during_the_handoff_still_asks_the_board_back(
        tmp_path):
    """-1712 at this end can still have been carried out in UTM. (M1)"""
    runner = VmRunner(fail={"connect": subprocess.TimeoutExpired("utmctl",
                                                                 150)})
    result, _ = _run_vm(UBUNTU_VM, runner, tmp_path, interactive=False)
    assert result["ok"] is False
    assert "disconnect" in runner.kinds


def test_a_ctrl_c_during_the_handoff_still_asks_the_board_back(tmp_path):
    runner = VmRunner(fail={"connect": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        _run_vm(UBUNTU_VM, runner, tmp_path, interactive=False)
    assert "disconnect" in runner.kinds


def test_a_ctrl_c_at_the_prompt_stops_the_run(tmp_path):
    """It used to be swallowed, and the run moved on to the next VM's
    handoff and prompt. (S6)"""
    runner = VmRunner(fail={"connect": _done(1, b"error")})

    def ask(prompt):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _run_vm(UBUNTU_VM, runner, tmp_path, ask=ask, interactive=True)
    assert "disconnect" in runner.kinds and "agent" not in runner.kinds


def test_an_end_of_input_at_the_prompt_is_a_failed_desk(tmp_path):
    runner = VmRunner(fail={"connect": _done(1, b"error")})

    def ask(prompt):
        raise EOFError

    result, _ = _run_vm(UBUNTU_VM, runner, tmp_path, ask=ask,
                        interactive=True)
    assert result["ok"] is False and "abandoned" in result["problems"][0]


# --------------------------------------------------------------------------
# This Mac's service
# --------------------------------------------------------------------------

def test_a_skipped_service_stop_hands_no_board_anywhere(tmp_path):
    runner = VmRunner(fail={"hold": _done(3, b"skipped")})
    bench = _bench(runner)
    assert bench.hold()
    result, _ = fleet_run.run_vm_host("vm", UBUNTU_VM, bench, runner=runner,
                                      leases=tmp_path / "none")
    assert result["ok"] is False
    assert "BLINK_SKIP_SERVICE" in result["problems"][0]
    assert "connect" not in runner.kinds
    # Nothing was stopped, so nothing is started on the way out.
    assert bench.release() == [] and "release" not in runner.kinds


@pytest.mark.parametrize("answer", [
    subprocess.TimeoutExpired("python", 300),
    _done(1, b"launchctl bootout: 5: Input/output error"),
])
def test_a_stop_that_failed_is_still_followed_by_a_start(tmp_path, answer):
    """It may have booted the agent out all the same; starting a running
    service costs nothing. (S1)"""
    runner = VmRunner(fail={"hold": answer})
    bench = _bench(runner)
    assert bench.hold()
    bench.release()
    assert "release" in runner.kinds


def test_the_service_is_started_again_and_says_so_when_it_will_not(tmp_path):
    runner = VmRunner(fail={"release": _done(1, b"launchctl said no")})
    bench = _bench(runner)
    assert bench.hold() == []
    warnings = bench.release()
    assert warnings and "dark" in warnings[0]


# --------------------------------------------------------------------------
# The PowerShell desk: nothing piped, one fixed ssh line
# --------------------------------------------------------------------------

def test_a_powershell_desk_is_sent_files_not_pipes(tmp_path):
    runner = VmRunner()
    result, _ = _run_vm(WIN_VM, runner, tmp_path)
    assert result["ok"] is True, result["problems"]
    assert result["note"] == "Windows 11 ARM64 VM"
    assert runner.inputs == [], "something was piped to PowerShell"
    for cmd in runner.calls:
        if cmd[0] == "ssh":
            assert cmd[-1] == fleet_run.PS_INVOKE
    sent = [c[-1].split(":", 1)[1] for c in runner.calls
            if c[0] == "scp" and "@" in c[-1]]
    assert fleet_run.PS_SNAPSHOT in sent and fleet_run.PS_STEP in sent


def test_the_powershell_step_runs_under_cmd_with_its_exit_code():
    """powershell -c would turn the agent's 1 and cmd's 9009 into one 1."""
    assert fleet_run.PS_INVOKE.startswith("cmd.exe /d /c ")
    assert fleet_run.PS_INVOKE.endswith("exit $LASTEXITCODE")
    text = fleet_run.step_file_text("cd /d x && python -m y")
    assert text == "@echo off\r\ncd /d x && python -m y\r\n"


def test_the_powershell_push_untars_the_file_it_was_sent():
    cfg = fleet_run.with_address(WIN_VM, "10.0.0.1")
    line = fleet_run.push_line(cfg)
    assert "%SystemRoot%\\System32\\tar.exe" in line
    assert fleet_run.PS_SNAPSHOT in line and " -f - " not in line
    assert not line.startswith('"')
    assert "result.json" in line.split("&")[1]


# --------------------------------------------------------------------------
# The whole run
# --------------------------------------------------------------------------

def _vm_inventory(tmp_path):
    wd = (tmp_path / "wd").as_posix()
    return _write(tmp_path, _FLEET.replace('kind = "vm"\n', 'kind = "vm"\n')
                  + '[hosts.win]\nssh = "tester@{address}"\nos = "windows"\n'
                  'shell = "powershell"\nboard = "claude"\npython = "py"\n'
                  'workdir = "%USERPROFILE%\\\\blink-fleet"\n'
                  'has_claude_desktop = false\n'
                  '[hosts.win.vm]\nutm = "BBBB"\n'
                  + _VM_HOST
                  + '[hosts.mac]\nssh = ""\nos = "darwin"\nboard = "claude"\n'
                  f'python = "p"\nworkdir = "{wd}"\n'
                  'has_claude_desktop = true\n')


def test_a_vm_run_goes_desk_by_desk_and_records_that_it_was_vms(tmp_path):
    (tmp_path / "wd").mkdir()
    runner = VmRunner(local_workdir=tmp_path / "wd")
    state = tmp_path / "last_run.json"
    code = fleet_run.main(["--inventory", str(_vm_inventory(tmp_path)),
                           "--state", str(state)], runner=runner,
                          ask=lambda p: None, interactive=False,
                          leases=tmp_path / "none")
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert code == 0, doc
    assert doc["fleet"]["kind"] == "vm" and doc["fleet"]["sequential"]
    assert set(doc["hosts"]) == {"mac", "win", "vm1"}
    assert doc["warnings"] == []
    # The Mac first, with its own board; the service held only around the
    # VMs; the VMs in file order.
    kinds = [k for k in runner.kinds
             if k in ("agent", "hold", "release", "connect")]
    assert kinds == ["agent", "hold", "connect", "agent", "connect",
                     "agent", "release"]
    connects = [c[3] for c in runner.calls if c[:3] == ["utmctl", "usb",
                                                        "connect"]]
    assert connects == ["BBBB", "AAAA"]


def test_the_vm_table_says_it_was_a_vm_run(tmp_path, capsys):
    (tmp_path / "wd").mkdir()
    runner = VmRunner(local_workdir=tmp_path / "wd")
    fleet_run.main(["--inventory", str(_vm_inventory(tmp_path)),
                    "--state", str(tmp_path / "s.json")], runner=runner,
                   ask=lambda p: None, interactive=False,
                   leases=tmp_path / "none")
    out = capsys.readouterr().out
    assert "A VM fleet run" in out and "Not the desk fleet" in out


def test_the_service_comes_back_when_the_run_is_interrupted(tmp_path):
    (tmp_path / "wd").mkdir()
    runner = VmRunner(local_workdir=tmp_path / "wd",
                      fail={"guest_wait": KeyboardInterrupt()})
    with pytest.raises(KeyboardInterrupt):
        fleet_run.main(["--inventory", str(_vm_inventory(tmp_path)),
                        "--state", str(tmp_path / "s.json")], runner=runner,
                       ask=lambda p: None, interactive=False,
                       leases=tmp_path / "none")
    assert runner.kinds[-1] == "release"
    assert "disconnect" in runner.kinds


def test_a_vm_dry_run_contacts_nothing(tmp_path, capsys):
    def boom(cmd, **kw):
        raise AssertionError(f"A dry run ran {cmd}")

    state = tmp_path / "last_run.json"
    state.write_text('{"ok": true}', encoding="utf-8")
    code = fleet_run.main(["--inventory", str(VM_INVENTORY), "--state",
                           str(state), "--dry-run"], runner=boom)
    assert code == 0
    assert state.read_text(encoding="utf-8") == '{"ok": true}'
    out = capsys.readouterr().out
    assert "utmctl', 'usb', 'connect'" in out
    assert fleet_run.PS_INVOKE in out.replace("\\\\", "\\")
    assert "tests.fleet.service', 'stop'" in out
    assert "tests.fleet.service', 'start'" in out


def test_a_desk_run_records_that_it_was_the_desks(tmp_path):
    from tests.pc.test_fleet_orchestrator import FakeRunner, _inventory
    state = tmp_path / "last_run.json"
    fleet_run.main(["--inventory", str(_inventory(tmp_path)),
                    "--state", str(state), "--only", "kfir-ubuntu"],
                   runner=FakeRunner())
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["fleet"]["kind"] == "desks"
    assert doc["fleet"]["sequential"] is False
    assert doc["real_account"] is True


# --------------------------------------------------------------------------
# The two helpers run on the far side
# --------------------------------------------------------------------------

class _Port:
    def __init__(self, device, vid, pid):
        self.device, self.vid, self.pid = device, vid, pid


def test_board_present_waits_for_the_board_and_names_its_port(capsys):
    from tests.fleet import board_present
    seen = iter([[], [], [_Port("COM3", 0x1A86, 0x7523),
                          _Port("COM1", 0x8087, 0x0AAA)]])
    clock = iter(range(100))
    code = board_present.main(["--usb", USB, "--wait", "10"],
                              comports=lambda: next(seen),
                              sleep=lambda s: None, now=lambda: next(clock))
    assert code == 0
    assert "COM3" in capsys.readouterr().out


def test_board_present_gives_up_and_says_so(capsys):
    from tests.fleet import board_present
    clock = iter(range(100))
    code = board_present.main(["--usb", USB, "--wait", "3"],
                              comports=lambda: [], sleep=lambda s: None,
                              now=lambda: next(clock))
    assert code == 1
    assert "no board" in capsys.readouterr().out


def test_board_present_can_wait_for_it_to_go():
    from tests.fleet import board_present
    clock = iter(range(100))
    assert board_present.main(["--usb", USB, "--gone"],
                              comports=lambda: [], sleep=lambda s: None,
                              now=lambda: next(clock)) == 0


def test_board_present_refuses_a_usb_id_it_cannot_read():
    from tests.fleet import board_present
    assert board_present.main(["--usb", "CH340"], comports=lambda: []) == 2


def test_the_service_helper_reports_a_skip_as_its_own_code():
    from pc.service_ctl import Outcome
    from tests.fleet import service
    skipped = Outcome(False, True, "skipped (BLINK_SKIP_SERVICE=1)")
    assert service.main(["stop"], stop=lambda: skipped) == service.SKIPPED
    assert service.main(["start"],
                        start=lambda: Outcome(True, False, "started")) == 0
    assert service.main(["stop"],
                        stop=lambda: Outcome(False, False, "no")) == 1
    assert service.main(["restart"]) == 2


def test_a_vm_run_with_the_desk_names_cannot_pass_the_desk_gate(tmp_path):
    """The reviewer's probe E2: a hand-written VM inventory reusing the
    desks' names. run.py now refuses it at load; and a run made with any
    other file is refused by the gate's inventory check."""
    from pc import fleet_gate
    desks = sorted(fleet_run.load_inventory(fleet_run.DEFAULT_INVENTORY))
    wd = tmp_path / "wd"
    wd.mkdir()
    text = ('[fleet]\nsequential = true\nboard_usb = "1A86:7523"\n'
            'host_python = "venv/bin/python"\n'
            f'[hosts.{desks[0]}]\nssh = ""\nos = "darwin"\nboard = "claude"\n'
            f'python = "p"\nworkdir = "{wd.as_posix()}"\n'
            'has_claude_desktop = true\n')
    for i, name in enumerate(desks[1:]):
        text += (f'[hosts.{name}]\nssh = "tester@{{address}}"\nos = "linux"\n'
                 'board = "claude"\npython = "/usr/bin/python3"\n'
                 'workdir = "~/blink-fleet"\nhas_claude_desktop = false\n'
                 f'[hosts.{name}.vm]\nutm = "UUID-{i}"\n')
    inv = _write(tmp_path, text)
    with pytest.raises(ValueError):
        fleet_run.load_inventory(inv)
    # Declared honestly, it runs -- and is then refused against fleet.toml.
    inv.write_text(text.replace("[fleet]\n", '[fleet]\nkind = "vm"\n'),
                   encoding="utf-8")
    state = tmp_path / "s.json"
    fleet_run.main(["--inventory", str(inv), "--state", str(state)],
                   runner=VmRunner(local_workdir=wd), ask=lambda q: None,
                   interactive=False, leases=tmp_path / "none")
    doc = json.loads(state.read_text(encoding="utf-8"))
    assert doc["ok"] is True
    assert fleet_gate.gate(str(state), doc["sha"],
                           str(fleet_run.DEFAULT_INVENTORY)) is not None
    # Even with the kind written as desks in the result, the file differs.
    doc["fleet"]["kind"] = "desks"
    state.write_text(json.dumps(doc), encoding="utf-8")
    reason = fleet_gate.gate(str(state), doc["sha"],
                             str(fleet_run.DEFAULT_INVENTORY))
    assert reason and "inventory" in reason


def test_a_percent_in_a_path_for_a_powershell_desk_is_refused(tmp_path,
                                                              capsys):
    """A batch file eats a % that a command line would have kept. (N2)"""
    code = fleet_run.main(["--inventory", str(VM_INVENTORY), "--dry-run",
                           "--bundle", "bundles/50%/blink.zip",
                           "--expect-version", "1.4.0"],
                          runner=lambda *a, **k: None)
    assert code == 2
    assert "%" in capsys.readouterr().err
