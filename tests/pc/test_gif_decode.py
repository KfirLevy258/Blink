"""pc/gif_decode against generated fixtures (tests/support/gen_gif.py).

The reference frames come from the generator's own compositor, written from
the GIF rules and not from the decoder; the decoder's rows are letterboxed
onto 320x240, so the expected rows are letterboxed the same way."""
import random
import tracemalloc

import pytest

from pc import gif_decode as gd
from tests.support import gen_gif as gg

BG = (9, 8, 7)
PAL = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)]


def _want(sw, sh, pal, frames):
    return [gd.letterbox(sw, sh, rows, BG)
            for rows in gg.reference(sw, sh, pal, frames, BG)]


def _got(data, max_frames=50):
    return list(gd.decode(data, max_frames, BG))


def _idx(w, h, fn):
    return bytes(fn(x, y) for y in range(h) for x in range(w))


def test_two_colour_87a_still():
    f = gg.frame(8, 6, _idx(8, 6, lambda x, y: (x + y) & 1))
    data = gg.build(8, 6, [(0, 0, 0), (255, 255, 255)], [f], version=b"87a")
    got = _got(data)
    assert [rows for _, rows in got] == _want(8, 6, [(0, 0, 0), (255, 255, 255)], [f])


def test_89a_clip_with_a_local_palette_on_one_frame():
    a = gg.frame(8, 6, _idx(8, 6, lambda x, y: x % 4), delay_cs=10)
    b = gg.frame(8, 6, _idx(8, 6, lambda x, y: y % 3), delay_cs=10,
                 palette=[(200, 100, 50), (1, 2, 3), (4, 5, 6)])
    c = gg.frame(8, 6, _idx(8, 6, lambda x, y: 3), delay_cs=10)
    data = gg.build(8, 6, PAL, [a, b, c])
    assert [r for _, r in _got(data)] == _want(8, 6, PAL, [a, b, c])


def test_interlaced():
    f = gg.frame(5, 19, _idx(5, 19, lambda x, y: y % 4), interlace=True)
    data = gg.build(5, 19, PAL, [f])
    assert _got(data)[0][1] == _want(5, 19, PAL, [f])[0]


@pytest.mark.parametrize("disposal", [0, 1, 2, 3])
def test_each_disposal_method_with_a_transparent_index(disposal):
    base = gg.frame(8, 6, _idx(8, 6, lambda x, y: 1), disposal=1)
    # index 0 is transparent: only the 1s paint
    mid = gg.frame(4, 3, _idx(4, 3, lambda x, y: (x + y) % 2 * 2),
                   left=2, top=1, disposal=disposal, transparent=0)
    last = gg.frame(3, 2, _idx(3, 2, lambda x, y: 3), left=0, top=3,
                    transparent=0, disposal=0)
    frames = [base, mid, last]
    data = gg.build(8, 6, PAL, frames)
    assert [r for _, r in _got(data)] == _want(8, 6, PAL, frames)


def test_delays_are_in_milliseconds():
    fs = [gg.frame(2, 2, bytes(4), delay_cs=cs) for cs in (0, 4, 100)]
    assert [d for d, _ in _got(gg.build(2, 2, PAL, fs))] == [0, 40, 1000]


def test_a_gif_one_frame_over_the_cap_is_refused_before_that_frames_data():
    fs = [gg.frame(2, 2, bytes(4)) for _ in range(3)]
    data = gg.build(2, 2, PAL, fs)
    with pytest.raises(gd.GifError, match="too many frames"):
        _got(data, max_frames=2)
    # cut right after the third descriptor: a decoder that read the data
    # first would say "truncated"
    third = data.rfind(b"\x2c")
    with pytest.raises(gd.GifError, match="too many frames"):
        _got(data[:third + 10], max_frames=2)
    assert len(_got(data, max_frames=3)) == 3


