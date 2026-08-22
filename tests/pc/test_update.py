"""The daemon replacing its own binary.

This is the highest-privilege thing the product does -- a login agent on
someone's machine, overwriting its own executable with bytes from the internet
-- so the tests here are mostly about what must NOT happen.
"""
import hashlib
import json
import os
import unittest

import ecdsa

from pc import update


def keypair():
    sk = ecdsa.SigningKey.generate(curve=ecdsa.NIST256p)
    return sk, sk.get_verifying_key().to_pem().decode()


def sign(sk, raw):
    return sk.sign(raw, hashfunc=hashlib.sha256,
                   sigencode=ecdsa.util.sigencode_der)


class TestPlatformKey(unittest.TestCase):
    def _key(self, system, machine):
        import platform
        real = (platform.system, platform.machine)
        platform.system, platform.machine = lambda: system, lambda: machine
        try:
            return update.platform_key()
        finally:
            platform.system, platform.machine = real

    def test_the_four_published_builds(self):
        self.assertEqual(self._key("Darwin", "arm64"), "macos-arm64")
        self.assertEqual(self._key("Darwin", "x86_64"), "macos-x86_64")
        self.assertEqual(self._key("Linux", "x86_64"), "linux-x86_64")
        self.assertEqual(self._key("Windows", "AMD64"), "windows-x86_64.exe")

    def test_an_intel_process_under_rosetta_stays_intel(self):
        """Keyed off the running process, not the silicon. Replacing an x86_64
        build with an arm64 one is an architecture change, not an update."""
        self.assertEqual(self._key("Darwin", "x86_64"), "macos-x86_64")

    def test_a_platform_we_do_not_publish_for_returns_none(self):
        self.assertIsNone(self._key("Linux", "aarch64"))
        self.assertIsNone(self._key("FreeBSD", "x86_64"))


class TestSignature(unittest.TestCase):
    def test_a_good_signature_verifies(self):
        sk, pub = keypair()
        raw = b'{"version":"0.6.1"}'
        self.assertTrue(update.verify_signature(raw, sign(sk, raw), pub))

    def test_tampered_content_does_not(self):
        sk, pub = keypair()
        raw = b'{"version":"0.6.1"}'
        sig = sign(sk, raw)
        self.assertFalse(update.verify_signature(raw + b" ", sig, pub))

    def test_another_key_does_not(self):
        sk, _ = keypair()
        _, other_pub = keypair()
        raw = b'{"version":"0.6.1"}'
        self.assertFalse(update.verify_signature(raw, sign(sk, raw), other_pub))

    def test_garbage_is_refused_rather_than_raising(self):
        _, pub = keypair()
        self.assertFalse(update.verify_signature(b"x", b"not a signature", pub))

    def test_an_unsigned_feed_yields_no_manifest(self):
        """The whole point: strip the signature and the feed goes dead, rather
        than falling back to trusting whatever arrived."""
        body = json.dumps({"daemon": {"version": "9.9.9"}}).encode()

        def get(url, timeout=30):
            return body if url.endswith("manifest.json") else b"nope"

        self.assertIsNone(update.fetch_signed_manifest(get=get))


class TestAvailable(unittest.TestCase):
    M = {"daemon": {"version": "0.7.0", "artifacts": {
        "macos-arm64": {"size": 10, "sha256": "ab" * 32}}}}

    def test_a_newer_build_for_this_platform(self):
        got = update.available(self.M, current="0.6.0", key="macos-arm64")
        self.assertEqual(got[0], "0.7.0")

    def test_same_or_older_is_not_an_update(self):
        self.assertIsNone(update.available(self.M, "0.7.0", "macos-arm64"))
        self.assertIsNone(update.available(self.M, "0.8.0", "macos-arm64"))

    def test_a_platform_the_release_did_not_publish(self):
        self.assertIsNone(update.available(self.M, "0.6.0", "linux-x86_64"))

    def test_a_manifest_with_no_daemon_block_is_not_an_error(self):
        """Every manifest published before this existed looks like that, and a
        daemon must keep working against the feed it already knows."""
        self.assertIsNone(update.available({"version": "9.9.9"}, "0.6.0",
                                           "macos-arm64"))
        self.assertIsNone(update.available(None, "0.6.0", "macos-arm64"))


class TestDownload(unittest.TestCase):
    def test_size_mismatch_raises(self):
        art = {"size": 99, "sha256": hashlib.sha256(b"abc").hexdigest()}
        with self.assertRaises(ValueError):
            update.download("linux-x86_64", art, get=lambda u, timeout=30: b"abc")

    def test_hash_mismatch_raises(self):
        art = {"size": 3, "sha256": "cd" * 32}
        with self.assertRaises(ValueError):
            update.download("linux-x86_64", art, get=lambda u, timeout=30: b"abc")

    def test_a_matching_download_is_returned(self):
        art = {"size": 3, "sha256": hashlib.sha256(b"abc").hexdigest()}
        self.assertEqual(
            update.download("linux-x86_64", art,
                            get=lambda u, timeout=30: b"abc"), b"abc")


