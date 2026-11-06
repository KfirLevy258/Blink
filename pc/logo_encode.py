"""The company logo image, built and checked with the standard library (S15.2).

tools/encode_logo.py and tools/encode_bootanim.py are NumPy throughout, and
moving them into pc/ would ship NumPy to every customer, so this is the same
algorithm written again in the same order, and tests/test_logo_encode_parity.py
holds the two to identical bytes on generated fixtures. The order matters: the
noise gate runs on RGB888 against the last frame SHOWN, and only then does
RGB565 conversion happen, because the gate's decision is not visible after it
(from black, red 10 and red 11 convert to one RGB565 value and must decide
differently).

The board refuses a logo over the caps below before it shows it (logo_parse.c)
and this refuses before it sends. They are provisional until plan Task 6.3
step 0 measures the factory logos that already exist; tools/encode_logo.py
imports them from here so there is one place to change them.

Image layout (unchanged from the factory tool, firmware/src/logo_parse.h):
  "BLGO" u16 version(1) u16 hold_ms u32 blob_len u32 crc32 u8 r g b  0-pad to 32
  BAN1 blob (tools/encode_bootanim.py): "BAN1" u16 w u16 h u8 fps u8 flags(1,
  big-endian pixels) u16 nframes; per frame u8 rect_count, per rect
  u16 x y w h u32 payload_len + PackBits16 payload. The image is 0xFF-padded
  to a multiple of 32.
"""
from __future__ import annotations

import struct
import zlib
from array import array

from pc import gif_decode

W, H = 320, 240
LOGO_MAX = 0x80000
LOGO_MAX_FRAMES = 180
LOGO_FPS_MIN, LOGO_FPS_MAX = 1, 60
LOGO_MAX_TOTAL_MS = 15000
# A frame record's rect count is one byte and the encoder cuts rects as bands
# of changed rows, so 240 rows give at most 120 bands (a band needs an
# unchanged row after it): 120 is every frame the encoder can write, and a
# count above it can only be a hand-built blob.
LOGO_MAX_RECTS = 120
HDR_LEN = 32
ALIGN = 32
MAGIC = b"BLGO"
VERSION = 1
# The page's "Stays up for". 30 s is gone: play plus hold must fit the total.
HOLD_CHOICES = (3000, 5000, 10000)
FIT_SCALE = {"fill": 1.0, "large": 0.8, "margin": 0.6}

# Fixed sentences: the factory tool prints the same ones.
E_FRAMES = "too many frames for the board"
E_NOFRAMES = "the logo has no frames"
E_FPS = "frame rate outside what the board plays"
E_TOTAL = "the logo would play too long"
E_RECTS = "too many changed areas in one frame"
E_RECT = "a changed area lies outside the screen"
E_SIZE = "the logo is too big for the board"
E_PAYLOAD = "the logo data is damaged"
E_HOLD = "hold time not allowed"
E_FIT = "size on the screen not allowed"
E_RGBA = "not a 320x240 picture"
E_BLGO = "not a logo image"

letterbox = gif_decode.letterbox


def quantise(frames888, threshold):
    """The tool's gate (encode_logo.py quantise): a pixel moves only if some
    channel differs by more than `threshold` from what is currently shown.
    frames888: list of frames, each a list of 240 rows of 960 bytes. Returns
    the frames as shown, still RGB888; convert after."""
    if not frames888:
        return []
    shown = [frames888[0]]
    for f in frames888[1:]:
        prev = shown[-1]
        out = []
        for fr, pr in zip(f, prev):
            if fr == pr:
                out.append(pr)
                continue
            row = bytearray(fr)
            for i in range(0, len(fr), 3):
                if (abs(fr[i] - pr[i]) <= threshold
                        and abs(fr[i + 1] - pr[i + 1]) <= threshold
                        and abs(fr[i + 2] - pr[i + 2]) <= threshold):
                    row[i:i + 3] = pr[i:i + 3]
            out.append(bytes(row))
        shown.append(out)
    return shown


