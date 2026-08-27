#!/usr/bin/env python3
"""Draft the Codex edition of the boot clip.

The brief: "just like the one we have now but the end screen will look more
like the logo of codex", landing on the prompt from inside that logo: > _

So this does not redraw the animation. It takes the SHIPPED frames verbatim
through the last blink and re-authors only the tail -- and even the tail is
built out of the clip's own artwork rather than new drawing:

  the >   is the clip's OWN chevron, mirrored. The right eye already morphs
          square -> chevron across frames 32..44 (66x80, fill 0.36). Playing
          those crops back on the LEFT eye, flipped, gives a > that matches
          the original's stroke weight and easing exactly, because it IS the
          original's stroke weight and easing.

  the _   is the clip's OWN blink. Frame 46's right eye is a 64x7 bar. It
          only has to thicken and move to read as an underscore.

Nothing here invents a shape, which is the point: a hand-drawn chevron next to
a filmed one would be visibly a different pen.
"""
import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ban1_decode as ban1

W, H = 320, 240

# Where the shipped clip's own morph lives. Measured, not guessed -- see
# the frame profile in the module docstring.
# Both boundaries are measured off the shipped frames, and both matter for
# the same reason: the first authored frame has to draw exactly what the last
# verbatim one did, or the join is a visible pop.
#
#   frame 27  the last frame with the right eye still a FULL box. The clip
#             starts closing it at 28 and reopens it as a "<" at 31, and
#             neither can come along: the chevron would point the wrong way
#             against the ending, and the close has to happen AFTER the left
#             eye blinks, not before it.
#
#             So the whole of the right eye's blink is dropped and re-timed.
#             The order on screen is: left eye blinks and opens as ">", and
#             only then does the box on the right close into "_".
KEEP_THROUGH = 27
MORPH_FIRST, MORPH_LAST = 27, 32   # right eye: square -> blink -> chevron

# The shipped morph, whole: square(27) -> closing(28) -> nearly shut(29) ->
# shut(30) -> opening(31) -> chevron(32). It was briefly split into two beats
# with a slide in between; with nothing travelling any more it is one gesture
# again, which is how the clip itself plays it.
EYE_BOX = (216, 81, 282, 161)      # x0, y0, x1, y1 covering both extremes

# Where the prompt settles. The chevron keeps the eye's own size; the
# underscore is sized to the chevron's stroke (~18 px, from area/length over
# the 66x80 silhouette) so the two read as one piece of type.
PROMPT_CHEV_C = (124, 114)
PROMPT_BAR_C = (206, 150)
PROMPT_BAR_WH = (56, 18)

# The whole tail, and it is deliberately tiny. Every frame of it is a frame
# the right eye spends shut, so length here is the cost being complained
# about. Six frames is the shipped morph played once, at its own speed.
CLOSE_FRAMES = 4    # the right box closing into the underscore, AFTER the blink
PAUSE_FRAMES = 4    # a beat between the two, so they read as two events

# The underscore keeps the eye's full width, so the right eye only ever gets
# SHORTER -- narrowing it as well would be movement by another name, and the
# note was to leave the two glyphs where their eyes are.
BAR_H = 16          # near the chevron's own stroke weight

# The bar sits on the BOTTOM of where the eye was, not on its middle. A bar at
# the eye's centre line is a hyphen; an underscore lives on the baseline, and
# that one pixel of intent is the whole difference between "> -" and "> _".
# So the box closes DOWNWARD -- its bottom edge pinned, its top edge falling --
# which is also a better close than shrinking to the centre would be: it reads
# like a shutter coming down rather than the shape deflating.
CLOSE_FROM_H = 67   # the full box, which is what frame 27 leaves it at
CLOSE_FROM_R = 9    # ...and its corner radius, so the first drawn frame
                    #    lands on top of the filmed one instead of popping
MORPH_FRAMES = 6
# Longer than it looks like it needs to be, for two reasons: the prompt is
# the resting state, so time on it is not dead time; and cutting the clip at
# frame 30 costs a second and a half that the boot splash spends as the
# daemon-handshake window (see ui_boot.c). Static frames are free in the blob
# -- a held frame encodes as zero rects.
HOLD_FRAMES = 24    # two seconds on the finished prompt

# The right eye's resting shape, and what it becomes. Lerping all three
# numbers turns one into the other continuously -- at 67 tall with a small
# radius it is the eye, at 18 tall with a radius of half its height it is the
# underscore, and every frame between reads as the eye closing.
EYE_WHR = (66, 67, 8)


def shipped(header):
    _, _, _, frames = ban1.decode(ban1.load_blob(header, "bootanim_blob"))
    return frames


def silhouettes(frames, bg):
    """Boolean ink masks, so the palette can be swapped without touching the
    artwork."""
    return [np.abs(f.astype(int) - bg).max(axis=-1) > 60 for f in frames]


def chevron_masks(masks, span):
    """A stretch of the mirrored morph, cropped to its own box.

    Mirrored here rather than at paste time so the caller only ever deals in
    'this is the left eye at stage i'.
    """
    x0, y0, x1, y1 = EYE_BOX
    first, last = span
    return [masks[i][y0:y1, x0:x1][:, ::-1].copy()
            for i in range(first, last + 1)]


