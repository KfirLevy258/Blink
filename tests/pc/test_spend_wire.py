"""The spend page on the wire (task 15c): the plan message, the countdown
it needs, what old firmware is sent, and the bus and Bridge around it.

Frames are built here, from the plan's figures: a $500 monthly cap with
$314.12 used, 62.8% of it.
"""
import json
import types

from pc import ingest, protocol
from pc.bridge import Bridge
from pc.providers import base

NOW = 1_790_000_000.0
_NO_FEED = dict(fetch_manifest=lambda: None, fetch_signed_manifest=lambda: None)


def spend(at=NOW - 5, pct=62.8, usd=314.12, lim=500.0, period="monthly",
          resets_in=18 * 86400 + 4 * 3600, provider="claude"):
    return base.NormalizedUsageFrame(
        provider=provider, src="cli", observed_at=at, session_pct=pct,
        session_resets_at=NOW + resets_in if resets_in else None,
        plan=base.PLAN_SPEND, spend_usd=usd, spend_limit_usd=lim,
        spend_period=period)


def windows(at=NOW - 5, s=41.0, w=12.0, provider="claude"):
    return base.NormalizedUsageFrame(
        provider=provider, src="cli", observed_at=at, session_pct=s,
        session_resets_at=NOW + 3600, weekly_pct=w,
        weekly_resets_at=NOW + 86400)


def test_the_plan_message():
    m = protocol.plan("claude", base.PLAN_SPEND, "monthly", 314.123, 500)
    assert m == {"t": "plan", "v": protocol.VERSION, "p": "claude",
                 "plan": "spend", "period": "monthly", "usd": 314.12,
                 "lim": 500.0}
    # Unknown amounts and period are omitted, as on the usage line.
    m = protocol.plan("claude", base.PLAN_SPEND)
    assert set(m) == {"t", "v", "p", "plan"}
    for bad in (-1, float("nan"), float("inf"), True, "314"):
        assert "usd" not in protocol.plan("claude", "spend", usd=bad)
    m = protocol.plan("claude", base.PLAN_UNREPORTED, account="enterprise")
    assert m["acct"] == "enterprise" and "usd" not in m
    # Windows says nothing but its name: the all-clear.
    m = protocol.plan("claude", base.PLAN_WINDOWS, "monthly", 1, 2, "team")
    assert m == {"t": "plan", "v": protocol.VERSION, "p": "claude",
                 "plan": "windows"}
    # Valid JSON, always.
    json.loads(protocol.encode(protocol.plan("claude", "spend", usd=1e9)))


def test_plan_for_names_the_page_with_a_plan():
    assert protocol.plan_for(spend())["plan"] == "spend"
    assert protocol.plan_for(windows()) == protocol.plan("claude", "windows")
    assert protocol.plan_for(None) == protocol.plan("claude", "windows")


def test_the_plan_names_claude_when_codex_holds_the_outer_ring():
    codex = windows(provider="codex")
    m = protocol.plan_for(codex, spend())
    assert m["p"] == "claude" and m["plan"] == "spend"
    assert m["usd"] == 314.12


def test_a_spend_countdown_is_not_clamped_to_twelve_days():
    month = 29 * 86400
    msg = protocol.frame_to_usage(spend(resets_in=month), NOW)
    assert msg["session_resets_in_s"] == month
    # Bounded all the same, at 32 days.
    msg = protocol.frame_to_usage(spend(resets_in=400 * 86400), NOW)
    assert msg["session_resets_in_s"] == protocol.SPEND_COUNTDOWN_MAX_S
    assert protocol.SPEND_COUNTDOWN_MAX_S == 32 * 86400
    # On the second page too.
    msg = protocol.frame_to_usage(windows(provider="codex"), NOW,
                                  secondary=spend(resets_in=month))
    assert msg["p2_s_in_s"] == month
    # And a window is still held to its old ceiling.
    w = windows()
    w.session_resets_at = NOW + 400 * 86400
    assert protocol.frame_to_usage(w, NOW)["session_resets_in_s"] == \
        protocol.COUNTDOWN_MAX_S


