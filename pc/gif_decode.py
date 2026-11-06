"""A GIF decoder for the company logo, standard library only (spec S15.2).

The settings page posts the owner's GIF as the file's own bytes, up to 2 MiB,
because a browser does not expose every frame of one. That makes this the one
parser in the daemon whose input is a stranger's file, so every size a header
declares is checked against a constant before anything is allocated for it:
a 200-byte GIF can declare a 65535x65535 screen, and one RGB canvas for that
is 12 GiB. Nothing here recurses; every loop is bounded by the length of
`data`, by GIF_MAX_SIDE or by `max_frames`.

What is decoded: GIF87a and 89a, LZW with code-size growth and clear codes,
global and local palettes, interlace, the Graphic Control Extension's four
disposal methods, the transparency index and the per-frame delay. Looping is
ignored (one pass). Every other extension is skipped unread.

Memory: ONE canvas at the logical-screen size, plus a copy of it only for a
frame whose disposal is "restore to previous". Each composited frame is
letterboxed onto 320x240 at once and yielded as rows, so what a caller keeps
is the 320x240 frames, at most `max_frames` of them (about 230 KB each, 41 MB
for 180, the worst case the frame cap bounds). A row identical to the same row
of the previous frame is the same object, so a mostly still clip costs far
less than that.

Never logs. An error says what is wrong with the file, never a byte of it.
"""
from __future__ import annotations

import operator
import struct

GIF_MAX_SIDE = 2048
# Sum of w*h over every frame's image descriptor. The side cap bounds one
# frame (4 M pixels), not the file: a 2 MiB GIF holds ~360 frames of that (a
# 2048x2048 two-colour frame is 5.8 KB). 64 Mi pixels is 16 such frames, which
# decode in 0.83 s with C-speed compositing (measured), and it lets an ordinary
# clip through: a 74-frame 640x360 GIF is 17 M, and 16 Mi refused it. The panel-
# sized worst case, 180 frames of 320x240, is 13.8 M.
GIF_MAX_PIXELS = 64 * 1024 * 1024
CANVAS_W, CANVAS_H = 320, 240

_NOT_GIF, _BIG, _MANY = "not a GIF", "too large", "too many frames"
_LZW, _CUT = "bad LZW", "truncated"


class GifError(ValueError):
    pass


def _fit_rows(get_row, w, h, bg, scale):
    """Nearest-neighbour fit of a w x h RGB888 picture onto 320x240, centred
    on `bg`. `get_row(y)` gives source row y as bytes-like of 3*w bytes."""
    k = min(CANVAS_W / w, CANVAS_H / h) * scale
    nw = min(CANVAS_W, max(1, round(w * k)))
    nh = min(CANVAS_H, max(1, round(h * k)))
    if (w, h, nw, nh) == (CANVAS_W, CANVAS_H, CANVAS_W, CANVAS_H):
        return [bytes(get_row(y)) for y in range(h)]
    x0, y0 = (CANVAS_W - nw) // 2, (CANVAS_H - nh) // 2
    pad = bytes(bg)
    band = pad * CANVAS_W
    left, right = pad * x0, pad * (CANVAS_W - x0 - nw)
    idx = []
    for dx in range(nw):
        sx = (2 * dx + 1) * w // (2 * nw)
        idx += (3 * sx, 3 * sx + 1, 3 * sx + 2)
    pick = operator.itemgetter(*idx) if len(idx) > 1 else None
    out = [band] * CANVAS_H
    last_sy, last = -1, None
    for dy in range(nh):
        sy = (2 * dy + 1) * h // (2 * nh)
        if sy != last_sy:
            src = get_row(sy)
            body = bytes(pick(src)) if pick else bytes(src[idx[0]:idx[0] + 3])
            last, last_sy = left + body + right, sy
        out[y0 + dy] = last
    return out


