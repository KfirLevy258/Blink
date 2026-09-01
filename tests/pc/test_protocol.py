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
        # Cutting a UTF-8 sequence in half must not produce an undecodable
        # field.
        m = protocol.session("א" * 40, 1)
        m["label"].encode()          # must not raise
        self.assertLessEqual(len(m["label"].encode()),
                             protocol.SESSION_LABEL_MAX_BYTES)
        self.assertTrue(m["label"].startswith("א"))

    def test_session_label_survives_a_three_byte_truncation(self):
        """Three bytes per character does not divide 24, so this is the case
        a raw byte slice actually halves."""
        m = protocol.session("中" * 40, 2)
        self.assertEqual(m["label"].encode().decode("utf-8"), m["label"])
        self.assertLessEqual(len(m["label"].encode()),
                             protocol.SESSION_LABEL_MAX_BYTES)

    @staticmethod
    def fully_loaded_usage_kwargs():
        """Every optional key populated at its widest: two providers, all
        three per-model percentages, both reset stamps, both countdowns,
        both ages, every count, the burn rate, and unrounded overage
        percentages on all four dials.

        Wider than the pipeline can actually produce -- the normalizer never
        sends a burn rate beside a reset time, and frame_to_usage, the only
        caller of usage(), passes models=[] -- deliberately, because the
        number this defends is a ceiling and a half-populated fixture would
        clear the bar without measuring anything. It comes out at 510 of
        512, which is the whole point: this line has two bytes of slack and
        the project name is a great deal more than two bytes.
        """
        return dict(
            session_pct=102.33333333333333,
            session_resets_at=1788193800.0,
            weekly_pct=102.66666666666667,
            weekly_resets_at=1788584800.0,
            models=[{"name": "fable", "weekly_pct": 91.11111111111111},
                    {"name": "sonnet", "weekly_pct": 72.22222222222223},
                    {"name": "opus", "weekly_pct": 63.33333333333333}],
            session_resets_in_s=15335, weekly_resets_in_s=406335,
            stale=True, provider="claude", src="desktop", state="waiting",
            n_sess=4, n_run=1, n_wait=2, n_stuck=1, n_agents=3,
            p2="codex", p2_session_pct=88.12345678901234,
            p2_weekly_pct=91.98765432109876,
            p2_session_resets_in_s=15335, p2_weekly_resets_in_s=406335,
            p2_stale=True, burn_pph=9.33333333,
            age_s=14400, p2_age_s=86399,
        )

    def test_usage_frame_did_not_grow(self):
        # The frame was measured at 506 of 512 and proto.c drops an over-long
        # line whole. This is the regression that would freeze panels: the
        # project name became its own message BECAUSE this line has no room
        # for it, so a label arriving here is the exact failure the new
        # message type exists to prevent.
        raw = protocol.encode(
            protocol.usage(**self.fully_loaded_usage_kwargs())).decode()
        self.assertLessEqual(len(raw.encode()), protocol.MAX_LINE_BYTES, raw)
        self.assertNotIn("label", raw)
        self.assertNotIn('"proj"', raw)
