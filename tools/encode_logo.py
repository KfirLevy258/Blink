#!/usr/bin/env python3
"""Build the company boot logo image (BLGO) for the `logo` flash partition.

A company unit shows its logo after the boot clip. The logo is a BAN1 clip --
the same delta-RLE format as the boot eyes, played by the same decoder -- with
a 32-byte header in front that says how long to hold the last frame, what to
paint the screen with before the first one, and a CRC that makes "this unit
has a logo" a checked fact rather than a guess (layout: firmware/src/logo_parse.h).

Three ways to author one:

  # A still picture, held for 3 s (scaled to fit 320x240, centred, background
  # taken from the picture's corner unless --bg says otherwise):
  tools/encode_logo.py --image acme.png --out acme.bin

  # A drawn clip: <DIR>/*.png in sorted order, each already 320x240:
  tools/encode_logo.py --frames acme-frames/ --fps 15 --hold 0.5 --out acme.bin

  # A short video (ffmpeg): scaled to fit, letterboxed onto --bg:
  tools/encode_logo.py --video acme.mp4 --duration 3 --out acme.bin

  # What is in a .bin:
  tools/encode_logo.py --info acme.bin [--preview acme.gif]

Then: tools/burn.sh --edition claude --logo acme.bin  (or pass the picture
straight to --logo and burn.sh runs this for you).

Size: the partition is 512 KB. A still logo on a flat background is a few KB;
a clip's cost is the pixels that CHANGE between frames, so animate the logo
over a stable background and keep gradients horizontal (a row that is one
colour is three bytes; a row that is a gradient is 640). The tool prints the
size and refuses anything that will not fit.
"""

import argparse
import glob
import os
import struct
import sys
import zlib

import numpy as np
from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import encode_bootanim as ba  # noqa: E402

CANVAS_W, CANVAS_H = ba.CANVAS_W, ba.CANVAS_H
HDR_LEN = 32
VERSION = 1
MAGIC = b"BLGO"
PARTITION_BYTES = 0x80000
# esptool pads to 4; the encrypted path (espsecure) wants 32-byte blocks, and
# a file that is already a multiple of 32 flashes identically both ways.
ALIGN = 32


def parse_bg(spec, im=None):
    """--bg: 'auto' (corner pixel of the picture), 'RRGGBB' or 'R,G,B'."""
    if spec == "auto":
        if im is None:
            return (0, 0, 0)
        px = im.convert("RGBA").getpixel((0, 0))
        if px[3] < 255:
            # A transparent corner means the artwork has no background of its
            # own; black is the honest default for a screen that is black
            # when off.
            return (0, 0, 0)
        return tuple(px[:3])
    if "," in spec:
        r, g, b = (int(v) for v in spec.split(","))
    else:
        s = spec.lstrip("#")
        if len(s) != 6:
            sys.exit(f"--bg: want RRGGBB or R,G,B, got {spec!r}")
        r, g, b = int(s[0:2], 16), int(s[2:4], 16), int(s[4:6], 16)
    return (r, g, b)


