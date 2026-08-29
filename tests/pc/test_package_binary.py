"""tools/package_binary.py and pc/update.unpack() are two halves of one
contract: what the release feed serves is what the daemon can put in place.
This round-trips a bundle shaped like the macOS one -- an executable, a
support file, a symlinked directory and a symlinked file -- because the
first packager listed regular files only, and every symlink in
Python.framework came out dangling (2026-08-29)."""
import os
import subprocess
import sys
import tempfile
import unittest

from pc import update

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PACKAGER = os.path.join(ROOT, "tools", "package_binary.py")


def make_bundle(root):
    os.makedirs(os.path.join(root, "_internal", "Fw", "Versions", "3.11"))
    with open(os.path.join(root, "blink"), "w") as f:
        f.write("#!/bin/sh\necho blink 9.9.9\n")
    os.chmod(os.path.join(root, "blink"), 0o755)
    with open(os.path.join(root, "_internal", "Fw", "Versions", "3.11", "lib"), "w") as f:
        f.write("lib")
    os.symlink("3.11", os.path.join(root, "_internal", "Fw", "Versions", "Current"))
    os.symlink("Versions/Current/lib", os.path.join(root, "_internal", "Fw", "lib"))


@unittest.skipIf(sys.platform == "win32", "symlinks are a POSIX bundle's shape")
class TestRoundTrip(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="blink-pkg-")
        self.src = os.path.join(self.d, "blink")
        make_bundle(self.src)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.d, ignore_errors=True)

    def test_symlinks_survive_packaging_and_unpacking(self):
        out = subprocess.run([sys.executable, PACKAGER, "linux-x86_64", self.src, self.d],
                             capture_output=True, text=True, check=True).stdout.strip()
        self.assertTrue(out.endswith("blink-linux-x86_64.tar.gz"))
        into = os.path.join(self.d, "unpacked")
        update.unpack(open(out, "rb").read(), into)
        cur = os.path.join(into, "_internal", "Fw", "Versions", "Current")
        self.assertTrue(os.path.islink(cur))
        self.assertEqual(os.readlink(cur), "3.11")
        # The chain resolves: the file is reachable through both links.
        self.assertEqual(open(os.path.join(into, "_internal", "Fw", "lib")).read(), "lib")
        self.assertTrue(os.access(os.path.join(into, "blink"), os.X_OK))

    def test_a_symlink_pointing_outside_is_refused(self):
        import io
        import tarfile
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as t:
            info = tarfile.TarInfo("blink/escape")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            t.addfile(info)
        with self.assertRaises(ValueError):
            update.unpack(buf.getvalue(), os.path.join(self.d, "x"))
        self.assertFalse(os.path.lexists(os.path.join(self.d, "x", "escape")))