def letterbox(w, h, rows, bg, scale=1.0):
    """`rows`: h byte strings of 3*w bytes. Returns 240 rows of 960 bytes.
    `scale` shrinks the fitted picture (the page's "Size on the screen")."""
    if w < 1 or h < 1 or len(rows) != h or any(len(r) != 3 * w for r in rows):
        raise ValueError("rows do not match the size given")
    return _fit_rows(rows.__getitem__, w, h, bg, scale)


class _Reader:
    def __init__(self, data):
        self.d, self.p = data, 0

    def take(self, n):
        if self.p + n > len(self.d):
            raise GifError(_CUT)
        b = self.d[self.p:self.p + n]
        self.p += n
        return b

    def byte(self):
        return self.take(1)[0]

    def blocks(self):
        """The data sub-blocks up to the zero terminator, joined."""
        parts = []
        while True:
            n = self.byte()
            if n == 0:
                return b"".join(parts)
            parts.append(self.take(n))

    def skip_blocks(self):
        while True:
            n = self.byte()
            if n == 0:
                return
            self.take(n)


def _palette(raw):
    """Three 256-entry translate tables (R, G, B); an index past the table
    is black."""
    rt, gt, bt = bytearray(256), bytearray(256), bytearray(256)
    for i in range(len(raw) // 3):
        rt[i], gt[i], bt[i] = raw[3 * i], raw[3 * i + 1], raw[3 * i + 2]
    return bytes(rt), bytes(gt), bytes(bt)


def _lzw(data, min_size, limit):
    """Expand to exactly `limit` indices, stopping the moment it has them: a
    stream that goes on producing is not read further, and one that ends
    short is refused."""
    if not 2 <= min_size <= 8:
        raise GifError(_LZW)
    clear = 1 << min_size
    end = clear + 1
    base = [bytes((i,)) for i in range(clear)] + [b"", b""]
    table = list(base)
    size = min_size + 1
    out = bytearray()
    prev = None
    acc = nbits = pos = 0
    n = len(data)
    while len(out) < limit:
        while nbits < size:
            if pos >= n:
                raise GifError(_LZW)
            acc |= data[pos] << nbits
            pos += 1
            nbits += 8
        code = acc & ((1 << size) - 1)
        acc >>= size
        nbits -= size
        if code == clear:
            table = list(base)
            size = min_size + 1
            prev = None
            continue
        if code == end:
            break
        if prev is None:
            if code >= clear:
                raise GifError(_LZW)
            entry = table[code]
        else:
            nxt = len(table)
            if code < nxt:
                entry = table[code]
            elif code == nxt:
                entry = prev + prev[:1]
            else:
                raise GifError(_LZW)
            if nxt < 4096:
                table.append(prev + entry[:1])
                if nxt + 1 == (1 << size) and size < 12:
                    size += 1
        out += entry
        prev = entry
    if len(out) < limit:
        raise GifError(_LZW)
    return bytes(out[:limit])


_MASKS = [bytes(255 if i == t else 0 for i in range(256)) for t in range(256)]
_PASSES = ((0, 8), (4, 8), (2, 4), (1, 2))


def _row_order(h, interlaced):
    if not interlaced:
        return range(h)
    return [y for start, step in _PASSES for y in range(start, h, step)]


def decode(data, max_frames, bg, scale=1.0):
    """Yield (delay_ms, rows) per frame: rows letterboxed onto 320x240
    RGB888 (240 byte strings of 960). `bg` fills the letterbox, the first
    canvas and "restore to background"; `scale` as in letterbox().
    GifError (a ValueError) on anything wrong, raised when the offending
    frame is reached."""
    r = _Reader(data)
    if r.take(6) not in (b"GIF87a", b"GIF89a"):
        raise GifError(_NOT_GIF)
    sw, sh, flags, _bgi, _asp = struct.unpack("<HHBBB", r.take(7))
    if sw > GIF_MAX_SIDE or sh > GIF_MAX_SIDE:
        raise GifError(_BIG)
    if sw == 0 or sh == 0:
        raise GifError(_NOT_GIF)
    gpal = _palette(r.take(3 * (2 << (flags & 7)))) if flags & 0x80 else None
    pad = bytes(bg)
    canvas = None
    count = pixels = 0
    delay = disposal = 0
    transp = None
    pending = None              # (disposal, rect, saved canvas) of the last frame
    shown = None
    stride = 3 * sw
    while True:
        b = r.byte()
        if b == 0x3B:
            if count == 0:
                raise GifError(_CUT)
            return
        if b == 0x21:
            label = r.byte()
            if label == 0xF9:
                body = r.blocks()
                if len(body) >= 4:
                    g, d, ti = body[0], body[1] | body[2] << 8, body[3]
                    disposal = (g >> 2) & 7
                    delay = d * 10
                    transp = ti if g & 1 else None
            else:
                r.skip_blocks()
            continue
        if b != 0x2C:
            raise GifError(_NOT_GIF)
        # Bounds first, then the frame count, both before this frame's data.
        left, top, w, h, iflags = struct.unpack("<HHHHB", r.take(9))
        if w > GIF_MAX_SIDE or h > GIF_MAX_SIDE:
            raise GifError(_BIG)
        if w == 0 or h == 0 or left + w > sw or top + h > sh:
            raise GifError(_BIG)
        if count >= max_frames:
            raise GifError(_MANY)
        pixels += w * h
        if pixels > GIF_MAX_PIXELS:
            raise GifError(_BIG)
        count += 1
        pal = _palette(r.take(3 * (2 << (iflags & 7)))) if iflags & 0x80 else gpal
        if pal is None:
            raise GifError(_NOT_GIF)
        min_size = r.byte()
        idx = _lzw(r.blocks(), min_size, w * h)

        if canvas is None:
            canvas = bytearray(pad * (sw * sh))
        if pending is not None:             # dispose of the frame shown last
            pd, (pl, pt, pw, ph), saved = pending
            if pd == 2:
                fill = pad * pw
                for y in range(pt, pt + ph):
                    canvas[y * stride + 3 * pl:y * stride + 3 * (pl + pw)] = fill
            elif pd == 3 and saved is not None:
                canvas = saved
            pending = None
        saved = bytearray(canvas) if disposal == 3 else None

        rt, gt, bt = pal
        for sy, y in enumerate(_row_order(h, iflags & 0x40)):
            row = idx[sy * w:(sy + 1) * w]
            o = y + top
            a, z = o * stride + 3 * left, o * stride + 3 * (left + w)
            if transp is not None and transp in row:
                # At C speed: a transparent index marks pixels to keep. The
                # mask is 0xFF where the frame is transparent, widened to 3
                # bytes a pixel, and the two rows combined as big integers
                # (a per-pixel loop took 7-10 s for one 2048x2048 frame).
                m = row.translate(_MASKS[transp])
                m3 = bytearray(3 * w)
                m3[0::3] = m3[1::3] = m3[2::3] = m
                new = bytearray(3 * w)
                new[0::3] = row.translate(rt)
                new[1::3] = row.translate(gt)
                new[2::3] = row.translate(bt)
                mi = int.from_bytes(m3, "big")
                full = (1 << (24 * w)) - 1
                keep = (int.from_bytes(canvas[a:z], "big") & mi) | (
                    int.from_bytes(new, "big") & (mi ^ full))
                canvas[a:z] = keep.to_bytes(3 * w, "big")
            else:
                new = bytearray(3 * w)
                new[0::3] = row.translate(rt)
                new[1::3] = row.translate(gt)
                new[2::3] = row.translate(bt)
                canvas[a:z] = new

        rows = _fit_rows(lambda y: canvas[y * stride:(y + 1) * stride],
                         sw, sh, bg, scale)
        if shown is not None:
            rows = [p if p == q else q for p, q in zip(shown, rows)]
        shown = rows
        pending = (disposal, (left, top, w, h), saved)
        yield delay, rows
        delay = disposal = 0
        transp = None
