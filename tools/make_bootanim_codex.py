#!/usr/bin/env python3
"""Draw the Codex boot clip: clean geometry, no filmed pixels.

An earlier version of this built the clip out of the shipped Claude clip's own
frames, on the reasoning that borrowed artwork could not clash with itself.
That was wrong, and the panel said so. The shipped clip is h264: its shapes
have antialiased, motion-blurred, compression-smeared edges, and hard-
thresholding those into two colours leaves a border of stray pixels that
CHANGES EVERY FRAME. On the original's busy orange ground, at speed, nobody
sees it. On a flat blue box it boils -- "glitters and jitters around it".

So every shape here is drawn. Same positions, same sizes, same rhythm as the
original; none of its pixels. Shapes are rendered at 4x and thresholded, which
gives a hard edge that follows the true geometry exactly and is identical for
identical geometry -- so a still shape is still, to the pixel, instead of
shimmering.
"""
import argparse
import os

import numpy as np
from PIL import Image, ImageDraw

W, H = 320, 240
SS = 4                      # supersample factor for the shape rasteriser

# Where the two eyes live. Taken from the shipped clip so the Codex edition
# sits exactly where the Claude one does -- the enclosure window is the same
# hole either way.
LEFT_C = (68.5, 120.0)
RIGHT_C = (248.5, 120.0)
EYE_W, EYE_H, EYE_R = 66, 67, 12

# The prompt the clip lands on.
CHEV_HALF_W, CHEV_HALF_H, CHEV_STROKE = 22, 32, 18
BAR_W, BAR_H = 66, 16
BAR_BOTTOM = 153.0          # the eye's own baseline, so the _ is an
                            # underscore rather than a hyphen

FPS = 12


def ease(t):
    return t * t * (3 - 2 * t)


def lerp(a, b, t):
    return a + (b - a) * t


def canvas():
    return Image.new("L", (W * SS, H * SS), 0)


def burn(im, arr_bg, ink):
    """Threshold the supersampled mask and paint it onto a frame."""
    small = im.resize((W, H), Image.LANCZOS)
    mask = np.asarray(small) >= 128
    out = np.empty((H, W, 3), np.uint8)
    out[:] = arr_bg
    out[mask] = ink
    return out


def box(d, cx, cy, w, h, r):
    r = max(0, min(r, min(w, h) / 2))
    d.rounded_rectangle(
        [(cx - w / 2) * SS, (cy - h / 2) * SS,
         (cx + w / 2) * SS, (cy + h / 2) * SS],
        radius=r * SS, fill=255)


def chevron(d, cx, cy, t=1.0, shut_w=EYE_W, shut_h=6):
    """A `>` with round caps and a round joint, unfolding from a flat bar.

    `t` is 0 at the closed eye -- a horizontal bar the width of the eye -- and
    1 at the full chevron. The three points are interpolated from
    (left, centre, right) all on one line to (top-left, point, bottom-left),
    so the bar visibly FOLDS. Scaling a chevron up from nothing instead was
    the first attempt and it popped: the closed eye is 66 px wide and a
    20%-scale chevron is 9, so the shape jumped inward before it grew.
    """
    hw = lerp(shut_w / 2, CHEV_HALF_W, t)
    hh = lerp(0.0, CHEV_HALF_H, t)
    # At t=0 the two arms lie along the bar; the joint travels right as the
    # arms swing back and up.
    tip_x = lerp(cx + shut_w / 2, cx + CHEV_HALF_W, t)
    pts = [(cx - hw, cy - hh), (tip_x, cy), (cx - hw, cy + hh)]
    width = int(round(lerp(shut_h, CHEV_STROKE, t)))

    d.line([(x * SS, y * SS) for x, y in pts],
           fill=255, width=max(1, width * SS), joint="curve")
    # PIL's line has square caps; round them by hand so the stroke matches the
    # rounded boxes it grew out of.
    r = width * SS / 2
    for x, y in pts:
        d.ellipse([x * SS - r, y * SS - r, x * SS + r, y * SS + r], fill=255)


