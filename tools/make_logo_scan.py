#!/usr/bin/env python3
"""Draw a company logo clip: a rule crosses the screen and leaves the mark.

A company unit shows its logo after the boot animation (tools/burn.sh --logo).
This writes the 320x240 frames for one; tools/encode_logo.py turns them into
the BLGO image the `logo` partition holds.

The clip is a scanner. A bright rule travels left to right; the artwork exists
behind it and not in front of it. Pixels the rule has just passed are
accent-coloured and cool to the settled ink over the next few centimetres of
travel, so the mark reads as being *resolved* rather than faded in.

It takes the company's own artwork -- a coverage mask, white on black, at any
size -- rather than setting a word in a substitute font. A wordmark is a
drawing, not a string: no font on this machine is the company's font, and one
that is merely close is worse than obviously borrowed, because it invites
nobody to check.

  tools/make_logo_scan.py --art tools/assets/pipl-wordmark.png \
      --bg FFFFFF --ink 4D93E9 --accent 1C4A7D --out /tmp/pipl-frames
  tools/encode_logo.py --frames /tmp/pipl-frames --fps 15 --hold 1.8 \
      --out logos/pipl.bin --preview /tmp/pipl.gif
  tools/burn.sh --edition claude --logo logos/pipl.bin

Why one moving vertical edge and nothing else: the BAN1 delta encoder stores,
per frame, the contiguous bands of rows that changed, cropped to the changed
columns (tools/encode_bootanim.py:changed_rects). One edge dirties a narrow
strip and leaves the rest of the screen alone, so a clip like this costs a few
percent of the 512 KB partition. Marks drifting or dissolving about the screen
would dirty every band on every frame and cost twenty times as much.

The artwork is composited antialiased, once, and then only *revealed* by
column. Antialiasing is safe here for the reason it was not safe in
make_bootanim_codex.py: nothing moves. A static soft edge is the same pixels
every frame, so it costs nothing after the frame that reveals it and it does
not shimmer. Only the rule and its cooling trail move, and both are hard-edged.
"""

import argparse
import os
import sys

import numpy as np
from PIL import Image

W, H = 320, 240
FPS = 15

ART_W = 210                 # the mark's width on a 320 px screen
ART_CY = 120                # its ink sits centred

# The scanning rule.
BAR_W = 3                   # px, the rule itself
TRAIL = 46                  # px behind it over which accent cools to ink
BAR_TOP, BAR_BOT = 42, 198  # it overhangs the mark, as a rule does

# Beats, in frames at FPS.
N_LEAD = 3                  # the bare ground, before anything happens
N_SWEEP = 22                # the crossing
N_SETTLE = 5                # stillness before encode_logo's own --hold


def hexcolor(s):
    s = s.lstrip("#")
    if len(s) != 6:
        sys.exit(f"want RRGGBB, got {s!r}")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], np.float64)


def load_art(path, width, cy):
    """The artwork as a (H, W) coverage plane, scaled and placed.

    Anything with an alpha channel is read through it; anything without is
    read as luminance, so a white-on-black mask and a transparent PNG both
    do the right thing.
    """
    im = Image.open(path)
    a = (np.asarray(im.convert("RGBA"), np.float64)[:, :, 3] / 255.0
         if "A" in im.getbands()
         else np.asarray(im.convert("L"), np.float64) / 255.0)
    ys, xs = np.nonzero(a > 0.02)
    if ys.size == 0:
        sys.exit(f"{path}: no ink in the artwork")
    a = a[ys.min():ys.max() + 1, xs.min():xs.max() + 1]

    ah, aw = a.shape
    h = max(1, round(ah * width / aw))
    if width > W or h > H:
        sys.exit(f"--width {width} makes the mark {width}x{h}, "
                 f"larger than the {W}x{H} screen")
    a = np.asarray(Image.fromarray((a * 255).astype(np.uint8))
                   .resize((width, h), Image.LANCZOS), np.float64) / 255.0

    plane = np.zeros((H, W))
    x0, y0 = (W - width) // 2, cy - h // 2
    if y0 < 0 or y0 + h > H:
        sys.exit(f"--cy {cy} puts the mark off the screen")
    plane[y0:y0 + h, x0:x0 + width] = a
    return plane


def build(art, bg, ink, accent):
    cols = np.arange(W, dtype=np.float64)
    frames = []

    def compose(line_x):
        """One frame: the mark revealed up to `line_x`, cooling behind it."""
        out = np.empty((H, W, 3), np.float64)
        out[:] = bg

        # Per-column reveal: 0 in front of the rule, 1 behind it. The tint
        # cools from accent to ink over TRAIL px of the rule's travel.
        behind = line_x - cols
        shown = (behind >= 0).astype(np.float64)
        heat = np.clip(1.0 - behind / TRAIL, 0.0, 1.0) * shown
        colour = (ink[None, :] * (1 - heat)[:, None]
                  + accent[None, :] * heat[:, None])          # (W, 3)

        a = (art * shown[None, :])[:, :, None]
        out = out * (1 - a) + colour[None, :, :] * a

        # The rule that does the revealing, drawn last so it sits on top.
        bx0 = int(round(line_x)) - BAR_W // 2
        bx0, bx1 = max(0, bx0), min(W, bx0 + BAR_W)
        if bx1 > bx0:
            out[BAR_TOP:BAR_BOT, bx0:bx1] = accent
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    for _ in range(N_LEAD):
        frames.append(compose(-BAR_W))
    # The rule leaves the screen and keeps going: the last column of the mark
    # still has to finish cooling after the rule itself is gone.
    for i in range(N_SWEEP):
        frames.append(compose((i + 1) / N_SWEEP * (W + TRAIL)))
    for _ in range(N_SETTLE):
        frames.append(compose(W + TRAIL))
    return frames


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--art", required=True,
                    help="the mark: a coverage mask (white on black) or a "
                         "PNG with alpha, at any size")
    ap.add_argument("--width", type=int, default=ART_W,
                    help=f"the mark's width in px (default {ART_W} of {W})")
    ap.add_argument("--cy", type=int, default=ART_CY,
                    help="the vertical centre of the mark's ink")
    ap.add_argument("--bg", default="FFFFFF", help="ground colour, hex")
    ap.add_argument("--ink", default="4D93E9", help="the settled mark")
    ap.add_argument("--accent", default="1C4A7D",
                    help="the rule and the trail it leaves cooling")
    ap.add_argument("--out", required=True, help="directory for the PNGs")
    a = ap.parse_args()

    frames = build(load_art(a.art, a.width, a.cy), hexcolor(a.bg),
                   hexcolor(a.ink), hexcolor(a.accent))
    os.makedirs(a.out, exist_ok=True)
    for old in [f for f in os.listdir(a.out) if f.endswith(".png")]:
        os.remove(os.path.join(a.out, old))
    for i, f in enumerate(frames):
        Image.fromarray(f).save(os.path.join(a.out, f"{i:03d}.png"))
    print(f"{len(frames)} frames @ {FPS} fps "
          f"({len(frames) / FPS:.1f} s) -> {a.out}")


if __name__ == "__main__":
    main()
