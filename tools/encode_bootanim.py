#!/usr/bin/env python3
"""Encode a video clip into the BAN1 boot-animation blob for the CYD firmware.

Pipeline: ffmpeg trims/scales the clip to the video area, frames are composed
onto a full 320x240 canvas (letterbox bars filled with the clip's own
background color), quantized to RGB565, then delta-encoded: frame 0 is the
full canvas, every later frame stores only the rectangles that changed,
PackBits16-compressed. The h264 source shimmers a little frame to frame, so a
pixel only counts as changed when it moves more than --threshold per channel
against what is currently *displayed* -- otherwise deltas would light up the
whole flat background every frame.

Blob layout (header fields little-endian, pixels big-endian unless
--little-endian-pixels): the tool emits two blobs (`bootanim_blob`,
`bootanim_last`) — the second is a standalone one-frame BAN1 blob of the final
frame for the skip path.

  "BAN1"  u16 width  u16 height  u8 fps  u8 flags(bit0=BE pixels)  u16 nframes
  per frame:
    u8 rect_count          (0 = hold previous frame)
    per rect:
      u16 x  u16 y  u16 w  u16 h  u32 payload_len
      payload: PackBits16 -- ctrl byte c, then
        c < 128:  c+1 literal pixels (2 bytes each)
        c >= 128: one pixel repeated c-126 times (2..129)

Typical use:
  tools/encode_bootanim.py --input ~/Downloads/claude_intro.mp4 \
      --start 2.4 --duration 4.0 --preview /tmp/anim.gif \
      --out firmware/src/bootanim.h
"""

import argparse
import struct
import subprocess
import sys

import numpy as np
from PIL import Image

CANVAS_W, CANVAS_H = 320, 240


def extract_frames(path, start, duration, fps, width, height):
    """Decode the clip via ffmpeg into (N, H, W, 3) uint8."""
    cmd = [
        "ffmpeg", "-v", "error",
        "-ss", str(start), "-t", str(duration),
        "-i", path,
        "-vf", f"fps={fps},scale={width}:{height}",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-",
    ]
    raw = subprocess.run(cmd, check=True, capture_output=True).stdout
    frame_bytes = width * height * 3
    n = len(raw) // frame_bytes
    if n == 0:
        sys.exit("ffmpeg produced no frames -- check --start/--duration")
    return np.frombuffer(raw[: n * frame_bytes], dtype=np.uint8).reshape(
        n, height, width, 3).copy()


def to_rgb565(rgb):
    """(..., 3) uint8 -> (...) uint16 RGB565."""
    r = rgb[..., 0].astype(np.uint16)
    g = rgb[..., 1].astype(np.uint16)
    b = rgb[..., 2].astype(np.uint16)
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def from_rgb565(p):
    """(...) uint16 RGB565 -> (..., 3) uint8, bit-replicated expansion."""
    r = ((p >> 11) & 0x1F).astype(np.uint8)
    g = ((p >> 5) & 0x3F).astype(np.uint8)
    b = (p & 0x1F).astype(np.uint8)
    return np.stack(
        [(r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2)],
        axis=-1)


def packbits16(pixels):
    """PackBits over uint16 pixels; returns bytes (pixel order set later)."""
    out = bytearray()
    i, n = 0, len(pixels)
    while i < n:
        # Measure the run starting here.
        run = 1
        while i + run < n and run < 129 and pixels[i + run] == pixels[i]:
            run += 1
        if run >= 2:
            out.append(128 + run - 2)
            out += struct.pack(">H", pixels[i])
            i += run
            continue
        # Literal: gather until the next run of >=3 (a 2-run mid-literal is
        # cheaper left literal) or the 128-pixel cap.
        j = i + 1
        while j < n and j - i < 128:
            if j + 2 < n and pixels[j] == pixels[j + 1] == pixels[j + 2]:
                break
            j += 1
        out.append(j - i - 1)
        for p in pixels[i:j]:
            out += struct.pack(">H", p)
        i = j
    return bytes(out)


def unpackbits16(data, count):
    """Inverse of packbits16, for the round-trip preview/test."""
    out = np.empty(count, dtype=np.uint16)
    i = w = 0
    while w < count:
        c = data[i]
        i += 1
        if c < 128:
            n = c + 1
            out[w:w + n] = np.frombuffer(data[i:i + 2 * n], dtype=">u2")
            i += 2 * n
        else:
            n = c - 126
            out[w:w + n] = struct.unpack(">H", data[i:i + 2])[0]
            i += 2
        w += n
    return out


def changed_rects(mask):
    """Split a change mask into rects: contiguous changed-row bands, each
    cropped to its changed columns. Two eyes usually land in one band; the
    corner sparkle gets its own instead of inflating the eye rect."""
    rows = np.flatnonzero(mask.any(axis=1))
    if rows.size == 0:
        return []
    rects = []
    band_start = prev = rows[0]
    for y in list(rows[1:]) + [None]:
        if y is not None and y == prev + 1:
            prev = y
            continue
        cols = np.flatnonzero(mask[band_start:prev + 1].any(axis=0))
        rects.append((int(cols[0]), int(band_start),
                      int(cols[-1] - cols[0] + 1), int(prev - band_start + 1)))
        if y is not None:
            band_start = prev = y
    return rects