def test_a_spend_page_sends_no_weekly_window():
    msg = protocol.frame_to_usage(spend(), NOW)
    assert msg["weekly_pct"] == -1.0
    assert msg["weekly_resets_in_s"] == -1


def test_the_widest_spend_line_fits():
    """Both slots carrying the widest spend page the daemon can build, every
    count at 99, ages at INT32_MAX, the longest source id -- through
    encode_checked, like every production writer. And the widest plan."""
    def widest(provider):
        f = spend(provider=provider, pct=102.33333333333333,
                  resets_in=400 * 86400)
        f.src = "cli-state"
        f.observed_at = f.active_at = NOW - 2147483647
        f.state = "running"
        f.stale = False
        f.session_burn_pph = 999.93333
        for k in ("n_run", "n_wait", "n_stuck", "n_idle", "n_agents"):
            setattr(f, k, 99)
        return f

    for pair in ((widest("claude"), widest("codex")),
                 (windows(provider="codex"), widest("claude"))):
        raw, why = protocol.encode_checked(
            protocol.frame_to_usage(pair[0], NOW, secondary=pair[1]))
        assert why is None and len(raw) <= protocol.MAX_LINE_BYTES

    raw, why = protocol.encode_checked(protocol.plan(
        "claude" * 10, base.PLAN_SPEND, "monthly", 999_999_999.99,
        999_999_999.99, "enterprise"))
    assert why is None and len(raw) < 200


def test_old_firmware_gets_dashes_not_a_mislabelled_spend_share():
    """Review focus 1: a board that has not said it draws plans must never
    be handed the share of a dollar cap as its "SESSION 5h" figure."""
    usage = protocol.frame_to_usage(spend(), NOW)
    p = protocol.plan_for(spend())
    out = protocol.hide_plan_for_board(usage, p, False)
    assert out["session_pct"] == -1.0
    assert out["session_resets_in_s"] == -1
    assert usage["session_pct"] == 62.8         # not mutated
    assert protocol.hide_plan_for_board(usage, p, True) is usage


def test_only_the_exact_cap_name_lets_the_share_through():
    """The Bridge asks _board_can(BOARD_CAP_PLAN); a board listing other
    caps, or the name in another case, is a board without plans."""
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    for caps in ([], ["svc"], ["PLAN"]):
        _hello(b, "1.4.1", caps=caps)
        usage = [m for m in sent if m["t"] == "usage"][-1]
        assert usage["session_pct"] == -1.0, caps
        assert "plan" not in [m["t"] for m in sent], caps


def test_old_firmware_hiding_reaches_the_second_page_too():
    usage = protocol.frame_to_usage(windows(provider="codex"), NOW,
                                    secondary=spend())
    assert usage["p2_session_pct"] == 62.8
    out = protocol.hide_plan_for_board(usage, protocol.plan_for(
        windows(provider="codex"), spend()), False)
    assert "p2_session_pct" not in out and "p2_s_in_s" not in out
    # The codex page on the outer ring is somebody else's, and untouched.
    assert out["session_pct"] == 41.0


def test_hiding_leaves_windows_and_unreported_pages_alone():
    usage = protocol.frame_to_usage(windows(), NOW)
    for p in (None, protocol.plan("claude", "windows"),
              protocol.plan("claude", "unreported", account="team")):
        assert protocol.hide_plan_for_board(usage, p, False) is usage


def test_the_gate_is_a_capability_not_a_version():
    """1.4.0 is already the svc status line's gate (FW_SHOWS_SVC), and this
    branch is held, so a 1.4.0 board need not draw plans at all. Only the
    board's own word counts."""
    assert not hasattr(protocol, "FW_KNOWS_PLAN")
    assert protocol.board_caps({"t": "hello", "fw": "9.9.9"}) == frozenset()
    assert protocol.board_caps({"t": "hello", "caps": ["plan", 3]}) == \
        frozenset({"plan"})
    for junk in ("plan", {"plan": 1}, None, 7):
        assert protocol.board_caps({"caps": junk}) == frozenset()


def _source(held):
    """A claude provider that reports whatever is in `held` at each poll;
    a test changes what it reports by refilling that list."""
    return types.SimpleNamespace(get_provider_id=lambda: "claude",
                                 poll=lambda now: list(held))


