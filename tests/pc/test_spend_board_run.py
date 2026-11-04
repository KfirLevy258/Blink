"""The spend board run (task 15e), everything but the board.

tests/fleet/spend_board_run.py drives a real board; this drives the same
generated timeline through the real ScriptedProvider, IngestionBus and Bridge
with a fake clock, and checks the judge that reads the board's transcript.
"""
import json

import pytest

from pc import ingest, protocol
from pc.bridge import Bridge
from pc.providers.scripted import ScriptedProvider
from tests.fleet import spend_board_run as run

_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)


def _hello(fw):
    """What a board on `fw` says at boot: this branch's firmware announces
    plans and the dollar burn line, 1.4.1 announces neither, 1.3.4 no caps
    at all."""
    msg = {"t": "hello", "v": protocol.VERSION, "fw": fw}
    if fw == "1.4.1":
        msg["caps"] = ["burn", "edit_done", "svc", "theme"]
    elif fw != "1.3.4":
        msg["caps"] = [protocol.BOARD_CAP_PLAN, protocol.BOARD_CAP_SPENDBURN]
    return msg


def _drive(tmp_path, fw):
    """The daemon's half of a run on a fake clock: the scenario bus
    claude_usage_bridge.build_bus makes, and the Bridge's serial loop."""
    p = tmp_path / "spend_pages.json"
    p.write_text(json.dumps(run.scenario_doc()), encoding="utf-8")
    clock = [1_790_000_000.0]
    sp = ScriptedProvider(p, now=lambda: clock[0])
    bus = ingest.IngestionBus(providers=[sp], now=lambda: clock[0],
                              burn={"claude": sp.burn_source()},
                              counter=sp.spend_count(), verdict=None)
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


def _records(sent, board_lines=None, fw="1.5.0"):
    """A transcript of a run: the hello, every message sent, and the
    board's [plan] lines -- by default one per plan message, as proto.c
    prints them."""
    if board_lines is None:
        board_lines = [f"[plan] claude {run._words(m)}" for m in sent
                       if m["t"] == "plan"]
    recs = [{"dir": "rx", "msg": _hello(fw)}]
    recs += [{"dir": "tx", "sent": True, "msg": m} for m in sent]
    recs += [{"dir": "console", "line": line} for line in board_lines]
    return recs


def test_every_step_of_the_timeline_is_a_frame(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps(run.scenario_doc()), encoding="utf-8")
    clock = [0.0]
    sp = ScriptedProvider(p, now=lambda: clock[0])
    got = []
    for _ in run.TIMELINE:
        f = sp.poll(clock[0])[0]
        got.append((f.plan, f.spend_exact, f.spend_hint))
        clock[0] += run.STEP_S
    assert got == [("windows", False, False),
                   ("spend", True, False),      # exact
                   ("spend", False, False),     # estimated
                   ("spend", False, True),      # hint
                   ("spend", False, False),     # near cap
                   ("spend", True, False),      # reached
                   ("spend", False, True),      # rollover
                   ("windows", False, False),   # Max
                   ("spend", True, False)]      # back


def test_the_timeline_walks_what_the_plan_asks_for():
    """exact -> estimated -> hint -> near cap -> reached -> month rollover
    -> login to Max -> back, with the page's own facts at each step."""
    steps = dict(run.TIMELINE)
    assert [n for n, _ in run.TIMELINE] == [
        "windows", "exact", "estimated", "hint", "near cap", "reached",
        "rollover", "Max", "back"]
    assert 90 <= steps["near cap"]["session_pct"] < 100
    assert steps["reached"]["session_pct"] >= 100
    roll = steps["rollover"]
    assert "lim" not in roll and roll["hint"] and roll["sburn"] == "idle"
    assert roll["session_resets_in_s"] <= protocol.SPEND_COUNTDOWN_MAX_S
    assert steps["Max"].get("plan") is None
    # Every spend share is the dollars over the cap, to the wire's 0.1.
    for _, s in run.TIMELINE:
        if s.get("lim"):
            assert s["session_pct"] == round(100 * s["usd"] / s["lim"], 1)