def encode(frames565, fps, big_endian):
    h, w = frames565[0].shape
    blob = bytearray(b"BAN1")
    blob += struct.pack("<HHBBH", w, h, fps,
                        1 if big_endian else 0, len(frames565))
    per_frame = []
    for idx, (frame, prev) in enumerate(
            zip(frames565, [None] + list(frames565[:-1]))):
        rects = ([(0, 0, w, h)] if prev is None
                 else changed_rects(frame != prev))
        rec = bytearray([len(rects)])
        for x, y, rw, rh in rects:
            payload = packbits16(frame[y:y + rh, x:x + rw].ravel())
            rec += struct.pack("<HHHHI", x, y, rw, rh, len(payload))
            rec += payload
        blob += rec
        per_frame.append(len(rec))
    if not big_endian:  # payload pixels were packed big-endian above
        sys.exit("little-endian pixel emission not implemented yet")
    return bytes(blob), per_frame


def decode(blob):
    """Independent decoder: blob -> list of (H, W) uint16 canvases."""
    magic, = struct.unpack_from("4s", blob)
    assert magic == b"BAN1"
    w, h, fps, _flags, n = struct.unpack_from("<HHBBH", blob, 4)
    off = 12
    canvas = np.zeros((h, w), dtype=np.uint16)
    frames = []
    for _ in range(n):
        nrects = blob[off]
        off += 1
        for _ in range(nrects):
            x, y, rw, rh, plen = struct.unpack_from("<HHHHI", blob, off)
            off += 12
            canvas[y:y + rh, x:x + rw] = unpackbits16(
                blob[off:off + plen], rw * rh).reshape(rh, rw)
            off += plen
        frames.append(canvas.copy())
    return frames, fps


def emit_header(path, blob, last, fps, nframes, bg_rgb, cmdline):
    def dump(name, data, lines):
        lines.append(f"static const uint8_t {name}[{len(data)}] = {{")
        for i in range(0, len(data), 16):
            chunk = ", ".join(f"0x{b:02x}" for b in data[i:i + 16])
            lines.append(f"\t{chunk},")
        lines.append("};")

    lines = [
        "/* Generated by tools/encode_bootanim.py -- do not edit.",
        f" * {cmdline}",
        f" * {nframes} frames @ {fps} fps; clip {len(blob)} bytes, "
        f"last frame {len(last)} bytes. */",
        "#pragma once",
        "#include <stdint.h>",
        "",
        f"#define BOOTANIM_BG_RGB 0x{bg_rgb:06x}",
        "",
    ]
    dump("bootanim_blob", blob, lines)
    lines.append("")
    dump("bootanim_last", last, lines)
    lines.append("")
    with open(path, "w") as f:
        f.write("\n".join(lines))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--input", required=True)
    ap.add_argument("--start", type=float, default=0.0)
    ap.add_argument("--duration", type=float, default=4.0)
    ap.add_argument("--fps", type=int, default=12)
    ap.add_argument("--threshold", type=int, default=10,
                    help="per-channel noise gate vs displayed pixel (0-255)")
    ap.add_argument("--mask", action="append", default=[], metavar="X,Y,W,H",
                    help="paint the background fill over this rect of the "
                         "scaled video (e.g. a watermark); repeatable")
    ap.add_argument("--out", help="write C header here")
    ap.add_argument("--preview", help="write round-trip GIF here")
    args = ap.parse_args()

    vid_h = round(CANVAS_W * 9 / 16)  # 16:9 source scaled to canvas width
    src = extract_frames(args.input, args.start, args.duration, args.fps,
                         CANVAS_W, vid_h)
    fill = np.median(src[0, :2].reshape(-1, 3), axis=0).astype(np.uint8)

    for m in args.mask:
        x, y, w, h = (int(v) for v in m.split(","))
        src[:, y:y + h, x:x + w] = fill

    y0 = (CANVAS_H - vid_h) // 2
    canvases = np.empty((len(src), CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    canvases[:] = fill
    canvases[:, y0:y0 + vid_h] = src

    # Noise-gate against the *displayed* state, then quantize.
    shown = [canvases[0]]
    for f in canvases[1:]:
        prev = shown[-1]
        still = (np.abs(f.astype(np.int16) - prev.astype(np.int16))
                 <= args.threshold).all(axis=-1, keepdims=True)
        shown.append(np.where(still, prev, f))
    frames565 = [to_rgb565(f) for f in shown]

    blob, per_frame = encode(frames565, args.fps, big_endian=True)
    last, _ = encode([frames565[-1]], args.fps, big_endian=True)

    dec_frames, _ = decode(blob)  # round-trip check, also feeds the preview
    for a, b in zip(dec_frames, frames565):
        assert (a == b).all(), "round-trip mismatch"
    dec_last, _ = decode(last)
    assert (dec_last[0] == frames565[-1]).all(), "last-frame mismatch"

    deltas = per_frame[1:]
    print(f"frames: {len(frames565)} @ {args.fps} fps "
          f"({len(frames565) / args.fps:.2f} s)")
    print(f"blob: {len(blob):,} bytes total; last frame {len(last):,} bytes")
    print(f"  frame 0 (full): {per_frame[0]:,} bytes")
    if deltas:
        print(f"  deltas: min {min(deltas):,} / median "
              f"{int(np.median(deltas)):,} / max {max(deltas):,} bytes")

    if args.preview:
        imgs = [Image.fromarray(from_rgb565(f)) for f in dec_frames]
        imgs[0].save(args.preview, save_all=True, append_images=imgs[1:],
                     duration=round(1000 / args.fps), loop=0)
        print(f"preview: {args.preview}")
    if args.out:
        bg = (int(fill[0]) << 16) | (int(fill[1]) << 8) | int(fill[2])
        emit_header(args.out, blob, last, args.fps, len(frames565), bg,
                    "tools/encode_bootanim.py " + " ".join(sys.argv[1:]))
        print(f"header: {args.out}")


if __name__ == "__main__":
    main()
