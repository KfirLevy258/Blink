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
        "claude", "spend", "monthly", 314.12, 500.0, est=True)
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
    """/login back to Max with no Max reading yet: the plan goes back to
    windows, and the spend share the board holds goes with it. The plan
    alone (what this test asserted until the /code-review max of
    2026-11-04, U15) left the board repainting 62.8 -- a share of dollars --
    under "SESSION 5h" beside a 26-day countdown, and "Session almost used
    up" near the cap, until Max next rendered: hours on an idle desk."""
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    f.usage, f.plan = None, protocol.plan("claude", "windows")
    n = len(sent)
    b.poll_if_changed()
    new = sent[n:]
    assert [m["t"] for m in new] == ["plan", "usage"]
    assert new[0] == protocol.plan("claude", "windows")
    blank = new[1]
    assert blank["provider"] == "claude"
    assert blank["session_pct"] == -1 and blank["weekly_pct"] == -1
    assert blank["session_resets_in_s"] == -1
    assert blank["weekly_resets_in_s"] == -1
    raw, why = protocol.encode_checked(blank)
    assert why is None
    # Said once: the next ticks have nothing new to take down.
    n = len(sent)
    b.poll_if_changed()
    b.poll_once()
    assert "usage" not in [m["t"] for m in sent[n:]]
    f.plan = protocol.plan("claude", "unreported", account="enterprise")
    b.poll_once()
    assert [m for m in sent if m["t"] == "plan"][-1]["plan"] == "unreported"