def to_rgb565(rows):
    """240 rows of 960 RGB888 bytes -> a flat array('H') of 76800 RGB565
    values (2 bytes each: a list of ints was ~500 MB over 180 frames)."""
    out, seen = array("H"), {}
    for row in rows:
        px = seen.get(row)
        if px is None:
            px = array("H", [((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)
                             for r, g, b in zip(row[0::3], row[1::3], row[2::3])])
            seen[row] = px
        out.extend(px)
    return out


def _bg565(bg):
    """The colour the screen really shows for `bg`: the header must carry the
    quantised one, as the tool does (bit-replicated expansion)."""
    r, g, b = bg[0] >> 3, bg[1] >> 2, bg[2] >> 3
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))


def _packbits16(px, start, stop):
    out = bytearray()
    i, n = start, stop
    while i < n:
        run = 1
        while i + run < n and run < 129 and px[i + run] == px[i]:
            run += 1
        if run >= 2:
            out.append(128 + run - 2)
            out += struct.pack(">H", px[i])
            i += run
            continue
        j = i + 1
        while j < n and j - i < 128:
            if j + 2 < n and px[j] == px[j + 1] == px[j + 2]:
                break
            j += 1
        out.append(j - i - 1)
        for p in px[i:j]:
            out += struct.pack(">H", p)
        i = j
    return bytes(out)


def _rect_payload(frame, x, y, w, h):
    px = array("H")
    for yy in range(y, y + h):
        px.extend(frame[yy * W + x:yy * W + x + w])
    return _packbits16(px, 0, len(px))


def _changed_rects(frame, prev):
    """Contiguous changed-row bands, each cropped to its changed columns
    (encode_bootanim.changed_rects)."""
    rects, band, last = [], None, None
    cols_lo, cols_hi = W, -1
    for y in range(H + 1):
        changed = False
        if y < H:
            a, b = frame[y * W:(y + 1) * W], prev[y * W:(y + 1) * W]
            if a != b:
                changed = True
                diff = [i for i, (p, q) in enumerate(zip(a, b)) if p != q]
                cols_lo, cols_hi = min(cols_lo, diff[0]), max(cols_hi, diff[-1])
        if changed:
            if band is None:
                band = y
            last = y
        elif band is not None:
            rects.append((cols_lo, band, cols_hi - cols_lo + 1, last - band + 1))
            band, cols_lo, cols_hi = None, W, -1
    return rects


def encode_ban1(frames565, fps):
    """Frame 0 full, later frames the changed rects only, PackBits16;
    byte-identical to tools/encode_bootanim.encode(big_endian=True)."""
    blob = bytearray(b"BAN1")
    blob += struct.pack("<HHBBH", W, H, fps, 1, len(frames565))
    prev = None
    for frame in frames565:
        rects = [(0, 0, W, H)] if prev is None else _changed_rects(frame, prev)
        blob.append(len(rects))     # at most 120 bands in 240 rows: fits a byte
        for x, y, w, h in rects:
            payload = _rect_payload(frame, x, y, w, h)
            blob += struct.pack("<HHHHI", x, y, w, h, len(payload)) + payload
        prev = frame
        if len(blob) + HDR_LEN > LOGO_MAX:
            # Early: a noisy 140-frame GIF burned 19.8 s encoding frames the
            # size cap was always going to refuse.
            raise ValueError(E_SIZE)
    return bytes(blob)


def check_limits(nframes, fps, hold_ms):
    """ValueError with a fixed sentence for what the board would refuse."""
    if nframes < 1:
        raise ValueError(E_NOFRAMES)
    if nframes > LOGO_MAX_FRAMES:
        raise ValueError(E_FRAMES)
    if not LOGO_FPS_MIN <= fps <= LOGO_FPS_MAX:
        raise ValueError(E_FPS)
    if not 0 <= hold_ms <= 0xFFFF:
        raise ValueError(E_HOLD)
    # nframes * 1000 / fps + hold_ms > MAX, in integers
    if nframes * 1000 > (LOGO_MAX_TOTAL_MS - hold_ms) * fps:
        raise ValueError(E_TOTAL)


