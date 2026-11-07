"""A copy of the logo the daemon last sent, so the settings page can show it.

The board reports only logo_state {present, bytes, frames} and has no way to
read the image back, so the page could say a logo was there and not draw it.
After a successful send the daemon keeps the first frame as a PNG plus the
bytes and frames the board will report, in ~/.blink/private (owner-only,
written atomically by private_file). The page gets the image only when the
board's own report matches that record: a logo set from another computer, or
by an older daemon, matches nothing and the page says so instead of showing a
stale picture. Standard library only; never logs image content.
"""
from __future__ import annotations

import json
import struct
import zlib

from pc import logo_encode, private_file

PNG_NAME = "logo-preview.png"
META_NAME = "logo-preview.json"
# A 320x240 RGB PNG is under 231 KB even uncompressed; anything bigger on
# disk is not ours.
MAX_PNG = 1024 * 1024


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))


def png_from_565(px, w=logo_encode.W, h=logo_encode.H):
    """RGB565 pixels (flat list) as an RGB PNG."""
    rows = bytearray()
    for y in range(h):
        rows.append(0)
        for v in px[y * w:(y + 1) * w]:
            r, g, b = (v >> 11) & 31, (v >> 5) & 63, v & 31
            rows += bytes(((r << 3) | (r >> 2), (g << 2) | (g >> 4),
                           (b << 3) | (b >> 2)))
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(rows), 6))
            + _chunk(b"IEND", b""))


def save(home, blob, state=None):
    """Keep the first frame of `blob` (what the board shows first) and the
    numbers the board will report. The board's own answer wins when it gave
    whole numbers; else they are computed the way logo_write.c does."""
    info = logo_encode.parse(blob, first_frame=True)
    png = png_from_565(info["frames"][0])
    state = state or {}
    n = state.get("bytes")
    f = state.get("frames")
    meta = {"bytes": n if isinstance(n, int) and n > 0
            else logo_encode.HDR_LEN + info["blob_len"],
            "frames": f if isinstance(f, int) and f > 0 else info["nframes"]}
    private_file.write_private(home, PNG_NAME, png)
    private_file.write_private(home, META_NAME, json.dumps(meta).encode())


def load_matching(home, state):
    """The PNG bytes when the board's logo_state is the logo we sent, else
    None. Any trouble reading is "no preview", never an error."""
    if not isinstance(state, dict) or state.get("present") != 1:
        return None
    try:
        meta = json.loads(private_file.open_private(home, META_NAME) or b"")
        if (meta.get("bytes"), meta.get("frames")) != (
                state.get("bytes"), state.get("frames")):
            return None
        png = private_file.open_private(home, PNG_NAME)
    except (OSError, ValueError, AttributeError, private_file.NotPrivate):
        return None
    return png if png and len(png) <= MAX_PNG else None


def remove(home):
    for name in (PNG_NAME, META_NAME):
        try:
            private_file.remove_private(home, name)
        except (OSError, private_file.NotPrivate):
            pass
