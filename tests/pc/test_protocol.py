import json
import unittest

from pc import protocol
from pc.providers import base


class TestProtocol(unittest.TestCase):
    def test_encode_appends_newline_and_type_version(self):
        line = protocol.encode({"t": "welcome", "v": 1, "app": "x"})
        self.assertTrue(line.endswith(b"\n"))
        self.assertEqual(protocol.decode(line.decode().strip()),
                         {"t": "welcome", "v": 1, "app": "x"})

    def test_decode_ignores_non_brace_lines(self):
        self.assertIsNone(protocol.decode("*** Booting Zephyr ***"))
        self.assertIsNone(protocol.decode("[usage] hello"))
        self.assertIsNone(protocol.decode(""))

    def test_decode_ignores_bad_json(self):
        self.assertIsNone(protocol.decode("{not json"))

    def test_linereader_assembles_and_filters(self):
        r = protocol.LineReader()
        msgs = []
        for chunk in [b'{"t":"hel', b'lo","v":1}\nlog text\n{"t":"pi', b'ng","v":1}\n']:
            msgs.extend(r.feed(chunk))
        self.assertEqual(msgs, [{"t": "hello", "v": 1}, {"t": "ping", "v": 1}])

    def test_builders(self):
        self.assertEqual(protocol.welcome("app", "0.2.0"),
                         {"t": "welcome", "v": 2, "app": "app", "app_ver": "0.2.0"})
        u = protocol.usage(61.0, "R1", 26.0, "R2", [{"name": "sonnet", "weekly_pct": 2.0}])
        self.assertEqual(u["t"], "usage")
        self.assertEqual(u["session_pct"], 61.0)
        # The array itself no longer goes on the wire; the flattened scalar
        # keys are what the board reads, and they are what is asserted here.
        self.assertNotIn("models", u)
        self.assertEqual(u["sonnet_pct"], 2.0)
        self.assertEqual(protocol.status("rate_limited", "x"),
                         {"t": "status", "v": 2, "state": "rate_limited", "detail": "x"})

    def test_usage_flattens_known_models(self):
        """The board's JSON scanner reads scalar keys only, so known models
        are flattened into scalars and the array is dropped -- it was thirteen
        bytes of a budget the second provider's fields made tight, and nothing
        ever read it."""
        u = protocol.usage(61.0, "R1", 26.0, "R2",
                           [{"name": "fable", "weekly_pct": 12.5},
                            {"name": "sonnet", "weekly_pct": 2.0},
                            {"name": "opus", "weekly_pct": 40.5},
                            {"name": "haiku", "weekly_pct": 1.0}])
        self.assertEqual(u["fable_pct"], 12.5)
        self.assertEqual(u["sonnet_pct"], 2.0)
        self.assertEqual(u["opus_pct"], 40.5)
        self.assertNotIn("haiku_pct", u)  # unknown models are simply dropped
        self.assertNotIn("models", u)

    def test_usage_flatten_handles_empty_and_none_models(self):
        self.assertNotIn("sonnet_pct", protocol.usage(1.0, "R", 2.0, "R", []))
        self.assertNotIn("sonnet_pct", protocol.usage(1.0, "R", 2.0, "R", None))

    def test_usage_stale_defaults_false_and_is_settable(self):
        """stale is a declared field of the message, not a key a caller bolts
        on after the fact -- the firmware parses it like any other."""
        self.assertFalse(protocol.usage(1.0, "R", 2.0, "R", [])["stale"])
        self.assertTrue(protocol.usage(1.0, "R", 2.0, "R", [], stale=True)["stale"])

    def test_time_msg_fields(self):
        m = protocol.time_msg(1752444000, -300)
        self.assertEqual(m["t"], "time")
        self.assertEqual(m["epoch"], 1752444000)
        self.assertEqual(m["utc_offset_min"], -300)
        self.assertEqual(m["v"], protocol.VERSION)

    def test_version_is_2(self):
        """time_msg is new in v2; both sides bump together."""
        self.assertEqual(protocol.VERSION, 2)




class WireBudget(unittest.TestCase):
    """The board drops an over-long line whole rather than truncating it
    (proto.c:367-371), so the daemon must never write one."""

    def test_a_normal_usage_message_fits(self):
        raw, why = protocol.encode_checked(
            protocol.usage(61.0, 1_787_203_200, 26.0, 1_787_644_800, [],
                           p2="codex", p2_session_pct=42.0))
        self.assertIsNone(why)
        self.assertLessEqual(len(raw), protocol.MAX_LINE_BYTES)

    def test_an_over_long_line_is_refused_with_a_reason(self):
        # Built by hand rather than through usage(): every field usage() can
        # emit is now either bounded or omitted when empty, which is the point
        # of the budget. encode_checked still has to refuse anything that
        # somehow gets past that.
        fat = protocol.usage(1.0, "R", 2.0, "R", [])
        fat["pad"] = "x" * 600
        raw, why = protocol.encode_checked(fat)
        self.assertIsNone(raw)
        self.assertIn("line limit", why)


