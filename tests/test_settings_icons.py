"""The Settings icons in firmware/src are exactly what the committed
generator draws.

settings_icons.c is 3 KB of hex nobody can review by eye. Without this, a
hand edit to it, or a change to tools/gen_settings_icons.py that was never
run, would ship pixels that no committed code produces -- which is the one
thing the generator exists to rule out.
"""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "gen_settings_icons.py"
SRC = ROOT / "firmware" / "src"


def _gen():
    spec = importlib.util.spec_from_file_location("gen_settings_icons", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_committed_icons_are_what_the_generator_draws():
    c, h = _gen().sources()
    assert (SRC / "settings_icons.c").read_text(encoding="utf-8") == c, (
        "settings_icons.c differs: run tools/gen_settings_icons.py")
    assert (SRC / "settings_icons.h").read_text(encoding="utf-8") == h, (
        "settings_icons.h differs: run tools/gen_settings_icons.py")


def test_each_icon_is_a_28_px_alpha_mask_with_something_drawn():
    gen = _gen()
    for name, fn in gen.ICONS:
        px = gen.render(fn)
        assert len(px) == 28 * 28, name
        assert all(0 <= v <= 255 for v in px), name
        # Some ink, some ground, and antialiased edges between them.
        assert 0.05 < sum(v == 255 for v in px) / len(px) < 0.6, name
        assert any(0 < v < 255 for v in px), name


def test_no_sample_sits_close_enough_to_an_edge_for_a_last_bit_to_flip_it():
    """The determinism argued in the generator's comment, checked. A pixel
    counts the samples whose distance is <= 0, so platform maths could only
    change the output at a sample within a rounding error of an edge. The
    nearest measured on 2026-10-28 was 1.26e-3, some twelve orders of
    magnitude clear of where a last-place difference lives."""
    gen = _gen()
    n = gen.SIZE * gen.SS
    for name, fn in gen.ICONS:
        nearest = min(abs(fn((k % n + 0.5) / gen.SS, (k // n + 0.5) / gen.SS))
                      for k in range(n * n))
        assert nearest > 1e-9, (name, nearest)
