"""A read-only reader for the LevelDB stores Chromium applications keep.

Deliberately knows nothing about Claude: this is the substrate under two
different Claude Desktop stores, and keeping it ignorant is what lets it be
tested against synthetic data rather than against an application we do not
control.

Read-only is not a style choice. These files belong to a running application
that compacts and deletes them underneath us, so nothing here opens a
database, takes a lock, or holds a handle open longer than one read.
"""

CRC32C_POLY_REVERSED = 0x82f63b78
CRC_MASK_DELTA = 0xa282ead8

# Built once. The table is 1 KiB and the alternative is a bit-at-a-time loop
# over every byte of a 600 KiB log on every poll.
_CRC_TABLE = []
for _i in range(256):
    _c = _i
    for _ in range(8):
        _c = (_c >> 1) ^ (CRC32C_POLY_REVERSED if _c & 1 else 0)
    _CRC_TABLE.append(_c)


def crc32c(data: bytes) -> int:
    """Castagnoli CRC-32, which is the one LevelDB uses -- not zlib's."""
    c = 0xffffffff
    for b in data:
        c = _CRC_TABLE[(c ^ b) & 0xff] ^ (c >> 8)
    return c ^ 0xffffffff


def unmask_crc(masked: int) -> int:
    """Undo the rotation LevelDB applies so a stored CRC is never its own."""
    rot = (masked - CRC_MASK_DELTA) & 0xffffffff
    return ((rot >> 17) | (rot << 15)) & 0xffffffff


def read_varint(buf: bytes, pos: int) -> tuple:
    """(value, position after it). Raises IndexError on a truncated buffer.

    The caller catches: a truncated varint means a torn record, and the right
    response is to skip that record, not to invent a length for it.
    """
    value = 0
    shift = 0
    while True:
        b = buf[pos]
        pos += 1
        value |= (b & 0x7f) << shift
        if b < 0x80:
            return value, pos
        shift += 7