def at_stage(stages, t):
    """The stage this far through a beat."""
    return stages[min(len(stages) - 1, int(t * (len(stages) - 1) + 0.5))]


def paste(canvas, mask, cx, cy, ink):
    """Stamp a boolean mask centred on (cx, cy), clipped to the canvas."""
    mh, mw = mask.shape
    x0 = int(round(cx - mw / 2))
    y0 = int(round(cy - mh / 2))
    ys, xs = np.nonzero(mask)
    ys, xs = ys + y0, xs + x0
    keep = (ys >= 0) & (ys < H) & (xs >= 0) & (xs < W)
    canvas[ys[keep], xs[keep]] = ink


def round_rect_mask(w, h, r):
    w, h = max(1, int(round(w))), max(1, int(round(h)))
    r = max(0, min(int(round(r)), min(w, h) // 2))
    im = Image.new("L", (w, h), 0)
    ImageDraw.Draw(im).rounded_rectangle([0, 0, w - 1, h - 1],
                                         radius=r, fill=255)
    return np.asarray(im) > 128


def ease(t):
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    return a + (b - a) * t


def build_tail(masks, bg, ink):
    """The left eye blinks into >. Only then does the right box close into _.

    The order is the point. Both eyes are open boxes when the tail starts; the
    left one blinks, which is the only blink in the clip; and the right one is
    untouched filmed pixels until that blink has finished. It then closes once
    and stays shut -- it never reopens, so it never blinks, and it never
    becomes a chevron.
    """
    morph = chevron_masks(masks, (MORPH_FIRST, MORPH_LAST))
    last = masks[KEEP_THROUGH]

    ys, xs = np.nonzero(last)
    mid = (xs.min() + xs.max()) / 2
    lsel = xs < mid
    lx = (xs[lsel].min() + xs[lsel].max()) / 2
    ly = (ys[lsel].min() + ys[lsel].max()) / 2
    rx = (xs[~lsel].min() + xs[~lsel].max()) / 2
    ry = (ys[~lsel].min() + ys[~lsel].max()) / 2
    rb = float(ys[~lsel].max())          # the eye's bottom edge: the baseline
    ew = int(xs[~lsel].max() - xs[~lsel].min() + 1)

    right_box = last.copy()          # the filmed box, kept as pixels
    right_box[:, :int(mid)] = False

    frames = []

    # Beat one: the left eye blinks and opens as >. The right box is the
    # last kept frame's own pixels -- nothing redraws it, so nothing about it
    # can flicker while the eye beside it is doing the only moving.
    for i in range(MORPH_FRAMES):
        t = (i + 1) / MORPH_FRAMES
        f = np.empty((H, W, 3), np.uint8)
        f[:] = bg
        f[right_box] = ink
        paste(f, at_stage(morph, t), lx, ly, ink)
        frames.append(f)

    # A beat in between, so the blink and the close are two events rather
    # than one long one. Nothing moves at all here.
    chevron = at_stage(morph, 1.0)
    frames.extend([frames[-1].copy()] * PAUSE_FRAMES)

    # Beat two: now the box closes, downward onto the baseline. The > is
    # finished and holds still.
    for i in range(CLOSE_FRAMES):
        t = (i + 1) / CLOSE_FRAMES
        f = np.empty((H, W, 3), np.uint8)
        f[:] = bg
        h_now = lerp(CLOSE_FROM_H, BAR_H, t)
        paste(f, round_rect_mask(ew, h_now,
                                 lerp(CLOSE_FROM_R, BAR_H / 2, t)),
              rx, rb - h_now / 2, ink)
        paste(f, chevron, lx, ly, ink)
        frames.append(f)

    frames.extend([frames[-1].copy()] * HOLD_FRAMES)
    return frames


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--shipped", default="firmware/src/bootanim.h")
    ap.add_argument("--bg", default="76b1db")
    ap.add_argument("--ink", default="ffffff")
    ap.add_argument("--out", required=True)
    a = ap.parse_args()

    bg = np.array([int(a.bg[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)
    ink = np.array([int(a.ink[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)

    src = shipped(a.shipped)
    masks = silhouettes(src, src[0][0, 0].astype(int))

    kept = []
    for m in masks[:KEEP_THROUGH + 1]:
        f = np.empty((H, W, 3), np.uint8)
        f[:] = bg
        f[m] = ink
        kept.append(f)

    tail = build_tail(masks, bg, ink)

    os.makedirs(a.out, exist_ok=True)
    for p in os.listdir(a.out):
        if p.endswith(".png"):
            os.remove(os.path.join(a.out, p))
    for i, f in enumerate(kept + tail):
        Image.fromarray(f).save(os.path.join(a.out, f"{i:03d}.png"))
    print(f"{len(kept)} kept + {len(tail)} authored = "
          f"{len(kept) + len(tail)} frames -> {a.out}")


if __name__ == "__main__":
    main()
