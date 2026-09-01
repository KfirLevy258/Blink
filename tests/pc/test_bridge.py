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


# --- the OTA guard must not depend on a reset --------------------------------
#
# hello is sent once, at boot. The daemon now connects to an already-running
# board without resetting it, so there is no hello -- and _board_ahead() gated
# the one operation that writes slot0 in place with no revert behind it.


def test_the_protocol_version_is_learned_without_a_hello():
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None)
    assert b._board_proto is None
    b.on_message({"t": "ping", "v": 99, "up_ms": 10})
    assert b._board_proto == 99
    assert b._board_ahead() is True


def test_a_board_ahead_is_refused_an_update_on_a_no_reset_connection(capsys):
    """The regression in full: connect with no hello, then a query."""
    sent = []
    b = Bridge(write_msg=sent.append, fetch_usage=lambda: None)
    b.on_message({"t": "ping", "v": 99, "up_ms": 10})      # no hello anywhere
    b.on_message({"t": "ota_query", "v": 99, "cur": "9.9.9"})
    assert any(m.get("t") == "ota_none" for m in sent)
    assert not any(m.get("t") == "ota_avail" for m in sent)


def test_the_firmware_version_is_learned_from_the_query():
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None)
    b.on_message({"t": "ota_query", "v": 2, "cur": "0.6.0"})
    assert b._board_fw == "0.6.0"


def test_hello_still_wins_and_does_not_double_announce(capsys):
    b = Bridge(write_msg=lambda m: None, fetch_usage=lambda: None)
    b.on_message({"t": "hello", "v": 2, "fw": "0.6.0", "board": "cyd"})
    assert b._board_proto == 2
    assert b._board_fw == "0.6.0"
    assert b._board_ahead() is False


class OverageCapIsWiredTest(unittest.TestCase):
    """poll_once must actually apply the cap.

    protocol.cap_overage_for_fw was well covered on its own, but nothing
    exercised the one line in poll_once that calls it -- deleting that line
    left the whole suite green while every board in the field went back to
    drawing 0% the moment its owner crossed into extra usage.
    """

    def _bridge(self, sent):
        return Bridge(write_msg=sent.append,
                      fetch_usage=lambda: {"t": "usage", "v": protocol.VERSION,
                                           "session_pct": 22.0,
                                           "weekly_pct": 102.0})

    def _weekly(self, sent):
        return [m for m in sent if m.get("t") == "usage"][0]["weekly_pct"]

    def test_an_old_board_is_capped_on_the_way_out(self):
        sent = []
        b = self._bridge(sent)
        b.on_message({"t": "hello", "v": 2, "fw": "1.2.3",
                      "board_id": "abc"})
        sent.clear()
        b.poll_once()
        self.assertEqual(self._weekly(sent), 100.0)

    def test_a_new_board_receives_the_true_number(self):
        sent = []
        b = self._bridge(sent)
        b.on_message({"t": "hello", "v": 2, "board_id": "abc",
                      "fw": ".".join(str(x)
                                     for x in protocol.FW_ACCEPTS_OVERAGE)})
        sent.clear()
        b.poll_once()
        self.assertEqual(self._weekly(sent), 102.0)

    def test_before_any_hello_the_push_is_capped(self):
        """greet() can push before a board has said hello."""
        sent = []
        self._bridge(sent).poll_once()
        self.assertEqual(self._weekly(sent), 100.0)


class SessionMessageIsSent(unittest.TestCase):
    """The project name, in its own message beside the usage line.

    The fetch callable carries it as an attribute: the Bridge is handed a
    zero-arg callable returning a finished usage dict and never sees the
    frame the name lives on (pc/ingest.make_fetch hangs session_pair on it).
    """

    def setUp(self):
        self.sent = []
        self.pair = ("LiveClaudeUi", 1)

        def fetch():
            return protocol.usage(61.0, "R1", 26.0, "R2", [])

        fetch.session_pair = lambda: self.pair
        self.bridge = Bridge(write_msg=self.sent.append, fetch_usage=fetch,
                             now=FakeClock().now, app_ver="0.2.0",
                             wall=lambda: (1752444000, 180))

    def _sessions(self):
        return [m for m in self.sent if m.get("t") == "session"]

    def test_session_message_is_sent_when_the_label_changes(self):
        self.bridge.poll_once()
        self.assertEqual(self._sessions()[-1]["label"], "LiveClaudeUi")

        self.bridge.poll_once()          # nothing changed
        self.assertEqual(len(self._sessions()), 1)

        self.pair = ("Blink", 2)
        self.bridge.poll_once()
        self.assertEqual(self._sessions()[-1]["label"], "Blink")
        self.assertEqual(self._sessions()[-1]["n"], 2)

    def test_session_message_is_resent_on_greet(self):
        # A board that just booted holds nothing. Without this a replugged
        # board shows a bare status until the next time the project happens
        # to change -- the same reason firmware currency is re-offered on
        # every connect rather than once per daemon lifetime.
        self.bridge.poll_once()
        self.sent.clear()
        self.bridge.greet()
        self.assertTrue(self._sessions())

    def test_the_count_travels_even_when_the_label_is_empty(self):
        """Several sessions share the state, so none of them can be named --
        the board falls back to the count, which still has to arrive."""
        self.pair = ("", 3)
        self.bridge.poll_once()
        m = self._sessions()[-1]
        self.assertNotIn("label", m)
        self.assertEqual(m["n"], 3)

    def test_a_fetch_without_the_accessor_sends_nothing(self):
        """Every other fetch in this file is a bare lambda, and the
        single-source fetch has no session to report. Sending nothing is
        exactly what an older daemon did."""
        sent = []
        b = Bridge(write_msg=sent.append,
                   fetch_usage=lambda: protocol.usage(1.0, "R", 2.0, "R", []),
                   now=FakeClock().now, wall=lambda: (1752444000, 180))
        b.poll_once()
        self.assertEqual([m["t"] for m in sent], ["time", "usage"])