def test_truncated_files():
    data = gg.build(4, 4, PAL, [gg.frame(4, 4, bytes(range(4)) * 4)])
    for cut in (3, 10, len(data) // 2, len(data) - 1):
        with pytest.raises(gd.GifError, match="truncated"):
            _got(data[:cut])


def test_not_a_gif():
    for junk in (b"", b"GIF90a" + bytes(20), b"\x89PNG" + bytes(50)):
        with pytest.raises(gd.GifError):
            _got(junk)
    with pytest.raises(gd.GifError, match="not a GIF"):
        _got(b"PK\x03\x04" + bytes(30))


@pytest.mark.parametrize("clear_every", [None, 400])
def test_lzw_grows_to_12_bits_and_survives_a_clear(clear_every):
    rng = random.Random(5)
    w, h = 120, 100
    idx = bytes(rng.randrange(256) for _ in range(w * h))
    pal = [(i, 255 - i, i // 2) for i in range(256)]
    f = gg.frame(w, h, idx, min_size=8, clear_every=clear_every)
    got = _got(gg.build(w, h, pal, [f]))
    assert got[0][1] == _want(w, h, pal, [f])[0]


def test_an_lzw_stream_that_keeps_producing_stops_at_w_times_h():
    f = gg.frame(4, 4, bytes(range(4)) * 4, extra=bytes(5000))
    got = _got(gg.build(4, 4, PAL, [f]))
    assert got[0][1] == _want(4, 4, PAL, [gg.frame(4, 4, bytes(range(4)) * 4)])[0]


def test_an_lzw_stream_that_ends_short_is_refused():
    f = gg.frame(4, 4, bytes(range(4)) * 3)       # 12 of 16, then the end code
    f["w"], f["h"] = 4, 4
    with pytest.raises(gd.GifError, match="bad LZW"):
        _got(gg.build(4, 4, PAL, [f]))


def test_a_code_past_the_table_is_refused():
    # first code after the clear is 7 with a 2-bit alphabet: not a literal
    data = bytearray(gg.build(2, 2, PAL, [gg.frame(2, 2, bytes(4))]))
    at = data.rfind(b"\x2c") + 10          # min code size, then the sub-block
    data[at + 2:at + 4] = b"\xff\xff"
    with pytest.raises(gd.GifError, match="bad LZW"):
        _got(bytes(data))


def _declares(sw, sh):
    return (b"GIF89a" + sw.to_bytes(2, "little") + sh.to_bytes(2, "little")
            + b"\x80\x00\x00" + bytes(6) + b"\x2c" + bytes(9) + b"\x02\x00")


def test_a_200_byte_gif_declaring_65535_square_allocates_almost_nothing():
    data = _declares(65535, 65535).ljust(200, b"\0")
    tracemalloc.start()
    try:
        with pytest.raises(gd.GifError, match="too large"):
            _got(data)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 1_000_000


def test_size_limits():
    with pytest.raises(gd.GifError, match="too large"):
        _got(_declares(2049, 1))
    # an image past the screen
    f = gg.frame(4, 4, bytes(16), left=6)
    with pytest.raises(gd.GifError, match="too large"):
        _got(gg.build(8, 8, PAL, [f]))
    # an image wider than the cap inside a (lying) wider screen is caught too
    f = gg.frame(2049, 1, bytes(2049))
    with pytest.raises(gd.GifError, match="too large"):
        _got(gg.build(2048, 1, PAL, [f]))


def test_2048_square_screen_with_a_1x1_image_decodes():
    f = gg.frame(1, 1, b"\x01")
    got = _got(gg.build(2048, 2048, PAL, [f]))
    assert len(got) == 1 and len(got[0][1]) == 240


def test_180_frames_of_320x240_stay_under_64_mb():
    # 8 distinct frames cycled: each differs from its predecessor, so no row
    # is shared and all 180 are really held, as a worst case would hold them
    variants = [gg.frame(320, 240, _idx(320, 240, lambda x, y, k=k: ((x // 8 + k) % 4)))
                for k in range(8)]
    data = gg.build(320, 240, PAL, [variants[i % 8] for i in range(180)])
    tracemalloc.start()
    try:
        got = _got(data, max_frames=180)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert len(got) == 180
    assert peak < 64 * 1024 * 1024


def test_a_transparent_2048_frame_composites_at_c_speed():
    """5.8 KB for a 2048x2048 frame with a transparent index: the per-pixel
    loop took 7-10 s a frame. Now it must decode in well under a second."""
    import time
    f = gg.frame(2048, 2048, bytes([0, 1]) * (2048 * 2048 // 2),
                 transparent=0, delay_cs=2)
    data = gg.build(2048, 2048, PAL, [f])
    assert len(data) < 10_000
    t = time.perf_counter()
    got = _got(data)
    assert time.perf_counter() - t < 5.0    # per-pixel was 7-10 s; a loose guard
    assert len(got) == 1


def test_a_transparent_row_keeps_what_is_under_it_and_paints_the_rest():
    f1 = gg.frame(5, 1, bytes([1, 1, 1, 1, 1]), disposal=1)
    f2 = gg.frame(5, 1, bytes([0, 2, 0, 3, 0]), transparent=0)
    got = _got(gg.build(5, 1, PAL, [f1, f2]))
    assert [r for _, r in got] == _want(5, 1, PAL, [f1, f2])


def test_the_pixel_budget_refuses_a_swarm_of_big_frames_fast(monkeypatch):
    """180 frames of 2048x2048 fit the 2 MiB body cap (~1 MB). The budget is
    checked before each frame's data, so the bomb is refused after sixteen
    (16 Mi would give four, no budget 180). Counted in LZW calls, the work
    done, not seconds: a clock bound flakes on a loaded CI machine."""
    import time
    f = gg.frame(2048, 2048, bytes([0, 1]) * (2048 * 2048 // 2),
                 transparent=0, delay_cs=2)
    one = gg.build(2048, 2048, PAL, [f])
    start = one.index(b"\x21\xf9")
    data = one[:start] + one[start:-1] * 180 + b"\x3b"
    assert len(data) < 2 * 1024 * 1024
    calls, real = [], gd._lzw
    monkeypatch.setattr(gd, "_lzw", lambda *a: calls.append(1) or real(*a))
    t = time.perf_counter()
    with pytest.raises(gd.GifError, match="too large"):
        _got(data, max_frames=180)
    assert len(calls) <= 17
    assert time.perf_counter() - t < 10.0       # a hang guard only
    # the panel-sized worst case still fits the budget
    assert 180 * 320 * 240 <= gd.GIF_MAX_PIXELS


def test_an_ordinary_75_frame_640x360_clip_decodes():
    """16 Mi pixels refused a 74-frame 640x360 clip (17 M) as "too large"."""
    fs = [gg.frame(640, 360, bytes([(x // 40 + i) % 4 for x in range(640)]) * 360,
                   delay_cs=4) for i in range(75)]
    data = gg.build(640, 360, PAL, fs)
    assert len(_got(data, max_frames=180)) == 75
    assert 75 * 640 * 360 <= gd.GIF_MAX_PIXELS
