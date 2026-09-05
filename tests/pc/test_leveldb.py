"""Chromium writes these stores; we only ever read them.

Every test here is about refusing to hand back a value we are not certain we
decoded, because a wrong number reaches the panel looking exactly like a right
one.
"""
import os
import struct
import time

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


def test_varint_refuses_a_run_of_continuation_bytes_promptly():
    """Ten bytes hold any 64-bit value; an eleventh is not a length these
    formats can express.

    Without the cap the accumulator widens by seven bits per byte, so the
    cost is quadratic in the run: a megabyte of 0xff inside one untrusted
    value is minutes of a blocked poll instead of a skipped record. Callers
    already treat IndexError as "skip this region", so that is what a
    too-long varint raises.
    """
    assert leveldb.read_varint(b"\xff" * 9 + b"\x00", 0)[1] == 10
    for run in (11, 64, 1_000_000):
        start = time.monotonic()
        try:
            leveldb.read_varint(b"\xff" * run, 0)
        except IndexError:
            pass
        else:
            raise AssertionError("a %d-byte varint was accepted" % run)
        assert time.monotonic() - start < 1.0


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


def test_scan_all_keeps_every_surviving_copy_of_a_key(tmp_path):
    """The gap scan() has and scan_all() exists to close: scan() would let
    the log's put silently replace the table's, discarding one copy before
    the caller ever gets a look at it."""
    d = _store(tmp_path,
               tables=[("000005.ldb", [("put", b"k", b"old")])],
               logs=[("000006.log", [[("put", b"k", b"new")]])])
    assert leveldb.scan_all(d, lambda k: k == b"k") == [
        (b"k", b"old"), (b"k", b"new")]


def test_scan_all_returns_copies_in_application_order(tmp_path):
    d = _store(tmp_path, logs=[("000001.log", [
        [("put", b"a", b"1"), ("put", b"b", b"2"), ("put", b"a", b"3")]])])
    assert leveldb.scan_all(d, lambda k: True) == [
        (b"a", b"1"), (b"b", b"2"), (b"a", b"3")]


def test_scan_all_lets_a_tombstone_remove_copies_before_it(tmp_path):
    """The guarantee scan() cannot give: once a key is genuinely deleted,
    every copy seen before that deletion -- including one sitting in an
    older table -- must not be resurrected."""
    d = _store(tmp_path,
               tables=[("000005.ldb", [("put", b"k", b"stale")])],
               logs=[("000006.log", [[("del", b"k", b"")]])])
    assert leveldb.scan_all(d, lambda k: k == b"k") == []


def test_scan_all_lets_puts_after_a_tombstone_accumulate_again(tmp_path):
    d = _store(tmp_path, logs=[("000001.log", [
        [("put", b"k", b"old")],
        [("del", b"k", b"")],
        [("put", b"k", b"new")],
    ])])
    assert leveldb.scan_all(d, lambda k: k == b"k") == [(b"k", b"new")]


def test_scan_all_is_silent_about_a_missing_directory():
    assert leveldb.scan_all("/nonexistent/leveldb", lambda k: True) == []


# --- the parsed-file cache -------------------------------------------------
#
# Before it existed, every poll re-read and fully re-parsed every file in the
# store. On the reference machine that was 0.47 s of pure-Python Snappy every
# two seconds on the thread that also reads the board's serial messages, and
# 0.0004 s once the files stopped moving.


# A fixed instant, well past leveldb.CACHE_SETTLE_S ago. Fixed rather than
# "now minus ten seconds" so that settling a file twice leaves its mtime
# exactly where it was: an unchanged file must look unchanged.
_SETTLED = 1_600_000_000.0


def _settle(tmp_path, at=_SETTLED):
    """Backdate every file past leveldb.CACHE_SETTLE_S.

    The cache deliberately refuses to trust a file written this instant --
    see CACHE_SETTLE_S -- so a test that wants a cache hit has to let the
    files sit, and a test does that by moving the clock rather than by
    sleeping through it.
    """
    for p in tmp_path.iterdir():
        os.utime(str(p), (at, at))


