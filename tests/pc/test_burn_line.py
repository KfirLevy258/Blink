"""The burn line: its encoder, the bus that decides it is wanted, and the
daemon's two sources for it (docs/plans/burn-5h.md).

Every status-line payload and transcript here is generated; none is captured
from a machine.
"""
import json
import os
import pathlib

from pc import burn_reader, ingest, protocol
from pc.providers import base, scripted
from pc.providers.claude_cli import ClaudeCliProvider
from tests.support import transcripts as tx

ROOT = pathlib.Path(__file__).resolve().parents[2]
Q = 1_793_430_000
NOW = Q + 600
RISING_K = [0, 0, 0, 0, 4, 9, 6, 12, 18, 15, 22, 30, 26, 34, 41, 38, 95, 140,
            210, 265]


def start(i):
    return Q - (19 - i) * 900


# ---------------------------------------------------------------- encoder


def test_the_line_is_the_contracts_to_the_byte():
    msg = protocol.burn("claude", Q, scripted.BURN_PRESETS["rising"])
    raw, why = protocol.encode_checked(msg)

    assert why is None
    assert raw == (b'{"t":"burn","v":2,"p":"claude","q":1793430000,"b":['
                   b'0,0,0,0,4,9,6,12,18,15,22,30,26,34,41,38,95,140,210,'
                   b'265]}\n')
    assert len(raw) == 109
    assert msg["b"] == RISING_K and msg["v"] == protocol.VERSION


def test_thousands_are_rounded_up_so_any_activity_shows():
    b = protocol.burn("claude", Q, [0, 1, 999, 1000, 1001, 2_100_000]
                      + [0] * 14)["b"]

    assert b[:6] == [0, 1, 1, 1, 2, 2100]


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
    """Every bucket at the ceiling and q in 2100: 193 bytes of the 512 the
    board takes before it drops a line whole."""
    msg = protocol.burn("claude", 4_102_444_800, [10 ** 15] * 20)
    raw, why = protocol.encode_checked(msg)

    assert why is None
    assert msg["b"] == [protocol.BURN_K_MAX] * 20
    assert len(raw) == 193 < protocol.MAX_LINE_BYTES
    assert protocol.burn("claude", Q, [-5] * 20)["b"] == [0] * 20


def test_burn_is_named_in_the_log_and_is_a_cap_the_board_can_list():
    msg = protocol.burn("claude", Q, [0] * 20)

    assert protocol.log_name(msg) == "burn"
    assert protocol.BOARD_CAP_BURN == "burn"
    assert "burn" in protocol.board_caps(
        {"caps": ["svc", "theme", "edit_done", "burn"]})


# -------------------------------------------------------------------- bus


def payload(tmp_path, five_hour=False, mtime=NOW - 1):
    """The status line as Claude Code writes it: seven_day, and five_hour
    only when the account has one (no key at all on Ultra)."""
    limits = {"seven_day": {"used_percentage": 12.0,
                            "resets_at": NOW + 3 * 86400}}
    if five_hour:
        limits["five_hour"] = {"used_percentage": 30.0,
                               "resets_at": NOW + 3600}
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
    transcripts(tmp_path, [0] * 19 + [7000])
    fetch = bus_for(tmp_path).fetch()
    fetch()
    assert fetch.burn() is not None

    payload(tmp_path, five_hour=True)
    fetch()

    assert fetch.burn() is None


def test_the_claude_page_as_the_second_page_still_gets_its_line(tmp_path):
    payload(tmp_path)
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
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(tmp_path / "statusline.json"))],
        now=lambda: NOW)
    fetch = bus.fetch()
    fetch()

    assert fetch.burn() is None


def test_a_reader_that_raises_costs_the_line_and_names_only_its_type(
        tmp_path, capsys):
    payload(tmp_path)

    class Broken:
        fail = True

        def poll(self, now):
            if self.fail:
                raise ValueError("MARKER-from-a-transcript")
            return Q, [0] * 20

    broken = Broken()
    fetch = bus_for(tmp_path, reader=broken).fetch()
    fetch()

    assert fetch.burn() is None and fetch.burn() is None
    err = capsys.readouterr().err
    assert "MARKER" not in err
    assert err.count("transcript reader failed (ValueError)") == 1

    broken.fail = False
    assert fetch.burn() == protocol.burn("claude", Q, [0] * 20)
    assert "working again" in capsys.readouterr().err


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
    assert line["b"] == protocol.burn(
        "claude", Q, scripted.BURN_PRESETS["steady"])["b"]


# ---------------------------------------------------------- scripted burn


def direction(b):
    """The board's rule (docs/plans/burn-5h.md), on the wire's thousands."""
    cur, prev = sum(b[16:]), sum(b[12:16])
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
    k = {name: protocol.burn("claude", Q, tokens)["b"]
         for name, tokens in scripted.BURN_PRESETS.items()}

    for name in ("idle", "steady", "rising", "easing"):
        assert direction(k[name]) == name, name
    assert sum(k["idle"]) == 0
    others = [v for i, v in enumerate(k["spike"]) if i != 9]
    assert k["spike"][9] >= 500 * max(others) and min(others) > 0
    assert sum(scripted.BURN_PRESETS["heavy"]) > 10_000_000


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
                            "b": RISING_K}


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

    spike = protocol.burn("claude", Q, scripted.BURN_PRESETS["spike"])["b"]
    assert at(0) == spike
    assert at(10) == spike                      # no burn key: held
    assert at(20) == protocol.burn("claude", Q, explicit)["b"]
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
