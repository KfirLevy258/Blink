# Company logos

One `.bin` per company: the BLGO image `tools/burn.sh --logo` writes to a
unit's `logo` partition, played after the boot animation. The format and its
limits are in `tools/encode_logo.py`.

| File | What it is | Size |
|---|---|---|
| `pipl.bin` | `pipl` wordmark, resolved by a scanning rule | 58 KB (11% of the partition) |

Flash a unit with one:

```sh
tools/burn.sh --edition claude --logo logos/pipl.bin
```

Look at one without a board:

```sh
tools/encode_logo.py --info logos/pipl.bin --preview /tmp/pipl.gif
```

## Rebuilding pipl.bin

```sh
tools/make_logo_pipl.py --out /tmp/pipl-frames
tools/encode_logo.py --frames /tmp/pipl-frames --fps 15 --hold 1.6 \
    --out logos/pipl.bin
```

`make_logo_pipl.py` takes `--word`, `--bg`, `--ink`, `--accent` and `--font`,
so the same clip sets another company's wordmark, and the colours below can be
replaced with the brand's own the moment they are to hand.

**The colours here are a stand-in, not Pipl's brand values.** They were chosen
to suit the panel (a deep navy `#0B1F3A`, white letters, a `#29B6F6` accent);
nobody has checked them against a Pipl style guide. Same for the letterforms:
the wordmark is set in Outfit Bold (`tools/fonts/`, OFL), a geometric sans of
the right family, not Pipl's own face. Replace both before a unit ships.
