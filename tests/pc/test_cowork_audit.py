"""A seven-day boundary out of a legacy Cowork audit file.

Plain JSON in a directory that holds no chat store, which is why this is
tried before the IndexedDB seeder. It is also rare: on the machine this was
written from, 3 of 218 rate-limit events carried the windows at all.
"""
import json
import os

from pc import cowork_audit

WED_0600Z = 1788933600.0


def _event(resets_at=WED_0600Z, ts="2026-09-05T07:37:29.276Z", windows=True):
    info = {"status": "allowed"}
    if windows:
        info["unifiedWindows"] = {
            "five_hour": {"resetsAt": 1788628200, "utilization": 0.05},
            "seven_day": {"resetsAt": resets_at, "utilization": 0.17}}
    return json.dumps({"type": "rate_limit_event",
                       "rate_limit_info": info, "timestamp": ts})


def _audit(root, lines, session="local_abc"):
    d = os.path.join(str(root), "acct", "org", session)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, "audit.jsonl"), "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")


def test_finds_the_seven_day_reset(tmp_path):
    _audit(tmp_path, [_event()])
    got = cowork_audit.seven_day_reset(root=str(tmp_path))
    assert got is not None
    assert got[0] == WED_0600Z


def test_ignores_events_without_the_windows(tmp_path):
    _audit(tmp_path, [_event(windows=False)])
    assert cowork_audit.seven_day_reset(root=str(tmp_path)) is None


def test_ignores_lines_that_are_not_json(tmp_path):
    _audit(tmp_path, ["{broken", _event()])
    assert cowork_audit.seven_day_reset(root=str(tmp_path))[0] == WED_0600Z


def test_an_absent_root_is_silent():
    assert cowork_audit.seven_day_reset(root="/nonexistent") is None


def test_a_nonsense_reset_is_refused(tmp_path):
    _audit(tmp_path, [_event(resets_at=99_999_999_999)])
    assert cowork_audit.seven_day_reset(root=str(tmp_path)) is None


# --- Task 11 fix round 1: the cheapness machinery, and the tie-break -------
#
# All five existing tests above use one small file where size <= tail_bytes,
# so _tail_lines' seek branch, the first-partial-line discard, and the
# max_files truncation never ran. That untested path is the one that runs
# almost always -- on the owner's own machine only 3 of 218 events carry
# unifiedWindows at all -- so it is pinned here with small, exact byte
# counts rather than a real 256 KB file.


def _write_raw(root, content, session="local_raw"):
    d = os.path.join(str(root), "acct", "org", session)
    os.makedirs(d, exist_ok=True)
    p = os.path.join(d, "audit.jsonl")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(content)
    return p


def test_a_record_inside_the_tail_window_is_found(tmp_path):
    """tail_bytes bounds the read to the end of the file. A record placed
    within that window must still be found even though the file as a whole
    is larger than tail_bytes."""
    junk = "z" * 500 + "\n"       # far older content, well before the window
    good = _event() + "\n"
    _write_raw(tmp_path, junk + good)
    tail_bytes = len(good) + 20    # covers `good` whole, plus a little slack
    got = cowork_audit.seven_day_reset(root=str(tmp_path), max_files=20,
                                        tail_bytes=tail_bytes)
    assert got is not None
    assert got[0] == WED_0600Z


def test_a_record_before_the_tail_window_is_not_found(tmp_path):
    """The mirror case: a record that sits further from the end of the file
    than tail_bytes reaches must never be seen -- proving the seek actually
    bounds the read rather than the whole file being read regardless."""
    good = _event() + "\n"
    junk = "z" * 500 + "\n"       # pushes `good` well outside the window
    _write_raw(tmp_path, good + junk)
    tail_bytes = 50                # far smaller than len(junk)
    assert cowork_audit.seven_day_reset(
        root=str(tmp_path), max_files=20, tail_bytes=tail_bytes) is None


def test_the_line_before_the_seek_point_is_always_dropped(tmp_path):
    """A seek into the middle of a file can, by pure chance, land exactly on
    a line boundary -- so the "first line after the seek" is not always
    garbage that fails to parse. This pins that the first line is dropped
    UNCONDITIONALLY whenever the seek moved (size > tail_bytes), not only
    when it happens to look truncated: `bad` is a fully valid, complete
    event sitting right where the seek lands, carrying a WRONG reset, with
    the real wanted event `good` immediately after it. If the discard were
    removed, this coincidentally-parseable `bad` record would win.
    """
    bad = _event(resets_at=1_700_000_000.0)
    good = _event()
    prefix = "PREFIXPREFIX"        # no newline in it -- stays glued to `bad`
    content = prefix + bad + "\n" + good + "\n"
    _write_raw(tmp_path, content)
    # Sized so the seek lands EXACTLY at the start of `bad`, skipping only
    # `prefix` -- i.e. `bad` itself is a complete, valid line in the tail.
    tail_bytes = len(bad) + 1 + len(good) + 1
    got = cowork_audit.seven_day_reset(root=str(tmp_path), max_files=20,
                                        tail_bytes=tail_bytes)
    assert got is not None
    assert got[0] == WED_0600Z     # not 1_700_000_000.0


