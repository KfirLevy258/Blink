"""A read-only reader for the LevelDB stores Chromium applications keep.

Deliberately knows nothing about Claude: this is the substrate under two
different Claude Desktop stores, and keeping it ignorant is what lets it be
tested against synthetic data rather than against an application we do not
control.

Read-only is not a style choice. These files belong to a running application
that compacts and deletes them underneath us, so nothing here opens a
database, takes a lock, or holds a handle open longer than one read.
"""
import os

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


def snappy_decompress(buf: bytes):
    """The raw Snappy format LevelDB uses for block bodies, or None.

    None rather than an exception: a block that will not decompress is a
    block we skip, and every caller here is already in the business of
    tolerating an unreadable region.
    """
    try:
        expected, pos = read_varint(buf, 0)
    except IndexError:
        return None
    out = bytearray()
    try:
        while pos < len(buf) and len(out) < expected:
            tag = buf[pos]
            pos += 1
            kind = tag & 3
            if kind == 0:
                n = tag >> 2
                if n < 60:
                    n += 1
                else:
                    width = n - 59
                    n = int.from_bytes(buf[pos:pos + width], "little") + 1
                    pos += width
                chunk = buf[pos:pos + n]
                if len(chunk) < n:
                    return None
                out += chunk
                pos += n
                continue
            if kind == 1:
                n = 4 + ((tag >> 2) & 7)
                offset = ((tag >> 5) << 8) | buf[pos]
                pos += 1
            elif kind == 2:
                n = (tag >> 2) + 1
                offset = int.from_bytes(buf[pos:pos + 2], "little")
                pos += 2
            else:
                n = (tag >> 2) + 1
                offset = int.from_bytes(buf[pos:pos + 4], "little")
                pos += 4
            if offset == 0 or offset > len(out):
                return None
            # A byte at a time: a copy may overlap the output it is still
            # producing, which is how Snappy encodes a repeated run.
            start = len(out) - offset
            for i in range(n):
                out.append(out[start + i])
    except IndexError:
        return None
    if len(out) != expected:
        return None
    return bytes(out)


LEVELDB_BLOCK_SIZE = 32768
LEVELDB_HEADER_SIZE = 7

_REC_FULL, _REC_FIRST, _REC_MIDDLE, _REC_LAST = 1, 2, 3, 4


def _wal_payloads(data: bytes):
    """Assembled WriteBatch payloads, skipping anything that fails its CRC."""
    pos = 0
    pending = bytearray()
    have_pending = False
    while pos + LEVELDB_HEADER_SIZE <= len(data):
        in_block = pos % LEVELDB_BLOCK_SIZE
        if LEVELDB_BLOCK_SIZE - in_block < LEVELDB_HEADER_SIZE:
            pos += LEVELDB_BLOCK_SIZE - in_block
            continue
        stored = int.from_bytes(data[pos:pos + 4], "little")
        length = int.from_bytes(data[pos + 4:pos + 6], "little")
        kind = data[pos + 6]
        body = data[pos + LEVELDB_HEADER_SIZE:pos + LEVELDB_HEADER_SIZE + length]
        if len(body) < length:
            return
        pos += LEVELDB_HEADER_SIZE + length
        if kind == 0:
            continue
        if crc32c(bytes([kind]) + body) != unmask_crc(stored):
            # Torn or concurrent write. Drop it and anything it was part of.
            pending = bytearray()
            have_pending = False
            continue
        if kind == _REC_FULL:
            yield bytes(body)
        elif kind == _REC_FIRST:
            pending = bytearray(body)
            have_pending = True
        elif kind == _REC_MIDDLE and have_pending:
            pending += body
        elif kind == _REC_LAST and have_pending:
            pending += body
            yield bytes(pending)
            pending = bytearray()
            have_pending = False


