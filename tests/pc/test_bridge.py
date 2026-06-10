import unittest
from pc import protocol
from pc.bridge import Bridge


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
        )

    def test_hello_triggers_welcome_then_usage(self):
        self.bridge.on_message({"t": "hello", "v": 1, "board_id": "ab"})
        types = [m["t"] for m in self.sent]
        self.assertEqual(types, ["welcome", "usage"])
        self.assertEqual(self.sent[1]["session_pct"], 61.0)

    def test_ping_updates_liveness(self):
        self.assertFalse(self.bridge.board_alive())
        self.bridge.on_message({"t": "ping", "v": 1, "up_ms": 5})
        self.assertTrue(self.bridge.board_alive())
        self.clock.t += 31
        self.assertFalse(self.bridge.board_alive())

    def test_unknown_type_ignored(self):
        self.bridge.on_message({"t": "wat", "v": 1})
        self.assertEqual(self.sent, [])

    def test_poll_sends_usage_or_status(self):
        self.bridge.poll_once()
        self.assertEqual(self.sent[-1]["t"], "usage")

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