def test_the_daemon_sends_each_plan_once_and_in_order(tmp_path):
    sent = _drive(tmp_path, "1.5.0")
    plans = [m for m in sent if m["t"] == "plan"]
    assert plans == run.expected_plans()
    assert [(m["plan"], m.get("est", 0), m.get("hint", 0)) for m in plans] \
        == [("spend", 0, 0), ("spend", 1, 0), ("spend", 1, 1),
            ("spend", 1, 0), ("spend", 0, 0), ("spend", 1, 1),
            ("windows", 0, 0), ("spend", 0, 0)]
    assert "lim" not in plans[5]
    for m in sent:
        assert protocol.encode_checked(m)[1] is None
    # Every plan goes out before the usage line that follows it.
    for i, m in enumerate(sent):
        if m["t"] == "plan" and m["plan"] == "spend":
            assert any(n["t"] == "usage" for n in sent[i + 1:])
    shares = [m["session_pct"] for m in sent if m["t"] == "usage"]
    for share in run.SPEND_SHARES:
        assert share in shares


def test_every_spend_step_puts_its_dollar_buckets_on_the_wire(tmp_path):
    sent = _drive(tmp_path, "1.5.0")
    sburns = [m for m in sent if m["t"] == "sburn"]
    shown = {tuple(m["b"]) for m in sburns}
    for b in run.expected_sburns():
        assert b in shown
    assert not [m for m in sent if m["t"] == "burn"]
    ok, lines = run.judge(_records(sent))
    assert ok, lines


def test_no_dollar_line_on_the_windows_pages(tmp_path):
    """The bus asks the count only while the page is a spend page, so the
    held buckets of the step before never reach a windows page."""
    sent = _drive(tmp_path, "1.5.0")
    page = None
    for m in sent:
        if m["t"] == "plan":
            page = m["plan"]
        if m["t"] == "sburn":
            assert page == "spend"


def test_a_plan_board_without_spendburn_gets_no_dollar_line(tmp_path):
    sent = _drive(tmp_path, "1.5.0")
    assert any(m["t"] == "sburn" for m in sent)
    hello = _hello("1.5.0")
    hello["caps"] = [protocol.BOARD_CAP_PLAN]
    recs = [{"dir": "rx", "msg": hello}]
    recs += [{"dir": "tx", "sent": True, "msg": m} for m in sent]
    recs += [{"dir": "console", "line": f"[plan] claude {run._words(m)}"}
             for m in sent if m["t"] == "plan"]
    ok, lines = run.judge(recs)
    assert not ok
    assert any("sburn lines sent to a board without" in x for x in lines)


@pytest.mark.parametrize("fw", ["1.4.1", "1.3.4"])
def test_an_old_board_driven_by_the_same_daemon_gets_dashes(tmp_path, fw):
    sent = _drive(tmp_path, fw)
    types = {m["t"] for m in sent}
    assert "plan" not in types and "sburn" not in types
    assert "burn" not in types
    shares = [m["session_pct"] for m in sent if m["t"] == "usage"]
    for share in run.SPEND_SHARES:
        assert share not in shares
    assert 41.0 in shares
    ok, lines = run.judge(_records(sent, fw=fw))
    assert ok, lines


def test_the_judge_passes_a_board_that_followed_along(tmp_path):
    sent = _drive(tmp_path, "1.5.0")
    ok, lines = run.judge(_records(sent))
    assert ok, lines
    # Kinds and counts only: no dollar figure in what it prints.
    text = "\n".join(lines)
    for figure in ("314", "318", "331", "482", "12.34"):
        assert figure not in text


def test_the_judge_fails_a_board_that_missed_a_page(tmp_path):
    sent = _drive(tmp_path, "1.5.0")
    board = ["[plan] claude spend monthly", "[plan] claude windows"]
    ok, lines = run.judge(_records(sent, board))
    assert not ok
    assert any(line.startswith("FAIL board") for line in lines)


@pytest.mark.parametrize("line", ["[proto] sburn line refused",
                                  "[proto] burn line refused",
                                  "[proto] rx ring overflow, dropped 3"])
def test_the_judge_fails_a_refused_line(tmp_path, line):
    sent = _drive(tmp_path, "1.5.0")
    recs = _records(sent) + [{"dir": "console", "line": line}]
    ok, lines = run.judge(recs)
    assert not ok
    assert "FAIL 1 refused lines" in lines


def test_the_judge_fails_a_reboot(tmp_path):
    sent = _drive(tmp_path, "1.5.0")
    recs = _records(sent) + [{"dir": "console",
                              "line": "[usage] firmware boot OK"}]
    ok, lines = run.judge(recs)
    assert not ok
    assert "FAIL 1 boot or fault lines" in lines


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