class AdditiveFields(unittest.TestCase):
    """The multi-provider fields ride on v2 rather than forcing a v3.

    A version bump would stop every deployed board being offered updates
    (pc/version.py), over the same link the update travels on.
    """

    def test_the_protocol_version_did_not_move(self):
        self.assertEqual(protocol.VERSION, 2)

    def test_provider_and_src_are_always_present(self):
        u = protocol.usage(1.0, "R", 2.0, "R", [])
        self.assertEqual(u["provider"], "claude")
        self.assertEqual(u["src"], "cli")

    def test_unknown_optional_fields_are_omitted_not_sentinelled(self):
        u = protocol.usage(1.0, "R", 2.0, "R", [])
        for k in ("state", "p2"):
            self.assertNotIn(k, u)

    def test_a_second_provider_names_itself(self):
        u = protocol.usage(1.0, "R", 2.0, "R", [], provider="codex",
                           src="desktop", state="running")
        self.assertEqual(u["provider"], "codex")
        self.assertEqual(u["src"], "desktop")
        self.assertEqual(u["state"], "running")

    def test_frame_to_usage_computes_both_countdowns(self):
        from pc.providers import base
        f = base.NormalizedUsageFrame(
            provider="claude", src="cli", observed_at=1_787_200_000,
            session_pct=10.0, session_resets_at=1_787_203_200,
            weekly_pct=20.0, weekly_resets_at=1_787_644_800)
        u = protocol.frame_to_usage(f, 1_787_200_000)
        self.assertEqual(u["session_resets_in_s"], 3200)
        self.assertEqual(u["weekly_resets_in_s"], 444800)

    def test_a_past_reset_stays_unknown_through_the_frame(self):
        from pc.providers import base
        f = base.NormalizedUsageFrame(
            provider="claude", src="cli", observed_at=1_787_200_000,
            session_pct=10.0, session_resets_at=1_787_100_000)
        u = protocol.frame_to_usage(f, 1_787_200_000)
        self.assertEqual(u["session_resets_in_s"], -1)


def test_the_second_provider_carries_its_own_staleness():
    """A live page must not be labelled old because the other one went quiet.

    `stale` describes the FIRST provider. With two providers on two pages that
    is a statement about one of them, and the board was showing it over
    whichever page happened to be in front -- so a machine running Claude Code
    all day with Codex touched once that morning announced "Reading is old"
    over numbers that were updating (user-reported 2026-08-28).
    """
    m = protocol.usage(0.0, None, 0.0, None, [], stale=True,
                       provider="codex", p2="claude", p2_session_pct=66.0,
                       p2_stale=False)
    assert m["stale"] is True
    assert m["p2_stale"] is False


def test_the_second_providers_staleness_is_independent():
    """...and it travels in the other direction too."""
    m = protocol.usage(0.0, None, 0.0, None, [], stale=False,
                       provider="claude", p2="codex", p2_stale=True)
    assert m["stale"] is False
    assert m["p2_stale"] is True


def test_no_second_provider_means_no_second_staleness():
    """p2_stale rides with the rest of p2 rather than standing alone.

    A board that receives p2_stale without p2 would have an age for a page it
    is not being told exists.
    """
    m = protocol.usage(0.0, None, 0.0, None, [], stale=True)
    assert "p2_stale" not in m
    assert "p2" not in m


# --- the reading's age on the wire -----------------------------------------
#
# The distinction every test here is about: how old the FIGURE is, not how
# long ago the daemon spoke. The board cannot work the first one out -- it
# receives a message every 60 s whether or not the number in it moved -- so
# the daemon has to say, and a Claude Desktop percentage that stopped being
# refreshed four hours ago was arriving every minute looking perfectly live.


def test_the_age_is_the_readings_not_the_messages():
    from pc.providers import base
    f = base.NormalizedUsageFrame(
        provider="claude", src="desktop", observed_at=1_787_200_000,
        session_pct=10.0, weekly_pct=20.0)
    # Four hours after the reading was taken, and this message is new.
    msg = protocol.frame_to_usage(f, 1_787_200_000 + 14400)
    assert msg["age_s"] == 14400


def test_each_provider_carries_its_own_age():
    """Per page, like `stale`, and for the same reason: two files, read at
    different times, one page in front of you."""
    from pc.providers import base
    now = 1_787_200_000
    f = base.NormalizedUsageFrame(provider="claude", src="desktop",
                                  observed_at=now - 3600, session_pct=1.0)
    s = base.NormalizedUsageFrame(provider="codex", src="cli",
                                  observed_at=now - 30, session_pct=2.0)
    msg = protocol.frame_to_usage(f, now, secondary=s)
    assert msg["age_s"] == 3600
    assert msg["p2_age_s"] == 30


def test_no_second_provider_means_no_second_age():
    """p2_age_s rides with the rest of p2, exactly as p2_stale does: an age
    for a page the board is not being told exists is worse than no age."""
    from pc.providers import base
    f = base.NormalizedUsageFrame(provider="claude", src="cli",
                                  observed_at=1_787_200_000, session_pct=1.0)
    msg = protocol.frame_to_usage(f, 1_787_200_000 + 60)
    assert "p2_age_s" not in msg


