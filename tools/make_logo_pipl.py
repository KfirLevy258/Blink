#!/usr/bin/env python3
"""Draw the Pipl company logo clip: a scan line that resolves the wordmark.

A company unit shows its logo after the boot animation (tools/burn.sh --logo).
This writes the 320x240 frames for one; tools/encode_logo.py turns them into
the BLGO image the `logo` partition holds.

The clip is a scanner: a bright rule crosses the screen left to right and the
`pipl` wordmark exists behind it and not in front of it. Pixels the rule has
just passed are accent-coloured and cool to white over the next few
centimetres of travel, so the letters read as being *resolved* rather than
faded in -- which is the company's own business, fragments of identity
becoming one answer. Then a rule draws underneath and the screen holds.

Why it is drawn this way rather than animated freely: the BAN1 delta encoder
stores, per frame, the contiguous bands of rows that changed, cropped to the
changed columns (tools/encode_bootanim.py:changed_rects). One moving vertical
edge changes a narrow strip and nothing else, so the whole clip costs a few
percent of the 512 KB partition. Dots drifting about the screen would have
dirtied every band on every frame and cost twenty times as much for a worse
picture.

The wordmark itself is rendered antialiased, once, and then only *revealed* by
column. Antialiasing is safe here for the reason it was not safe in
make_bootanim_codex.py: nothing moves. A static soft edge is the same pixels
every frame, so it costs nothing after the frame that reveals it and it does
not shimmer. Only the rule and the fading trail move, and both are hard-edged.

  tools/make_logo_pipl.py --out /tmp/pipl-frames
  tools/encode_logo.py --frames /tmp/pipl-frames --fps 15 --hold 1.6 \
      --out pipl.bin --preview /tmp/pipl.gif
  tools/burn.sh --edition claude --logo pipl.bin

The colours and the word are arguments, so the same clip serves the next
company without a second script -- and so the exact brand values can be
dropped in when they are to hand.
"""

import argparse
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFont

W, H = 320, 240
FPS = 15

# The wordmark. Outfit is a geometric sans: circular bowls, even weight, a
# single-storey `l` -- the shape family a lowercase tech wordmark is set in.
FONT_CANDIDATES = (
    os.path.join(os.path.dirname(os.path.abspath(__file__)),
                 "fonts", "Outfit-Bold.ttf"),
    "/mnt/skills/examples/canvas-design/canvas-fonts/Outfit-Bold.ttf",
)
FONT_SIZE = 112
WORD_CY = 104               # centre of the glyph ink, not of the em box

# The scanning rule.
BAR_W = 3                   # px, the rule itself
TRAIL = 44                  # px behind it over which accent cools to ink
BAR_TOP, BAR_BOT = 50, 190  # the rule overhangs the letters, as a rule does

# The underline that lands last.
RULE_H = 3
RULE_Y = 176
RULE_PAD = 4                # it runs the width of the word, plus this

# Beats, in frames at FPS.
N_LEAD = 3                  # black before anything happens
N_SWEEP = 20                # the crossing
N_BEAT = 3                  # a pause on the finished word
N_RULE = 7                  # the underline drawing
N_SETTLE = 4                # stillness before encode_logo's own --hold


def hexcolor(s):
    s = s.lstrip("#")
    if len(s) != 6:
        sys.exit(f"want RRGGBB, got {s!r}")
    return np.array([int(s[i:i + 2], 16) for i in (0, 2, 4)], np.float64)


def ease(t):
    return t * t * (3 - 2 * t)


def find_font():
    for p in FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    sys.exit("no wordmark font found; pass --font")