def _batch_entries(payload: bytes):
    """(op, key, value) from one WriteBatch, stopping at the first oddity."""
    if len(payload) < 12:
        return
    pos = 12
    while pos < len(payload):
        tag = payload[pos]
        pos += 1
        try:
            klen, pos = read_varint(payload, pos)
            key = payload[pos:pos + klen]
            pos += klen
            if len(key) < klen:
                return
            if tag == 1:
                vlen, pos = read_varint(payload, pos)
                value = payload[pos:pos + vlen]
                pos += vlen
                if len(value) < vlen:
                    return
                yield "put", key, value
            elif tag == 0:
                yield "del", key, b""
            else:
                return
        except IndexError:
            return


def wal_entries(data: bytes) -> list:
    """Every entry in a write-ahead log, in file order."""
    out = []
    for payload in _wal_payloads(data):
        out.extend(_batch_entries(payload))
    return out


SST_FOOTER_SIZE = 48
SST_MAGIC = 0xdb4775248b80fb57


def _block_body(data: bytes, offset: int, size: int):
    """One block's uncompressed body, or None."""
    raw = data[offset:offset + size]
    if len(raw) < size:
        return None
    kind = data[offset + size:offset + size + 1]
    if kind == b"\x00":
        return raw
    if kind == b"\x01":
        return snappy_decompress(raw)
    return None


def _block_pairs(body: bytes):
    """(key, value) from a block body, ignoring the restart array."""
    if len(body) < 4:
        return
    n_restarts = int.from_bytes(body[-4:], "little")
    end = len(body) - 4 - n_restarts * 4
    if end < 0:
        return
    pos = 0
    prev = b""
    while pos < end:
        try:
            shared, pos = read_varint(body, pos)
            unshared, pos = read_varint(body, pos)
            vlen, pos = read_varint(body, pos)
        except IndexError:
            return
        if shared > len(prev):
            return
        key = prev[:shared] + body[pos:pos + unshared]
        pos += unshared
        value = body[pos:pos + vlen]
        pos += vlen
        if len(key) < shared + unshared or len(value) < vlen:
            return
        prev = key
        yield key, value


def sst_entries(data: bytes) -> list:
    """Every entry in an immutable table, in key order.

    Walks the index block to find data blocks rather than guessing at
    offsets, and returns [] for anything that does not look like a table --
    an application we do not control is allowed to change its format, and the
    correct response is silence.
    """
    if len(data) < SST_FOOTER_SIZE:
        return []
    footer = data[-SST_FOOTER_SIZE:]
    if int.from_bytes(footer[-8:], "little") != SST_MAGIC:
        return []
    try:
        _, pos = read_varint(footer, 0)          # metaindex offset
        _, pos = read_varint(footer, pos)        # metaindex size
        index_off, pos = read_varint(footer, pos)
        index_size, pos = read_varint(footer, pos)
    except IndexError:
        return []

    index_body = _block_body(data, index_off, index_size)
    if index_body is None:
        return []

    out = []
    for _, handle in _block_pairs(index_body):
        try:
            off, hpos = read_varint(handle, 0)
            size, _ = read_varint(handle, hpos)
        except IndexError:
            continue
        body = _block_body(data, off, size)
        if body is None:
            continue
        for key, value in _block_pairs(body):
            if len(key) < 8:
                continue
            op = "put" if key[-8] == 1 else "del"
            out.append((op, key[:-8], value if op == "put" else b""))
    return out


def _read_whole(path: str):
    """The file's bytes, or None. Opened and closed immediately.

    Chromium compacts and deletes these files underneath us. Holding a handle
    open risks blocking the application's own housekeeping -- on Windows, an
    open handle without FILE_SHARE_DELETE can fail its delete outright.
    """
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _file_number_key(name: str):
    """Sort key that orders '<digits>.<ext>' by the numeric file number.

    Chromium formats LevelDB file numbers with %06llu, so names stay
    equal-width and a plain lexicographic sort happens to match numeric order
    only while the counter is under 1,000,000. Past that the width changes
    and lexicographic order inverts -- "1000000.ldb" would sort before
    "999999.ldb" under str sort. Extracting the digits and comparing them as
    an int avoids that.

    A name that is not '<digits>.<ext>' cannot be a real LevelDB file number;
    it is sorted last (deliberately, not skipped) so one odd file name does
    not disturb the relative order of the real ones.
    """
    stem = name.split(".", 1)[0]
    if stem.isdigit():
        return (0, int(stem))
    return (1, name)