def test_only_the_newest_max_files_are_scanned(tmp_path):
    """More than max_files audit files exist; only the newest max_files may
    ever be opened. Proven by putting the valid record ONLY in the older,
    excluded files -- a scan that ignored the cap would still find it."""
    root = str(tmp_path)
    old_mtime = 1_700_000_000.0
    new_mtime = 1_800_000_000.0
    for i in range(2):
        session = f"local_old{i}"
        _audit(root, [_event()], session=session)
        p = os.path.join(root, "acct", "org", session, "audit.jsonl")
        os.utime(p, (old_mtime + i, old_mtime + i))
    for i in range(3):
        session = f"local_new{i}"
        _audit(root, [_event(windows=False)], session=session)
        p = os.path.join(root, "acct", "org", session, "audit.jsonl")
        os.utime(p, (new_mtime + i, new_mtime + i))
    # Bounded to the newest 3 -- exactly the ones with no usable record.
    assert cowork_audit.seven_day_reset(root=root, max_files=3) is None
    # Sanity: with the cap lifted, the valid old record IS reachable, so the
    # None above is the cap at work, not a fixture mistake.
    assert cowork_audit.seven_day_reset(root=root, max_files=20) is not None


def test_the_newer_timestamped_event_wins(tmp_path):
    """When multiple usable events carry real timestamps, the newest
    ORIGINALLY-timestamped one wins -- not file order. The newer event is
    written FIRST here to prove that."""
    older = _event(resets_at=1_700_000_000.0, ts="2020-01-02T00:00:00Z")
    newer = _event(resets_at=WED_0600Z, ts="2020-01-03T00:00:00Z")
    _audit(tmp_path, [newer, older])
    got = cowork_audit.seven_day_reset(root=str(tmp_path))
    assert got is not None
    assert got[0] == WED_0600Z


def test_the_last_seen_event_wins_the_mtime_fallback_tie(tmp_path):
    """Neither event carries a usable timestamp, so both fall back to the
    SAME file mtime -- a true tie. The intent is the newest event, which in
    an append-ordered file is the LAST one written, so the tie-break must be
    >=, not >, or the oldest event in the file would win every tie."""
    first = _event(resets_at=1_700_000_000.0, ts="not-a-timestamp")
    last = _event(resets_at=WED_0600Z, ts="not-a-timestamp")
    _audit(tmp_path, [first, last])
    got = cowork_audit.seven_day_reset(root=str(tmp_path))
    assert got is not None
    assert got[0] == WED_0600Z


# --- The shared timestamp parser
#
# Public because pc/desktop_idb reuses it rather than shipping a second date
# parser. It accepted a trailing Z only; desktop_idb has no second timestamp
# to fall back to, so an offset-form created_at would have cost that machine
# its only reading of the seven-day boundary.


def test_a_trailing_z_is_read():
    assert cowork_audit.iso_to_epoch(
        "2026-09-05T12:58:59.702809Z") == 1788613139.702809


def test_a_trailing_z_without_fractional_seconds_is_read():
    assert cowork_audit.iso_to_epoch("2026-09-05T12:58:59Z") == 1788613139.0


def test_a_zero_offset_is_the_same_instant_as_z():
    assert (cowork_audit.iso_to_epoch("2026-09-05T12:58:59.702809+00:00")
            == cowork_audit.iso_to_epoch("2026-09-05T12:58:59.702809Z"))


def test_a_positive_offset_is_subtracted_not_added():
    """+03:00 is a wall clock three hours AHEAD of UTC, so the instant is
    three hours EARLIER. Getting this backwards is a silent six-hour error."""
    assert cowork_audit.iso_to_epoch(
        "2026-09-05T15:58:59.702809+03:00") == 1788613139.702809


def test_a_negative_offset_is_added():
    assert cowork_audit.iso_to_epoch(
        "2026-09-05T05:58:59.702809-07:00") == 1788613139.702809


def test_a_compact_offset_is_read():
    assert cowork_audit.iso_to_epoch(
        "2026-09-05T15:58:59.702809+0300") == 1788613139.702809


def test_an_hour_only_offset_is_read():
    assert cowork_audit.iso_to_epoch(
        "2026-09-05T15:58:59.702809+03") == 1788613139.702809


def test_a_timestamp_with_no_zone_stays_unreadable():
    """Guessing a zone turns an honest absence into a multi-hour error."""
    assert cowork_audit.iso_to_epoch("2026-09-05T12:58:59.702809") is None
    assert cowork_audit.iso_to_epoch("2026-09-05T12:58:59") is None


def test_the_date_separator_is_never_mistaken_for_an_offset():
    assert cowork_audit.iso_to_epoch("2026-09-05") is None


def test_a_malformed_zone_is_refused_rather_than_raised():
    for ts in ("2026-09-05T12:58:59+9:99", "2026-09-05T12:58:59+xx:00",
               "2026-09-05T12:58:59+99:00", "2026-09-05T12:58:59+00:99",
               "2026-09-05T12:58:59+", "2026-09-05T12:58:59+000",
               "2026-09-05T12:58:59+\u0663\u0663:00", "", "Z", 17, None):
        assert cowork_audit.iso_to_epoch(ts) is None


def test_an_event_timestamped_with_an_offset_is_used(tmp_path):
    """End to end through the audit reader, not just the parser."""
    ev = {"type": "rate_limit_event", "timestamp": "2026-09-05T12:58:59+00:00",
          "rate_limit_info": {"unifiedWindows": {
              "seven_day": {"resetsAt": 1788933600.0}}}}
    d = tmp_path / "sess"
    d.mkdir()
    (d / "audit.jsonl").write_text(json.dumps(ev) + "\n", encoding="utf-8")
    got = cowork_audit.seven_day_reset(str(tmp_path))
    assert got == (1788933600.0, 1788613139.0)