def test_a_future_mtime_reads_as_zero_not_as_negative():
    """observed_at is a file mtime from a clock we do not own, and a stamp a
    few seconds ahead is a real thing. A negative would reach fmt_age() on the
    board and print "never" over a reading we are holding in our hand."""
    from pc.providers import base
    f = base.NormalizedUsageFrame(provider="claude", src="desktop",
                                  observed_at=1_787_200_005, session_pct=1.0)
    msg = protocol.frame_to_usage(f, 1_787_200_000)
    assert msg["age_s"] == 0


def test_an_unknown_age_is_omitted_rather_than_sent_as_minus_one():
    """-1 is already the firmware's default, so the key would spend
    MAX_LINE_BYTES to say what silence says."""
    msg = protocol.usage(40, None, 20, None, [], age_s=-1)
    assert "age_s" not in msg


def test_the_age_fields_fit_the_line_budget():
    """The check that matters: proto.c DROPS an over-long line rather than
    truncating it, so a field that overflows stops the panel updating while
    the daemon reports success."""
    msg = protocol.usage(
        44.0, 1788049800, 41.0, 1788328800, [],
        session_resets_in_s=3600, weekly_resets_in_s=90000,
        provider="claude", src="cli", state="running",
        p2="codex", p2_session_pct=12.0, p2_weekly_pct=9.0,
        p2_session_resets_in_s=1200, p2_weekly_resets_in_s=50000,
        burn_pph=3.2, age_s=999999, p2_age_s=999999)
    raw, err = protocol.encode_checked(msg)
    assert err is None
    assert len(raw) <= protocol.MAX_LINE_BYTES


# --- active_age_s: how long since we heard a voice -------------------------
#
# `age_s` is how old the number on the dial is. `active_age_s` is how long
# since ANY tool on this desk wrote anything at all. The board dozes on the
# second and captions on the first, because a remembered reading can be
# twelve hours old at the same instant the file it came from was rewritten
# five seconds ago -- and dozing on that age puts the panel to sleep in
# front of its owner (field review 2026-09-02).


def test_an_unknown_active_age_is_omitted_like_every_other_unknown():
    assert "active_age_s" not in protocol.usage(40, None, 20, None, [],
                                                active_age_s=-1)


def test_a_known_active_age_is_sent_even_when_it_equals_the_reading_age():
    """Twenty bytes to keep "the desk is as quiet as the dial" distinct from
    "this daemon predates the field", which is the one thing the firmware's
    fallback cannot recover for itself."""
    msg = protocol.usage(40, None, 20, None, [], age_s=900, active_age_s=900)
    assert msg["active_age_s"] == 900


def test_the_active_age_is_the_freshest_page_not_the_shown_one():
    """One field for the whole board, because dozing is a whole-board
    decision: a Codex rollout written a minute ago is evidence somebody is
    at this machine even on a day when the Claude page holds the dial."""
    now = 1_787_700_000.0
    claude = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=now - 12 * 3600,
        active_at=now - 12 * 3600, session_pct=27.0)
    codex = base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=now - 60,
        active_at=now - 60, session_pct=8.0)
    msg = protocol.frame_to_usage(claude, now, secondary=codex)
    assert msg["age_s"] == 12 * 3600
    assert msg["active_age_s"] == 60


def test_the_shown_page_can_be_the_fresh_one_too():
    """The mirror, so the test above is measuring a minimum and not just
    reading the second argument."""
    now = 1_787_700_000.0
    claude = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=now - 60,
        active_at=now - 60, session_pct=27.0)
    codex = base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=now - 12 * 3600,
        active_at=now - 12 * 3600, session_pct=8.0)
    msg = protocol.frame_to_usage(claude, now, secondary=codex)
    assert msg["active_age_s"] == 60


def test_a_remembered_reading_does_not_age_the_desk():
    """The whole point of the field, at the seam that produces it: an old
    dial and a live machine in one frame."""
    now = 1_787_700_000.0
    f = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=now - 12 * 3600,
        active_at=now - 5, session_pct=27.0, weekly_pct=26.0)
    msg = protocol.frame_to_usage(f, now)
    assert msg["age_s"] == 12 * 3600
    assert msg["active_age_s"] == 5


def test_a_frame_with_no_epoch_at_all_says_so():
    """-1, the same "we cannot say" age_s already uses, so the firmware's
    one unknown rule covers both."""
    f = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=None, session_pct=27.0)
    msg = protocol.frame_to_usage(f, 1_787_700_000.0)
    assert "active_age_s" not in msg
    assert "age_s" not in msg