def build(frames565, fps, hold_ms, bg):
    check_limits(len(frames565), fps, hold_ms)
    blob = encode_ban1(frames565, fps)
    hdr = MAGIC + struct.pack("<HHII", VERSION, hold_ms, len(blob),
                              zlib.crc32(blob) & 0xffffffff)
    hdr = (hdr + bytes(bg) + b"\0").ljust(HDR_LEN, b"\0")
    img = hdr + blob
    img += b"\xff" * (-len(img) % ALIGN)
    parse(img)                      # the round trip: refuse what we would not accept
    return img


def _walk_payload(img, off, plen, count):
    """PackBits16 over exactly `count` pixels and exactly `plen` bytes; no run
    may cross `count`. Returns the pixels as a list of ints when asked."""
    end = off + plen
    px, got = [], 0
    while got < count:
        if off >= end:
            raise ValueError(E_PAYLOAD)
        c = img[off]
        off += 1
        if c < 128:
            n = c + 1
            if off + 2 * n > end or got + n > count:
                raise ValueError(E_PAYLOAD)
            px += struct.unpack(">%dH" % n, img[off:off + 2 * n])
            off += 2 * n
        else:
            n = c - 126
            if off + 2 > end or got + n > count:
                raise ValueError(E_PAYLOAD)
            px += struct.unpack(">H", img[off:off + 2]) * n
            off += 2
        got += n
    if off != end:
        raise ValueError(E_PAYLOAD)
    return px


def parse(img, check=True, frames=False):
    """The daemon's own check of an image, with the board's caps (`check`
    False reads the headers and walks without enforcing them: tools/
    encode_logo.py --info on a factory .bin that predates the caps).
    Returns the header fields, `rects` (per frame) and `max_rects`, and with
    `frames` the decoded frames as flat RGB565 lists."""
    if len(img) < HDR_LEN + 12 or img[:4] != MAGIC:
        raise ValueError(E_BLGO)
    version, hold_ms, blob_len, crc = struct.unpack_from("<HHII", img, 4)
    if version != VERSION:
        raise ValueError(E_BLGO)
    if blob_len < 12 or blob_len > len(img) - HDR_LEN:
        raise ValueError(E_PAYLOAD)
    if check and len(img) > LOGO_MAX:
        raise ValueError(E_SIZE)
    blob = img[HDR_LEN:HDR_LEN + blob_len]
    if zlib.crc32(blob) & 0xffffffff != crc:
        raise ValueError(E_PAYLOAD)
    if blob[:4] != b"BAN1":
        raise ValueError(E_PAYLOAD)
    w, h, fps, flags, n = struct.unpack_from("<HHBBH", blob, 4)
    if (w, h, flags) != (W, H, 1):
        raise ValueError(E_PAYLOAD)
    if check:
        check_limits(n, fps, hold_ms)
    elif n < 1:
        raise ValueError(E_NOFRAMES)
    off, rects, out = 12, [], []
    canvas = [0] * (W * H) if frames else None
    for _ in range(n):
        if off >= blob_len:
            raise ValueError(E_PAYLOAD)
        nr = blob[off]
        off += 1
        if check and nr > LOGO_MAX_RECTS:
            raise ValueError(E_RECTS)
        rects.append(nr)
        for _ in range(nr):
            if off + 12 > blob_len:
                raise ValueError(E_PAYLOAD)
            x, y, rw, rh, plen = struct.unpack_from("<HHHHI", blob, off)
            off += 12
            if rw == 0 or rh == 0 or x + rw > W or y + rh > H:
                raise ValueError(E_RECT)
            if plen > blob_len - off:
                raise ValueError(E_PAYLOAD)
            px = _walk_payload(blob, off, plen, rw * rh)
            off += plen
            if frames:
                for r in range(rh):
                    base = (y + r) * W + x
                    canvas[base:base + rw] = px[r * rw:(r + 1) * rw]
        if frames:
            out.append(list(canvas))
    if off != blob_len:
        raise ValueError(E_PAYLOAD)
    info = {"hold_ms": hold_ms, "bg": tuple(img[16:19]), "blob_len": blob_len,
            "fps": fps, "nframes": n, "rects": rects,
            "max_rects": max(rects)}
    if frames:
        info["frames"] = out
    return info


