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
                         {"t": "welcome", "v": 1, "app": "app", "app_ver": "0.2.0"})
        u = protocol.usage(61.0, "R1", 26.0, "R2", [{"name": "sonnet", "weekly_pct": 2.0}])
        self.assertEqual(u["t"], "usage")
        self.assertEqual(u["session_pct"], 61.0)
        self.assertEqual(u["models"][0]["name"], "sonnet")
        self.assertEqual(protocol.status("rate_limited", "x"),
                         {"t": "status", "v": 1, "state": "rate_limited", "detail": "x"})


if __name__ == "__main__":
    unittest.main()