def test_the_widest_line_the_daemon_can_build_still_fits():
    """The measurement that decides whether this field was affordable.

    Built through frame_to_usage, which is the only caller of usage() and
    therefore the only thing that can put a line on the wire: both providers
    populated, every count at 99, both reset stamps and both countdowns at
    the far end of their own windows (five hours and seven days), a burn
    rate beside them although the normalizer never sends both, overage on
    all four dials, the longest source id that reaches this field, and all
    three ages at INT32_MAX -- the bound proto.c's SECS_MAX permits, and
    reachable from a file stamped at the epoch.

    509 of 512 bytes, measured. The board drops an over-long line whole with
    no error, so this test is the guard: the next field, a longer provider or
    source id, or a fourth age fails here rather than on a desk.

    Three things here were NOT in the first version of this test, and each of
    them on its own put the line over the cap once active_age_s arrived:

      - `src="cli-state"`, which is codex_cli.STATE_SRC_ID and two characters
        wider than "desktop". It is a real source, not a hypothetical one.
      - `stale=False`, because `false` is a byte wider than `true`, twice.
      - fractional reset stamps and a reset a year out. A resets_at arrives
        from provider JSON and is free to carry a fraction, and secs_until had
        no upper bound, so these wrote 521 and 517 of 512 respectively --
        refused by encode_checked, and a panel that quietly stops updating.
        protocol._whole_epoch and protocol.COUNTDOWN_MAX_S are what bring them
        back, and this test is what would notice if either were removed.
    """
    now = 1_787_700_000.0

    def widest(provider):
        return base.NormalizedUsageFrame(
            provider=provider, src="cli-state",   # codex_cli.STATE_SRC_ID
            observed_at=now - 2147483647, active_at=now - 2147483647,
            session_pct=102.33333333333333,
            # Fractional, and far past any real window: both are shapes a
            # provider's JSON can hand us, and both used to reach the wire
            # at full width.
            session_resets_at=now + 31536000.1234567,
            weekly_pct=102.66666666666667,
            weekly_resets_at=now + 31536000.1234567,
            state="running", stale=False, session_burn_pph=999.93333,
            n_run=99, n_wait=99, n_stuck=99, n_idle=99, n_agents=99,
            label="a-project-with-a-long-name")

    msg = protocol.frame_to_usage(widest("claude"), now,
                                  secondary=widest("codex"))
    raw, why = protocol.encode_checked(msg)
    assert why is None
    assert msg["active_age_s"] == 2147483647
    assert len(raw) <= protocol.MAX_LINE_BYTES, len(raw)


def test_an_absolute_reset_stamp_goes_out_as_whole_seconds():
    """A fraction on resets_at costs eight bytes and buys nothing.

    Providers hand us whatever their JSON held, and json.dumps writes every
    digit: 1787718000.1234567 is 18 bytes where 1787718000 is 10. Two of them
    took the widest line to 521 of 512. Nothing reads the fraction -- the
    board counts down from *_resets_in_s, and these stamps exist for
    readability -- so they are rounded at the one place the wire is defined.
    """
    msg = protocol.usage(50.0, 1787718000.1234567, 50.0, 1788300000.9,
                         [], session_resets_in_s=18000)
    assert msg["session_resets_at"] == 1787718000
    assert msg["weekly_resets_at"] == 1788300000
    assert isinstance(msg["session_resets_at"], int)


def test_a_reset_stamp_that_is_not_a_time_is_left_alone():
    """None and -1 are not times, and rounding would change what they say."""
    msg = protocol.usage(50.0, None, 50.0, -1, [])
    assert msg["session_resets_at"] is None
    assert msg["weekly_resets_at"] == -1


def test_a_countdown_beyond_any_real_window_is_clamped():
    """A year-long countdown is a misparse, and it is eight digits wide.

    The longest window the panel shows is the weekly one. Anything further
    out is a stamp that changed meaning -- milliseconds, say -- and rendering
    it as a confident "resets in 412 days" is the wrong-but-certain number
    this module exists to avoid. Four countdown fields ride every usage line,
    and unclamped they wrote 517 of 512.
    """
    now = 1_787_700_000.0
    assert protocol.secs_until(now + 31536000, now) == protocol.COUNTDOWN_MAX_S
    assert len(str(protocol.COUNTDOWN_MAX_S)) == 6
    # Well clear of the seven-day window it must never truncate.
    assert protocol.COUNTDOWN_MAX_S > 7 * 24 * 3600
    assert protocol.secs_until(now + 604800, now) == 604800


# --- burn_pph on the wire --------------------------------------------------


def test_burn_is_omitted_when_absent():
    """The MAX_LINE_BYTES rule: a key carrying no information spends budget a
    future field will need, and an absent key already means unknown on both
    sides. This is the common case -- every machine with Claude Code."""
    msg = protocol.usage(40, None, 20, None, [])
    assert "burn_pph" not in msg


def test_burn_is_sent_when_present():
    msg = protocol.usage(40, None, 20, None, [], burn_pph=14.23)
    assert msg["burn_pph"] == 14.2          # one decimal, rounded


def test_a_zero_or_negative_rate_is_not_sent():
    for bad in (0, -1.0):
        assert "burn_pph" not in protocol.usage(40, None, 20, None, [],
                                                burn_pph=bad)


def test_the_frame_carries_the_rate_onto_the_wire():
    f = base.NormalizedUsageFrame(
        provider="claude", src="desktop", observed_at=1_787_700_000.0,
        session_pct=40, weekly_pct=20, session_burn_pph=14.0)
    msg = protocol.frame_to_usage(f, 1_787_700_000.0)
    assert msg["burn_pph"] == 14.0
    # And the invariant the firmware leans on, restated on the wire: no
    # countdown alongside it.
    assert msg["session_resets_in_s"] == -1


