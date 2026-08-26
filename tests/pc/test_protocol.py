import unittest
from pc import protocol


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


if __name__ == "__main__":
    unittest.main()


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
