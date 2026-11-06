"""pc/logo_encode: the daemon's own logo encoder and its board-limit checks."""
import random
import struct
import zlib

import pytest

from pc import logo_encode as le
from tests.support import gen_gif as gg
from tests.support import gen_logo_frames as lf

PAL = [(0, 0, 0), (255, 0, 0), (0, 255, 0), (0, 0, 255)]


def _gif(n, delays_cs, sw=320, sh=240):
    fs = []
    for i in range(n):
        idx = bytearray(sw * sh)
        idx[(i * 7) % (sw * sh)] = 1
        for k in range(10):
            idx[(i * 7 + k) % (sw * sh)] = 1 + (i % 3)
        fs.append(gg.frame(sw, sh, bytes(idx),
                           delay_cs=delays_cs[i % len(delays_cs)]))
    return gg.build(sw, sh, PAL, fs)


def _wrap(blob, hold=1000, bg=(0, 0, 0)):
    hdr = (b"BLGO" + struct.pack("<HHII", 1, hold, len(blob), zlib.crc32(blob))
           + bytes(bg) + b"\0").ljust(32, b"\0")
    img = hdr + blob
    return img + b"\xff" * (-len(img) % 32)


def _ban(frames):
    """frames: list of lists of (x, y, w, h, payload_bytes, declared_len)."""
    b = bytearray(b"BAN1" + struct.pack("<HHBBH", 320, 240, 10, 1, len(frames)))
    for rects in frames:
        b.append(len(rects))
        for x, y, w, h, payload, plen in rects:
            b += struct.pack("<HHHHI", x, y, w, h, plen) + payload
    return bytes(b)


def test_from_rgba_round_trips_through_parse():
    rgba = bytearray(320 * 240 * 4)
    for i in range(0, len(rgba), 4):
        rgba[i + 3] = 255
    for y in range(80, 160):
        for x in range(100, 220):
            rgba[(y * 320 + x) * 4:(y * 320 + x) * 4 + 3] = bytes((250, 200, 20))
    img = le.from_rgba(bytes(rgba), (0, 0, 0), 5000)
    info = le.parse(img, frames=True)
    assert info["hold_ms"] == 5000 and info["nframes"] == 1 and info["fps"] == 1
    assert info["frames"][0][100 * 320 + 150] == (250 >> 3) << 11 | (200 >> 2) << 5 | 20 >> 3
    assert info["frames"][0][0] == 0
    assert len(img) % 32 == 0


def test_from_rgba_composites_alpha_over_the_background():
    rgba = bytes((255, 255, 255, 0)) * (320 * 240)
    img = le.from_rgba(rgba, (80, 160, 240), 3000)
    f = le.parse(img, frames=True)["frames"][0]
    assert f[0] == (80 >> 3) << 11 | (160 >> 2) << 5 | 240 >> 3
    assert le.parse(img)["bg"] == le._bg565((80, 160, 240))


def test_hold_and_fit_are_validated():
    rgba = bytes(320 * 240 * 4)
    with pytest.raises(ValueError):
        le.from_rgba(rgba, (0, 0, 0), 30000)
    data = _gif(1, [10])
    with pytest.raises(ValueError):
        le.from_gif(data, (0, 0, 0), 30000, "fill")
    with pytest.raises(ValueError):
        le.from_gif(data, (0, 0, 0), 3000, "huge")
    with pytest.raises(ValueError):
        le.from_rgba(rgba[:-1], (0, 0, 0), 3000)
    flat = gg.build(320, 240, [(255, 0, 0), (0, 0, 0)],
                    [gg.frame(320, 240, bytes(320 * 240))])
    f = le.parse(le.from_gif(flat, (9, 8, 7), 3000, "margin"),
                 frames=True)["frames"][0]
    bg = ((9 >> 3) << 11) | ((8 >> 2) << 5) | (7 >> 3)
    mid = 120 * 320
    assert f[mid] == bg and f[mid + 63] == bg and f[mid + 64] == 0xF800
    assert f[mid + 255] == 0xF800 and f[mid + 256] == bg
    # the band is vertical too: 0.6 * 240 = 144, so 48 rows above and below
    assert f[47 * 320 + 160] == bg and f[48 * 320 + 160] == 0xF800


def test_from_gif_round_trips():
    info = le.parse(le.from_gif(_gif(12, [10]), (0, 0, 0), 3000, "fill"))
    assert (info["fps"], info["nframes"], len(info["rects"])) == (10, 12, 12)
    info = le.parse(le.from_gif(_gif(3, [10, 20, 10][:3]), (0, 0, 0), 3000, "fill"))
    assert info["fps"] == 10 and info["nframes"] == 4
    assert info["rects"][2] == 0 and 0 not in info["rects"][:2] + info["rects"][3:]
    assert info["hold_ms"] == 3000


def _oracle(delays):
    ds = [100 if d < 20 else d for d in delays]
    best = None
    for fps in range(1, 61):
        tick = 1000 // fps
        ticks = [max(1, round(d / tick)) for d in ds]
        err = sum(abs(d - n * tick) for d, n in zip(ds, ticks))
        if best is None or err < best[0]:
            best = (err, fps, ticks)
    return best[1], best[2]


