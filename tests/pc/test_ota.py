import unittest

from pc import ota
from pc.bridge import Bridge


def bridge(manifest=None, firmware=b"", fw_raises=None, flasher=True):
    """A Bridge with the network and the flasher replaced.

    Returns (bridge, sent, flashed) -- `flashed` collects (blob, version)
    tuples so a test can assert what would have been written.
    """
    sent, flashed = [], []

    def fetch_fw():
        if fw_raises:
            raise fw_raises
        return firmware

    b = Bridge(write_msg=sent.append, fetch_usage=lambda: {},
               fetch_manifest=lambda: manifest, fetch_firmware=fetch_fw,
               flash_image=(lambda blob, v: flashed.append((blob, v)))
               if flasher else None)
    return b, sent, flashed


def types(sent):
    return [m["t"] for m in sent]


class TestVersion(unittest.TestCase):
    def test_strictly_newer(self):
        self.assertTrue(ota.is_newer("0.4.9", "0.4.8"))
        self.assertTrue(ota.is_newer("1.0.0", "0.9.9"))

    def test_same_or_older_is_not_newer(self):
        self.assertFalse(ota.is_newer("0.4.8", "0.4.8"))
        self.assertFalse(ota.is_newer("0.4.7", "0.4.8"))

    def test_malformed_never_triggers_an_install(self):
        for bad in ("", "x", "1.2", "1.2.3.4", None):
            self.assertFalse(ota.is_newer(bad, "0.4.8"))
            self.assertFalse(ota.is_newer("0.4.9", bad))


class TestOffer(unittest.TestCase):
    M = {"version": "0.4.9", "size": 600, "sha256": "ab" * 32}

    def test_offers_a_newer_release(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_avail"])
        self.assertEqual(sent[0]["version"], "0.4.9")

    def test_declines_when_board_is_current(self):
        b, sent, _ = bridge(manifest=self.M)
        b.on_message({"t": "ota_query", "cur": "0.4.9"})
        self.assertEqual(types(sent), ["ota_none"])

    def test_declines_when_board_is_ahead_of_the_release(self):
        """Normal during development: the board runs newer than any release."""
        b, sent, _ = bridge(manifest={"version": "0.4.7", "size": 1,
                                      "sha256": "a"})
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_none"])

    def test_unreachable_feed_declines_rather_than_errors(self):
        b, sent, _ = bridge(manifest=None)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        self.assertEqual(types(sent), ["ota_none"])


class TestFlash(unittest.TestCase):
    M = {"version": "0.4.9", "size": 5, "sha256": "ab" * 32}

    def test_approval_flashes_the_downloaded_image(self):
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(flashed, [(b"hello", "0.4.9")])

    def test_flash_without_an_offer_is_refused(self):
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello")
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_error"])
        self.assertEqual(flashed, [])

    def test_size_mismatch_is_never_written(self):
        """slot0 has no auto-revert, so a bad image must not reach it."""
        b, sent, flashed = bridge(manifest=self.M, firmware=b"much longer")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_error"])
        self.assertEqual(flashed, [])

    def test_download_failure_reports_instead_of_raising(self):
        b, sent, flashed = bridge(manifest=self.M, fw_raises=OSError("no route"))
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_error"])
        self.assertEqual(flashed, [])

    def test_missing_flasher_reports_instead_of_crashing(self):
        b, sent, _ = bridge(manifest=self.M, firmware=b"hello", flasher=False)
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(types(sent), ["ota_avail", "ota_error"])

    def test_offer_is_consumed_so_a_repeat_cannot_reflash(self):
        b, sent, flashed = bridge(manifest=self.M, firmware=b"hello")
        b.on_message({"t": "ota_query", "cur": "0.4.8"})
        b.on_message({"t": "ota_flash"})
        b.on_message({"t": "ota_flash"})
        self.assertEqual(len(flashed), 1)


class TestFlashGuards(unittest.TestCase):
    """ota.flash() must not write a chip it cannot vouch for."""

    def _run(self, efuse_out=None, rc=0, fail_probe=False):
        calls = []

        def run(cmd, **kw):
            calls.append(cmd)
            class R:
                returncode = rc
                stdout = efuse_out if "espefuse" in " ".join(cmd) else "ok"
                stderr = ""
            if fail_probe and "espefuse" in " ".join(cmd):
                raise OSError("probe failed")
            return R()
        return run, calls

    def test_refuses_an_encrypted_chip(self):
        run, calls = self._run(efuse_out="FLASH_CRYPT_CNT (BLOCK0) = 1 R/W")
        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertFalse(ok)
        self.assertIn("encryption", why)
        self.assertFalse(any("write_flash" in " ".join(c) for c in calls))

    def test_refuses_when_the_efuses_cannot_be_read(self):
        run, calls = self._run(fail_probe=True)
        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertFalse(ok)
        self.assertIn("eFuses", why)
        self.assertFalse(any("write_flash" in " ".join(c) for c in calls))

    def test_writes_a_plaintext_chip(self):
        run, calls = self._run(efuse_out="FLASH_CRYPT_CNT (BLOCK0) = 0 R/W")
        ok, why = ota.flash("/dev/null", b"x", run=run)
        self.assertTrue(ok, why)
        self.assertTrue(any("write_flash" in " ".join(c) for c in calls))


if __name__ == "__main__":
    unittest.main()