def test_the_line_still_fits_with_everything_on_it():
    """burn_pph is additive, and additive only counts if the fully loaded
    line still fits -- the board drops an over-long line whole."""
    f = base.NormalizedUsageFrame(
        provider="claude", src="desktop", observed_at=1_787_700_000.0,
        session_pct=99.9, weekly_pct=99.9, state="running",
        n_run=9, n_wait=9, n_stuck=9, n_idle=9, n_agents=99,
        session_burn_pph=999.9)
    g = base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=1_787_700_000.0,
        session_pct=99.9, weekly_pct=99.9, stale=True,
        session_resets_at=1_787_999_999.0, weekly_resets_at=1_788_999_999.0)
    line = json.dumps(protocol.frame_to_usage(f, 1_787_700_000.0, secondary=g))
    assert len(line.encode()) < protocol.MAX_LINE_BYTES


if __name__ == "__main__":
    unittest.main()


class TestOneLightForBothProviders(unittest.TestCase):
    """The board has one activity pip for both pages. It shows the worse of
    the two providers' states and the sessions of both, because a Codex
    session waiting on the person is their turn just as much as a Claude
    one -- whichever page happens to be in front."""

    def frame(self, provider, state, **counts):
        from pc.providers import base
        return base.NormalizedUsageFrame(provider=provider, src="cli",
                                         observed_at=1.0, session_pct=10.0,
                                         state=state, **counts)

    def test_a_finished_codex_session_shows_on_a_claude_page(self):
        u = protocol.frame_to_usage(self.frame("claude", "running", n_run=2),
                                    100.0,
                                    self.frame("codex", "idle", n_idle=1))
        self.assertEqual(u["state"], "idle")
        self.assertEqual((u["n_sess"], u["n_run"]), (3, 2))

    def test_the_primary_alone_is_unchanged(self):
        u = protocol.frame_to_usage(self.frame("claude", "running", n_run=1),
                                    100.0)
        self.assertEqual(u["state"], "running")
        self.assertEqual(u["n_sess"], 1)

    def test_a_secondary_with_no_claim_changes_nothing(self):
        u = protocol.frame_to_usage(self.frame("claude", "waiting", n_wait=1),
                                    100.0, self.frame("codex", ""))
        self.assertEqual(u["state"], "waiting")
        self.assertEqual(u["n_sess"], 1)


def test_bye_is_a_plain_versioned_message():
    """Sent once by uninstall so a board does not doze over a computer that
    has no app any more (docs/sleep-mode-design.md)."""
    m = protocol.bye()
    assert m == {"t": "bye", "v": protocol.VERSION}