def test_gif_timing_uses_the_players_integer_tick():
    assert le.gif_timing([30]) == (33, [1])
    assert le.gif_timing([10, 0]) == (10, [1, 1])
    assert le.gif_timing([40, 80]) == (25, [1, 2])
    assert le.gif_timing([30, 100, 1000]) == (39, [1, 4, 40])
    assert le.gif_timing([3000]) == (1, [3])
    rng = random.Random(3)
    for _ in range(200):
        ds = [rng.choice([0, 10, 20, 30, 40, 50, 70, 100, 130, 250, 1000])
              + rng.randrange(0, 15) for _ in range(rng.randrange(1, 9))]
        assert le.gif_timing(ds) == _oracle(ds), ds
    fps, _ = le.gif_timing([30, 100, 1000])
    struct.pack("B", fps)
    err = sum(abs(d - n * (1000 // fps)) for d, n in zip([30, 100, 1000], [1, 4, 40]))
    assert err == 5


def test_each_board_limit_is_refused_before_send(monkeypatch):
    frame = [0] * (320 * 240)
    with pytest.raises(ValueError, match=le.E_FRAMES):
        le.build([frame] * (le.LOGO_MAX_FRAMES + 1), 60, 1000, (0, 0, 0))
    for bad in (0, 61):
        with pytest.raises(ValueError, match=le.E_FPS):
            le.build([frame], bad, 1000, (0, 0, 0))
    with pytest.raises(ValueError, match=le.E_TOTAL):
        le.build([frame] * 180, 12, 1000, (0, 0, 0))
    le.build([frame] * 180, 12, 0, (0, 0, 0))               # 15000 exactly fits
    # a frame with two changed bands, with the cap lowered to one
    a, b = list(frame), list(frame)
    b[10 * 320 + 5] = b[200 * 320 + 9] = 1
    monkeypatch.setattr(le, "LOGO_MAX_RECTS", 1)
    with pytest.raises(ValueError, match=le.E_RECTS):
        le.build([a, b], 10, 1000, (0, 0, 0))
    monkeypatch.undo()
    # hand-built blobs the encoder would never write
    run2 = bytes((128,)) + b"\x00\x01"
    ok = _wrap(_ban([[(0, 0, 320, 240, b"", 0)]]))
    with pytest.raises(ValueError):
        le.parse(ok)                                   # payload walks nothing
    with pytest.raises(ValueError, match=le.E_RECT):
        le.parse(_wrap(_ban([[(319, 0, 2, 1, run2, 3)]])))
    with pytest.raises(ValueError, match=le.E_PAYLOAD):
        le.parse(_wrap(_ban([[(0, 0, 2, 1, run2[:2], 2)]])))   # short by a byte
    with pytest.raises(ValueError, match=le.E_PAYLOAD):
        le.parse(_wrap(_ban([[(0, 0, 2, 1, run2 + b"\0", 4)]])))  # trailing byte
    with pytest.raises(ValueError, match=le.E_PAYLOAD):
        le.parse(_wrap(_ban([[(0, 0, 1, 1, run2, 3)]])))       # run crosses w*h
    assert le.parse(_wrap(_ban([[(0, 0, 2, 1, run2, 3)]])))["nframes"] == 1
    # over LOGO_MAX: noise does not compress
    rng = random.Random(9)
    noise = [[rng.getrandbits(16) for _ in range(320 * 240)] for _ in range(5)]
    with pytest.raises(ValueError, match=le.E_SIZE):
        le.build(noise, 10, 1000, (0, 0, 0))


def test_a_flipped_byte_or_a_wrong_crc_is_refused():
    img = bytearray(le.from_rgba(bytes((1, 2, 3, 255)) * (320 * 240), (0, 0, 0), 3000))
    img[40] ^= 1
    with pytest.raises(ValueError):
        le.parse(bytes(img))


def test_letterbox_nearest():
    rows = [bytes((10, 0, 0, 20, 0, 0)), bytes((30, 0, 0, 40, 0, 0))]
    out = le.letterbox(2, 2, rows, (7, 7, 7))
    assert len(out) == 240 and all(len(r) == 960 for r in out)
    # 2x2 onto 320x240 -> 240x240, 40 px of bg either side
    assert out[0][:3 * 40] == bytes((7, 7, 7)) * 40
    assert out[0][3 * 40:3 * 41] == bytes((10, 0, 0))
    assert out[0][3 * 159:3 * 160] == bytes((10, 0, 0))
    assert out[0][3 * 160:3 * 161] == bytes((20, 0, 0))
    assert out[120][3 * 40:3 * 41] == bytes((30, 0, 0))
    assert out[239][-3:] == bytes((7, 7, 7))
    with pytest.raises(ValueError):
        le.letterbox(2, 2, rows[:1], (0, 0, 0))


def test_the_gate_decides_on_rgb888_before_conversion():
    black, r10, r11 = (lf.solid(c) for c in ((0, 0, 0), (10, 0, 0), (11, 0, 0)))
    shown = le.quantise([black, r10, r11], 10)
    assert shown[1] == black and shown[2] == r11
    assert le.to_rgb565(r10) == le.to_rgb565(r11)       # one RGB565 value


def test_a_repeated_frame_is_a_zero_rect_record():
    fr = [le.to_rgb565(f) for f in lf.with_repeat(5)]
    info = le.parse(le.build(fr, 10, 500, (10, 20, 30)))
    assert info["rects"][3] == 0 and info["rects"][0] == 1
    assert info["max_rects"] == 2