def _counting_reads(monkeypatch):
    """Names of the files actually opened, in order."""
    read = []
    real = leveldb._read_whole

    def spy(path):
        read.append(os.path.basename(path))
        return real(path)

    monkeypatch.setattr(leveldb, "_read_whole", spy)
    return read


def test_an_unchanged_file_is_not_reparsed_on_a_second_scan(
        tmp_path, monkeypatch):
    leveldb.clear_cache()
    d = _store(tmp_path,
               tables=[("000005.ldb", [("put", b"k", b"table")])],
               logs=[("000006.log", [[("put", b"j", b"log")]])])
    _settle(tmp_path)
    read = _counting_reads(monkeypatch)

    first = leveldb.scan_all(d, lambda k: True)
    assert sorted(read) == ["000005.ldb", "000006.log"]
    read.clear()

    second = leveldb.scan_all(d, lambda k: True)
    assert read == []                    # neither re-read nor re-parsed
    assert second == first               # and the answer is unchanged


def test_a_changed_file_is_reparsed_and_only_that_file(tmp_path, monkeypatch):
    """Invalidation is per file. A store where only the .log moved must
    re-parse the .log and keep the table -- that table is the expensive one."""
    leveldb.clear_cache()
    d = _store(tmp_path,
               tables=[("000005.ldb", [("put", b"k", b"table")])],
               logs=[("000006.log", [[("put", b"j", b"one")]])])
    _settle(tmp_path)
    leveldb.scan_all(d, lambda k: True)

    (tmp_path / "000006.log").write_bytes(
        fx.build_log([[("put", b"j", b"two")], [("put", b"extra", b"x")]]))
    later = _SETTLED + 100
    os.utime(str(tmp_path / "000006.log"), (later, later))
    read = _counting_reads(monkeypatch)

    after = leveldb.scan(d, lambda k: True)
    assert read == ["000006.log"]
    assert after == [(b"k", b"table"), (b"j", b"two"), (b"extra", b"x")]


def test_a_file_still_being_written_is_never_cached(tmp_path, monkeypatch):
    """The correctness trap. mtime granularity is a filesystem property we do
    not control, so a file whose timestamp is younger than CACHE_SETTLE_S is
    parsed fresh every time -- a same-size rewrite inside one granule would
    otherwise be served stale, and a stale usage percentage on the panel is
    far worse than a slow poll."""
    leveldb.clear_cache()
    d = _store(tmp_path, logs=[("000006.log", [[("put", b"k", b"one")]])])
    read = _counting_reads(monkeypatch)

    leveldb.scan(d, lambda k: True)
    leveldb.scan(d, lambda k: True)
    assert read == ["000006.log", "000006.log"]

    # Same length, same instant, different bytes: still seen.
    (tmp_path / "000006.log").write_bytes(
        fx.build_log([[("put", b"k", b"two")]]))
    assert leveldb.scan(d, lambda k: True) == [(b"k", b"two")]


def test_the_cache_cannot_grow_without_limit(tmp_path):
    """Chromium compacts files in and out of existence, so a cache keyed on
    file number accumulates one dead entry per compaction unless it is
    bounded."""
    leveldb.clear_cache()
    for i in range(leveldb._CACHE_MAX_FILES * 3):
        (tmp_path / ("%06d.log" % i)).write_bytes(
            fx.build_log([[("put", b"k%d" % i, b"v")]]))
    _settle(tmp_path)
    leveldb.scan_all(tmp_path and str(tmp_path), lambda k: True)
    assert len(leveldb._parse_cache) <= leveldb._CACHE_MAX_FILES
    leveldb.clear_cache()


def test_a_cached_store_still_honours_deletions_and_file_order(tmp_path):
    """The cache holds one file's entries in that file's own order; the walk
    over files is the walk it always was. Both survive a second call."""
    leveldb.clear_cache()
    d = _store(tmp_path,
               tables=[("000005.ldb", [("put", b"k", b"stale")])],
               logs=[("000006.log", [[("del", b"k", b"")]]),
                     ("000010.log", [[("put", b"n", b"newer")]])])
    _settle(tmp_path)
    for _ in range(2):
        assert leveldb.scan_all(d, lambda k: True) == [(b"n", b"newer")]