class OverageCapTest(unittest.TestCase):
    """A percentage above 100 must never reach a board that renders it as 0.

    proto.c parses these with num(..., -1, 100) and num() does not clamp: it
    returns without writing, leaving the caller's `double wp = 0`. So 102 shows
    as 0% -- empty ring at the exact moment the user went over. Seen on a
    customer's board 2026-08-31, flipping 100 -> 0 every minute as two sources
    took turns being newest (Desktop caps at 100, Claude Code reports 102).
    """

    def msg(self, **kw):
        m = {"t": "usage", "v": protocol.VERSION, "session_pct": 22.0,
             "weekly_pct": 102.0}
        m.update(kw)
        return m

    def test_old_firmware_is_held_at_100(self):
        out = protocol.cap_overage_for_fw(self.msg(), "1.2.3")
        self.assertEqual(out["weekly_pct"], 100.0)
        self.assertEqual(out["session_pct"], 22.0)

    def test_new_firmware_gets_the_true_number(self):
        out = protocol.cap_overage_for_fw(self.msg(), "1.2.5")
        self.assertEqual(out["weekly_pct"], 102.0)

    def test_the_release_that_only_bumped_the_version_still_caps(self):
        """1.2.4 bumped the version; 1.2.5 shipped PCT_MAX. A board built from
        the former renders 102 as 0, so the gate must not trust it."""
        out = protocol.cap_overage_for_fw(self.msg(), "1.2.4")
        self.assertEqual(out["weekly_pct"], 100.0)

    def test_a_non_finite_percentage_becomes_unknown(self):
        """nan compares false against everything, so it would slip past the
        cap and json.dumps would emit a bare NaN -- not valid JSON."""
        for bad in (float("nan"), float("inf"), float("-inf")):
            out = protocol.cap_overage_for_fw(self.msg(weekly_pct=bad), "1.2.3")
            self.assertEqual(out["weekly_pct"], -1.0, bad)

    def test_exactly_one_hundred_is_untouched(self):
        for fw in ("1.2.3", "1.2.5"):
            out = protocol.cap_overage_for_fw(self.msg(weekly_pct=100.0), fw)
            self.assertEqual(out["weekly_pct"], 100.0, fw)

    def test_a_later_firmware_also_gets_it(self):
        self.assertEqual(
            protocol.cap_overage_for_fw(self.msg(), "1.3.0")["weekly_pct"],
            102.0)

    def test_an_unknown_board_version_caps(self):
        """greet() can push before any hello, so None means "not yet told".
        Guessing modern is the one guess that puts a zero on a panel."""
        for fw in (None, "", "1.2", "nightly", 124):
            self.assertEqual(
                protocol.cap_overage_for_fw(self.msg(), fw)["weekly_pct"],
                100.0, fw)

    def test_the_unknown_sentinel_and_zero_travel_untouched(self):
        out = protocol.cap_overage_for_fw(
            self.msg(session_pct=-1.0, weekly_pct=0.0), "1.2.3")
        self.assertEqual(out["session_pct"], -1.0)
        self.assertEqual(out["weekly_pct"], 0.0)

    def test_every_capped_field_is_covered(self):
        m = self.msg(fable_pct=115.0, p2_session_pct=101.0,
                     p2_weekly_pct=140.0, session_pct=103.0)
        out = protocol.cap_overage_for_fw(m, "1.2.3")
        for k in ("session_pct", "weekly_pct", "fable_pct",
                  "p2_session_pct", "p2_weekly_pct"):
            self.assertEqual(out[k], 100.0, k)

    def test_the_caller_s_message_is_not_mutated(self):
        m = self.msg()
        protocol.cap_overage_for_fw(m, "1.2.3")
        self.assertEqual(m["weekly_pct"], 102.0)

    def test_the_worst_real_message_fits_the_line_budget(self):
        """The one that matters: a two-provider message built through the real
        pipeline, carrying age_s/p2_age_s and unrounded overage percentages.

        proto.c DROPS an over-long line rather than truncating it, so going
        past MAX_LINE_BYTES freezes the panel with one stderr line nobody
        reads. Measured at 506 of 512 before percentages were rounded on the
        wire -- six bytes, with float("102.33333333333333") the normal case
        rather than a contrived one. An earlier version of this test built a
        60-byte dict by hand and proved nothing.
        """
        from pc.providers.base import NormalizedUsageFrame as F
        now = 1788178465.0

        def frame(provider, s_pct, w_pct):
            return F(provider=provider, src="cli", observed_at=now - 3,
                     session_pct=s_pct, session_resets_at=now + 15335,
                     weekly_pct=w_pct, weekly_resets_at=now + 6335,
                     state="waiting", stale=False,
                     session_burn_pph=9.33333333, n_run=1, n_wait=1)

        msg = protocol.frame_to_usage(
            frame("claude", 102.33333333333333, 102.66666666666667), now,
            frame("codex", 88.12345678901234, 91.98765432109876))
        for board_fw in ("1.2.3", "1.2.5"):
            line = json.dumps(
                protocol.cap_overage_for_fw(msg, board_fw)) + "\n"
            self.assertLessEqual(len(line.encode()),
                                 protocol.MAX_LINE_BYTES, board_fw)

    def test_percentages_are_rounded_on_the_wire(self):
        """One decimal. The firmware label is (int)(pct + 0.5) and the arc is
        an int32, so nothing downstream can tell -- but 18-byte floats are
        what spent the line budget."""
        from pc.providers.base import NormalizedUsageFrame as F
        now = 1788178465.0
        f = F(provider="claude", src="cli", observed_at=now, state="",
              session_pct=102.33333333333333, weekly_pct=88.98765432109876)
        msg = protocol.frame_to_usage(f, now)
        self.assertEqual(msg["session_pct"], 102.3)
        self.assertEqual(msg["weekly_pct"], 89.0)


