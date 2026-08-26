import unittest
from pc import protocol
from pc.bridge import Bridge


def raising(exc):
    def _f():
        raise exc
    return _f


class FakeClock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t


class TestBridge(unittest.TestCase):
    def setUp(self):
        self.sent = []
        self.clock = FakeClock()
        self.bridge = Bridge(
            write_msg=lambda m: self.sent.append(m),
            fetch_usage=lambda: protocol.usage(61.0, "R1", 26.0, "R2",
                                               [{"name": "sonnet", "weekly_pct": 2.0}]),
            now=self.clock.now,
            app_ver="0.2.0",
            wall=lambda: (1752444000, 180),
        )

    def test_hello_triggers_welcome_time_usage(self):
        self.bridge.on_message({"t": "hello", "v": 1, "board_id": "ab"})
        types = [m["t"] for m in self.sent]
        self.assertEqual(types, ["welcome", "time", "usage"])
        self.assertEqual(self.sent[2]["session_pct"], 61.0)

    def test_time_uses_injected_wall_clock(self):
        b = Bridge(write_msg=self.sent.append,
                   fetch_usage=lambda: protocol.usage(1.0, "R", 2.0, "R", []),
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.on_message({"t": "hello", "v": 1})
        t = [m for m in self.sent if m["t"] == "time"][0]
        self.assertEqual(t["epoch"], 1752444000)
        self.assertEqual(t["utc_offset_min"], 180)

    def test_poll_pushes_time_then_usage(self):
        self.bridge.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "usage"])

    def test_poll_with_no_payload_yet_sends_time_only(self):
        """statusline_source.make_fetch() returns None before Claude Code has
        ever written a payload. That is not an error -- there is simply
        nothing to report yet -- so no 'usage' or 'status' message should
        follow, and the board just keeps whatever it last had."""
        b = Bridge(write_msg=self.sent.append, fetch_usage=lambda: None,
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time"])

    def test_ping_updates_liveness(self):
        self.assertFalse(self.bridge.board_alive())
        self.bridge.on_message({"t": "ping", "v": 1, "up_ms": 5})
        self.assertTrue(self.bridge.board_alive())
        self.clock.t += 31
        self.assertFalse(self.bridge.board_alive())

    def test_ping_is_answered_with_pong(self):
        """Liveness must be bidirectional. The daemon only pushes usage every
        60 s, so without an answer to the board's 10 s ping the board cannot
        tell 'host alive, not due to poll yet' from 'host died' -- and would
        keep showing a green dot over frozen numbers."""
        self.bridge.on_message({"t": "ping", "v": 1, "up_ms": 5})
        self.assertEqual([m["t"] for m in self.sent], ["pong"])

    def test_pong_costs_no_api_call(self):
        """A pong must be free. The usage endpoint is aggressively rate-limited,
        so answering a 10 s ping by fetching would get us 429'd."""
        calls = []

        def counting_fetch():
            calls.append(1)
            return protocol.usage(1.0, "R", 2.0, "R", [])

        b = Bridge(write_msg=self.sent.append, fetch_usage=counting_fetch,
                   now=self.clock.now)
        b.on_message({"t": "ping", "v": 1, "up_ms": 5})
        self.assertEqual(calls, [])

    def test_unknown_type_ignored(self):
        self.bridge.on_message({"t": "wat", "v": 1})
        self.assertEqual(self.sent, [])

    def test_poll_sends_usage_or_status(self):
        self.bridge.poll_once()
        self.assertEqual(self.sent[-1]["t"], "usage")

    def test_hello_with_failed_fetch_still_welcomes(self):
        """A dead API must not mute the handshake: the board needs welcome
        (mode detection) and time (clock) even when usage is unavailable,
        and the failure must arrive as a status message, not an exception."""
        def boom():
            raise RuntimeError("api down")
        b = Bridge(write_msg=self.sent.append, fetch_usage=boom,
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.on_message({"t": "hello", "v": 2, "board_id": "ab"})
        self.assertEqual([m["t"] for m in self.sent],
                         ["welcome", "time", "status"])
        self.assertEqual(self.sent[-1]["state"], "error")

    def test_poll_error_sends_status(self):
        def boom():
            raise RuntimeError("429")
        b = Bridge(write_msg=lambda m: self.sent.append(m),
                   fetch_usage=boom, now=self.clock.now, app_ver="0.2.0")
        b.poll_once()
        self.assertEqual(self.sent[-1]["t"], "status")
        self.assertEqual(self.sent[-1]["state"], "error")

    def test_stale_usage_needs_no_second_message(self):
        """The usage message carries `stale` and proto.c reads it, so the
        board colours its own dot from the reading it was just given.

        This used to assert the opposite -- that a status "rate_limited"
        followed -- which was a workaround for firmware that had no bool
        getter. It made a stale reading and a real rate limit identical on
        the panel and in the log.
        """
        b = Bridge(write_msg=self.sent.append,
                   fetch_usage=lambda: protocol.usage(
                       61.0, "R1", 26.0, "R2", [], stale=True),
                   now=self.clock.now, wall=lambda: (1752444000, 180))
        b.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "usage"])
        self.assertTrue(self.sent[-1]["stale"])

    def test_fresh_usage_sends_no_extra_status(self):
        self.bridge.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "usage"])


if __name__ == "__main__":
    unittest.main()


# TestRateLimit lived here and is gone with the code it covered.
#
# Seven tests of an HTTP 429 ladder, against a fetch that now reads a local
# file and cannot be rate limited by anyone. They passed by injecting an
# exception production could not produce, which is the shape of a test that
# outlives its subject: green, specific, and describing something that stopped
# being true when pc/usage_api.py was deleted.


class PrefMessage(unittest.TestCase):
    """The board announces which provider the user made primary."""

    def _bridge(self, applied):
        return Bridge(write_msg=lambda m: None, fetch_usage=lambda: None,
                      set_preferred=lambda p: applied.append(p) or True)

    def test_a_pref_message_reaches_the_bus(self):
        applied = []
        self._bridge(applied).on_message(
            {"t": "pref", "v": 2, "provider": "codex"})
        self.assertEqual(applied, ["codex"])

    def test_a_pref_without_a_provider_is_ignored(self):
        applied = []
        self._bridge(applied).on_message({"t": "pref", "v": 2})
        self.assertEqual(applied, [])

    def test_a_non_string_provider_is_ignored(self):
        applied = []
        self._bridge(applied).on_message(
            {"t": "pref", "v": 2, "provider": 7})
        self.assertEqual(applied, [])

    def test_a_daemon_without_a_bus_does_not_crash_on_pref(self):
        """The tests wire a Bridge with no bus at all; a board that announces
        its preference to one must not take it down."""
        b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None)
        b.on_message({"t": "pref", "v": 2, "provider": "codex"})
