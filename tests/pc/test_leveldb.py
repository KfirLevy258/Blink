"""Chromium writes these stores; we only ever read them.

Every test here is about refusing to hand back a value we are not certain we
decoded, because a wrong number reaches the panel looking exactly like a right
one.
"""
from pc import leveldb
from tests.support import leveldb_fixture as fx


def test_varint_reads_single_and_multi_byte():
    assert leveldb.read_varint(b"\x00", 0) == (0, 1)
    assert leveldb.read_varint(b"\x7f", 0) == (127, 1)
    assert leveldb.read_varint(b"\x80\x01", 0) == (128, 2)
    assert leveldb.read_varint(b"\xff\xff\x03", 0) == (65535, 3)


def test_varint_reports_the_position_after_the_value():
    _, pos = leveldb.read_varint(b"\x80\x01\xaa", 0)
    assert pos == 2


def test_crc32c_matches_the_castagnoli_reference_vectors():
    assert leveldb.crc32c(b"") == 0
    assert leveldb.crc32c(b"123456789") == 0xE3069283
    assert leveldb.crc32c(b"a") == 0xC1D04330


def test_unmask_inverts_leveldb_crc_masking():
    raw = leveldb.crc32c(b"hello")
    masked = (((raw >> 15) | (raw << 17)) + 0xa282ead8) & 0xffffffff
    assert leveldb.unmask_crc(masked) == raw


def test_snappy_expands_a_literal_then_an_overlapping_copy():
    """Preamble 8, a one-byte literal 'a', then a 7-byte copy at offset 1.

    The copy overlaps its own output, which is the case a naive slice-based
    implementation gets wrong: it must be produced a byte at a time.
    """
    blob = bytes([0x08, 0x00, 0x61, 0x0d, 0x01])
    assert leveldb.snappy_decompress(blob) == b"aaaaaaaa"


def test_snappy_handles_a_plain_literal_run():
    blob = bytes([0x03, 0x08]) + b"xyz"
    assert leveldb.snappy_decompress(blob) == b"xyz"


def test_snappy_refuses_a_truncated_stream():
    assert leveldb.snappy_decompress(bytes([0x10, 0x00])) is None


def test_wal_reads_puts_and_deletes_in_order():
    data = fx.build_log([
        [("put", b"a", b"1"), ("put", b"b", b"2")],
        [("del", b"a", b"")],
    ])
    assert leveldb.wal_entries(data) == [
        ("put", b"a", b"1"), ("put", b"b", b"2"), ("del", b"a", b"")]


def test_wal_reassembles_a_record_split_across_blocks():
    """A payload longer than a block is written as FIRST/MIDDLE/LAST, which
    injects a 7-byte header into the middle of it. Scanning raw file bytes
    corrupts exactly these records -- the reason this reader exists."""
    big = b"v" * 80_000
    data = fx.build_log([[("put", b"k", big)]])
    assert leveldb.wal_entries(data) == [("put", b"k", big)]


def test_wal_skips_a_record_whose_checksum_fails():
    """A torn concurrent write must be dropped, not assembled into a value
    that decodes to a plausible wrong number."""
    good = fx.frame(fx.batch([("put", b"ok", b"1")]))
    bad = fx.frame(fx.batch([("put", b"no", b"2")]), corrupt_crc=True)
    assert leveldb.wal_entries(good + bad) == [("put", b"ok", b"1")]


def test_wal_tolerates_a_truncated_tail():
    data = fx.build_log([[("put", b"k", b"v")]])
    assert leveldb.wal_entries(data + b"\x01\x02\x03") == [("put", b"k", b"v")]
