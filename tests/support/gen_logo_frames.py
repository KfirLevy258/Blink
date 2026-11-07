"""Generated RGB888 frame sets for the logo encoder tests (never captured).
Frames are lists of 240 rows of 960 bytes, the shape pc/gif_decode yields."""
from __future__ import annotations

W, H = 320, 240


def solid(rgb):
    return [bytes(rgb) * W for _ in range(H)]


def with_block(base, x, y, w, h, rgb):
    rows = list(base)
    for yy in range(y, y + h):
        r = bytearray(rows[yy])
        r[3 * x:3 * (x + w)] = bytes(rgb) * w
        rows[yy] = bytes(r)
    return rows


def still(bg=(0, 0, 0)):
    return [with_block(solid(bg), 100, 80, 120, 80, (250, 200, 20))]


def clip(n=12, bg=(10, 20, 30)):
    """Motion in two bands: a block along the top, another near the bottom."""
    out = []
    for i in range(n):
        f = with_block(solid(bg), 10 + 20 * i, 20, 30, 20, (255, 0, 0))
        f = with_block(f, 300 - 20 * i, 200, 16, 16, (0, 255, 0))
        out.append(f)
    return out


def with_repeat(n=6):
    c = clip(n)
    return c[:3] + [c[2]] + c[3:]