def test_the_blank_keeps_the_other_page():
    """A spend page on the second slot, Codex main: only the spend page's
    figures are taken down, from the line the board last had."""
    sent = []
    codex = windows(provider="codex", s=33.0, w=7.0)
    f = _Fetch(protocol.frame_to_usage(codex, NOW, secondary=spend()),
               protocol.plan_for(codex, spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    f.usage, f.plan = None, protocol.plan("claude", "windows")
    b.poll_once()
    blank = [m for m in sent if m["t"] == "usage"][-1]
    assert blank["provider"] == "codex" and blank["p2"] == "claude"
    assert blank["session_pct"] == 33.0 and blank["weekly_pct"] == 7.0
    # Absent is -1 to proto.c, and how usage() says unknown on page two.
    for k in ("p2_session_pct", "p2_s_in_s", "p2_weekly_pct", "p2_w_in_s"):
        assert k not in blank


def test_no_blank_when_a_reading_comes_with_the_switch():
    """With a Max reading in hand, the usage line itself replaces the share:
    no second line."""
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    f.usage = protocol.frame_to_usage(windows(), NOW)
    f.plan = protocol.plan("claude", "windows")
    n = len(sent)
    b.poll_if_changed()
    usages = [m for m in sent[n:] if m["t"] == "usage"]
    assert len(usages) == 1 and usages[0]["session_pct"] == 41.0


def test_the_heartbeat_restates_the_plan():
    """One plan line lost on the wire -- dropped whole, as any line can be
    -- was repaired only when the dollars next moved, which on an idle desk
    is hours: the board drew the spend share under "SESSION 5h" all that
    time (/code-review max of 2026-11-04, U16). The heartbeat restates the
    plan, as it restates svc and the usage line, and always before the
    usage line it describes; the fast tick still sends it on change only."""
    sent = []
    f = _Fetch(protocol.frame_to_usage(spend(), NOW),
               protocol.plan_for(spend()))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    n = len(sent)
    for _ in range(3):
        b.poll_once()
    types_ = [m["t"] for m in sent[n:]]
    assert types_.count("plan") == 3
    for i, t in enumerate(types_):
        if t == "usage":
            assert types_[i - 1] == "plan"
    n = len(sent)
    for _ in range(3):
        b.poll_if_changed()
    assert "plan" not in [m["t"] for m in sent[n:]]
    # With no reading at all the heartbeat still restates it.
    f.usage = None
    n = len(sent)
    b.poll_once()
    assert [m["t"] for m in sent[n:]].count("plan") == 1
    # And a desk that has never seen a plan still sends none.
    sent2 = []
    b2 = _bridge(_Fetch(protocol.frame_to_usage(windows(), NOW), None), sent2)
    _hello(b2, "1.4.0")
    b2.poll_once()
    assert "plan" not in [m["t"] for m in sent2]


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


# ---------------------------------------------------- est/hint and sburn


def test_plan_flags_only_when_true():
    m = protocol.plan("claude", base.PLAN_SPEND, "monthly", 1, 2)
    assert "est" not in m and "hint" not in m
    m = protocol.plan("claude", base.PLAN_SPEND, "monthly", 1, 2,
                      est=True, hint=True)
    assert m["est"] == 1 and m["hint"] == 1
    # Windows and unreported plans never carry them.
    m = protocol.plan("claude", base.PLAN_WINDOWS, est=True, hint=True)
    assert set(m) == {"t", "v", "p", "plan"}
    m = protocol.plan("claude", base.PLAN_UNREPORTED, account="team",
                      est=True, hint=True)
    assert "est" not in m and "hint" not in m
    # plan_for reads them off the spend primary.
    f = spend()
    assert "est" in protocol.plan_for(f) and "hint" not in protocol.plan_for(f)
    f.spend_exact = True
    assert "est" not in protocol.plan_for(f)
    f.spend_hint = True
    assert protocol.plan_for(f)["hint"] == 1
    assert "est" not in protocol.plan_for(windows())


def test_plan_resent_when_estimate_starts():
    """The anchor turning old flips spend_exact; the whole plan dict is
    compared, so the flag goes out on its own."""
    sent = []
    exact = spend()
    exact.spend_exact = True
    f = _Fetch(protocol.frame_to_usage(exact, NOW), protocol.plan_for(exact))
    b = _bridge(f, sent)
    _hello(b, "1.4.0")
    assert [m["t"] for m in sent].count("plan") == 1
    assert "est" not in [m for m in sent if m["t"] == "plan"][0]
    b.poll_if_changed()
    assert [m["t"] for m in sent].count("plan") == 1
    old = spend()                       # anchor 900 s old: not exact now
    f.plan = protocol.plan_for(old)
    b.poll_if_changed()
    plans = [m for m in sent if m["t"] == "plan"]
    assert len(plans) == 2 and plans[-1]["est"] == 1
    old.spend_hint = True
    f.plan = protocol.plan_for(old)
    b.poll_if_changed()
    assert [m for m in sent if m["t"] == "plan"][-1]["hint"] == 1


def test_spend_burn_shape():
    q = protocol.burn_quarter(NOW)
    cents = list(range(20))
    m = protocol.spend_burn("claude", q, cents)
    assert m == {"t": "sburn", "v": protocol.VERSION, "p": "claude",
                 "q": q, "b": cents}
    assert "sburn" in protocol.WIRE_TYPES
    assert protocol.log_name(m) == "sburn"
    try:
        protocol.spend_burn("claude", q + 1, cents)
    except ValueError:
        pass
    else:
        raise AssertionError("q must be a whole quarter")
    try:
        protocol.spend_burn("claude", q, cents[:19])
    except ValueError:
        pass
    else:
        raise AssertionError("twenty buckets")


def test_spend_burn_clamps():
    q = protocol.burn_quarter(NOW)
    m = protocol.spend_burn("claude", q, [-5, 10**12] + [7] * 18)
    assert m["b"][:3] == [0, protocol.SBURN_MAX_CENTS, 7]
    assert protocol.SBURN_MAX_CENTS == 99_999_999


class _SFetch(_Fetch):
    def __init__(self, line):
        super().__init__(protocol.frame_to_usage(spend(), NOW),
                         protocol.plan_for(spend()))
        self.line = line

    def spend_burn(self, provider="claude"):
        if isinstance(self.line, Exception):
            raise self.line
        return self.line


def _timed(fetch, sent, clock):
    return Bridge(write_msg=sent.append, fetch_usage=fetch,
                  now=lambda: clock[0], wall=lambda: (1752444000, 0),
                  **_NO_FEED)


def _sburns(sent):
    return [m for m in sent if m["t"] == "sburn"]


def test_no_sburn_without_the_cap():
    q = protocol.burn_quarter(NOW)
    sent, clock = [], [1000.0]
    f = _SFetch(protocol.spend_burn("claude", q, [1] * 20))
    b = _timed(f, sent, clock)
    _hello(b, "1.4.0")                      # plan only, no spendburn
    clock[0] += 100
    b.poll_if_changed()
    assert _sburns(sent) == []
    b.on_message({"t": "pref", "v": protocol.VERSION, "caps": [
        protocol.BOARD_CAP_PLAN, protocol.BOARD_CAP_SPENDBURN]})
    clock[0] += 100
    b.poll_if_changed()
    assert len(_sburns(sent)) == 1


def test_sburn_cadence_and_greet_reset(capsys):
    q = protocol.burn_quarter(NOW)
    line = protocol.spend_burn("claude", q, [1] * 20)
    sent, clock = [], [1000.0]
    f = _SFetch(line)
    b = _timed(f, sent, clock)
    _hello(b, "1.4.0", caps=[protocol.BOARD_CAP_PLAN,
                             protocol.BOARD_CAP_SPENDBURN])
    clock[0] += 100
    b.poll_if_changed()
    b.poll_if_changed()
    assert len(_sburns(sent)) == 1                  # unchanged: held
    clock[0] += 20
    b.poll_if_changed()
    assert len(_sburns(sent)) == 1                  # inside the heartbeat
    clock[0] += 60
    b.poll_if_changed()
    assert len(_sburns(sent)) == 2                  # heartbeat
    f.line = protocol.spend_burn("claude", q, [1] * 19 + [9])
    clock[0] += 16
    b.poll_if_changed()
    assert len(_sburns(sent)) == 3                  # changed, gap over
    _hello(b, "1.4.0", caps=[protocol.BOARD_CAP_PLAN,
                             protocol.BOARD_CAP_SPENDBURN])
    clock[0] += 100
    b.poll_if_changed()
    assert len(_sburns(sent)) == 4                  # new connection
    f.line = None                                   # spend page gone
    clock[0] += 100
    b.poll_if_changed()
    assert len(_sburns(sent)) == 4


def test_a_failing_sburn_is_said_once_by_type(capsys):
    sent, clock = [], [1000.0]
    f = _SFetch(RuntimeError("secret path /x/y"))
    b = _timed(f, sent, clock)
    _hello(b, "1.4.0", caps=[protocol.BOARD_CAP_PLAN,
                             protocol.BOARD_CAP_SPENDBURN])
    for _ in range(3):
        clock[0] += 100
        b.poll_if_changed()
    err = capsys.readouterr().err
    assert err.count("spend burn not sent: RuntimeError") == 1
    assert "secret" not in err and _sburns(sent) == []


def test_the_bus_builds_sburn_only_for_a_spend_page():
    from pc import spend_count
    counter = spend_count.SpendCounter(persist=False)
    # The config's page, the one the count feeds (IngestionBus.spend_burn).
    page = spend()
    page.src = base.SPEND_SRC_ID
    held = [page]
    bus = ingest.IngestionBus(providers=[_source(held)], now=lambda: NOW,
                              account=_Watch("enterprise"), counter=counter)
    bus.poll()
    m = bus.fetch().spend_burn()
    assert m["t"] == "sburn" and m["q"] == protocol.burn_quarter(NOW)
    assert m["b"] == counter.buckets(NOW) and len(m["b"]) == 20
    # A gateway's page, from the status line: a spend page the count does
    # not feed, so no line.
    held[:] = [spend()]
    bus.poll()
    assert bus.fetch().spend_burn() is None
    held[:] = [windows()]
    bus.poll()
    assert bus.fetch().spend_burn() is None


def test_widest_usage_line_with_spend():
    """Every declared source id, spend frames on both pages, a seven-digit
    countdown (a monthly reset can be 31 days = 2,678,400 s)."""
    from tests.pc.test_protocol import _declared_source_ids

    def widest(provider, src):
        f = spend(provider=provider, pct=102.33333333333333,
                  resets_in=9_999_999)
        f.src = src
        f.observed_at = f.active_at = NOW - 2147483647
        f.state = "running"
        f.stale = False
        f.session_burn_pph = 999.93333
        for k in ("n_run", "n_wait", "n_stuck", "n_idle", "n_agents"):
            setattr(f, k, 99)
        return f

    widths = {}
    for src in _declared_source_ids():
        assert base.SPEND_SRC_ID in _declared_source_ids()
        raw, why = protocol.encode_checked(protocol.frame_to_usage(
            widest("claude", src), NOW, secondary=widest("codex", src)))
        assert why is None and len(raw) <= protocol.MAX_LINE_BYTES, src
        widths[src] = len(raw)
    top = max(widths, key=widths.get)
    assert (top, widths[top]) == MEASURED_WIDEST_SPEND, widths
    # And the widest plan, flags and all.
    raw, why = protocol.encode_checked(protocol.plan(
        "claude" * 10, base.PLAN_SPEND, "monthly", 999_999_999.99,
        999_999_999.99, "enterprise", est=True, hint=True))
    assert why is None and len(raw) < 200


# Measured 2026-11-04: narrower than the windows line (454, in
# test_protocol.py) because a spend page sends no weekly window.
MEASURED_WIDEST_SPEND = ("weekly_anchor", 410)


def test_widest_sburn_line():
    q = protocol.burn_quarter(NOW)
    raw, why = protocol.encode_checked(protocol.spend_burn(
        "claude", q, [protocol.SBURN_MAX_CENTS] * 20))
    assert why is None and len(raw) <= protocol.MAX_LINE_BYTES
    assert len(raw) == 234, len(raw)


# ------------------------------------------------------------- the log


def test_the_daemon_logs_plan_and_sburn_by_change_not_by_send():
    """/code-review max of 2026-11-04, U5: on a working spend desk the plan
    went out with every cent and the sburn line every 15 s, and neither was
    in _REPEATED, so each send was a line in bridge.log -- with the board's
    "[plan]" echo of each plan beside it, some 1000 lines an hour against a
    Max desk's 370. The plan is compared on what it says, not the dollars,
    which move by construction; sburn on the page and quarter-hour, as the
    token burn line is."""
    import ast
    import pathlib
    from pc import logbook
    root = pathlib.Path(__file__).resolve().parents[2]
    src = ast.unparse(ast.parse(
        (root / "claude_usage_bridge.py").read_text(encoding="utf-8")))
    assert "'plan': plan_frames" in src and "'sburn': sburn_frames" in src
    assert "plan_frames.due()" in src and "sburn_frames.due()" in src
    assert ("logbook.RepeatsBy('p', PLAN_LOGGED_FIELDS, 'plan lines')"
            in src)
    assert "logbook.RepeatsBy('p', ('q',), 'spend burn lines')" in src

    import claude_usage_bridge as cub
    r = logbook.RepeatsBy("p", cub.PLAN_LOGGED_FIELDS, "plan lines")
    logged = 0
    for cents in range(31412, 31412 + 300):
        m = protocol.plan("claude", base.PLAN_SPEND, "monthly",
                          cents / 100, 500.0, est=True)
        logged += r.worth_logging(m)
    assert logged == 1                       # three hundred cents, one line
    assert r.worth_logging(protocol.plan("claude", base.PLAN_WINDOWS))
    assert "usd" not in cub.PLAN_LOGGED_FIELDS


def test_the_boards_plan_echo_is_collapsed():
    """The board prints "[plan] <page> <kind>" for every plan it takes, and
    bridge.log keeps only the tag (console_line), so each restated plan was
    an identical line. Logged at most once per ten minutes, with a count of
    what was held back; other tags are untouched."""
    from pc import logbook
    t = [0.0]
    e = logbook.ConsoleEcho(now=lambda: t[0])
    out = []
    for _ in range(100):
        out += e.feed(b"[plan] claude spend monthly\n[usage] x 5\n")
        t[0] += 5
    assert out.count("[plan]") == 1
    assert out.count("[usage]") == 100
    t[0] += 600
    out = e.feed(b"[plan] claude windows\n")
    assert out == ["[plan] (99 more not logged)"]
    assert e.feed(b"[plan] claude windows\n") == []


def test_a_benched_provider_is_logged_by_type_only(capsys):
    """The message of an exception out of a provider can carry what it was
    reading -- "int too large to convert to float" from a damaged status
    line, a path, a character of Claude Code's config -- and the bench line
    put it in bridge.log. The type is enough to say what broke."""
    class Broken:
        def poll(self, now):
            raise ValueError("secret /Users/someone/.claude.json 31412")

    bus = ingest.IngestionBus(providers=[Broken()], now=lambda: NOW,
                              verdict=None)
    bus.poll()
    err = capsys.readouterr().err
    assert "Broken failed and will be skipped (ValueError)" in err
    assert "secret" not in err and "31412" not in err


def test_a_share_over_the_boards_range_is_clamped_not_refused():
    """/code-review max of 2026-11-04, U3: $300 spent against a cap cut to
    $20 is 1500%, and proto.c refuses a percentage over PCT_MAX (1000) and
    keeps its default, so the board drew 0% and no "used up". A spend share
    goes out at most PCT_MAX: far over the cap still reads as over it."""
    assert protocol.PCT_MAX == 1000.0
    msg = protocol.frame_to_usage(spend(pct=1500.0), NOW)
    assert msg["session_pct"] == protocol.PCT_MAX
    msg = protocol.frame_to_usage(windows(provider="codex"), NOW,
                                  secondary=spend(pct=1e300))
    assert msg["p2_session_pct"] == protocol.PCT_MAX
    # Under it, untouched.
    assert protocol.frame_to_usage(spend(pct=102.3), NOW)["session_pct"] \
        == 102.3