def fake_run(ok=True, version="0.7.0"):
    def run(cmd, **kw):
        class R:
            returncode = 0 if ok else 1
            stdout = f"clauge {version}\n" if ok else ""
            stderr = ""
        return R()
    return run


class TestApply(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="clauge-apply-")
        self.target = os.path.join(self.d, "bin", "clauge")
        os.makedirs(os.path.dirname(self.target))
        with open(self.target, "wb") as f:
            f.write(b"old binary")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_it_replaces_the_binary_and_keeps_a_rollback(self):
        ok, msg = update.apply(b"new binary", self.target, "0.7.0",
                               run=fake_run())
        self.assertTrue(ok, msg)
        self.assertEqual(open(self.target, "rb").read(), b"new binary")
        self.assertEqual(open(self.target + ".old", "rb").read(), b"old binary")
        self.assertFalse(os.path.exists(self.target + ".new"))

    def test_the_replacement_is_executable(self):
        update.apply(b"new binary", self.target, "0.7.0", run=fake_run())
        self.assertTrue(os.access(self.target, os.X_OK))

    def test_a_binary_that_will_not_run_never_becomes_the_target(self):
        """The failure this exists for: a login service pointed at a broken
        binary is a device that never comes back, with nothing on screen to
        explain it. The old binary has to survive."""
        ok, msg = update.apply(b"corrupt", self.target, "0.7.0",
                               run=fake_run(ok=False))
        self.assertFalse(ok)
        self.assertIn("did not run", msg)
        self.assertEqual(open(self.target, "rb").read(), b"old binary")
        self.assertFalse(os.path.exists(self.target + ".new"))

    def test_a_binary_reporting_the_wrong_version_is_refused(self):
        ok, _ = update.apply(b"someone else's build", self.target, "0.7.0",
                             run=fake_run(version="0.5.0"))
        self.assertFalse(ok)
        self.assertEqual(open(self.target, "rb").read(), b"old binary")

    def test_a_first_install_needs_no_previous_binary(self):
        os.remove(self.target)
        ok, _ = update.apply(b"new binary", self.target, "0.7.0",
                             run=fake_run())
        self.assertTrue(ok)
        self.assertEqual(open(self.target, "rb").read(), b"new binary")


class TestRecover(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="clauge-recover-")
        self.target = os.path.join(self.d, "clauge")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_a_missing_binary_is_restored_from_the_rollback(self):
        with open(self.target + ".old", "wb") as f:
            f.write(b"previous")
        self.assertTrue(update.recover(self.target))
        self.assertEqual(open(self.target, "rb").read(), b"previous")

    def test_an_empty_binary_is_treated_as_missing(self):
        open(self.target, "wb").close()
        with open(self.target + ".old", "wb") as f:
            f.write(b"previous")
        self.assertTrue(update.recover(self.target))
        self.assertEqual(open(self.target, "rb").read(), b"previous")

    def test_a_healthy_binary_is_left_alone(self):
        with open(self.target, "wb") as f:
            f.write(b"current")
        with open(self.target + ".old", "wb") as f:
            f.write(b"previous")
        self.assertFalse(update.recover(self.target))
        self.assertEqual(open(self.target, "rb").read(), b"current")

    def test_nothing_to_recover_from_is_not_an_error(self):
        self.assertFalse(update.recover(self.target))


class TestOptOut(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.d = tempfile.mkdtemp(prefix="clauge-optout-")
        self._env = os.environ.pop(update.NO_AUTO_ENV, None)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)
        if self._env is not None:
            os.environ[update.NO_AUTO_ENV] = self._env
        else:
            os.environ.pop(update.NO_AUTO_ENV, None)

    def test_allowed_by_default(self):
        self.assertTrue(update.auto_update_allowed(self.d))

    def test_a_marker_file_turns_it_off(self):
        """A file, not only an environment variable: the daemon is started by
        launchd, where nobody's shell has exported anything."""
        open(os.path.join(self.d, "no-auto-update"), "w").close()
        self.assertFalse(update.auto_update_allowed(self.d))

    def test_the_environment_variable_turns_it_off(self):
        os.environ[update.NO_AUTO_ENV] = "1"
        self.assertFalse(update.auto_update_allowed(self.d))


if __name__ == "__main__":
    unittest.main()
