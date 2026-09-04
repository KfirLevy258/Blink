# Company logos

One `.bin` per company: the BLGO image `tools/burn.sh --logo` writes to a
unit's `logo` partition, played after the boot animation. The format and its
limits are in `tools/encode_logo.py`; the clip is drawn by
`tools/make_logo_scan.py`.

| File | What it is | Size |
|---|---|---|
| `pipl.bin` | `pipl` in brand blue on white | 97 KB (18% of the partition) |
| `pipl-dark.bin` | the same mark in white on near-black | 109 KB (21%) |

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
    --bg FFFFFF --ink 3AA1FF --accent 005AAC --out /tmp/pipl
tools/encode_logo.py --frames /tmp/pipl --fps 15 --hold 1.8 --threshold 0 \
    --out logos/pipl.bin

tools/make_logo_scan.py --art tools/assets/pipl-wordmark.png \
    --bg 0E1621 --ink FFFFFF --accent 3AA1FF --out /tmp/pipl-dark
tools/encode_logo.py --frames /tmp/pipl-dark --fps 15 --hold 1.8 --threshold 0 \
    --out logos/pipl-dark.bin
```

`--threshold 0` matters: see the note in `tools/make_logo_scan.py`. With the
default gate of 10 the cooling trail moves too slowly to clear it, columns
freeze part-cooled, and the finished mark keeps faint vertical bands.

`make_logo_scan.py` takes any company's artwork and any three colours, so the
next company needs no new script -- only a mask in `tools/assets/`.

## The pipl artwork and colours

`tools/assets/pipl-wordmark.png` is a coverage mask made from the official
569x351 logo the customer supplied. That file is flat blue on an opaque white
ground with no alpha channel, so coverage was recovered from how far each
pixel travelled from white towards the ink, read off the red channel -- the
longest throw (255 -> 58) and so the least quantisation. Recomposing the mask
over white returns the supplied file to a mean error of 0.04/255, which is to
say the mask is the artwork and not a resemblance of it.

- `#3AA1FF` is the mark's own fill, read straight out of that file.
- `#005AAC` is the same hue and saturation at 55% of the lightness. It is the
  scanning rule and the trail it leaves cooling. It is *derived*, not a second
  brand colour -- swap it if Pipl publishes one.

The registered mark is not on this artwork; on a 320x240 panel it would be
about three pixels across.
