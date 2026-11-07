"""The WiFi fleet scenario: the pairing reaches the daemon's sandbox home
owner-only, and the board's console, read by tools/console_tail.py, is what
tap_asserts already counts. No board, no socket, no daemon."""
import json
import os

import pytest

from pc import wifi_pair
from tests.fleet import agent, tap_asserts
from tools import console_tail


def _paired(home):
    wifi_pair.save(str(home), "20500d342b68", bytes(range(32)),
                   "192.0.2.7", 49731)


def test_pairing_is_copied_owner_only_into_the_sandbox_home(tmp_path):
    real = tmp_path / "real" / ".blink"
    _paired(real)
    sandbox = tmp_path / "run" / "home"
    sandbox.mkdir(parents=True)

    assert agent.install_pairing(sandbox, str(real)) is True

    # The daemon's blink_home() is <HOME>/.blink, so that is where it looks.
    dest = sandbox / ".blink"
    assert wifi_pair.load(str(dest)) == wifi_pair.load(str(real))
    if os.name != "nt":
        assert (dest / "private" / "wifi_pair.json").stat().st_mode & 0o777 == 0o600
        assert (dest / "private").stat().st_mode & 0o777 == 0o700


def test_a_desk_without_a_pairing_copies_nothing(tmp_path):
    sandbox = tmp_path / "home"
    sandbox.mkdir()
    assert agent.install_pairing(sandbox, str(tmp_path / "none")) is False
    assert not (sandbox / ".blink").exists()


def test_console_tail_records_satisfy_tap_asserts(tmp_path):
    class Port:
        def __init__(self):
            self.data = [b"[usage] session 3%  (1s)  weekly 4% (2s)\r\n"
                         b"[usage] session 4% (1s)  weekly 4% (2s)  STALE\r\n"]

        def read(self, n):
            if self.data:
                return self.data.pop(0)
            raise KeyboardInterrupt

    tap = tmp_path / "tap.jsonl"
    console_tail.main(["PORT", "--tap", str(tap)], open_port=lambda p: Port(),
                      out=open(os.devnull, "w"))
    records = [json.loads(l) for l in tap.read_text().splitlines()]
    # The daemon's own rx record (the hello over WiFi); the console alone
    # is not proof the daemon parsed anything.
    records.append({"dir": "rx", "t": 0.5, "msg": {"t": "hello", "v": 1}})
    records += [{"dir": "tx", "t": 1.0 + i, "sent": True,
                 "msg": {"t": "usage"}} for i in range(2)]

    doc = json.load(open(os.path.join(os.path.dirname(__file__), "scenarios",
                                      "wifi_link.json")))
    expect = dict(doc["expect"], min_tx=2, min_board_usage=2,
                  min_stale_lines=1)
    assert tap_asserts.check(records, expect, "wifi_link") == []


def test_wifi_scenario_forces_wifi_and_runs_the_tail_beside_the_daemon(tmp_path):
    real = tmp_path / "real" / ".blink"
    _paired(real)
    src = tmp_path / "wifi_link.json"
    doc = json.load(open(os.path.join(os.path.dirname(__file__), "scenarios",
                                      "wifi_link.json")))
    src.write_text(json.dumps(doc))
    cmds = []

    class Proc:
        def poll(self): return None
        def terminate(self): pass
        def kill(self): pass
        def wait(self, timeout=None): return 0

    def spawn(cmd, env, cwd):
        cmds.append((cmd, env))
        return Proc()

    deps = agent.Deps(spawn=spawn, sleep=lambda s: None,
                      wall=lambda: 0.0)
    agent.run_scenario(src, "claude", tmp_path / "w", "/dev/ttyX", deps,
                       real_blink_home=str(real))
    daemon = [c for c in cmds if "run" in c[0]]
    tail = [c for c in cmds if any("console_tail" in a for a in c[0])]
    assert daemon and daemon[0][1]["BLINK_FORCE_WIFI"] == "1"
    assert tail and "--tap" in tail[0][0] and tail[0][0][-1] == "/dev/ttyX"
    assert wifi_pair.load(str(tmp_path / "w" / "home" / ".blink"))
