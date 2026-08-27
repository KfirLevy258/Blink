"""Decode a BAN1 blob out of a generated bootanim header. Round-trip helper."""
import re, struct, sys
import numpy as np

def load_blob(path, name):
    src = open(path).read()
    m = re.search(r"static const uint8_t %s\[\d+\] = \{(.*?)\};" % name, src, re.S)
    if not m:
        raise SystemExit(f"no blob named {name} in {path}")
    body = m.group(1)
    return bytes(int(x, 16) for x in re.findall(r"0x([0-9a-fA-F]{2})", body))

def unpack565(v, be=True):
    r = (v >> 11) & 0x1F; g = (v >> 5) & 0x3F; b = v & 0x1F
    return ((r << 3) | (r >> 2), (g << 2) | (g >> 4), (b << 3) | (b >> 2))

def decode(blob):
    assert blob[:4] == b"BAN1", blob[:4]
    w, h, fps, flags, n = struct.unpack_from("<HHBBH", blob, 4)
    be = bool(flags & 1)
    off = 12
    canvas = np.zeros((h, w, 3), np.uint8)
    frames = []
    for _ in range(n):
        nrect = blob[off]; off += 1
        for _ in range(nrect):
            x, y, rw, rh, plen = struct.unpack_from("<HHHHI", blob, off); off += 12
            end = off + plen
            px = []
            while off < end:
                c = blob[off]; off += 1
                if c < 128:
                    cnt = c + 1
                    for _ in range(cnt):
                        v = struct.unpack_from(">H" if be else "<H", blob, off)[0]; off += 2
                        px.append(v)
                else:
                    cnt = c - 126
                    v = struct.unpack_from(">H" if be else "<H", blob, off)[0]; off += 2
                    px.extend([v] * cnt)
            arr = np.array([unpack565(v, be) for v in px], np.uint8).reshape(rh, rw, 3)
            canvas[y:y+rh, x:x+rw] = arr
        frames.append(canvas.copy())
    return w, h, fps, frames