def build(bg, ink):
    """The clip, as a list of frames.

    The rhythm follows the shipped clip -- sit, blink, sit -- and then does
    the one thing that makes this the Codex edition.
    """
    frames = []

    def frame(draw_fn):
        im = canvas()
        draw_fn(ImageDraw.Draw(im))
        frames.append(burn(im, bg, ink))

    def both(lh, rh, ly=None, ry=None):
        """Both eyes as boxes of the given heights."""
        def go(d):
            box(d, LEFT_C[0], ly or LEFT_C[1], EYE_W, lh, EYE_R)
            box(d, RIGHT_C[0], ry or RIGHT_C[1], EYE_W, rh, EYE_R)
        return go

    def blink_heights(n):
        """One blink: open -> shut -> open, on an eased curve."""
        out = []
        for i in range(n):
            t = (i + 1) / n
            # A blink is fast down, fast up, with the shut moment in the
            # middle -- a triangle, not a sine, or it reads as a slow squint.
            k = 1 - abs(2 * t - 1)
            out.append(lerp(EYE_H, 6, ease(k)))
        return out

    # 1. Both eyes, open and still.
    for _ in range(8):
        frame(both(EYE_H, EYE_H))

    # 2. A blink, together.
    for h in blink_heights(6):
        frame(both(h, h))

    # 3. Still again.
    for _ in range(7):
        frame(both(EYE_H, EYE_H))

    # 4. The LEFT eye alone blinks shut. The right one holds, open and
    #    perfectly still -- only one thing moves.
    shut = 6
    for i in range(4):
        t = ease((i + 1) / 4)
        frame(both(lerp(EYE_H, shut, t), EYE_H))

    # 5. It opens as `>`. The bar it was becomes the chevron's own stroke.
    for i in range(5):
        t = ease((i + 1) / 5)

        def go(d, t=t):
            chevron(d, *LEFT_C, t=t)
            box(d, RIGHT_C[0], RIGHT_C[1], EYE_W, EYE_H, EYE_R)
        frame(go)

    # 6. A beat. Nothing moves at all, so the blink and the close read as
    #    two events rather than one long one.
    for _ in range(4):
        def go(d):
            chevron(d, *LEFT_C)
            box(d, RIGHT_C[0], RIGHT_C[1], EYE_W, EYE_H, EYE_R)
        frame(go)

    # 7. Only now does the right box close -- downward, bottom edge pinned to
    #    the baseline, so it lands as `_` and not as `-`.
    for i in range(5):
        t = ease((i + 1) / 5)
        h = lerp(EYE_H, BAR_H, t)
        w = lerp(EYE_W, BAR_W, t)

        def go(d, h=h, w=w):
            chevron(d, *LEFT_C)
            box(d, RIGHT_C[0], BAR_BOTTOM - h / 2, w, h,
                lerp(EYE_R, BAR_H / 2, t))
        frame(go)

    # 8. Two seconds on the finished prompt. The last frame is also what a
    #    warm reboot lands on, so it has to be the whole of it.
    for _ in range(24):
        def go(d):
            chevron(d, *LEFT_C)
            box(d, RIGHT_C[0], BAR_BOTTOM - BAR_H / 2, BAR_W, BAR_H,
                BAR_H / 2)
        frame(go)

    return frames


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--bg", default="76b1db", help="ground colour, hex")
    ap.add_argument("--ink", default="ffffff", help="shape colour, hex")
    ap.add_argument("--out", required=True, help="directory for the PNGs")
    a = ap.parse_args()

    bg = np.array([int(a.bg[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)
    ink = np.array([int(a.ink[i:i + 2], 16) for i in (0, 2, 4)], np.uint8)

    frames = build(bg, ink)
    os.makedirs(a.out, exist_ok=True)
    for p in os.listdir(a.out):
        if p.endswith(".png"):
            os.remove(os.path.join(a.out, p))
    for i, f in enumerate(frames):
        Image.fromarray(f).save(os.path.join(a.out, f"{i:03d}.png"))
    print(f"{len(frames)} frames ({len(frames) / FPS:.2f} s) -> {a.out}")


if __name__ == "__main__":
    main()
