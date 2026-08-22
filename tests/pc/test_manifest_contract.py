"""The manifest shape, pinned.

Not a test of behaviour. A test that the document we publish still looks the
way the software already in customers' hands expects it to look -- which is the
one thing in this project that cannot be fixed after the fact, because the
reader that would need the fix is the reader that stopped accepting updates.

A failure here is not a bug. It is a question: is this change additive, or is it
about to strand every install that predates it?
"""
import unittest

from pc import manifest, ota, update


ART = {k: {"size": 10 + i, "sha256": f"{i:02x}" * 32}
       for i, k in enumerate(manifest.ARTIFACT_KEYS)}


class TestShape(unittest.TestCase):
    def setUp(self):
        self.m = manifest.build("0.6.0", 661744, "ab" * 32, 2, ART)

    def test_the_top_level_keys_are_exactly_these(self):
        self.assertEqual(sorted(self.m), ["daemon", "fw", "schema", "sha256",
                                          "size", "version"])

    def test_the_firmware_is_at_the_top_level(self):
        """Where pc/ota.py has always read it from. Nesting these under "fw"
        for symmetry would be the one change that cannot be rolled back."""
        self.assertEqual(self.m["version"], "0.6.0")
        self.assertEqual(self.m["size"], 661744)
        self.assertEqual(self.m["sha256"], "ab" * 32)

    def test_the_shipped_reader_accepts_it(self):
        got = ota.fetch_manifest(get=lambda url, timeout=30: _json(self.m))
        self.assertEqual(got["version"], "0.6.0")

    def test_the_daemon_block_is_exactly_these_keys(self):
        self.assertEqual(sorted(self.m["daemon"]),
                         ["artifacts", "auto", "proto", "version"])

    def test_auto_ships_off(self):
        self.assertIs(self.m["daemon"]["auto"], False)

    def test_every_platform_the_updater_asks_for_can_be_published(self):
        """platform_key() and ARTIFACT_KEYS have to agree, or some machine gets
        "no published build" for a build that was in fact published."""
        for key in manifest.ARTIFACT_KEYS:
            found = update.available(self.m, current="0.0.1", key=key)
            self.assertIsNotNone(found, key)

    def test_a_reader_that_predates_the_new_keys_still_works(self):
        """The property that lets this grow: additive fields are ignored, so
        an app from before schema 2 keeps installing firmware."""
        v1 = {k: self.m[k] for k in ("version", "size", "sha256")}
        self.assertIsNotNone(ota.fetch_manifest(
            get=lambda url, timeout=30: _json(v1)))


def _json(obj):
    import json
    return json.dumps(obj).encode()


if __name__ == "__main__":
    unittest.main()
