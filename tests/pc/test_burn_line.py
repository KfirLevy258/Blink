"""The burn line: its encoder, the bus that decides it is wanted, and the
daemon's two sources for it (docs/plans/burn-5h.md).

Every status-line payload and transcript here is generated; none is captured
from a machine.
"""
import json
import os
import pathlib

from pc import burn_reader, ingest, protocol, session_verdict
from pc.providers import base, scripted
from pc.providers.claude_cli import ClaudeCliProvider
from tests.support import transcripts as tx
from tests.support.home import inside

ROOT = pathlib.Path(__file__).resolve().parents[2]
Q = 1_793_430_000
NOW = Q + 600
RISING = [0, 0, 0, 0, 4_000, 9_000, 6_000, 12_000, 18_000, 15_000, 22_000,
          30_000, 26_000, 34_000, 41_000, 38_000, 95_000, 140_000, 210_000,
          265_000]


def start(i):
    return Q - (19 - i) * 900


# ---------------------------------------------------------------- encoder


def test_the_line_is_the_contracts_to_the_byte():
    msg = protocol.burn("claude", Q, scripted.BURN_PRESETS["rising"])
    raw, why = protocol.encode_checked(msg)

    assert why is None
    assert raw == (b'{"t":"burn","v":2,"p":"claude","q":1793430000,"b":['
                   b'0,0,0,0,4000,9000,6000,12000,18000,15000,22000,30000,'
                   b'26000,34000,41000,38000,95000,140000,210000,265000]}\n')
    assert len(raw) == 157
    assert msg["b"] == RISING and msg["v"] == protocol.VERSION


def test_the_buckets_are_exact_tokens_so_light_use_keeps_its_shape():
    """They were thousands rounded up, and the rounding decided the arrow
    at light use: 2001 then 1999 tokens a quarter-hour went out as 3 then 2
    and read "easing", and 1000 then 4 went out as 1 and 1 and read
    "steady" (review, 2026-10-31). The board is sent what was counted."""
    b = protocol.burn("claude", Q, [0, 1, 4, 999, 1000, 1001, 1999, 2001,
                                    2_100_000] + [0] * 11)["b"]

    assert b[:9] == [0, 1, 4, 999, 1000, 1001, 1999, 2001, 2_100_000]
    assert all(type(v) is int for v in b)


def test_a_line_has_twenty_buckets_starting_on_a_quarter_hour():
    for n in (19, 21):
        try:
            protocol.burn("claude", Q, [0] * n)
        except ValueError:
            continue
        raise AssertionError(n)
    try:
        protocol.burn("claude", Q + 1, [0] * 20)
    except ValueError:
        pass
    else:
        raise AssertionError("q off a quarter-hour")


def test_the_widest_line_fits_with_room_to_spare():
    """Every bucket at the ceiling and q in 2100: 233 bytes of the 512 the
    board takes before it drops a line whole."""
    msg = protocol.burn("claude", 4_102_444_800, [10 ** 15] * 20)
    raw, why = protocol.encode_checked(msg)

    assert why is None
    assert protocol.BURN_MAX == 99_999_999
    assert msg["b"] == [protocol.BURN_MAX] * 20
    assert len(raw) == 233 < protocol.MAX_LINE_BYTES
    assert protocol.burn("claude", Q, [-5] * 20)["b"] == [0] * 20


def test_twenty_buckets_at_the_ceiling_still_sum_in_a_signed_32_bit_int():
    """The board adds all twenty for the total. 99,999,999 is the widest
    eight-digit count, and twenty of them stay under 2**31, so no sum the
    board makes of the line can wrap, signed or not."""
    assert protocol.BURN_MAX * protocol.BURN_BUCKETS < 2 ** 31


def test_burn_is_named_in_the_log_and_is_a_cap_the_board_can_list():
    msg = protocol.burn("claude", Q, [0] * 20)

    assert protocol.log_name(msg) == "burn"
    assert protocol.BOARD_CAP_BURN == "burn"
    assert "burn" in protocol.board_caps(
        {"caps": ["svc", "theme", "edit_done", "burn"]})


