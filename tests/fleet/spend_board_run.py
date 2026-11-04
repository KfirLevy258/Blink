"""Drive one board through the spend pages with a scripted timeline.

    .venv-test/bin/python -m tests.fleet.spend_board_run [--port PORT] \\
        [--out DIR]

Task 15e of the fork-picks plan, kept out of tests/fleet/scenarios on
purpose: every file in that directory runs on all three fleet desks before a
release, and the spend pages are held from release. This runs only when
somebody asks for it, against the board on this desk.

The timeline, generated below rather than kept as a file (fixtures in this
repository are made by committed code, never captured), is the life of one
usage-billed seat through a month (docs/plans/2026-11-04-spend-page-plan.md,
task 8):

    windows      41% / 12%, the page as every desk has it today
    exact        $314.12 of $500 just after /usage            dollars: steady
    estimated    $318.40, counted from the status line since  dollars: rising
    hint         $331.80 on a /usage figure over a day old    dollars: easing
    near cap     $482.00, 96.4%: "almost used up"             dollars: heavy
    reached      $500.00 of $500, exact: "used up"            dollars: rising
    rollover     the 1st: $0.40 counted, no cap known, hint   dollars: idle
    Max          /login to Max: windows again                 (no dollar line)
    back         /login back, /usage run: $12.34 of $500      dollars: steady

On a board whose hello announces protocol.BOARD_CAP_PLAN, the daemon sends
a plan message per change, and the board prints one `[plan]` line for each
message it is sent; a board that also announces BOARD_CAP_SPENDBURN is sent
the dollar burn line (`sburn`) on every spend step. The panel should match
panel_render scenes 45-61 along the way. A board that does not announce
plans -- 1.4.1, 1.3.4 -- is sent no plan message and no `sburn`, and the
usage lines on the spend steps carry dashes instead of the share: the
review's first focus, proved on hardware. No step has a token burn line, so
no `burn` line goes to either.

What it needs and what it does, so nobody is surprised:
  - The installed service is stopped for the run and started again after,
    from a finally, the way tests/fleet/agent.py does it (and with its code).
  - The daemon runs from this checkout under a sandboxed HOME, with
    BLINK_SCENARIO replacing every real source: nothing on this desk is read,
    and no usage is generated anywhere.
  - Nothing is flashed. Which firmware it checks is whatever the board says
    in its hello.
  - It prints which page kinds the board reported and whether they matched,
    never a raw serial line.
"""
import argparse
import json
import sys
import tempfile
import time
from pathlib import Path

from pc import protocol
from tests.fleet import agent

# Seconds between steps. Long enough for a 3 s poll to land each step at
# least twice, for the dollar burn line's 15 s minimum gap
# (bridge.BURN_MIN_GAP_S) to let one line out per step, and for a person
# watching the panel to compare it with the matching render.
STEP_S = 20
POLL_S = 3.0

_MONTHLY = 16 * 86400 + 9 * 3600
_WINDOWS = {"session_pct": 41.0, "weekly_pct": 12.0,
            "session_resets_in_s": 3 * 3600 + 40 * 60,
            "weekly_resets_in_s": 6 * 86400 + 22 * 3600, "state": "running"}


def _spend(pct, usd, lim, est, *, hint=False, resets=_MONTHLY, sburn):
    step = {"plan": "spend", "period": "monthly", "session_pct": pct,
            "usd": usd, "est": est, "hint": hint,
            "session_resets_in_s": resets, "sburn": sburn,
            "state": "running"}
    if lim is not None:
        step["lim"] = lim
    return step


# (name, the step itself)
TIMELINE = (
    ("windows", dict(_WINDOWS)),
    ("exact", _spend(62.8, 314.12, 500, False, sburn="steady")),
    ("estimated", _spend(63.7, 318.40, 500, True, sburn="rising")),
    ("hint", _spend(66.4, 331.80, 500, True, hint=True, sburn="easing")),
    ("near cap", _spend(96.4, 482.00, 500, True, sburn="heavy")),
    ("reached", _spend(100.0, 500.00, 500, False, sburn="rising")),
    # Past 00:00 UTC on the 1st: the anchor is last month's, so the figure
    # is the count since midnight, no cap is known and the hint shows. The
    # five-hour dollar history was cleared with the period.
    ("rollover", _spend(-1.0, 0.40, None, True, hint=True,
                        resets=30 * 86400 + 23 * 3600, sburn="idle")),
    ("Max", dict(_WINDOWS, sburn=None)),
    ("back", _spend(2.5, 12.34, 500, False,
                    resets=30 * 86400 + 22 * 3600, sburn="steady")),
)

# Every share a spend step shows. None of them may reach a board that does
# not draw plans: there it would read as the five-hour window.
SPEND_SHARES = tuple(step["session_pct"] for _, step in TIMELINE
                     if step.get("plan") == "spend"
                     and step["session_pct"] >= 0)


