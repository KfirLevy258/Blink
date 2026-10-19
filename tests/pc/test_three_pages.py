"""A third page on the board: Claude, Codex and OpenCode each get one.

The usage line carries pages 1 and 2 and cannot carry a third -- it is at 484
of the 512 bytes proto.c accepts, and an over-long line is dropped whole --
so page 3 travels as its own `page3` message. Three things have to hold, and
each has a section here:

  - the message fits, at its worst, through encode_checked;
  - pages 2 and 3 are in a FIXED order when all three are present, and a
    two-provider desk is sent exactly what it always was;
  - only firmware at or past protocol.FW_SHOWS_PAGE3 is sent any of it, and
    it is told when the third page goes away.

Every frame below is built here, by code. Nothing is captured from a real
machine.
"""
import json

from pc import ingest, normalizer, protocol
from pc.bridge import Bridge
from pc.providers import base

NOW = 1_787_700_000.0
NEW_FW = ".".join(str(n) for n in protocol.FW_SHOWS_PAGE3)


class Fixed(base.ProviderParser):
    def __init__(self, frame):
        self._frame = frame

    def get_provider_id(self):
        return self._frame.provider

    def poll(self, now_epoch):
        return [self._frame]


def used(provider, used_at, session=30.0, weekly=10.0, state="", **counts):
    f = base.NormalizedUsageFrame(
        provider=provider, src="zai" if provider == "opencode" else "cli",
        observed_at=used_at, active_at=used_at,
        session_pct=session, weekly_pct=weekly,
        session_resets_at=NOW + 3600, weekly_resets_at=NOW + 5 * 86400)
    f.state = state
    for k, v in counts.items():
        setattr(f, k, v)
    return f


def bus_of(*frames, preferred="claude"):
    return ingest.IngestionBus(providers=[Fixed(f) for f in frames],
                               preferred_provider=preferred,
                               now=lambda: NOW)


# --- the message -------------------------------------------------------------


def test_page3_is_its_own_type_and_fits_proto_c_type_buffer():
    msg = protocol.page3("opencode", 41.5, 7200, 12.0, 400000, False, 30)
    assert msg["t"] == "page3" and msg["t"] == protocol.PAGE3_TYPE
    # proto.c reads `t` into char[16]; a longer name would be truncated and
    # match nothing.
    assert len(msg["t"]) < 16
    assert msg == {"t": "page3", "v": protocol.VERSION, "provider": "opencode",
                   "session_pct": 41.5, "session_resets_in_s": 7200,
                   "weekly_pct": 12.0, "weekly_resets_in_s": 400000,
                   "stale": False, "age_s": 30}


def test_the_worst_page3_line_is_far_under_the_budget():
    """Every field at the widest value the daemon can put in it: the
    provider name at its cap, percentages at proto.c's PCT_MAX, countdowns at
    COUNTDOWN_MAX_S, an age at the firmware's SECS_MAX, and stale true."""
    msg = protocol.page3("x" * 200, 1000.0, protocol.COUNTDOWN_MAX_S,
                         1000.0, protocol.COUNTDOWN_MAX_S, True,
                         2_147_483_647)
    raw, why = protocol.encode_checked(msg)
    assert why is None
    assert len(raw) <= 200, len(raw)
    assert len(msg["provider"]) == protocol.MODEL_MAX_CHARS


def test_the_worst_page3_built_from_a_frame_fits_too():
    f = used("opencode", NOW - 10**9, session=999.94, weekly=999.94)
    f.session_resets_at = NOW + 10**9          # clamped by secs_until
    f.weekly_resets_at = NOW + 10**9
    f.stale = True
    raw, why = protocol.encode_checked(protocol.frame_to_page3(f, NOW))
    assert why is None and len(raw) <= 200


def test_unknowns_stay_unknown_and_the_age_is_omitted():
    msg = protocol.page3("opencode")
    assert msg["session_pct"] == -1.0 and msg["weekly_pct"] == -1.0
    assert msg["session_resets_in_s"] == -1
    assert "age_s" not in msg


def test_the_clear_is_a_present_empty_provider_and_nothing_else():
    assert protocol.page3("") == {"t": "page3", "v": protocol.VERSION,
                                  "provider": ""}
    assert protocol.frame_to_page3(None, NOW) == protocol.page3("")


def test_page3_timers_are_volatile_so_the_fast_tick_ignores_them():
    a = protocol.page3("opencode", 40.0, 100, 10.0, 1000, False, 5)
    b = protocol.page3("opencode", 40.0, 99, 10.0, 999, False, 6)
    assert protocol.meaningful_usage(a) == protocol.meaningful_usage(b)
    c = protocol.page3("opencode", 41.0, 99, 10.0, 999, False, 6)
    assert protocol.meaningful_usage(a) != protocol.meaningful_usage(c)