def test_the_board_keeps_the_view_for_three_heartbeats():
    """The board drops the view BURN_FRESH_MS after the last line
    (firmware/src/usage_view.c), and the daemon re-sends at least every
    BURN_HEARTBEAT_S (pc/bridge). Both comments say three missed lines, and
    nothing tied the two numbers: a heartbeat raised past the board's window,
    or the window cut below it, and the view would blink off between lines
    with every test still green (/code-review max, 2026-11-01). Read from
    the C rather than copied, the way test_theme_page_burn reads burn_inks().
    """
    import re
    from pc import bridge
    src = (ROOT / "firmware" / "src" / "usage_view.c").read_text(
        encoding="utf-8")
    m = re.search(r"#define\s+BURN_FRESH_MS\s+\((\d+)\s*\*\s*1000\)", src)
    assert m, "BURN_FRESH_MS is no longer (<seconds> * 1000)"
    fresh_s = int(m.group(1))
    assert fresh_s == 3 * bridge.BURN_HEARTBEAT_S
    # And a line can be held back by the minimum gap, so even one late
    # heartbeat lands inside the window.
    assert fresh_s > bridge.BURN_HEARTBEAT_S + bridge.BURN_MIN_GAP_S


# -------------------------------------------------------------------- bus


def ultra(home, at=NOW - 60):
    """The verdict a daemon leaves on an Ultra desk (pc/session_verdict),
    where it leaves it, under `home`: proof, a minute old, that Claude Code
    replied for over five hours without a five-hour window. A weekly-only
    status line alone proves nothing."""
    session_verdict.save(
        inside(home, session_verdict.verdict_path()),
        session_verdict.evidence(
            False, session_verdict.KIND_STATUSLINE_SUSTAINED, at))


def payload(tmp_path, five_hour=False, mtime=NOW - 1):
    """The status line as Claude Code writes it: seven_day, and five_hour
    only when the account has one (no key at all on Ultra)."""
    limits = {"seven_day": {"used_percentage": 12.0,
                            "resets_at": NOW + 3 * 86400}}
    if five_hour:
        # Rendered after ultra()'s proof, which is what lifts it, whenever
        # the window opened (claude_cli._frame).
        limits["five_hour"] = {"used_percentage": 30.0,
                               "resets_at": NOW + 2 * 3600}
    p = tmp_path / "statusline.json"
    p.write_text(json.dumps({"rate_limits": limits}), encoding="utf-8")
    os.utime(p, (mtime, mtime))
    return p


def transcripts(tmp_path, tokens):
    tx.write(tmp_path, tmp_path / ".claude" / "projects" / "p" / "s.jsonl",
             [tx.assistant("msg_%d" % i, "req_%d" % i, start(i), t)
              for i, t in enumerate(tokens) if t],
             mtime=NOW - 2)


class Counting:
    """A reader that counts how often it is asked."""

    def __init__(self, inner):
        self.inner, self.asked = inner, 0

    def poll(self, now):
        self.asked += 1
        return self.inner.poll(now)


class Fixed(base.ProviderParser):
    def __init__(self, frame):
        self._f = frame

    def get_provider_id(self):
        return self._f.provider

    def poll(self, now_epoch):
        return [self._f]


def bus_for(tmp_path, *others, preferred="claude", reader=None):
    if reader is None:
        reader = burn_reader.BurnReader(
            root=str(tmp_path / ".claude" / "projects"))
    return ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(tmp_path / "statusline.json")),
                   *others],
        preferred_provider=preferred, now=lambda: NOW, burn=reader)


def test_an_account_with_no_session_window_is_sent_its_burn(tmp_path):
    payload(tmp_path)
    ultra(tmp_path)
    tokens = [0] * 18 + [1500, 260_000]
    transcripts(tmp_path, tokens)
    fetch = bus_for(tmp_path).fetch()

    assert fetch()["session_pct"] == -1
    assert fetch.burn() == protocol.burn("claude", Q, tokens)


def test_an_account_with_a_window_is_sent_none_and_nothing_is_read(tmp_path):
    payload(tmp_path, five_hour=True)
    transcripts(tmp_path, [5000] * 20)
    reader = Counting(burn_reader.BurnReader(
        root=str(tmp_path / ".claude" / "projects")))
    fetch = bus_for(tmp_path, reader=reader).fetch()

    assert fetch()["session_pct"] == 30.0
    assert fetch.burn() is None
    assert reader.asked == 0


def test_when_the_window_comes_back_the_line_stops(tmp_path):
    payload(tmp_path)
    ultra(tmp_path)
    transcripts(tmp_path, [0] * 19 + [7000])
    fetch = bus_for(tmp_path).fetch()
    fetch()
    assert fetch.burn() is not None

    payload(tmp_path, five_hour=True)
    fetch()

    assert fetch.burn() is None


