"""Synthetic V8 structured-clone values.

Generated, never captured. The store this format appears in on a real machine
holds the owner's conversations, so a captured fixture would put chat text in
a public repository.

Local constants and a local writer, not imports from `pc.v8_clone`: a fixture
that borrowed the module under test would agree with it about a wrong tag
table, and the first TDD run has to fail for the right reason.
"""
import struct

VERSION = 15


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


class Smi(int):
    """An integer that must be written with the I tag, not as a double.

    Exists so a test can pin the case that matters: a `utilization` of
    exactly 0, the minute after a window refills, is a Smi on the wire.
    """


def ints(value):
    """Rewrite every int in a structure as a Smi."""
    if isinstance(value, dict):
        return {k: ints(v) for k, v in value.items()}
    if isinstance(value, list):
        return [ints(v) for v in value]
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return Smi(value)
    return value


def _write(value, out: bytearray) -> None:
    if value is None:
        out += b"0"
    elif value is True:
        out += b"T"
    elif value is False:
        out += b"F"
    elif isinstance(value, Smi):
        z = (value << 1) if value >= 0 else ((-value << 1) - 1)
        out += b"I" + _varint(z)
    elif isinstance(value, (int, float)):
        out += b"N" + struct.pack("<d", float(value))
    elif isinstance(value, str):
        if all(ord(c) < 128 for c in value):
            raw = value.encode("latin-1")
            out += b'"' + _varint(len(raw)) + raw
        else:
            raw = value.encode("utf-16-le")
            out += b"c" + _varint(len(raw)) + raw
    elif isinstance(value, dict):
        out += b"o"
        for k, v in value.items():
            _write(k, out)
            _write(v, out)
        out += b"{" + _varint(len(value))
    elif isinstance(value, list):
        # A dense array: the length, then the elements BARE -- no index
        # before each one. The index/value form is the separate sparse tag
        # `a`, and writing pairs here made the fixture agree with a parser
        # that had the same misreading, which is how it went unnoticed.
        out += b"A" + _varint(len(value))
        for v in value:
            _write(v, out)
        out += b"$" + _varint(0) + _varint(len(value))
    else:
        raise TypeError(type(value).__name__)


def wrap(payload: bytes, version: int = VERSION) -> bytes:
    """The \\xff + varint version header a real value carries."""
    return b"\xff" + _varint(version) + payload


def dumps(value, version: int = VERSION) -> bytes:
    out = bytearray()
    _write(value, out)
    return wrap(bytes(out), version)
