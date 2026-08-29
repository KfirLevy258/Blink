#!/usr/bin/env python3
"""Draw the sleep and wake clips -- one design, two grounds.

    tools/make_sleepanim.py --out DIR

The eyes close, and Zs rise from the pillow side like the sleeping-face
emoji. Same for both editions; only the ground and ink differ (terracotta
and black for Claude, steel blue and white for Codex). Shapes are drawn at
4x and thresholded, as the boot clips are, so nothing shimmers; the Zs fade
by blending toward the ground, which the encoder carries as ordinary pixels.
"""
import argparse
import math
import os
import sys

import numpy as np
from PIL import Image, ImageDraw

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import make_bootanim_codex as bc  # noqa: E402

W, H, SS, FPS = bc.W, bc.H, bc.SS, bc.FPS
LEFT_C, RIGHT_C = bc.LEFT_C, bc.RIGHT_C
EYE_W, EYE_H, EYE_R = bc.EYE_W, bc.EYE_H, bc.EYE_R
SHUT_H = 6
ease, lerp = bc.ease, bc.lerp

EDITIONS = {
    "claude": dict(bg=(0xcd, 0x79, 0x5a), ink=(0, 0, 0)),
    "codex": dict(bg=(0x4c, 0x82, 0xa8), ink=(0xff, 0xff, 0xff)),
}

# The Zs: born just above the right eye, drifting up and to the right,
# growing as they go and fading out in the last third of their life.
Z_BORN = (232.0, 88.0)
Z_DRIFT = (52.0, -62.0)
LOOP_S = 2.5                # the sleeping loop; every motion is periodic in it
Z_IN_AIR = 3
Z_EVERY_S = LOOP_S / Z_IN_AIR
Z_LIFE_S = LOOP_S
Z_SIZE = (12.0, 24.0)       # glyph height at birth and at death


def layers(bg, parts):
    """Compose [(mask_image_4x, ink_rgb), ...] over the ground."""
    out = np.empty((H, W, 3), np.float32)
    out[:] = bg
    for im, ink in parts:
        a = np.asarray(im.resize((W, H), Image.LANCZOS), np.float32) / 255.0
        a = a[..., None]
        out = out * (1 - a) + np.array(ink, np.float32) * a
    return out.round().clip(0, 255).astype(np.uint8)


def mask():
    return Image.new("L", (W * SS, H * SS), 0)


def eyes(d, lh, rh, dy=0.0):
    """Two eyes closing from the top: the bottom edge stays put."""
    bc.box(d, LEFT_C[0], LEFT_C[1] + dy + (EYE_H - lh) / 2, EYE_W, lh, min(EYE_R, lh / 2))
    bc.box(d, RIGHT_C[0], RIGHT_C[1] + dy + (EYE_H - rh) / 2, EYE_W, rh, min(EYE_R, rh / 2))


def zed(d, cx, cy, size, stroke):
    """A capital Z with round caps: top bar, diagonal, bottom bar."""
    w = size * 0.8
    pts = [(cx - w / 2, cy - size / 2), (cx + w / 2, cy - size / 2),
           (cx - w / 2, cy + size / 2), (cx + w / 2, cy + size / 2)]
    d.line([(x * SS, y * SS) for x, y in pts], fill=255,
           width=max(1, int(stroke * SS)), joint="curve")
    r = stroke * SS / 2
    for x, y in pts:
        d.ellipse([x * SS - r, y * SS - r, x * SS + r, y * SS + r], fill=255)


def z_layer(t, bg, ink):
    """Every Z alive at time t (seconds into the loop), as one blended mask
    per Z so each can carry its own fade."""
    parts = []
    for k in range(-Z_IN_AIR, Z_IN_AIR + 1):
        born = k * Z_EVERY_S
        age = t - born
        if age < 0 or age >= Z_LIFE_S:
            continue
        u = age / Z_LIFE_S
        sway = 6 * math.sin(u * math.pi * 2)
        x = Z_BORN[0] + Z_DRIFT[0] * u + sway
        y = Z_BORN[1] + Z_DRIFT[1] * ease(u)
        size = lerp(Z_SIZE[0], Z_SIZE[1], u)
        fade = 1.0 if u < 0.62 else 1 - (u - 0.62) / 0.38
        im = mask()
        zed(ImageDraw.Draw(im), x, y, size, stroke=max(3.0, size * 0.22))
        colour = tuple(lerp(b, i, fade) for b, i in zip(bg, ink))
        parts.append((im, colour))
    return parts