def test_the_claude_page_as_the_second_page_still_gets_its_line(tmp_path):
    payload(tmp_path)
    ultra(tmp_path)
    transcripts(tmp_path, [0] * 19 + [3000])
    codex = Fixed(base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=NOW - 5, session_pct=40.0,
        weekly_pct=20.0))
    fetch = bus_for(tmp_path, codex, preferred="codex").fetch()

    usage = fetch()

    assert usage["provider"] == "codex" and usage["p2"] == "claude"
    assert fetch.burn()["p"] == "claude"


def test_a_bus_without_a_reader_sends_none(tmp_path):
    payload(tmp_path)
    ultra(tmp_path)
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(tmp_path / "statusline.json"))],
        now=lambda: NOW)
    fetch = bus.fetch()
    fetch()

    assert fetch.burn() is None


# A reader that raises is caught once, by Bridge._send_burn_if_due:
# tests/pc/test_burn_bridge.py, test_a_burn_source_that_raises_... and
# test_the_bus_leaves_a_reader_failure_to_the_bridge.


# ------------------------------------------------- the daemon's two sources


def test_the_daemon_reads_transcripts_and_a_scenario_never_does(
        tmp_path, monkeypatch):
    """BLINK_SCENARIO replaces every real source, so the board shows the
    same thing on every desk (build_bus). The transcripts under this home
    say 9000 tokens; the scenario says its own preset."""
    import claude_usage_bridge     # here, under the sandbox's BLINK_SKIP_SERVICE
    monkeypatch.delenv("BLINK_SCENARIO", raising=False)
    assert isinstance(claude_usage_bridge.build_bus()._burn,
                      burn_reader.BurnReader)

    transcripts(tmp_path, [0] * 19 + [9000])
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"name": "t", "steps": [
        {"at": 0, "provider": "claude", "weekly_pct": 12.0,
         "no_session_window": True, "burn": "steady"}]}), encoding="utf-8")
    monkeypatch.setenv("BLINK_SCENARIO", str(scenario))
    fetch = claude_usage_bridge.build_bus().fetch()
    fetch()

    line = fetch.burn()
    assert line["b"] == scripted.BURN_PRESETS["steady"]


# ---------------------------------------------------------- scripted burn


def direction(b):
    """The board's rule (docs/plans/burn-5h.md), on the wire's tokens, over
    complete quarter-hours only: the last hour is buckets 15-18 and the hour
    before it 11-14. Bucket 19 is the quarter-hour still filling."""
    cur, prev = sum(b[15:19]), sum(b[11:15])
    if cur == 0 and prev == 0:
        return "idle"
    if prev == 0:
        return "rising"
    if cur > prev * 1.1:
        return "rising"
    if cur < prev * 0.9:
        return "easing"
    return "steady"


def test_each_preset_shows_the_state_it_is_named_for():
    """On the line as it goes out, which is each preset unchanged."""
    b = {name: protocol.burn("claude", Q, tokens)["b"]
         for name, tokens in scripted.BURN_PRESETS.items()}

    for name, tokens in scripted.BURN_PRESETS.items():
        assert b[name] == tokens, name
    for name in ("idle", "steady", "rising", "easing"):
        assert direction(b[name]) == name, name
    assert sum(b["idle"]) == 0
    others = [v for i, v in enumerate(b["spike"]) if i != 9]
    assert b["spike"][9] >= 500 * max(others) and min(others) > 0
    assert sum(b["heavy"]) > 10_000_000


def scripted_bus(tmp_path, steps, clock):
    path = tmp_path / "scenario.json"
    path.write_text(json.dumps({"name": "t", "steps": steps}),
                    encoding="utf-8")
    sp = scripted.ScriptedProvider(str(path), now=lambda: clock[0])
    return ingest.IngestionBus(providers=[sp], now=lambda: clock[0],
                               burn=sp.burn_source()).fetch()


def step(at, burn=..., flag=True):
    s = {"at": at, "provider": "claude", "weekly_pct": 12.0}
    if flag:
        s["no_session_window"] = True
    else:
        s["session_pct"] = 37.0
    if burn is not ...:
        s["burn"] = burn
    return s


def test_a_scenario_step_puts_its_buckets_on_the_line(tmp_path):
    clock = [float(NOW)]
    fetch = scripted_bus(tmp_path, [step(0, "rising")], clock)

    assert fetch()["session_pct"] == -1
    assert fetch.burn() == {"t": "burn", "v": 2, "p": "claude", "q": Q,
                            "b": RISING}


