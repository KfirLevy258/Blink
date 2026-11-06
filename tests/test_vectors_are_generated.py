"""tests/wl_crypto_real/vectors.h is what tools/gen_aead_vectors.py writes.

The header is how the board's mbedTLS is held to the PC's pure-Python cipher
at every frame length (tests/wl_crypto_real/host_test.c). A hand edit, or a
change to pc/wifi_crypto.py that moved its output, would leave the C test
checking the board against bytes the PC no longer produces; this regenerates
in memory and compares.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GEN = ROOT / "tools" / "gen_aead_vectors.py"
HEADER = ROOT / "tests" / "wl_crypto_real" / "vectors.h"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_aead_vectors", GEN)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_committed_header_is_what_the_generator_writes():
    assert HEADER.read_text(encoding="utf-8") == _gen().source(), (
        "tests/wl_crypto_real/vectors.h differs from what pc/wifi_crypto.py"
        " produces: run tools/gen_aead_vectors.py")


def test_the_generator_covers_what_the_host_test_counts_on():
    gen = _gen()
    assert gen.PT_MAX == 517            # WL_PT_MAX, spec S6.3
    assert gen.COUNTERS == (0, 1, 1 << 32, (1 << 64) - 2)
    assert len(gen.BAD_LENGTHS) * gen.BAD_PER_LENGTH == 60
