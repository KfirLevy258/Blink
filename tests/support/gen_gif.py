"""Generated GIF fixtures (spec S15.2): never captured, so no stranger's
picture is in the repo. A small writer of our own rather than Pillow, because
the decoder's features (disposal 0..3 with a transparent index, interlace,
a local palette on one frame, LZW growing to 12 bits, a clear code mid-stream,
a stream that goes on after w*h, a screen header that lies) are exactly what
Pillow will not let a test choose. `reference()` composites the frames by the
GIF rules written independently of pc/gif_decode, as the screen's RGB rows.
"""
from __future__ import annotations

import struct


def lzw_encode(indices, min_size, clear_every=None, extra=b""):
    """GIF LZW. `clear_every`: emit a clear code after that many codes, to
    exercise a clear mid-stream. `extra` indices are encoded after the real
    ones (a stream that keeps producing)."""
    clear, end = 1 << min_size, (1 << min_size) + 1
    out, acc, nb = bytearray(), 0, 0

    def emit(code, size):
        nonlocal acc, nb
        acc |= code << nb
        nb += size
        while nb >= 8:
            out.append(acc & 0xFF)
            acc >>= 8
            nb -= 8

    size, nxt, table = min_size + 1, end + 1, {}
    emit(clear, size)
    data = bytes(indices) + bytes(extra)
    cur, codes, since = None, 0, 0
    for k in data:
        if cur is None:
            cur = k
            continue
        if (cur, k) in table:
            cur = table[(cur, k)]
            continue
        emit(cur, size)
        codes += 1
        since += 1
        # The decoder adds its entry on reading this code, and may widen
        # because of it, so the clear (or the next code) is sent at the
        # widened size even when the encoder adds nothing.
        full = nxt == 4096
        if not full:
            table[(cur, k)] = nxt
        nxt += 1
        if nxt > (1 << size) and size < 12:
            size += 1
        if full or (clear_every and codes % clear_every == 0):
            emit(clear, size)
            table, nxt, size, since = {}, end + 1, min_size + 1, 0
        cur = k
    if cur is not None:
        emit(cur, size)
        if since:
            nxt += 1
            if nxt > (1 << size) and size < 12:
                size += 1
    emit(end, size)
    if nb:
        out.append(acc & 0xFF)
    return bytes(out)


def _blocks(data):
    out = bytearray()
    for i in range(0, len(data), 255):
        part = data[i:i + 255]
        out += bytes([len(part)]) + part
    return bytes(out) + b"\0"


def _table(colors):
    n = 2
    while n < len(colors):
        n *= 2
    bits = n.bit_length() - 2
    raw = b"".join(bytes(c) for c in colors) + bytes(3 * (n - len(colors)))
    return bits, raw


def interlace_order(h):
    return [y for s, st in ((0, 8), (4, 8), (2, 4), (1, 2))
            for y in range(s, h, st)]


def frame(w, h, indices, left=0, top=0, delay_cs=0, disposal=0,
          transparent=None, palette=None, interlace=False, min_size=None,
          clear_every=None, extra=b"", gce=True):
    return dict(w=w, h=h, indices=bytes(indices), left=left, top=top,
                delay_cs=delay_cs, disposal=disposal, transparent=transparent,
                palette=palette, interlace=interlace, min_size=min_size,
                clear_every=clear_every, extra=extra, gce=gce)


def build(sw, sh, palette, frames, version=b"89a", trailer=True):
    bits, raw = _table(palette)
    out = bytearray(b"GIF" + version + struct.pack("<HHBBB", sw, sh,
                                                   0x80 | bits, 0, 0) + raw)
    for f in frames:
        if f["gce"] and version == b"89a":
            flags = (f["disposal"] << 2) | (1 if f["transparent"] is not None else 0)
            out += b"\x21\xf9\x04" + bytes([flags]) + struct.pack(
                "<H", f["delay_cs"]) + bytes([f["transparent"] or 0, 0])
        pal = f["palette"]
        lbits, lraw = _table(pal) if pal else (0, b"")
        iflags = (0x80 | lbits if pal else 0) | (0x40 if f["interlace"] else 0)
        out += b"\x2c" + struct.pack("<HHHHB", f["left"], f["top"], f["w"],
                                     f["h"], iflags) + lraw
        idx = f["indices"]
        if f["interlace"]:
            rows = [idx[y * f["w"]:(y + 1) * f["w"]] for y in range(f["h"])]
            idx = b"".join(rows[y] for y in interlace_order(f["h"]))
        ncol = len(pal) if pal else len(palette)
        ms = f["min_size"] or max(2, (ncol - 1).bit_length())
        out += bytes([ms]) + _blocks(lzw_encode(idx, ms, f["clear_every"],
                                                f["extra"]))
    if trailer:
        out += b"\x3b"
    return bytes(out)


def reference(sw, sh, palette, frames, bg):
    """The expected screen after each frame, as lists of sh rows of 3*sw
    bytes, by the GIF disposal rules."""
    canvas = [list(bg) * sw for _ in range(sh)]
    prev = None                     # (disposal, rect, saved)
    out = []
    for f in frames:
        if prev:
            d, (l, t, w, h), saved = prev
            if d == 2:
                for y in range(t, t + h):
                    canvas[y][3 * l:3 * (l + w)] = list(bg) * w
            elif d == 3:
                canvas = saved
        saved = [r[:] for r in canvas] if f["disposal"] == 3 else None
        pal = f["palette"] or palette
        for y in range(f["h"]):
            for x in range(f["w"]):
                v = f["indices"][y * f["w"] + x]
                if v != f["transparent"]:
                    canvas[f["top"] + y][3 * (f["left"] + x):3 * (f["left"] + x) + 3] = list(pal[v])
        out.append([bytes(r) for r in canvas])
        prev = (f["disposal"], (f["left"], f["top"], f["w"], f["h"]), saved)
    return out
