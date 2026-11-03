"""The spend board run (task 15e), everything but the board.

tests/fleet/spend_board_run.py drives a real board; this drives the same
generated timeline through the real ScriptedProvider, IngestionBus and Bridge
with a fake clock, and checks the judge that reads the board's transcript.
"""
import json

from pc import ingest, protocol
from pc.bridge import Bridge
from pc.providers.scripted import ScriptedProvider
from tests.fleet import spend_board_run as run

_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)


def _hello(fw):
    """What a board on `fw` says at boot: this branch's firmware announces
    plans, 1.3.4 does not."""
    msg = {"t": "hello", "v": protocol.VERSION, "fw": fw}
    if fw != "1.3.4":
        msg["caps"] = [protocol.BOARD_CAP_PLAN]
    return msg


def _drive(tmp_path, fw):
    p = tmp_path / "spend_pages.json"
    p.write_text(json.dumps(run.scenario_doc()), encoding="utf-8")
    clock = [1_790_000_000.0]
    bus = ingest.IngestionBus(
        providers=[ScriptedProvider(p, now=lambda: clock[0])],
        now=lambda: clock[0])
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=bus.fetch(),
               now=lambda: clock[0], wall=lambda: (int(clock[0]), 0),
               **_NO_FEED)
    b.on_message(_hello(fw))
    end = run.scenario_doc()["duration_s"]
    t = 0.0
    while t <= end:
        b.poll_if_changed()
        clock[0] += run.POLL_S
        t += run.POLL_S
    return sent


def _records(sent, board_lines=(), fw="1.4.0"):
    recs = [{"dir": "rx", "msg": _hello(fw)}]
    recs += [{"dir": "tx", "sent": True, "msg": m} for m in sent]
    recs += [{"dir": "console", "line": line} for line in board_lines]
    return recs


def test_every_step_of_the_timeline_is_a_frame(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(run.scenario_doc()), encoding="utf-8")
    clock = [0.0]
    sp = ScriptedProvider(p, now=lambda: clock[0])
    plans = []
    for _ in run.TIMELINE:
        plans.append(sp.poll(clock[0])[0].plan)
        clock[0] += run.STEP_S
    assert plans == ["windows", "spend", "spend", "unreported", "windows"]


def test_the_daemon_sends_each_plan_once_and_in_order(tmp_path):
    sent = _drive(tmp_path, "1.4.0")
    plans = [m["plan"] for m in sent if m["t"] == "plan"]
    assert plans == ["spend", "spend", "unreported", "windows"]
    monthly = [m for m in sent if m["t"] == "plan"][0]
    # The scripted frame carries no /usage anchor, so its dollars are an
    # estimate and the plan says so.
    assert monthly == protocol.plan("claude", "spend", "monthly", 314.12,
                                    500, est=True)
    for m in sent:
        assert protocol.encode_checked(m)[1] is None
    # Every plan goes out before the usage line that follows it.
    for i, m in enumerate(sent):
        if m["t"] == "plan" and m["plan"] == "spend":
            assert any(n["t"] == "usage" for n in sent[i + 1:])
    shares = [m["session_pct"] for m in sent if m["t"] == "usage"]
    assert 62.8 in shares and 96.4 in shares


def test_an_old_board_driven_by_the_same_daemon_gets_dashes(tmp_path):
    sent = _drive(tmp_path, "1.3.4")
    assert "plan" not in [m["t"] for m in sent]
    shares = [m["session_pct"] for m in sent if m["t"] == "usage"]
    assert 62.8 not in shares and 96.4 not in shares
    assert 41.0 in shares
    ok, lines = run.judge(_records(sent, fw="1.3.4"))
    assert ok, lines


def test_the_judge_passes_a_board_that_followed_along(tmp_path):
    sent = _drive(tmp_path, "1.4.0")
    board = ["[plan] claude spend monthly", "[usage] session 63",
             "[plan] claude spend daily", "[plan] claude unreported",
             "[plan] claude windows"]
    ok, lines = run.judge(_records(sent, board))
    assert ok, lines


def test_the_judge_fails_a_board_that_missed_a_page(tmp_path):
    sent = _drive(tmp_path, "1.4.0")
    board = ["[plan] claude spend monthly", "[plan] claude windows"]
    ok, lines = run.judge(_records(sent, board))
    assert not ok
    assert any(line.startswith("FAIL board") for line in lines)


def test_the_judge_fails_an_old_board_shown_a_share():
    sent = [protocol.plan("claude", "spend", "monthly", 314.12, 500),
            protocol.usage(62.8, None, -1, None, [])]
    ok, lines = run.judge(_records(sent, fw="1.3.4"))
    assert not ok


def test_the_timeline_is_not_a_fleet_scenario():
    """Held from release: the fleet runs every file in its scenarios
    directory on all three desks, so this one must not be among them."""
    from pathlib import Path
    scen = Path(run.__file__).parent / "scenarios"
    assert not list(scen.glob("spend*"))