def test_shows_page3_versions():
    assert protocol.shows_page3(NEW_FW)
    assert protocol.shows_page3("1.4.1") and protocol.shows_page3("2.0.0")
    assert not protocol.shows_page3("1.3.3")
    assert not protocol.shows_page3(None)
    assert not protocol.shows_page3("garbage")


def test_the_gate_is_the_version_this_tree_builds():
    """The constant and the bump must be the same number, or the feature
    ships to boards that cannot draw it -- or to none."""
    import pathlib
    vh = (pathlib.Path(__file__).resolve().parents[2]
          / "firmware" / "src" / "version.h").read_text()
    line = [l for l in vh.splitlines()
            if l.startswith("#define BLINK_FW_VERSION")][0]
    assert line.split('"')[1] == NEW_FW


# --- the order ---------------------------------------------------------------


def test_three_providers_get_pages_in_a_fixed_order_not_by_recency():
    """OpenCode used a moment ago, Codex an hour ago. Recency puts OpenCode
    on page 2; the three-page layout keeps Codex there, because the order of
    pages must not move under a reader tapping through them."""
    bus = bus_of(used("claude", NOW - 10), used("codex", NOW - 3600),
                 used("opencode", NOW - 5, session=55.0))
    legacy = bus.poll()
    assert legacy["p2"] == "opencode"            # recency, as before
    usage3, page3 = bus.paged()
    assert (usage3["provider"], usage3["p2"]) == ("claude", "codex")
    assert page3["provider"] == "opencode" and page3["session_pct"] == 55.0


def test_the_order_holds_whichever_was_used_last():
    seen = set()
    for codex_at, oc_at in ((NOW - 5, NOW - 3600), (NOW - 3600, NOW - 5)):
        bus = bus_of(used("claude", NOW - 10), used("codex", codex_at),
                     used("opencode", oc_at))
        bus.poll()
        usage3, page3 = bus.paged()
        seen.add((usage3["p2"], page3["provider"]))
    assert seen == {("codex", "opencode")}


def test_the_primary_stays_page_one_and_the_rest_keep_the_order():
    bus = bus_of(used("claude", NOW - 10), used("codex", NOW - 3600),
                 used("opencode", NOW - 5), preferred="opencode")
    bus.poll()
    usage3, page3 = bus.paged()
    assert (usage3["provider"], usage3["p2"], page3["provider"]) == (
        "opencode", "claude", "codex")


def test_two_providers_have_no_third_page_and_the_line_is_unchanged():
    bus = bus_of(used("claude", NOW - 10), used("opencode", NOW - 5))
    msg = bus.poll()
    assert bus.paged() is None
    assert msg["p2"] == "opencode"


def test_the_three_page_line_folds_every_provider_once():
    """The light, the counts and active_age_s cover the same three providers
    on both layouts; only p2 differs. A page-3 provider counted twice would
    show one waiting session as two."""
    bus = bus_of(used("claude", NOW - 10, state=base.STATE_RUNNING, n_run=1),
                 used("codex", NOW - 3600, state=base.STATE_RUNNING, n_run=2),
                 used("opencode", NOW - 5, state=base.STATE_WAITING,
                      n_wait=1, label="glm"))
    legacy = bus.poll()
    usage3, _ = bus.paged()
    for k in ("state", "n_sess", "n_run", "n_wait", "n_stuck", "n_agents",
              "active_age_s"):
        assert usage3.get(k) == legacy.get(k), k
    assert (usage3["n_run"], usage3["n_wait"]) == (3, 1)
    assert usage3["state"] == base.STATE_WAITING
    assert bus.session_pair() == ("glm", 1)


def test_the_three_page_usage_line_still_fits():
    """Same width as the line it replaces: it carries the same keys with a
    different provider in p2."""
    bus = bus_of(used("claude", NOW - 10), used("codex", NOW - 3600),
                 used("opencode", NOW - 5))
    bus.poll()
    for m in bus.paged():
        assert protocol.encode_checked(m)[1] is None


def test_fixed_pages_puts_unnamed_providers_last_in_rank_order():
    frames = [used("scripted", NOW - 1), used("claude", NOW - 10),
              used("opencode", NOW - 50), used("codex", NOW - 70)]
    ranked = normalizer.rank(frames, preferred="claude")
    second, third, rest = normalizer.fixed_pages(ranked)
    assert (second.provider, third.provider) == ("codex", "opencode")
    assert [m.provider for m in rest] == ["scripted"]
    assert normalizer.fixed_pages(ranked[:2]) is None


def test_a_desk_with_no_numbers_has_no_third_page():
    bus = ingest.IngestionBus(providers=[], now=lambda: NOW)
    assert bus.poll() is None
    assert bus.paged() is None


