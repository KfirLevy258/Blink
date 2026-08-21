import unittest
import urllib.error
from pc import protocol
from pc.bridge import Bridge


def http_error(code, retry_after=None):
    """A urllib HTTPError shaped like the real thing, headers included."""
    hdrs = {"Retry-After": str(retry_after)} if retry_after is not None else {}
    return urllib.error.HTTPError("http://x", code, "boom", hdrs, None)


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


if __name__ == "__main__":
    unittest.main()


class TestRateLimit(unittest.TestCase):
    """A 429 is not a failure: the numbers already on screen are still true,
    and the board has an amber "showing last known" state for exactly this.
    Reporting it as an error paints a healthy board red, and retrying at the
    usual cadence keeps the throttle closed."""

    def setUp(self):
        self.sent = []
        self.clock = FakeClock()
        self.calls = []

    def _bridge(self, fetch):
        return Bridge(write_msg=self.sent.append, fetch_usage=fetch,
                      now=self.clock.now, wall=lambda: (1752444000, 180))

    def _counting_429(self, retry_after=None):
        def _f():
            self.calls.append(self.clock.t)
            raise http_error(429, retry_after)
        return _f

    def test_429_reports_rate_limited_not_error(self):
        b = self._bridge(self._counting_429())
        b.poll_once()
        self.assertEqual(self.sent[-1]["t"], "status")
        self.assertEqual(self.sent[-1]["state"], "rate_limited")

    def test_429_suppresses_the_next_fetch(self):
        b = self._bridge(self._counting_429())
        b.poll_once()
        self.clock.t += 60          # the normal poll cadence
        b.poll_once()
        self.assertEqual(len(self.calls), 1, "second poll must not hit the API")

    def test_backoff_expires(self):
        b = self._bridge(self._counting_429())
        b.poll_once()
        self.clock.t += 10000
        b.poll_once()
        self.assertEqual(len(self.calls), 2)

    def test_still_reports_while_backed_off(self):
        """Silence would leave the board showing a stale green dot."""
        b = self._bridge(self._counting_429())
        b.poll_once()
        self.sent.clear()
        self.clock.t += 60
        b.poll_once()
        self.assertEqual([m["t"] for m in self.sent], ["time", "status"])
        self.assertEqual(self.sent[-1]["state"], "rate_limited")

    def test_zero_retry_after_does_not_disable_the_backoff(self):
        """The live endpoint sends `Retry-After: 0` (observed 2026-08-18).
        Taken literally that is an invitation to keep knocking every cadence,
        which is precisely what got us throttled. Retry-After is the EARLIEST
        a retry is allowed, not a recommended interval: it may only extend our
        own ladder, never shorten it."""
        b = self._bridge(self._counting_429(retry_after=0))
        b.poll_once()
        self.clock.t += 60
        b.poll_once()
        self.assertEqual(len(self.calls), 1, "Retry-After: 0 must not mean 'no backoff'")

    def test_retry_after_longer_than_our_ladder_wins(self):
        b = self._bridge(self._counting_429(retry_after=300))
        b.poll_once()
        self.clock.t += 200         # past our 120 s floor, inside the server's 300
        b.poll_once()
        self.assertEqual(len(self.calls), 1)

    def test_retry_after_is_honoured(self):
        b = self._bridge(self._counting_429(retry_after=300))
        b.poll_once()
        self.clock.t += 299
        b.poll_once()
        self.assertEqual(len(self.calls), 1)
        self.clock.t += 2
        b.poll_once()
        self.assertEqual(len(self.calls), 2)

    def test_backoff_grows_then_resets_on_success(self):
        state = {"fail": True}

        def flaky():
            self.calls.append(self.clock.t)
            if state["fail"]:
                raise http_error(429)
            return protocol.usage(1.0, "R1", 2.0, "R2", [])

        b = self._bridge(flaky)
        b.poll_once()                       # 1st 429 -> min backoff
        self.clock.t += 120
        b.poll_once()                       # 2nd 429 -> doubled
        self.clock.t += 120
        b.poll_once()                       # still inside the longer window
        self.assertEqual(len(self.calls), 2)
        state["fail"] = False
        self.clock.t += 10000
        b.poll_once()                       # succeeds -> backoff cleared
        self.assertEqual(self.sent[-1]["t"], "usage")
        self.clock.t += 60
        b.poll_once()
        self.assertEqual(len(self.calls), 4, "success must clear the backoff")

    def test_other_http_errors_stay_errors(self):
        b = self._bridge(raising(http_error(500)))
        b.poll_once()
        self.assertEqual(self.sent[-1]["state"], "error")

    def test_other_errors_do_not_block_polling(self):
        def boom():
            self.calls.append(self.clock.t)
            raise RuntimeError("api down")
        b = self._bridge(boom)
        b.poll_once()
        self.clock.t += 60
        b.poll_once()
        self.assertEqual(len(self.calls), 2)

    def test_reconnect_while_throttled_does_not_refetch(self):
        """Every board reboot sends hello, and hello pushes immediately --
        so a flashing session used to fire an off-cadence call each time."""
        b = self._bridge(self._counting_429())
        b.poll_once()
        self.sent.clear()
        b.on_message({"t": "hello", "v": 2, "board_id": "ab"})
        self.assertEqual(len(self.calls), 1)
        self.assertEqual([m["t"] for m in self.sent],
                         ["welcome", "time", "status"])
