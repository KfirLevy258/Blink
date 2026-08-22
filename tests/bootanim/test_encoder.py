"""Round-trip tests for the BAN1 boot-animation encoder.

Run: ~/zephyr-v4.4.0/.venv/bin/python3 tests/bootanim/test_encoder.py -v
Uses synthetic frames so neither the mp4 nor ffmpeg is needed.
"""
import sys
import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
import encode_bootanim as enc


def synth_frames():
    """3 frames, 16x12: noisy static field with a 3x3 square moving 2 px
    right. The noise makes the keyframe expensive (literals) while deltas
    stay confined to the square -- the property the size test asserts."""
    rng = np.random.default_rng(42)
    base = rng.integers(0, 0x10000, (12, 16), dtype=np.uint16)
    frames = np.repeat(base[None, :, :], 3, axis=0)
    for i in range(3):
        frames[i, 4:7, 2 + 2 * i:5 + 2 * i] = 0xBEEF
    return list(frames)


class TestPackBits(unittest.TestCase):
    def roundtrip(self, px):
        out = enc.unpackbits16(enc.packbits16(px), len(px))
        self.assertTrue((out == px).all())

    def test_long_run(self):
        self.roundtrip(np.full(1000, 0xABCD, dtype=np.uint16))

    def test_all_literals(self):
        self.roundtrip(np.arange(300, dtype=np.uint16))

    def test_mixed(self):
        self.roundtrip(np.array([1, 1, 1, 2, 3, 4, 4, 4, 4, 5],
                                dtype=np.uint16))

    def test_boundary_run_lengths(self):
        for n in (2, 129, 130, 258):
            self.roundtrip(np.full(n, 7, dtype=np.uint16))


class TestEncodeDecode(unittest.TestCase):
    def test_round_trip_small_canvas(self):
        frames = synth_frames()
        blob, per_frame = enc.encode(frames, 12, big_endian=True)
        dec, fps = enc.decode(blob)
        self.assertEqual(fps, 12)
        self.assertEqual(len(dec), 3)
        for a, b in zip(dec, frames):
            self.assertTrue((a == b).all())

    def test_deltas_smaller_than_keyframe(self):
        blob, per_frame = enc.encode(synth_frames(), 12, big_endian=True)
        self.assertLess(per_frame[1], per_frame[0] / 4)

    def test_single_frame_blob(self):
        frames = synth_frames()
        last, _ = enc.encode([frames[-1]], 12, big_endian=True)
        dec, _ = enc.decode(last)
        self.assertEqual(len(dec), 1)
        self.assertTrue((dec[0] == frames[-1]).all())


class TestEmitHeader(unittest.TestCase):
    def test_header_contains_both_blobs_and_bg(self):
        frames = synth_frames()
        blob, _ = enc.encode(frames, 12, big_endian=True)
        last, _ = enc.encode([frames[-1]], 12, big_endian=True)
        # A NamedTemporaryFile handed to another writer while still open is a
        # Windows error (the file cannot be opened twice), not a bug in
        # emit_header. Write, close, then read.
        d = tempfile.mkdtemp()
        try:
            path = os.path.join(d, "bootanim.h")
            enc.emit_header(path, blob, last, 12, 3, 0xD14A24, "test-cmdline")
            with open(path) as f:
                text = f.read()
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.assertIn(f"bootanim_blob[{len(blob)}]", text)
        self.assertIn(f"bootanim_last[{len(last)}]", text)
        self.assertIn("#define BOOTANIM_BG_RGB 0xd14a24", text)
        self.assertIn("test-cmdline", text)


if __name__ == "__main__":
    unittest.main()