def scenario_doc():
    steps = [dict(step, at=i * STEP_S, provider="claude")
             for i, (_, step) in enumerate(TIMELINE)]
    return {"name": "spend_pages",
            "note": "Task 8: windows, then a usage-billed seat exact, "
                    "estimated, hinted, near and at its cap, across a month "
                    "rollover, to Max and back. Generated by "
                    "tests/fleet/spend_board_run.py.",
            "steps": steps,
            "duration_s": len(TIMELINE) * STEP_S}


def _plan_of(step):
    if step.get("plan") != "spend":
        return protocol.plan("claude", protocol.PLAN_WINDOWS)
    return protocol.plan("claude", "spend", step["period"], step["usd"],
                         step.get("lim"), est=step["est"],
                         hint=step["hint"])


def expected_plans():
    """The plan messages a board that draws plans is sent, one per change.
    The first windows step sends none: the bus has no plan message until it
    has seen a spend reading, which is the release gate."""
    out = []
    for _, step in TIMELINE[1:]:
        msg = _plan_of(step)
        if not out or out[-1] != msg:
            out.append(msg)
    return out


def expected_sburns():
    """The dollar buckets each spend step puts on the wire."""
    from pc.providers.scripted import SBURN_PRESETS
    return [tuple(SBURN_PRESETS[step["sburn"]]) for _, step in TIMELINE
            if step.get("plan") == "spend"]


def _words(msg):
    """What the board's `[plan]` line says for one plan message, after the
    page: proto.c prints kind and period, never the dollars."""
    return " ".join(w for w in (msg.get("plan"), msg.get("period")) if w)


def board_plans(records):
    """The page kinds the board reported, from its own [plan] lines."""
    out = []
    for r in records:
        if r.get("dir") != "console":
            continue
        line = str(r.get("line", ""))
        at = line.find("[plan] ")
        if at < 0:
            continue
        words = line[at + len("[plan] "):].split()
        # "[plan] claude spend monthly" -> "spend monthly"
        out.append(" ".join(words[1:3]) if len(words) > 1 else "?")
    return out


def board_hello(records):
    """The hello's fields, with `caps` as the last hello or pref left it --
    the same rule the Bridge applies (a pref without caps clears them)."""
    found = {}
    for r in records:
        msg = r.get("msg")
        if r.get("dir") != "rx" or not isinstance(msg, dict) \
                or msg.get("t") not in ("hello", "pref"):
            continue
        if msg["t"] == "hello":
            found = dict(msg)
        else:
            found.pop("caps", None)
            found.update({k: v for k, v in msg.items() if k == "caps"})
    return found


def board_fw(records):
    """The firmware the board said it runs: its hello's `fw`, else the
    `cur` of its OTA query. A board that was already running when the
    daemon connected sends no hello (on the board run of 2026-11-04 it sent
    a pref, a theme_now and an ota_query), so the hello alone left the
    summary saying "not heard" about a board it had just talked to."""
    fw = board_hello(records).get("fw")
    if fw:
        return str(fw)
    for r in records:
        msg = r.get("msg")
        if (r.get("dir") == "rx" and isinstance(msg, dict)
                and msg.get("t") == "ota_query" and msg.get("cur")):
            return str(msg["cur"])
    return None


def board_uptimes(records):
    """The board's own uptime from each ping, in milliseconds, in order."""
    return [r["msg"]["up_ms"] for r in records
            if r.get("dir") == "rx" and isinstance(r.get("msg"), dict)
            and r["msg"].get("t") == "ping"
            and isinstance(r["msg"].get("up_ms"), int)]


def sent(records, t):
    return [r["msg"] for r in records
            if r.get("dir") == "tx" and r.get("sent") is not False
            and isinstance(r.get("msg"), dict) and r["msg"].get("t") == t]


# Console lines that end a run's claim to be clean, by what they mean. A
# refused line is a message the board parsed and threw away; a boot line in
# the middle of a run is a reboot.
_REFUSED = ("line refused", "rx ring overflow")
_REBOOTED = ("[usage] firmware boot OK", "[fatal]", "not healthy within 90 s")


def _console(records):
    return [str(r.get("line", "")) for r in records
            if r.get("dir") == "console"]