# --- the bridge --------------------------------------------------------------


class Desk(base.ProviderParser):
    """A bus whose providers can be swapped between polls."""

    def __init__(self, *frames):
        self.frames = list(frames)
        self.bus = ingest.IngestionBus(providers=[self],
                                       preferred_provider="claude",
                                       now=lambda: NOW)

    def get_provider_id(self):
        return "desk"

    def poll(self, now_epoch):
        return list(self.frames)


def bridge_for(desk, fw):
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=desk.bus.fetch(),
               wall=lambda: (1752444000, 180),
               fetch_manifest=lambda: None,
               fetch_signed_manifest=lambda: None)
    if fw is not None:
        b.on_message({"t": "hello", "v": 2, "fw": fw, "board": "cyd"})
    return b, sent


def three():
    return Desk(used("claude", NOW - 10), used("codex", NOW - 3600),
                used("opencode", NOW - 5, session=55.0))


def of(sent, t):
    return [m for m in sent if m["t"] == t]


def test_a_new_board_gets_page3_after_the_fixed_order_usage_line():
    b, sent = bridge_for(three(), NEW_FW)
    kinds = [m["t"] for m in sent]
    assert "page3" in kinds
    assert kinds.index("usage") < kinds.index("page3")
    assert of(sent, "usage")[-1]["p2"] == "codex"
    assert of(sent, "page3")[-1]["provider"] == "opencode"


def test_an_old_board_gets_nothing_new_and_the_recency_line():
    for fw in ("1.3.3", None):
        b, sent = bridge_for(three(), fw)
        b.poll_once()
        b.poll_if_changed()
        assert not of(sent, "page3"), fw
        assert of(sent, "usage")[-1]["p2"] == "opencode", fw


def test_an_old_board_gets_byte_identical_lines_to_a_bus_without_page3():
    desk = three()
    b, sent = bridge_for(desk, "1.3.3")
    sent.clear()
    b.poll_once()
    expected = desk.bus.poll()
    assert [protocol.encode(m) for m in of(sent, "usage")] == [
        protocol.encode(expected)]


def test_the_third_page_is_cleared_when_its_provider_goes_away():
    desk = three()
    b, sent = bridge_for(desk, NEW_FW)
    sent.clear()
    desk.frames = desk.frames[:2]               # OpenCode gone
    b.poll_if_changed()
    assert of(sent, "page3") == [protocol.page3("")]
    # Said once, not on every heartbeat after.
    sent.clear()
    b.poll_once()
    b.poll_if_changed()
    assert not of(sent, "page3")


def test_a_page3_change_alone_reaches_the_board_on_the_fast_tick():
    desk = three()
    b, sent = bridge_for(desk, NEW_FW)
    sent.clear()
    desk.frames[2] = used("opencode", NOW - 5, session=70.0)
    b.poll_if_changed()
    assert not of(sent, "usage")                 # line 1/2 did not move
    assert of(sent, "page3")[-1]["session_pct"] == 70.0


def test_an_unchanged_page3_is_not_repeated_on_the_fast_tick():
    b, sent = bridge_for(three(), NEW_FW)
    sent.clear()
    b.poll_if_changed()
    assert not sent


def test_the_heartbeat_restates_a_live_third_page():
    b, sent = bridge_for(three(), NEW_FW)
    sent.clear()
    b.poll_once()
    assert len(of(sent, "page3")) == 1


def test_a_two_provider_desk_on_new_firmware_is_told_there_is_no_page3_once():
    desk = Desk(used("claude", NOW - 10), used("codex", NOW - 60))
    b, sent = bridge_for(desk, NEW_FW)
    assert of(sent, "page3") == [protocol.page3("")]
    sent.clear()
    b.poll_once()
    assert not of(sent, "page3")
    # And a reconnect says it again: the board may hold a page from before.
    b.greet()
    assert of(sent, "page3") == [protocol.page3("")]


def test_a_fetch_without_the_accessor_sends_no_page3():
    b = Bridge(write_msg=(sent := []).append,
               fetch_usage=lambda: protocol.usage(10.0, None, 20.0, None, []),
               wall=lambda: (1752444000, 180), fetch_manifest=lambda: None,
               fetch_signed_manifest=lambda: None)
    b.on_message({"t": "hello", "v": 2, "fw": NEW_FW, "board": "cyd"})
    b.poll_once()
    assert not of(sent, "page3")


def test_every_message_the_bridge_sends_here_is_valid_json_under_budget():
    b, sent = bridge_for(three(), NEW_FW)
    for m in sent:
        raw, why = protocol.encode_checked(m)
        assert why is None
        json.loads(raw)