def fit_canvas(im, bg, scale=1.0):
    """Scale a picture to fit the canvas (times `scale`, so a logo can be
    made smaller than the screen), composite it over the background colour,
    centre it. Returns (H, W, 3) uint8."""
    im = im.convert("RGBA")
    w, h = im.size
    k = min(CANVAS_W / w, CANVAS_H / h) * scale
    nw, nh = max(1, round(w * k)), max(1, round(h * k))
    im = im.resize((nw, nh), Image.LANCZOS)
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), bg + (255,))
    canvas.alpha_composite(im, ((CANVAS_W - nw) // 2, (CANVAS_H - nh) // 2))
    return np.asarray(canvas.convert("RGB"))


def load_frames_dir(directory):
    paths = sorted(glob.glob(os.path.join(directory, "*.png")))
    if not paths:
        sys.exit(f"no PNGs in {directory}")
    out = np.empty((len(paths), CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    for i, p in enumerate(paths):
        im = Image.open(p).convert("RGB")
        if im.size != (CANVAS_W, CANVAS_H):
            sys.exit(f"{p} is {im.size[0]}x{im.size[1]}, "
                     f"need {CANVAS_W}x{CANVAS_H}")
        out[i] = np.asarray(im)
    return out


def load_video(path, start, duration, fps, bg, scale):
    """ffmpeg -> frames scaled to fit the canvas, letterboxed onto bg."""
    sw, sh = ba.probe_dims(path)
    k = min(CANVAS_W / sw, CANVAS_H / sh) * scale
    vw, vh = max(2, round(sw * k) // 2 * 2), max(2, round(sh * k) // 2 * 2)
    src = ba.extract_frames(path, start, duration, fps, vw, vh)
    out = np.empty((len(src), CANVAS_H, CANVAS_W, 3), dtype=np.uint8)
    out[:] = np.array(bg, dtype=np.uint8)
    x0, y0 = (CANVAS_W - vw) // 2, (CANVAS_H - vh) // 2
    out[:, y0:y0 + vh, x0:x0 + vw] = src
    return out


def quantise(canvases, threshold):
    """Noise-gate against the displayed frame, then RGB565 -- the same two
    steps encode_bootanim.finish() takes, so a logo clip and a boot clip go
    through one pipeline."""
    shown = [canvases[0]]
    for f in canvases[1:]:
        prev = shown[-1]
        still = (np.abs(f.astype(np.int16) - prev.astype(np.int16))
                 <= threshold).all(axis=-1, keepdims=True)
        shown.append(np.where(still, prev, f))
    return [ba.to_rgb565(f) for f in shown]


def build(frames565, fps, hold_ms, bg):
    """frames565: list of (H, W) uint16. Returns the padded BLGO image."""
    blob, _per_frame = ba.encode(frames565, fps, big_endian=True)
    hdr = MAGIC + struct.pack("<HHII", VERSION, hold_ms, len(blob),
                              zlib.crc32(blob) & 0xffffffff)
    hdr += bytes(bg) + b"\0"
    hdr = hdr.ljust(HDR_LEN, b"\0")
    assert len(hdr) == HDR_LEN
    img = hdr + blob
    img += b"\xff" * (-len(img) % ALIGN)
    return img


def parse(img):
    """Inverse of build(): the header fields and the decoded frames. Raises
    ValueError on anything the firmware would treat as absent."""
    if len(img) < HDR_LEN + 12 or img[:4] != MAGIC:
        raise ValueError("not a BLGO image")
    version, hold_ms, blob_len, crc = struct.unpack_from("<HHII", img, 4)
    if version != VERSION:
        raise ValueError(f"version {version}, expected {VERSION}")
    if blob_len < 12 or blob_len > len(img) - HDR_LEN:
        raise ValueError("blob length outside the image")
    blob = img[HDR_LEN:HDR_LEN + blob_len]
    if zlib.crc32(blob) & 0xffffffff != crc:
        raise ValueError("CRC mismatch")
    frames, fps = ba.decode(blob)
    return {
        "hold_ms": hold_ms,
        "bg": tuple(img[16:19]),
        "blob_len": blob_len,
        "fps": fps,
        "frames": frames,
    }


def write_preview(path, frames, fps, hold_ms):
    ims = [Image.fromarray(ba.from_rgb565(f)) for f in frames]
    durations = [max(20, 1000 // fps)] * len(ims)
    durations[-1] += hold_ms
    ims[0].save(path, save_all=True, append_images=ims[1:],
                duration=durations, loop=0)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    src = ap.add_mutually_exclusive_group()
    src.add_argument("--image", help="a still picture (PNG, JPEG, ...)")
    src.add_argument("--frames", metavar="DIR",
                     help="a drawn clip: <DIR>/*.png, each 320x240")
    src.add_argument("--video", help="a short video (needs ffmpeg)")
    src.add_argument("--info", metavar="BIN",
                     help="describe an existing .bin instead of building")
    ap.add_argument("--start", type=float, default=0.0,
                    help="--video: seconds to skip first")
    ap.add_argument("--duration", type=float, default=3.0,
                    help="--video: seconds to keep")
    ap.add_argument("--fps", type=int, default=15,
                    help="--video / --frames: frames per second")
    ap.add_argument("--hold", type=float, default=None,
                    help="seconds the last frame stays up (default 3 for a "
                         "still, 0.5 for a clip; max 60)")
    ap.add_argument("--bg", default="auto",
                    help="background: auto (corner pixel), RRGGBB or R,G,B")
    ap.add_argument("--scale", type=float, default=1.0,
                    help="--image / --video: shrink the picture to this "
                         "fraction of the screen (0.6 leaves a margin)")
    ap.add_argument("--threshold", type=int, default=10,
                    help="per-channel noise gate between frames (0-255)")
    ap.add_argument("--out", help="write the BLGO image here")
    ap.add_argument("--preview", help="write a round-trip GIF here")
    args = ap.parse_args()

    if args.info:
        with open(args.info, "rb") as f:
            info = parse(f.read())
        frames = info["frames"]
        print(f"{args.info}: {len(frames)} frame(s) @ {info['fps']} fps, "
              f"hold {info['hold_ms']} ms, bg #{bytes(info['bg']).hex()}, "
              f"blob {info['blob_len']} bytes")
        if args.preview:
            write_preview(args.preview, frames, info["fps"], info["hold_ms"])
            print(f"preview: {args.preview}")
        return

    if not (args.image or args.frames or args.video):
        sys.exit("give one of --image, --frames, --video or --info")
    if not (args.out or args.preview):
        sys.exit("give --out and/or --preview")

    still = bool(args.image)
    hold = args.hold if args.hold is not None else (3.0 if still else 0.5)
    if not 0 <= hold <= 60:
        sys.exit("--hold must be 0..60 seconds")
    hold_ms = int(round(hold * 1000))

    if args.image:
        im = Image.open(args.image)
        bg = parse_bg(args.bg, im)
        canvases = fit_canvas(im, bg, args.scale)[None]
        fps = 1
    elif args.frames:
        canvases = load_frames_dir(args.frames)
        bg = (parse_bg(args.bg) if args.bg != "auto"
              else tuple(int(v) for v in canvases[0, 0, 0]))
        fps = args.fps
    else:
        bg = parse_bg(args.bg)
        canvases = load_video(args.video, args.start, args.duration,
                              args.fps, bg, args.scale)
        fps = args.fps
    if not 1 <= fps <= 60:
        sys.exit("--fps must be 1..60")

    frames565 = quantise(canvases, args.threshold)
    # The header's background is what the screen shows before frame 0
    # lands, so it must be the QUANTISED colour the frame actually carries.
    bg = tuple(int(v) for v in ba.from_rgb565(ba.to_rgb565(
        np.array(bg, dtype=np.uint8))))
    img = build(frames565, fps, hold_ms, bg)

    # Round-trip through the independent decoder before anything is written:
    # a logo that the firmware could not play is a boot that hangs on a
    # half-drawn picture, which is worse than no logo.
    back = parse(img)
    assert len(back["frames"]) == len(frames565)
    for a, b in zip(back["frames"], frames565):
        assert (a == b).all()

    print(f"{len(frames565)} frame(s) @ {fps} fps, hold {hold_ms} ms, "
          f"bg #{bytes(bg).hex()}: {len(img)} bytes "
          f"({len(img) * 100 // PARTITION_BYTES}% of the partition)")
    if len(img) > PARTITION_BYTES:
        sys.exit(f"too big: {len(img)} bytes, the partition holds "
                 f"{PARTITION_BYTES}. Fewer frames, or less motion.")
    if args.preview:
        write_preview(args.preview, frames565, fps, hold_ms)
        print(f"preview: {args.preview}")
    if args.out:
        with open(args.out, "wb") as f:
            f.write(img)
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