def frame(bg, ink, lh, rh, t=None, dy=0.0):
    m = mask()
    eyes(ImageDraw.Draw(m), lh, rh, dy)
    parts = [(m, ink)]
    if t is not None:
        parts += z_layer(t, bg, ink)
    return layers(bg, parts)


def closing(bg, ink):
    """Part 1: the eyes as the boot left them, then one slow fall to shut."""
    F = []
    for _ in range(int(0.6 * FPS)):
        F.append(frame(bg, ink, EYE_H, EYE_H))
    n = int(0.7 * FPS)
    for i in range(n):
        h = lerp(EYE_H, SHUT_H, ease((i + 1) / n))
        F.append(frame(bg, ink, h, h))
    for _ in range(int(0.3 * FPS)):
        F.append(frame(bg, ink, SHUT_H, SHUT_H))
    return F


def sleeping(bg, ink):
    """Part 2: the loop the device plays for as long as the computer sleeps.

    Exactly LOOP_S long and periodic in every motion -- Z births, Z fades,
    the breathing sine -- so the last frame hands over to the first with
    nothing popping. The device replays this blob end to end."""
    F = []
    n = int(round(LOOP_S * FPS))
    for i in range(n):
        t = i / FPS
        dy = 1.5 * (0.5 - 0.5 * math.cos(2 * math.pi * t / LOOP_S))
        F.append(frame(bg, ink, SHUT_H, SHUT_H, t=t, dy=dy))
    return F


def opening(bg, ink):
    """Part 3: the eyes open, one quick blink, done."""
    F = []
    for _ in range(3):
        F.append(frame(bg, ink, SHUT_H, SHUT_H))
    for i in range(5):
        h = lerp(SHUT_H, EYE_H, ease((i + 1) / 5))
        F.append(frame(bg, ink, h, h))
    for _ in range(4):
        F.append(frame(bg, ink, EYE_H, EYE_H))
    for i in range(4):          # one quick blink: awake
        k = 1 - abs(2 * (i + 1) / 4 - 1)
        h = lerp(EYE_H, SHUT_H, ease(k))
        F.append(frame(bg, ink, h, h))
    for _ in range(6):
        F.append(frame(bg, ink, EYE_H, EYE_H))
    return F


def gif(frames, path, scale=2):
    ims = [Image.fromarray(fr).resize((W * scale, H * scale), Image.NEAREST) for fr in frames]
    ims[0].save(path, save_all=True, append_images=ims[1:], duration=int(1000 / FPS),
                loop=0, optimize=False)


def sheet(frames, path, picks):
    cols = len(picks)
    out = Image.new("RGB", (W * cols + 8 * (cols - 1), H), (20, 20, 20))
    for i, p in enumerate(picks):
        out.paste(Image.fromarray(frames[min(p, len(frames) - 1)]), (i * (W + 8), 0))
    out.save(path)


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--out", required=True, help="GIF previews and sheets")
    ap.add_argument("--frames-out", help="also write PNG frames: DIR/<edition>/<part>/NNN.png, "
                    "the input tools/encode_bootanim.py --frames takes")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    for name, ed in EDITIONS.items():
        c, l, o = (closing(ed["bg"], ed["ink"]), sleeping(ed["bg"], ed["ink"]),
                   opening(ed["bg"], ed["ink"]))
        if a.frames_out:
            for part, frames in (("close", c), ("loop", l), ("open", o)):
                d = os.path.join(a.frames_out, name, part)
                os.makedirs(d, exist_ok=True)
                for old in os.listdir(d):
                    if old.endswith(".png"):
                        os.remove(os.path.join(d, old))
                for i, fr in enumerate(frames):
                    Image.fromarray(fr).save(os.path.join(d, f"{i:03d}.png"))
        gif(c, os.path.join(a.out, f"{name}-1-closing.gif"))
        gif(l, os.path.join(a.out, f"{name}-2-sleeping-loop.gif"))
        gif(o, os.path.join(a.out, f"{name}-3-opening.gif"))
        gif(c + l * 3 + o, os.path.join(a.out, f"{name}-preview.gif"))
        sheet(l, os.path.join(a.out, f"{name}-loop-sheet.png"),
              [0, len(l) // 6, len(l) // 3, len(l) // 2, 2 * len(l) // 3, 5 * len(l) // 6])
        print(f"{name}: closing {len(c)} f ({len(c) / FPS:.1f} s), loop {len(l)} f ({len(l) / FPS:.1f} s), opening {len(o)} f ({len(o) / FPS:.1f} s)")


if __name__ == "__main__":
    main()
