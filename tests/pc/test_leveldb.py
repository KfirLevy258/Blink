"""Chromium writes these stores; we only ever read them.

Every test here is about refusing to hand back a value we are not certain we
decoded, because a wrong number reaches the panel looking exactly like a right
one.
"""
import struct

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


def test_table_reads_entries_and_strips_the_internal_key_trailer():
    data = fx.build_table([("put", b"alpha", b"1"), ("put", b"beta", b"2")])
    assert leveldb.sst_entries(data) == [
        ("put", b"alpha", b"1"), ("put", b"beta", b"2")]


def test_table_reports_a_deletion_as_a_deletion():
    data = fx.build_table([("del", b"gone", b"")])
    assert leveldb.sst_entries(data) == [("del", b"gone", b"")]


def test_table_shares_key_prefixes_between_entries():
    """Prefix compression is the format's normal state, not an edge case."""
    data = fx.build_table([("put", b"kkkk1", b"a"), ("put", b"kkkk2", b"b")])
    assert leveldb.sst_entries(data) == [
        ("put", b"kkkk1", b"a"), ("put", b"kkkk2", b"b")]


def test_table_returns_nothing_for_a_file_with_no_magic():
    assert leveldb.sst_entries(b"not a table at all") == []



def test_table_reads_entries_from_a_snappy_compressed_block():
    """Every data block in a real Claude Desktop table is Snappy-compressed --
    this is the branch that always runs in production, so it needs its own
    round trip through sst_entries, not just the uncompressed path."""
    data = fx.build_table(
        [("put", b"alpha", b"1"), ("put", b"beta", b"2")], compress=True)
    assert leveldb.sst_entries(data) == [
        ("put", b"alpha", b"1"), ("put", b"beta", b"2")]


def test_table_returns_nothing_when_truncated_to_just_the_footer():
    """The magic is intact but the index and data blocks it points to are
    gone -- the correct response is silence, not a guess."""
    data = fx.build_table([("put", b"alpha", b"1")])
    truncated = data[-leveldb.SST_FOOTER_SIZE:]
    assert leveldb.sst_entries(truncated) == []


def test_table_returns_nothing_when_a_block_handle_points_past_the_buffer():
    """The footer's index handle names an offset past the end of the file --
    the index behind it is unreachable, and the correct response is
    silence, not whatever bytes happen to lie at some other offset."""
    data = fx.build_table([("put", b"alpha", b"1")])
    body = data[:-leveldb.SST_FOOTER_SIZE]
    forged_footer = bytearray()
    forged_footer += fx._varint(0) + fx._varint(0)
    forged_footer += fx._varint(len(body) + 10_000) + fx._varint(16)
    forged_footer += b"\x00" * (40 - len(forged_footer))
    forged_footer += struct.pack("<Q", leveldb.SST_MAGIC)
    forged = body + bytes(forged_footer)
    assert leveldb.sst_entries(forged) == []


import os


def _store(tmp_path, tables=(), logs=()):
    for name, entries in tables:
        (tmp_path / name).write_bytes(fx.build_table(entries))
    for name, batches in logs:
        (tmp_path / name).write_bytes(fx.build_log(batches))
    return str(tmp_path)


def test_scan_returns_only_matching_keys(tmp_path):
    d = _store(tmp_path, logs=[("000001.log", [
        [("put", b"want-me", b"1"), ("put", b"skip-me", b"2")]])])
    assert leveldb.scan(d, lambda k: b"want" in k) == [(b"want-me", b"1")]


def test_scan_lets_the_log_win_over_a_table(tmp_path):
    """The fresh value lives in the log. A table-only reader serves hours-old
    data that looks current -- measured on a real machine, 3.5 hours stale."""
    d = _store(tmp_path,
               tables=[("000005.ldb", [("put", b"k", b"old")])],
               logs=[("000006.log", [[("put", b"k", b"new")]])])
    assert leveldb.scan(d, lambda k: k == b"k") == [(b"k", b"new")]


def test_scan_honours_a_deletion(tmp_path):
    d = _store(tmp_path,
               tables=[("000005.ldb", [("put", b"k", b"v")])],
               logs=[("000006.log", [[("del", b"k", b"")]])])
    assert leveldb.scan(d, lambda k: k == b"k") == []


def test_scan_is_silent_about_a_missing_directory():
    assert leveldb.scan("/nonexistent/leveldb", lambda k: True) == []


def test_scan_survives_one_unreadable_file(tmp_path):
    (tmp_path / "000009.ldb").write_bytes(b"garbage, not a table")
    d = _store(tmp_path, logs=[("000010.log", [[("put", b"k", b"v")]])])
    assert leveldb.scan(d, lambda k: k == b"k") == [(b"k", b"v")]


def test_scan_orders_tables_numerically_not_lexicographically(tmp_path):
    """Chromium formats file numbers with %06llu, so names stay equal-width
    only below 1,000,000. Past that, a plain string sort puts "1000000.ldb"
    before "999999.ldb" (because "1" precedes "9"), applying the newer table
    first and letting the older table silently overwrite it with stale data.
    This is the one case that distinguishes numeric order from lexicographic
    order, so it is the only case worth testing."""
    d = _store(tmp_path, tables=[
        ("999999.ldb", [("put", b"k", b"old")]),
        ("1000000.ldb", [("put", b"k", b"new")]),
    ])
    assert leveldb.scan(d, lambda k: k == b"k") == [(b"k", b"new")]


def test_scan_treats_a_raising_predicate_as_no_match(tmp_path):
    """`want` is a caller-supplied predicate reaching `scan` from arbitrary
    code (Tasks 6 and 13). A predicate that raises must not be able to take
    the whole poll down with it -- scan is a parser and parsers never raise."""
    d = _store(tmp_path, logs=[("000001.log", [[("put", b"k", b"v")]])])

    def boom(key):
        raise ValueError("predicate blew up")

    assert leveldb.scan(d, boom) == []