def judge(records):
    """(ok, lines) for one run's transcript. Pure, so it is tested.

    The lines name message types, page kinds and counts -- never a raw
    serial line or a dollar figure."""
    lines = []
    hello = board_hello(records)
    caps = protocol.board_caps(hello)
    new = protocol.BOARD_CAP_PLAN in caps
    dollars = protocol.BOARD_CAP_SPENDBURN in caps
    lines.append(f"board firmware: {board_fw(records) or 'not heard'}"
                 f" ({'announces' if new else 'does not announce'} plans,"
                 f" {'announces' if dollars else 'does not announce'}"
                 f" spendburn)")
    ok = True

    def check(good, ok_line, fail_line):
        nonlocal ok
        ok = ok and good
        lines.append(("ok   " + ok_line) if good else ("FAIL " + fail_line))

    plans = sent(records, "plan")
    want = expected_plans() if new else []
    # By message: a reconnect re-sends the plan it had, which is a repeat,
    # not a step.
    got = _dedupe(plans)
    check(got == want,
          f"daemon sent {len(got)} plan messages, as the timeline says",
          f"daemon sent plans {[_words(m) for m in got]}"
          f" {[(m.get('est', 0), m.get('hint', 0)) for m in got]},"
          f" wanted {[_words(m) for m in want]}"
          f" {[(m.get('est', 0), m.get('hint', 0)) for m in want]}")
    # One [plan] line for every plan message sent, in order: the board
    # prints one per message received, the same page or not.
    board = board_plans(records)
    want_board = [_words(m) for m in plans]
    check(board == want_board,
          f"board printed {len(board)} [plan] lines, one per message",
          f"board printed {board}, wanted {want_board}")

    sburns = sent(records, "sburn")
    if dollars:
        shown = {tuple(m.get("b") or ()) for m in sburns}
        missing = [b for b in expected_sburns() if b not in shown]
        check(not missing,
              f"daemon sent {len(sburns)} sburn lines, every spend step's",
              f"{len(missing)} spend steps' dollar buckets never sent")
    else:
        check(not sburns, "no sburn line sent",
              f"{len(sburns)} sburn lines sent to a board without the cap")
    burns = sent(records, "burn")
    check(not burns, "no token burn line sent",
          f"{len(burns)} token burn lines sent")
    if not new:
        shares = [m.get("session_pct") for m in sent(records, "usage")]
        check(not any(s in shares for s in SPEND_SHARES),
              "no spend share reached the board",
              "a board without plans was sent a spend share as its session"
              " figure")

    console = _console(records)
    refused = [x for x in console if any(k in x for k in _REFUSED)]
    check(not refused, "no refused line",
          f"{len(refused)} refused lines")
    reboots = [x for x in console if any(k in x for k in _REBOOTED)]
    check(not reboots, "no boot or fault line",
          f"{len(reboots)} boot or fault lines")
    # The board's uptime only grows on one boot. The console can miss a
    # boot line (it prints before the port is open again); the pings
    # cannot.
    up = board_uptimes(records)
    restarted = sum(1 for a, b in zip(up, up[1:]) if b < a)
    if up:
        check(not restarted,
              f"no reboot: board uptime {up[0] // 1000} s to"
              f" {up[-1] // 1000} s over {len(up)} pings",
              f"board uptime went back {restarted} times")
    else:
        check(False, "", "no ping heard, so no uptime to judge a reboot by")
    return ok, lines


def _dedupe(seq):
    out = []
    for x in seq:
        if not out or out[-1] != x:
            out.append(x)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--port", default="auto")
    ap.add_argument("--out", default=None,
                    help="directory for the scenario, tap and daemon log")
    args = ap.parse_args(argv)

    work = Path(args.out or tempfile.mkdtemp(prefix="blink-spend-"))
    work.mkdir(parents=True, exist_ok=True)
    scenario = work / "spend_pages.json"
    scenario.write_text(json.dumps(scenario_doc(), indent=2),
                        encoding="utf-8")
    tap = work / "tap.jsonl"
    if tap.exists():
        tap.unlink()
    home = work / "home"
    home.mkdir(exist_ok=True)
    env = agent.env_for_run(home, scenario=scenario, tap=tap, sandbox=True,
                            poll_interval=POLL_S)

    stopped = agent.stop_service()
    print(f"stop service: {stopped}")
    if stopped.skipped:
        print("BLINK_SKIP_SERVICE is set in this shell, so the installed"
              " service still holds the port. Nothing was run.")
        return 2
    if not stopped.ok:
        print("The installed service would not stop, so the port is"
              " probably still held. Nothing was run.")
        print(f"start service: {agent.start_service()}")
        return 2
    problems = []
    try:
        time.sleep(agent.PORT_SETTLE_S)
        proc = agent._popen(agent.daemon_cmd(args.port), env,
                            agent.REPO_ROOT)
        try:
            time.sleep(scenario_doc()["duration_s"] + 20)
        finally:
            problems += agent._stop_daemon(proc, "the spend run's daemon")
    finally:
        print(f"start service: {agent.start_service()}")

    ok, lines = judge(agent.read_tap(tap))
    for line in problems + lines:
        print(line)
    print(f"transcript in {work}")
    print("PASS" if ok and not problems else "FAIL")
    return 0 if ok and not problems else 1


if __name__ == "__main__":
    sys.exit(main())