class _Watch:
    def __init__(self, kind="", since=None):
        self.kind, self.since, self.touched_at = kind, since, None

    def poll(self, now):
        pass


def test_the_bus_shows_spend_and_says_so():
    bus = ingest.IngestionBus(providers=[_source([spend()])],
                              now=lambda: NOW, account=_Watch("enterprise"))
    msg = bus.poll()
    assert msg["session_pct"] == 62.8
    assert msg["session_resets_in_s"] == 18 * 86400 + 4 * 3600
    assert bus.plan_msg() == protocol.plan(
        "claude", "spend", "monthly", 314.12, 500.0)
    assert bus.fetch().plan_msg() == bus.plan_msg()


def test_a_windows_bus_has_no_plan_to_send():
    bus = ingest.IngestionBus(providers=[_source([windows()])],
                              now=lambda: NOW, account=_Watch("enterprise"))
    bus.poll()
    assert bus.plan_msg() is None


def test_the_bus_drops_the_old_account_at_a_switch():
    """Review focus 5, end to end on the bus: Max figures fresh in the
    files, a switch to Enterprise seen at NOW-30, and no spend reading yet."""
    src_frames = [spend(at=NOW - 3000)]
    src = _source(src_frames)
    watch = _Watch("consumer")
    bus = ingest.IngestionBus(providers=[src], now=lambda: NOW,
                              account=watch)
    bus.poll()                       # armed by the old spend reading
    src_frames[:] = [windows(at=NOW - 60, s=77.0), windows(at=NOW - 40)]
    watch.kind, watch.since = "enterprise", NOW - 30
    msg = bus.poll()
    assert msg["session_pct"] == -1.0 and msg["weekly_pct"] == -1.0
    assert bus.plan_msg()["plan"] == "unreported"
    assert bus.plan_msg()["acct"] == "enterprise"

    # And back to Max, seen at NOW+10, before Max has rendered anything:
    # no reading at all, and the board is told it is windows again.
    watch.kind, watch.since = "consumer", NOW + 10
    src_frames[:] = [spend(at=NOW - 1)]
    assert bus.poll() is None
    assert bus.plan_msg() == protocol.plan("claude", "windows")


def test_a_scenario_bus_does_not_read_this_desks_account(monkeypatch):
    from pc import claude_account
    monkeypatch.setattr(claude_account, "AccountWatch",
                        lambda *a, **k: (_ for _ in ()).throw(
                            AssertionError("read the desk's account")))
    bus = ingest.IngestionBus(providers=[_source([spend()])],
                              now=lambda: NOW)
    assert bus.poll()["session_pct"] == 62.8
    assert bus.plan_msg()["plan"] == "spend"


def test_an_account_that_raises_costs_nothing():
    class Broken(_Watch):
        def poll(self, now):
            raise OSError("profile vanished")

    bus = ingest.IngestionBus(providers=[_source([spend()])],
                              now=lambda: NOW, account=Broken())
    assert bus.poll()["session_pct"] == 62.8
    assert bus.plan_msg()["plan"] == "spend"


class _Fetch:
    """A fetch with the bus's accessors, driven by hand."""

    def __init__(self, usage, plan=None):
        self.usage, self.plan = usage, plan

    def __call__(self):
        return self.usage

    def session_pair(self):
        return ("", 0)

    def plan_msg(self):
        return self.plan


def _bridge(fetch, sent):
    return Bridge(write_msg=sent.append, fetch_usage=fetch,
                  now=lambda: 1000.0, wall=lambda: (1752444000, 0),
                  **_NO_FEED)


def _hello(b, fw, caps=None):
    """A hello as a board on `fw` sends it. Firmware past 1.3.4 built from
    this branch announces BOARD_CAP_PLAN; pass caps=[] for one that does not."""
    if caps is None:
        caps = [] if fw == "1.3.4" else [protocol.BOARD_CAP_PLAN]
    hello = {"t": "hello", "v": protocol.VERSION, "fw": fw}
    if caps:
        hello["caps"] = caps
    b.on_message(hello)


def test_the_plan_goes_out_before_the_usage_it_describes():
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    types = [m["t"] for m in sent]
    assert types.index("plan") < types.index("usage")
    usage = [m for m in sent if m["t"] == "usage"][0]
    assert usage["session_pct"] == 62.8


