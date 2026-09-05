"""Chromium writes these stores; we only ever read them.

Every test here is about refusing to hand back a value we are not certain we
decoded, because a wrong number reaches the panel looking exactly like a right
one.
"""
from pc import leveldb


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