class SessionMessage(unittest.TestCase):
    """The project name travels as its own message type rather than as a
    field on the usage line -- see the byte budget test at the bottom."""

    def test_session_message_shape(self):
        m = protocol.session("LiveClaudeUi", 1)
        self.assertEqual(m["t"], "session")
        self.assertEqual(m["label"], "LiveClaudeUi")
        self.assertEqual(m["n"], 1)

    def test_session_omits_an_empty_label(self):
        # Absent already means unknown on both sides, and every optional key
        # on this wire is omitted rather than sent as a sentinel.
        self.assertNotIn("label", protocol.session("", 3))

    def test_session_caps_the_label(self):
        m = protocol.session("x" * 100, 1)
        self.assertEqual(len(m["label"].encode()),
                         protocol.SESSION_LABEL_MAX_BYTES)

    def test_session_label_survives_multibyte_truncation(self):
        """Cutting a UTF-8 sequence in half must not produce an undecodable
        field.

        The label is MIXED-WIDTH on purpose. 24 divides by 2, 3 and 4, so a
        label of one repeated character always lands on a boundary however
        it is cut -- a naive `label.encode()[:24].decode()` passes such a
        test and this guard would be measuring nothing. Five ASCII bytes in
        front push the cut off the boundary: 19 bytes of aleph is nine
        characters and a dangling half.
        """
        label = "proj-" + "א" * 20
        with self.assertRaises(UnicodeDecodeError):
            label.encode()[:protocol.SESSION_LABEL_MAX_BYTES].decode()

        m = protocol.session(label, 1)
        self.assertEqual(m["label"].encode().decode("utf-8"), m["label"])
        self.assertLessEqual(len(m["label"].encode()),
                             protocol.SESSION_LABEL_MAX_BYTES)
        self.assertTrue(label.startswith(m["label"]))

    def test_encode_puts_utf8_on_the_wire_not_escapes(self):
        """The firmware does no unescaping, so an escape is drawn literally.

        msg_parse.c copies the bytes between two quotes and hands them to
        fmt_ascii(), which decodes UTF-8 and transliterates what it cannot
        draw. json.dumps' default ensure_ascii=True defeated that entirely:
        "café" arrived as the six characters \\u00e9 and the panel drew them.
        """
        raw = protocol.encode(protocol.session("café", 1))
        self.assertIn("café".encode("utf-8"), raw)
        self.assertNotIn(b"\\u00e9", raw)

    def test_encode_of_non_ascii_is_never_longer_than_the_escaped_form(self):
        """The byte budget can only improve. proto.c drops an over-long line
        whole, so a change to the encoder has to be shown not to lengthen
        anything before it can be believed."""
        import json as _json
        msg = protocol.session("café-project-name-abcdef", 9999)
        escaped = (_json.dumps(msg, separators=(",", ":"),
                               ensure_ascii=True) + "\n").encode("utf-8")
        self.assertLessEqual(len(protocol.encode(msg)), len(escaped))
        self.assertLessEqual(len(protocol.encode(msg)),
                             protocol.MAX_LINE_BYTES)

    def test_a_non_ascii_label_still_fits_the_line_limit(self):
        """The worst case for the label field: 24 bytes of four-byte
        codepoints, which ensure_ascii would have turned into 12 escapes."""
        msg = protocol.session("𝔅" * 12, 9999)
        raw, reason = protocol.encode_checked(msg)
        self.assertIsNone(reason)
        self.assertLessEqual(len(raw), protocol.MAX_LINE_BYTES)

    def test_session_label_survives_a_three_byte_truncation(self):
        """The same again at three bytes a character, where the dangling
        remainder is two bytes rather than one."""
        label = "x" + "中" * 20
        with self.assertRaises(UnicodeDecodeError):
            label.encode()[:protocol.SESSION_LABEL_MAX_BYTES].decode()

        m = protocol.session(label, 2)
        self.assertEqual(m["label"].encode().decode("utf-8"), m["label"])
        self.assertLessEqual(len(m["label"].encode()),
                             protocol.SESSION_LABEL_MAX_BYTES)
        self.assertTrue(label.startswith(m["label"]))

    # The three flattened per-model percentages, kept out of the fixture
    # below and measured on their own in
    # test_the_model_percentages_no_longer_fit_beside_the_active_age.
    MODEL_ROWS = [{"name": "fable", "weekly_pct": 91.11111111111111},
                  {"name": "sonnet", "weekly_pct": 72.22222222222223},
                  {"name": "opus", "weekly_pct": 63.33333333333333}]

    @staticmethod
    def fully_loaded_usage_kwargs():
        """Every optional key the pipeline can populate, at its widest: two
        providers, both reset stamps, both countdowns, all three ages, every
        count, the burn rate, and unrounded overage percentages on all four
        dials.

        Still wider than the pipeline can actually produce -- the normalizer
        never sends a burn rate beside a reset time -- deliberately, because
        the number this defends is a ceiling and a half-populated fixture
        would clear the bar without measuring anything. 480 of 512.

        `models` is the one thing left out, and it is left out because it
        can no longer be in: frame_to_usage, the only caller of usage(),
        passes models=[] and has since the status line became the sole
        source, and the three keys it would flatten cost 51 bytes the line
        does not have now that active_age_s is on it (531 of 512, measured
        in the test named above). That is a real narrowing of the wire, not
        a fixture convenience, so it is asserted rather than assumed.
        """
        return dict(
            session_pct=102.33333333333333,
            session_resets_at=1788193800.0,
            weekly_pct=102.66666666666667,
            weekly_resets_at=1788584800.0,
            models=[],
            session_resets_in_s=15335, weekly_resets_in_s=406335,
            stale=True, provider="claude", src="desktop", state="waiting",
            n_sess=4, n_run=1, n_wait=2, n_stuck=1, n_agents=3,
            p2="codex", p2_session_pct=88.12345678901234,
            p2_weekly_pct=91.98765432109876,
            p2_session_resets_in_s=15335, p2_weekly_resets_in_s=406335,
            p2_stale=True, burn_pph=9.33333333,
            age_s=14400, p2_age_s=86399, active_age_s=86400,
        )

    def test_the_model_percentages_no_longer_fit_beside_the_active_age(self):
        """What the new field cost, pinned rather than left to be discovered
        on a desk.

        usage() can still flatten fable/sonnet/opus into three scalar keys
        and proto.c still reads them, but nothing has produced them since
        frame_to_usage became the only caller and started passing models=[].
        Putting them back now writes a 531-byte line, and the board DROPS an
        over-long line whole with no error on either side.

        The daemon does not: claude_usage_bridge's only writer is
        encode_checked, which refuses and logs. So the failure mode of
        re-enabling the model keys is a panel that stops updating with a
        stderr line naming the reason -- which is why this asserts the
        refusal, and why a future task that wants them back will land here
        and read the arithmetic before it ships.
        """
        kw = self.fully_loaded_usage_kwargs()
        kw["models"] = self.MODEL_ROWS
        raw, why = protocol.encode_checked(protocol.usage(**kw))
        self.assertIsNone(raw)
        self.assertIn("line limit", why)
        self.assertGreater(
            len(protocol.encode(protocol.usage(**kw))),
            protocol.MAX_LINE_BYTES)

    def test_usage_frame_did_not_grow(self):
        # This is NOT the byte guard any more, and saying so matters: this
        # fixture measures 480 of 512, so 32 bytes could be added without it
        # noticing. test_the_widest_line_the_daemon_can_build_still_fits is
        # the guard, at 509, because it builds through frame_to_usage -- the
        # only caller that can actually put a line on the wire -- rather than
        # calling usage() with a hand-written set of kwargs.
        #
        # What this test still does, and still should: the project name became
        # its own message BECAUSE the usage line has no room for it, so a
        # label or a "proj" key appearing here is the exact regression the
        # separate message type exists to prevent. The size assertion below is
        # kept as a floor, not as the ceiling it used to be.
        raw = protocol.encode(
            protocol.usage(**self.fully_loaded_usage_kwargs())).decode()
        self.assertLessEqual(len(raw.encode()), protocol.MAX_LINE_BYTES, raw)
        self.assertNotIn("label", raw)
        self.assertNotIn('"proj"', raw)

    def test_a_named_frame_does_not_put_its_name_on_the_usage_line(self):
        """The same bound one level up, on the function the daemon actually
        calls. The fixture above builds the message through usage() directly,
        so a label threaded through frame_to_usage -- the only caller, and
        the one that now has a named frame in front of it -- would slip past
        it entirely."""
        from pc.providers.base import NormalizedUsageFrame as F
        now = 1788178465.0

        def frame(provider, s_pct, w_pct):
            return F(provider=provider, src="desktop", observed_at=now - 14400,
                     session_pct=s_pct, session_resets_at=now + 15335,
                     weekly_pct=w_pct, weekly_resets_at=now + 406335,
                     state="waiting", stale=True,
                     session_burn_pph=9.33333333, n_run=1, n_wait=2,
                     n_stuck=1, n_idle=1, n_agents=3,
                     label="a-project-with-a-long-name")

        raw = protocol.encode(protocol.frame_to_usage(
            frame("claude", 102.33333333333333, 102.66666666666667), now,
            frame("codex", 88.12345678901234, 91.98765432109876))).decode()
        self.assertLessEqual(len(raw.encode()), protocol.MAX_LINE_BYTES, raw)
        self.assertNotIn("label", raw)
        self.assertNotIn("a-project", raw)