def render_word(word, font_path, size):
    """The wordmark as two alpha layers: the body, and the tittles.

    The dot over an `i` is the one part of a lowercase wordmark that can carry
    a colour without looking like a mistake, so it is separated out and kept
    accent-coloured after the rest has cooled to white. It is found by
    rendering the word a second time with every `i` swapped for a dotless one
    and taking the difference -- exact, and it needs no guesses about where
    the tittle sits.
    """
    font = ImageFont.truetype(font_path, size)
    dotless = word.replace("i", "ı")

    def draw(text):
        big = Image.new("L", (W * 2, H * 2), 0)
        ImageDraw.Draw(big).text((W // 2, H // 2), text, fill=255,
                                 font=font, anchor="mm")
        return np.asarray(big, np.float64) / 255.0

    body = draw(word)
    if dotless != word:
        tittles = np.clip(body - draw(dotless), 0.0, 1.0)
        # A font without a dotless i renders .notdef and the difference is
        # nonsense; a tittle is a small blob, so refuse anything large.
        if tittles.sum() > 0.25 * body.sum():
            tittles = np.zeros_like(body)
    else:
        tittles = np.zeros_like(body)
    return body, tittles


def build(word, font_path, size, bg, ink, accent):
    body_big, tittle_big = render_word(word, font_path, size)
    # Both layers must be cropped by the SAME offsets or the dot leaves the i.
    ys, xs = np.nonzero(body_big > 0.02)
    if ys.size == 0:
        sys.exit("the wordmark rendered empty")
    ox = (xs.min() + xs.max() + 1) // 2 - W // 2
    oy = (ys.min() + ys.max() + 1) // 2 - WORD_CY
    body = body_big[oy:oy + H, ox:ox + W]
    tittle = tittle_big[oy:oy + H, ox:ox + W]
    word_x0, word_x1 = int(xs.min() - ox), int(xs.max() - ox)

    cols = np.arange(W, dtype=np.float64)
    frames = []

    def compose(line_x, rule_len):
        """One frame: the word revealed up to `line_x`, cooling behind it."""
        out = np.empty((H, W, 3), np.float64)
        out[:] = bg

        # Per-column reveal: 0 in front of the rule, 1 behind it; the tint
        # cools from accent to ink over TRAIL px of travel.
        behind = line_x - cols
        shown = (behind >= 0).astype(np.float64)
        heat = np.clip(1.0 - behind / TRAIL, 0.0, 1.0) * shown
        colour = (ink[None, :] * (1 - heat)[:, None]
                  + accent[None, :] * heat[:, None])          # (W, 3)

        a = (body * shown[None, :])[:, :, None]
        out = out * (1 - a) + colour[None, :, :] * a
        # The tittle never cools: it is the one accent the finished mark keeps.
        at = (tittle * shown[None, :])[:, :, None]
        out = out * (1 - at) + accent[None, None, :] * at

        if rule_len > 0:
            x0 = word_x0 - RULE_PAD
            x1 = min(W, x0 + rule_len)
            out[RULE_Y:RULE_Y + RULE_H, x0:x1] = accent

        # The rule that does the revealing, drawn last so it sits on top.
        bx0 = int(round(line_x)) - BAR_W // 2
        bx0, bx1 = max(0, bx0), min(W, bx0 + BAR_W)
        if bx1 > bx0:
            out[BAR_TOP:BAR_BOT, bx0:bx1] = accent
        return np.clip(out + 0.5, 0, 255).astype(np.uint8)

    for _ in range(N_LEAD):
        frames.append(compose(-BAR_W, 0))
    # The rule leaves the screen and keeps going, because the last column of
    # the word still has to finish cooling after the rule is gone.
    for i in range(N_SWEEP):
        t = (i + 1) / N_SWEEP
        frames.append(compose(t * (W + TRAIL), 0))
    for _ in range(N_BEAT):
        frames.append(compose(W + TRAIL, 0))
    full = word_x1 - word_x0 + 1 + 2 * RULE_PAD
    for i in range(N_RULE):
        frames.append(compose(W + TRAIL,
                              int(round(ease((i + 1) / N_RULE) * full))))
    for _ in range(N_SETTLE):
        frames.append(compose(W + TRAIL, full))
    return frames


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--word", default="pipl", help="the wordmark to set")
    ap.add_argument("--font", default=None, help="a .ttf for the wordmark")
    ap.add_argument("--size", type=int, default=FONT_SIZE)
    ap.add_argument("--bg", default="0B1F3A", help="ground colour, hex")
    ap.add_argument("--ink", default="FFFFFF", help="the settled wordmark")
    ap.add_argument("--accent", default="29B6F6",
                    help="the scanning rule, its trail, the tittle, the rule")
    ap.add_argument("--out", required=True, help="directory for the PNGs")
    a = ap.parse_args()

    frames = build(a.word, a.font or find_font(), a.size,
                   hexcolor(a.bg), hexcolor(a.ink), hexcolor(a.accent))
    os.makedirs(a.out, exist_ok=True)
    for old in sorted(f for f in os.listdir(a.out) if f.endswith(".png")):
        os.remove(os.path.join(a.out, old))
    for i, f in enumerate(frames):
        Image.fromarray(f).save(os.path.join(a.out, f"{i:03d}.png"))
    print(f"{len(frames)} frames @ {FPS} fps "
          f"({len(frames) / FPS:.1f} s) -> {a.out}")


if __name__ == "__main__":
    main()
