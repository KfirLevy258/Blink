"""A structural reader for V8 ValueSerializer values.

Structural, not pattern-anchored, and that is the entire point. The obvious
decoder finds a field name and reads the number after it -- which works until
a CONVERSATION in the same buffer mentions the field name, at which point the
panel shows a number lifted out of somebody's chat. This project's own
transcripts discuss `resetsAt` and `unifiedWindows`, so that is not a
hypothetical.

Only the subset Claude Desktop's records actually use is implemented. An
unknown tag ends the parse and yields None, because a format we do not
recognise is one we must not guess at: this is a private format inside an
application we do not control, it is already versioned three ways on disk,
and Chromium is migrating these stores to SQLite.

NOTHING FROM THE BUFFER MAY LEAVE THIS MODULE except as decoded values a
caller asked for. No exception message, log line or repr may carry buffer
bytes -- see README.md's promise about bridge.log.
"""
import struct

from pc.leveldb import read_varint

# Deep enough for any record shape observed, shallow enough that a malformed
# buffer cannot exhaust the stack.
MAX_DEPTH = 64

_NULL, _UNDEF = ord("0"), ord("_")
_TRUE, _FALSE = ord("T"), ord("F")
_DOUBLE, _INT = ord("N"), ord("I")
_STR1, _STR2, _STR8 = ord('"'), ord("c"), ord("S")
_OBJ, _OBJ_END = ord("o"), ord("{")
# Two array forms, and they are shaped differently. Dense (`A`) writes its
# elements BARE, one after another; sparse (`a`) writes index/value pairs.
# Reading pairs under `A` costs an element every other slot and turns an
# odd-length array into a refused record, so the distinction is not cosmetic.
_ARR, _ARR_END = ord("A"), ord("$")
_SPARSE_ARR, _SPARSE_ARR_END = ord("a"), ord("@")
_BACKREF = ord("^")


def _slice(buf, pos, n):
    chunk = buf[pos:pos + n]
    if len(chunk) < n:
        raise ValueError("truncated")
    return chunk, pos + n


def _read(buf, pos, depth):
    # The outermost value is depth 0, so MAX_DEPTH levels are readable and
    # the next one is refused. `>` here would have allowed 65.
    if depth >= MAX_DEPTH:
        raise ValueError("too deep")
    tag = buf[pos]
    pos += 1

    if tag in (_NULL, _UNDEF):
        return None, pos
    if tag == _TRUE:
        return True, pos
    if tag == _FALSE:
        return False, pos
    if tag == _DOUBLE:
        raw, pos = _slice(buf, pos, 8)
        return struct.unpack("<d", raw)[0], pos
    if tag == _INT:
        z, pos = read_varint(buf, pos)
        # Zigzag: even encodes a non-negative, odd encodes a negative.
        return (-((z + 1) >> 1) if z & 1 else z >> 1), pos
    if tag == _STR1:
        # A varint length, not one byte. One byte is right for every string
        # shorter than 128 characters and then silently shifts the rest of
        # the parse -- which is worse than failing, because it still returns
        # a number.
        n, pos = read_varint(buf, pos)
        raw, pos = _slice(buf, pos, n)
        return raw.decode("latin-1"), pos
    if tag == _STR2:
        n, pos = read_varint(buf, pos)
        raw, pos = _slice(buf, pos, n)
        return raw.decode("utf-16-le", "replace"), pos
    if tag == _STR8:
        n, pos = read_varint(buf, pos)
        raw, pos = _slice(buf, pos, n)
        return raw.decode("utf-8", "replace"), pos
    if tag == _OBJ:
        obj = {}
        while True:
            if pos >= len(buf):
                raise ValueError("unterminated object")
            if buf[pos] == _OBJ_END:
                pos += 1
                break
            key, pos = _read(buf, pos, depth + 1)
            val, pos = _read(buf, pos, depth + 1)
            if isinstance(key, str):
                obj[key] = val
        _count, pos = read_varint(buf, pos)
        return obj, pos
    if tag == _ARR:
        # Dense: a length, then that many bare elements, then any named
        # properties as key/value pairs, then the end tag with a property
        # count and the length again.
        n, pos = read_varint(buf, pos)
        # Every element costs at least its tag byte, so a length longer than
        # what is left cannot be honest. Checking here is also what stops a
        # nonsense length from being walked one imaginary element at a time.
        if n > len(buf) - pos:
            raise ValueError("array longer than its buffer")
        items = []
        for _ in range(n):
            val, pos = _read(buf, pos, depth + 1)
            items.append(val)
        while True:
            if pos >= len(buf):
                raise ValueError("unterminated array")
            if buf[pos] == _ARR_END:
                pos += 1
                break
            # A named property on an array. Read structurally so the walk
            # stays aligned, then drop it: a Python list cannot hold it, and
            # nothing this project reads lives there.
            _key, pos = _read(buf, pos, depth + 1)
            _val, pos = _read(buf, pos, depth + 1)
        _props, pos = read_varint(buf, pos)
        _length, pos = read_varint(buf, pos)
        return items, pos
    if tag == _SPARSE_ARR:
        # Sparse: index/value pairs, the form V8 uses for an array with
        # holes. Indices are kept only to order the elements; the holes
        # themselves collapse, because a Python list has no hole.
        _n, pos = read_varint(buf, pos)
        keyed = {}
        while True:
            if pos >= len(buf):
                raise ValueError("unterminated array")
            if buf[pos] == _SPARSE_ARR_END:
                pos += 1
                break
            key, pos = _read(buf, pos, depth + 1)
            val, pos = _read(buf, pos, depth + 1)
            if isinstance(key, int) and not isinstance(key, bool):
                keyed[key] = val
        _props, pos = read_varint(buf, pos)
        _length, pos = read_varint(buf, pos)
        return [keyed[i] for i in sorted(keyed)], pos
    if tag == _BACKREF:
        # An object we have already seen. We do not keep the table: every
        # field this project reads is spelled out (V8 back-references
        # JSReceivers, never strings), so None here loses nothing we want and
        # keeps the parser from carrying state it could get wrong.
        _id, pos = read_varint(buf, pos)
        return None, pos

    raise ValueError("unknown tag")


def parse(buf):
    """A Python value from one serialized buffer, or None.

    None, never an exception. Every caller is already tolerating an
    application that may have changed its format underneath them.
    """
    if not isinstance(buf, (bytes, bytearray)):
        return None
    data = bytes(buf)
    try:
        pos = 0
        # A real value carries a Blink envelope version and then V8's, both
        # written as \xff + varint. Skip however many are present.
        while pos < len(data) and data[pos] == 0xff:
            pos += 1
            _version, pos = read_varint(data, pos)
        if pos >= len(data):
            return None
        value, _pos = _read(data, pos, 0)
        return value
    except Exception:
        # Deliberately broad, and deliberately silent. Anything raised here
        # would otherwise be free to carry buffer bytes into a log.
        return None