class VolatileUsageKeys(unittest.TestCase):
    """The exclusion list the change-driven push compares around.

    Bridge.poll_if_changed sends when a usage message differs from the last one
    sent, ignoring protocol.VOLATILE_USAGE_KEYS. Both halves of that can rot
    silently and in opposite directions: a timer key missing from the list
    makes every 2 s tick a push, and a real field wrongly in it makes the panel
    wait for the 60 s heartbeat again.
    """

    def _loaded(self):
        """A message with every optional key on it, both providers included.

        The volatile names are only checkable against a message that carries
        them, and the common single-provider line carries neither p2 countdown.
        """
        now = 1_787_700_000.0

        def frame(provider):
            return base.NormalizedUsageFrame(
                provider=provider, src="cli", observed_at=now - 30,
                active_at=now - 5, session_pct=61.0,
                session_resets_at=now + 3600, weekly_pct=26.0,
                weekly_resets_at=now + 86400, state="running", stale=False,
                n_run=1, n_wait=2, n_stuck=0, n_idle=0, n_agents=3)

        return protocol.frame_to_usage(frame("claude"), now,
                                       secondary=frame("codex"))

    def test_every_named_volatile_key_is_one_the_daemon_really_sends(self):
        """A typo here would be invisible: an unknown name excludes nothing,
        so the real timer field stays in the comparison and moves on every
        poll -- which is the 2 s unconditional push this design exists to
        avoid, arriving quietly and passing every other test."""
        msg = self._loaded()
        missing = sorted(protocol.VOLATILE_USAGE_KEYS - set(msg))
        self.assertEqual(missing, [])

    def test_the_timers_are_dropped_and_the_state_is_kept(self):
        msg = self._loaded()
        kept = protocol.meaningful_usage(msg)
        for k in protocol.VOLATILE_USAGE_KEYS:
            self.assertNotIn(k, kept)
        for k in ("state", "n_run", "n_wait", "n_agents", "session_pct",
                  "weekly_pct", "stale", "src", "provider", "p2",
                  "p2_session_pct", "p2_stale"):
            self.assertIn(k, kept, k)

    def test_the_absolute_reset_stamps_stay_in_the_comparison(self):
        """They move only when a window rolls over -- exactly the event worth
        a push -- unlike the *_in_s countdowns derived from them."""
        kept = protocol.meaningful_usage(self._loaded())
        self.assertIn("session_resets_at", kept)
        self.assertIn("weekly_resets_at", kept)

    def test_a_field_this_list_has_never_heard_of_is_meaningful(self):
        """The point of an exclusion list. A future key that matters must make
        the panel fast without anybody remembering to come back here."""
        msg = dict(self._loaded(), some_field_from_2027=7)
        self.assertEqual(
            protocol.meaningful_usage(msg)["some_field_from_2027"], 7)
