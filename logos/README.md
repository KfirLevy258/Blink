# Company logos

One `.bin` per company: the BLGO image `tools/burn.sh --logo` writes to a
unit's `logo` partition, played after the boot animation. The format and its
limits are in `tools/encode_logo.py`; the clip is drawn by
`tools/make_logo_scan.py`.

| File | What it is | Size |
|---|---|---|
| `pipl.bin` | `pipl` in brand blue on white | 92 KB (17% of the partition) |
| `pipl-dark.bin` | the same mark in white on near-black | 95 KB (18%) |

Flash a unit with one:

```sh
tools/burn.sh --edition claude --logo logos/pipl.bin
```

Look at one without a board:

```sh
tools/encode_logo.py --info logos/pipl.bin --preview /tmp/pipl.gif
```

## Rebuilding them

```sh
tools/make_logo_scan.py --art tools/assets/pipl-wordmark.png \
    --bg FFFFFF --ink 4D93E9 --accent 1C4A7D --out /tmp/pipl
tools/encode_logo.py --frames /tmp/pipl --fps 15 --hold 1.8 --out logos/pipl.bin

tools/make_logo_scan.py --art tools/assets/pipl-wordmark.png \
    --bg 0E1621 --ink FFFFFF --accent 4D93E9 --out /tmp/pipl-dark
tools/encode_logo.py --frames /tmp/pipl-dark --fps 15 --hold 1.8 \
    --out logos/pipl-dark.bin
```

`make_logo_scan.py` takes any company's artwork and any three colours, so the
next company needs no new script -- only a mask in `tools/assets/`.

## Where the pipl artwork came from, and what is wrong with it

`tools/assets/pipl-wordmark.png` is a coverage mask **traced from a photograph
of a screen**, because this machine has no route to the network and no
official file could be fetched. The two `p`s are the same glyph, so the line
between their centroids gave the baseline and levelled the shot (-8.3 deg);
the `l` stem gave the residual shear (-4.3 deg); the contour was then
regularised at 4x to pull the LCD moire and JPEG wobble off the edges.

So the silhouette is Pipl's own -- the tailed `l`, the large detached tittle,
the circular bowls -- but it is a trace, not the drawing: the joins are a
little softer than the original and the counters a little rounder. `#4D93E9`
is the median of the mark's fill in that same photo, white-balanced against
the transparency checkerboard behind it; `#1C4A7D` is the darker blue of the
older lockup in the same screenshot. Both are within a few percent, not exact.

**Replace the mask with the official SVG or PNG, and the two hexes with the
brand's own, before a unit ships.** Nothing else has to change: re-run the two
commands above.