def gif_timing(delays_ms, hold_ms=0):
    """(fps, ticks): BAN1 has one integer fps and the player ticks every
    1000 // fps ms, so pick the fps in range whose tick best fits the delays
    (least sum |d - n*tick|, n = max(1, round(d/tick)); ties to the lower
    fps). A delay of 0 or under 20 ms is 100 ms, as browsers treat it.
    Only fps values whose ticks fit LOGO_MAX_FRAMES and the total cap (with
    `hold_ms`) are candidates, and the LAST frame's ticks are cut to fit
    first, because the hold keeps that frame up anyway: 30 frames of 30 ms
    then a 5 s last frame is fps 29 (tick 34, 177 ticks, fits whole; fps 33
    would need 197 and a cut last frame), not an error. With no candidate
    the unconstrained best is returned and check_limits says why."""
    ds = [100 if d < 20 else d for d in delays_ms]
    best = fallback = None
    for fps in range(LOGO_FPS_MIN, LOGO_FPS_MAX + 1):
        tick = 1000 // fps
        ticks = [max(1, round(d / tick)) for d in ds]
        err0 = sum(abs(d - n * tick) for d, n in zip(ds, ticks))
        if fallback is None or err0 < fallback[0]:
            fallback = (err0, fps, ticks)
        cap = min(LOGO_MAX_FRAMES, (LOGO_MAX_TOTAL_MS - hold_ms) * fps // 1000)
        over = sum(ticks) - cap
        if over > 0:
            ticks = ticks[:-1] + [ticks[-1] - min(over, ticks[-1] - 1)]
            if sum(ticks) > cap:
                continue
        err = sum(abs(d - n * tick) for d, n in zip(ds, ticks))
        if best is None or err < best[0]:
            best = (err, fps, ticks)
    best = best or fallback
    return best[1], best[2]


def from_rgba(rgba, bg, hold_ms):
    """One still. The browser drew the fit, so none is applied here."""
    if hold_ms not in HOLD_CHOICES:
        raise ValueError(E_HOLD)
    if len(rgba) != W * H * 4:
        raise ValueError(E_RGBA)
    bg = tuple(bg)
    alpha = rgba[3::4]
    solid = alpha.count(255) == W * H
    rows = []
    for y in range(H):
        row = rgba[y * W * 4:(y + 1) * W * 4]
        out = bytearray(W * 3)
        if solid:
            out[0::3], out[1::3], out[2::3] = row[0::4], row[1::4], row[2::4]
        else:
            for x in range(W):
                a = row[4 * x + 3]
                for c in range(3):
                    out[3 * x + c] = (row[4 * x + c] * a
                                      + bg[c] * (255 - a)) // 255
        rows.append(bytes(out))
    return build([to_rgb565(rows)], 1, hold_ms, _bg565(bg))


def decode_gif(data, bg, scale=1.0):
    """gif_decode.decode as a list, with the frame cap in the board's own
    sentence (one sentence for one limit)."""
    try:
        return list(gif_decode.decode(data, LOGO_MAX_FRAMES, bg, scale))
    except gif_decode.GifError as e:
        if str(e) == "too many frames":
            raise ValueError(E_FRAMES) from None
        raise


def gif_frames565(data, bg, scale=1.0, threshold=10, hold_ms=0):
    """Decode, gate and convert a GIF: (frames565 with each source frame
    repeated for its ticks, fps). A repeated frame costs a zero-rect record.
    The caps are checked before the gate runs, so a clip that is too long is
    refused having cost only the decode."""
    got = decode_gif(data, bg, scale)
    fps, ticks = gif_timing([d for d, _ in got], hold_ms)
    check_limits(sum(ticks), fps, hold_ms)
    shown = quantise([rows for _, rows in got], threshold)
    del got
    out = []
    for rows, n in zip(shown, ticks):
        out += [to_rgb565(rows)] * n
    return out, fps


def from_gif(data, bg, hold_ms, fit):
    if hold_ms not in HOLD_CHOICES:
        raise ValueError(E_HOLD)
    if fit not in FIT_SCALE:
        raise ValueError(E_FIT)
    bg = tuple(bg)
    frames, fps = gif_frames565(data, bg, FIT_SCALE[fit], hold_ms=hold_ms)
    return build(frames, fps, hold_ms, _bg565(bg))