def _safe_want(want, key: bytes) -> bool:
    """`want(key)`, or False if the caller's predicate raises.

    `scan` is the public entry point Tasks 6 and 13 call with arbitrary
    predicates; a predicate that raises must not be able to kill the poll,
    so treat "raised" the same as "did not match" rather than propagating.
    """
    try:
        return bool(want(key))
    except Exception:
        return False


def _iter_entries(dir_path: str, want):
    """(op, key, value) triples in application order, matching keys only.

    Shared by `scan` and `scan_all`: the file discovery, the numeric
    `_file_number_key` sort, the `_safe_want` containment, and the
    open-read-close discipline all live here exactly once. Both callers
    differ only in what they do with the sequence this yields -- `scan`
    reduces it to one final value per key, `scan_all` does not.

    A missing directory or an unreadable/malformed file yields nothing for
    that source and moves on; this is a parser and parsers never raise.
    """
    try:
        names = os.listdir(dir_path)
    except OSError:
        return

    for suffix, reader in ((".ldb", sst_entries), (".log", wal_entries)):
        matching = sorted(
            (n for n in names if n.endswith(suffix)), key=_file_number_key)
        for name in matching:
            data = _read_whole(os.path.join(dir_path, name))
            if data is None:
                continue
            try:
                entries = reader(data)
            except Exception:
                # An application we do not control is allowed to change its
                # format. One unreadable file must not silence the store.
                continue
            for op, key, value in entries:
                if _safe_want(want, key):
                    yield op, key, value


def scan(dir_path: str, want) -> list:
    """Surviving (key, value) pairs whose key satisfies `want`, one per key.

    ORDERING IS DELIBERATELY SIMPLIFIED. Real LevelDB decides which copy of a
    key wins from the MANIFEST and from per-entry sequence numbers; this
    applies immutable tables first and then write-ahead logs, both in file
    order, which is correct whenever the log holds writes not yet compacted
    -- the normal state of a running application.

    A caller that must be certain which of several surviving copies is
    newest, or that must not let a false "final" value hide a real deletion
    ordering mistake, should reach for `scan_all` instead and break the tie
    itself -- pc/desktop_local_storage does exactly that.
    """
    surviving = {}
    order = []
    for op, key, value in _iter_entries(dir_path, want):
        if op == "del":
            surviving.pop(key, None)
            continue
        if key not in surviving:
            order.append(key)
        surviving[key] = value
    return [(k, surviving[k]) for k in order if k in surviving]


def scan_all(dir_path: str, want) -> list:
    """Every surviving (key, value) pair whose key satisfies `want`.

    Unlike `scan`, this does not collapse repeated keys to one final value:
    if two files each hold a put for the same key, both come back, in the
    same application order `scan` uses (.ldb tables by ascending file
    number, then .log files by ascending file number). That is the only way
    a caller can pick the true newest copy by the record's own timestamp
    instead of by which file the walk happened to reach last -- exactly the
    situation `scan`'s docstring warns about.

    A deletion is still honoured: a "del" for a key discards every copy of
    that key accumulated so far (not the file, not other keys -- just that
    key's copies up to that point), and a put after the deletion accumulates
    again from there. A key that is deleted and never re-put comes back as
    nothing, the same as `scan`.
    """
    result = []
    positions = {}
    for op, key, value in _iter_entries(dir_path, want):
        if op == "del":
            for idx in positions.pop(key, []):
                result[idx] = None
            continue
        idx = len(result)
        result.append((key, value))
        positions.setdefault(key, []).append(idx)
    return [kv for kv in result if kv is not None]
