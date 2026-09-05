"""Synthetic LevelDB files, built in memory.

Fixtures are generated, never captured. The stores this reader targets hold
the owner's conversations, so a captured fixture would put chat text in a
public repository -- see the plan's global constraints.
"""
import struct

from pc import leveldb

BLOCK = 32768
# Local, not imported from pc.leveldb: this fixture has to be importable
# before wal_entries and its constants exist, or the first TDD run fails with
# the wrong error and the next agent debugs the fixture instead of the code.
HEADER = 7
FULL, FIRST, MIDDLE, LAST = 1, 2, 3, 4


def _mask(raw: int) -> int:
    return (((raw >> 15) | (raw << 17)) + leveldb.CRC_MASK_DELTA) & 0xffffffff


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7f
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def batch(entries) -> bytes:
    """One WriteBatch payload: seq(8) count(4) then tagged entries."""
    body = bytearray()
    for op, key, value in entries:
        if op == "put":
            body += b"\x01" + _varint(len(key)) + key
            body += _varint(len(value)) + value
        else:
            body += b"\x00" + _varint(len(key)) + key
    return struct.pack("<QI", 1, len(entries)) + bytes(body)


def frame(payload: bytes, block_size: int = BLOCK,
          corrupt_crc: bool = False) -> bytes:
    """Wrap one payload in physical records, splitting across blocks."""
    out = bytearray()
    pos = 0
    first = True
    while True:
        room = block_size - (len(out) % block_size)
        if room < HEADER:
            out += b"\x00" * room
            room = block_size
        avail = room - HEADER
        chunk = payload[pos:pos + avail]
        pos += len(chunk)
        done = pos >= len(payload)
        if first and done:
            kind = FULL
        elif first:
            kind = FIRST
        elif done:
            kind = LAST
        else:
            kind = MIDDLE
        crc = _mask(leveldb.crc32c(bytes([kind]) + chunk))
        if corrupt_crc:
            crc ^= 0xffffffff
        out += struct.pack("<IHB", crc, len(chunk), kind) + chunk
        first = False
        if done:
            return bytes(out)


def build_log(batches, block_size: int = BLOCK) -> bytes:
    """A whole .log file from a list of entry-lists."""
    return b"".join(frame(batch(e), block_size) for e in batches)