def test_the_plan_is_sent_on_change_and_again_on_every_connect():
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    for _ in range(5):
        b.poll_if_changed()
        b.poll_once()
    assert [m["t"] for m in sent].count("plan") == 1

    # The dollars move on their own schedule; the usage line does not.
    f.plan = protocol.plan_for(spend(usd=318.0))
    b.poll_if_changed()
    plans = [m for m in sent if m["t"] == "plan"]
    assert len(plans) == 2 and plans[-1]["usd"] == 318.0

    _hello(b, "1.4.0")              # a reboot: told again
    assert [m["t"] for m in sent].count("plan") == 3


def test_an_old_board_is_sent_dashes():
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.3.4")
    usage = [m for m in sent if m["t"] == "usage"][-1]
    assert usage["session_pct"] == -1.0
    assert usage["session_resets_in_s"] == -1


def test_a_desk_without_plans_sends_nothing_new():
    """The release gate at the Bridge: no plan message, the usage line as
    it always was."""
    sent = []
    f = _Fetch(protocol.frame_to_usage(windows(), NOW), None)
    b = _bridge(f, sent)
    _hello(b, "1.3.4")
    b.poll_once()
    b.poll_if_changed()
    assert "plan" not in [m["t"] for m in sent]
    usage = [m for m in sent if m["t"] == "usage"][-1]
    assert usage["session_pct"] == 41.0


def test_switching_back_clears_the_plan_before_max_has_said_anything():
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    f.usage, f.plan = None, protocol.plan("claude", "windows")
    b.poll_if_changed()
    assert sent[-1] == protocol.plan("claude", "windows")
    f.plan = protocol.plan("claude", "unreported", account="enterprise")
    b.poll_once()
    assert [m for m in sent if m["t"] == "plan"][-1]["plan"] == "unreported"


def test_every_message_the_bridge_writes_fits_the_line():
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    for m in sent:
        raw, why = protocol.encode_checked(m)
        assert why is None, m["t"]


def test_a_1_4_0_board_without_the_capability_is_sent_dashes_and_no_plan():
    """The collision the version gate had: svc's 1.4.0 without plans."""
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0", caps=[])
    assert "plan" not in [m["t"] for m in sent]
    usage = [m for m in sent if m["t"] == "usage"][-1]
    assert usage["session_pct"] == -1.0


def test_a_board_already_running_says_what_it_draws_in_its_pref():
    """A daemon that connects without resetting the board gets no hello;
    the pref that answers its welcome carries the capability instead, and
    the next tick sends the plan and the share it had been blanking."""
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    b.greet()
    assert [m for m in sent if m["t"] == "usage"][-1]["session_pct"] == -1.0
    b.on_message({"t": "pref", "v": protocol.VERSION, "provider": "claude",
                  "caps": [protocol.BOARD_CAP_PLAN]})
    b.poll_if_changed()
    types_ = [m["t"] for m in sent]
    assert types_[-2:] == ["plan", "usage"]
    assert sent[-1]["session_pct"] == 62.8
    # And a pref without caps -- the board reflashed to older firmware --
    # takes it away again.
    b.on_message({"t": "pref", "v": protocol.VERSION, "provider": "claude"})
    b.poll_if_changed()
    assert sent[-1]["t"] == "usage" and sent[-1]["session_pct"] == -1.0


def test_plan_is_a_known_wire_type():
    """The bridge's log names a message by its type only, and only a type in
    WIRE_TYPES; a plan line outside it would log as "(unknown type)"."""
    assert "plan" in protocol.WIRE_TYPES
    msg = protocol.plan("claude", base.PLAN_SPEND, "monthly", 314.12, 500.0)
    assert protocol.log_name(msg) == "plan"


def test_plan_console_line_is_allowed():
    """proto.c prints "[plan] <page> <kind> [period]" for each plan it takes.
    bridge.log keeps the tag and nothing the board formatted after it."""
    from pc import logbook
    for line in ("[plan] claude spend monthly", "[plan] claude windows"):
        assert logbook.console_line(line) == "[plan]"