def test_scenario_buckets_hold_until_a_step_changes_them(tmp_path):
    clock = [float(NOW)]
    explicit = list(range(0, 20_000, 1000))
    fetch = scripted_bus(tmp_path, [
        step(0, "spike"), step(10), step(20, explicit),
        step(30, flag=False), step(40, None)], clock)

    def at(t):
        clock[0] = NOW + t
        fetch()
        line = fetch.burn()
        return None if line is None else line["b"]

    spike = scripted.BURN_PRESETS["spike"]
    assert at(0) == spike
    assert at(10) == spike                      # no burn key: held
    assert at(20) == explicit
    assert at(30) is None                       # a window again: no line
    assert at(40) is None                       # null clears it


def test_a_bad_burn_is_reported_and_the_step_still_plays(tmp_path, capsys):
    clock = [float(NOW)]
    fetch = scripted_bus(tmp_path, [step(0, "idle"), step(10, "risng"),
                                    step(20, [1, 2, 3])], clock)
    fetch()
    for t in (10, 20):
        clock[0] = NOW + t
        assert fetch()["weekly_pct"] == 12.0     # the frame itself played

    assert fetch.burn()["b"] == [0] * 20        # idle, held
    err = capsys.readouterr().err
    assert err.count("burn ignored") == 2


def test_a_burn_that_is_not_finite_is_reported_not_raised(tmp_path, capsys):
    """A scenario is JSON, and Python's json reads the bare literals
    Infinity and NaN. int(inf) raises OverflowError, which nothing caught:
    a step with one took the provider's poll down (review, 2026-10-31)."""
    path = tmp_path / "scenario.json"
    path.write_text(
        '{"name": "t", "steps": ['
        '{"at": 0, "provider": "claude", "weekly_pct": 12.0,'
        ' "no_session_window": true, "burn": "idle"},'
        '{"at": 10, "provider": "claude", "weekly_pct": 12.0,'
        ' "no_session_window": true, "burn": [Infinity' + ', 0' * 19 + ']},'
        '{"at": 20, "provider": "claude", "weekly_pct": 12.0,'
        ' "no_session_window": true, "burn": [NaN' + ', 0' * 19 + ']}]}',
        encoding="utf-8")
    clock = [float(NOW)]
    sp = scripted.ScriptedProvider(str(path), now=lambda: clock[0])
    fetch = ingest.IngestionBus(providers=[sp], now=lambda: clock[0],
                                burn=sp.burn_source()).fetch()
    fetch()
    for t in (10, 20):
        clock[0] = NOW + t
        assert fetch()["weekly_pct"] == 12.0

    assert fetch.burn()["b"] == [0] * 20        # idle, held
    assert capsys.readouterr().err.count("burn ignored") == 2


def test_a_burn_count_too_big_for_a_float_is_capped_not_raised(tmp_path,
                                                                capsys):
    """math.isfinite() of an int past a float's range raises OverflowError
    rather than answering, and _take_burn caught only ValueError, so a
    step with a 400-digit count took the provider's poll down (/code-review
    max, 2026-11-01). An int is always finite; the line caps it at
    BURN_MAX like any other count."""
    huge = "9" * 400
    path = tmp_path / "scenario.json"
    path.write_text(
        '{"name": "t", "steps": ['
        '{"at": 0, "provider": "claude", "weekly_pct": 12.0,'
        ' "no_session_window": true, "burn": [' + huge + ', 0' * 19 + ']}]}',
        encoding="utf-8")
    clock = [float(NOW)]
    sp = scripted.ScriptedProvider(str(path), now=lambda: clock[0])
    fetch = ingest.IngestionBus(providers=[sp], now=lambda: clock[0],
                                burn=sp.burn_source()).fetch()
    assert fetch()["weekly_pct"] == 12.0

    assert fetch.burn()["b"] == [protocol.BURN_MAX] + [0] * 19
    assert "burn ignored" not in capsys.readouterr().err


def test_the_bench_scenario_walks_every_state_and_is_not_a_fleet_one():
    path = ROOT / "tests" / "fleet" / "bench" / "burn_states.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    used = {s["burn"] for s in doc["steps"] if "burn" in s}

    assert used == set(scripted.BURN_PRESETS)
    assert any("session_pct" in s and not s.get("no_session_window")
               for s in doc["steps"])
    # The fleet agent globs tests/fleet/scenarios/*.json for release runs.
    fleet = ROOT / "tests" / "fleet" / "scenarios"
    assert path.parent != fleet and not (fleet / path.name).exists()
