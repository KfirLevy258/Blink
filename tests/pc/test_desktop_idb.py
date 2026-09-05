"""The seven-day boundary out of Claude Desktop's IndexedDB.

Last resort by design. This is the only source in the project that reads a
store holding the customer's conversations, so the tests below are as much
about what it refuses as about what it finds.

Every fixture here is built in memory by tests/support. Nothing in this file
opens, walks, or otherwise touches a real Claude Desktop store, and the
autouse _sandboxed_home fixture in tests/conftest.py keeps ~ inside tmp_path
besides.
"""
from pc import desktop_idb
from tests.support import leveldb_fixture as fx
from tests.support import v8_fixture as vfx

WED_0600Z = 1788933600.0
PREV_WED_0600Z = 1788328800.0


def _record(seven=WED_0600Z, created="2026-09-05T12:58:59.702809Z"):
    return {"payload": {"rate_limit_info": {
        "rateLimitType": "five_hour",
        "resetsAt": 1788628200.0,
        "status": "allowed",
        "unifiedWindows": {
            "five_hour": {"resetsAt": 1788628200.0, "utilization": 0.05},
            "seven_day": {"resetsAt": seven, "utilization": 0.17}}}},
        "type": "rate_limit_event", "created_at": created}


def _store(tmp_path, key, value):
    (tmp_path / "000103.log").write_bytes(
        fx.build_log([[("put", key, value)]]))
    return str(tmp_path)


def _store_many(tmp_path, pairs):
    (tmp_path / "000103.log").write_bytes(
        fx.build_log([[("put", k, v) for k, v in pairs]]))
    return str(tmp_path)


def test_finds_the_seven_day_reset(tmp_path):
    d = _store(tmp_path, b"\x00cowork:cse_abc", vfx.dumps(_record()))
    got = desktop_idb.seven_day_reset(d)
    assert got is not None and got[0] == WED_0600Z


def test_does_not_take_the_outer_resets_at(tmp_path):
    """Three resetsAt live in one record. Positional pairing takes the wrong
    one the day a window is absent."""
    d = _store(tmp_path, b"\x00cowork:cse_abc", vfx.dumps(_record()))
    assert desktop_idb.seven_day_reset(d)[0] != 1788628200.0


def test_skips_a_blob_wrapped_value(tmp_path):
    """Over 65536 bytes the value moves to a sibling blob file, leaving a
    reference. Serving an older record instead would be a silent lie."""
    d = _store(tmp_path, b"\x00cowork:cse_abc", b"\xff\x11\x01" + b"\x00" * 8)
    assert desktop_idb.seven_day_reset(d) is None


def test_an_absent_store_is_silent():
    assert desktop_idb.seven_day_reset("/nonexistent/idb") is None


def test_a_conversation_mentioning_the_field_yields_nothing(tmp_path):
    """The poisoning case, end to end."""
    chat = {"messages": [{"text": "resetsAt 1788000000 unifiedWindows"}]}
    d = _store(tmp_path, b"\x00conv:plain", vfx.dumps(chat))
    assert desktop_idb.seven_day_reset(d) is None


# --- What it refuses beyond the brief's five


def test_a_blob_wrapped_value_never_falls_back_to_an_older_record(tmp_path):
    """The whole point of refusing the wrap.

    A wrapped value is the one record we cannot judge, and on a real store
    it is the LONG session -- so it is exactly the one most likely to be
    current. Answering with a readable older sibling would publish a week-old
    boundary as if it were today's, which is the failure this plan exists to
    avoid. No answer beats a stale answer.
    """
    d = _store_many(tmp_path, [
        (b"\x00cowork:cse_old", vfx.dumps(_record(
            seven=PREV_WED_0600Z, created="2026-08-28T09:00:00.000000Z"))),
        (b"\x00cowork:cse_big", b"\xff\x11\x01" + b"\x00" * 8),
    ])
    assert desktop_idb.seven_day_reset(d) is None


def test_a_nan_reset_is_refused(tmp_path):
    """A NaN passes isinstance and then loses every comparison silently.

    This project has been bitten by exactly that three times, once already
    on this branch, and a NaN written into the anchor would sit there
    forever.
    """
    d = _store(tmp_path, b"\x00cowork:cse_abc",
               vfx.dumps(_record(seven=float("nan"))))
    assert desktop_idb.seven_day_reset(d) is None


def test_a_far_future_reset_is_refused(tmp_path):
    """A boundary in 2160 is never stale, so it would beat every real
    reading forever. Refused on the way in rather than left to expire."""
    d = _store(tmp_path, b"\x00cowork:cse_abc",
               vfx.dumps(_record(seven=6_000_000_000.0)))
    assert desktop_idb.seven_day_reset(d) is None


def test_a_record_with_no_created_at_is_not_used(tmp_path):
    """observed_at is what the anchor's staleness withdrawal runs on. A file
    mtime is not the record's own idea of when it was taken, so absence is
    absence -- never a substitute timestamp."""
    rec = _record()
    del rec["created_at"]
    d = _store(tmp_path, b"\x00cowork:cse_abc", vfx.dumps(rec))
    assert desktop_idb.seven_day_reset(d) is None


def test_an_implausible_created_at_is_not_used(tmp_path):
    d = _store(tmp_path, b"\x00cowork:cse_abc",
               vfx.dumps(_record(created="1970-01-02T00:00:00Z")))
    assert desktop_idb.seven_day_reset(d) is None


# --- What it picks when there is more than one


def test_the_newest_record_wins(tmp_path):
    d = _store_many(tmp_path, [
        (b"\x00cowork:cse_old", vfx.dumps(_record(
            seven=PREV_WED_0600Z, created="2026-08-28T09:00:00.000000Z"))),
        (b"\x00cowork:cse_new", vfx.dumps(_record(
            seven=WED_0600Z, created="2026-09-05T12:58:59.702809Z"))),
    ])
    got = desktop_idb.seven_day_reset(d)
    assert got is not None and got[0] == WED_0600Z


def test_a_window_pairs_with_its_own_records_created_at(tmp_path):
    """One value can hold several events. Taking the first created_at in the
    document and the first window found would pair a boundary with a
    timestamp belonging to something else -- and observed_at is what decides
    whether the anchor is still believed."""
    doc = {"created_at": "2026-08-01T00:00:00Z",
           "events": [_record(seven=PREV_WED_0600Z,
                              created="2026-08-28T09:00:00.000000Z"),
                      _record(seven=WED_0600Z,
                              created="2026-09-05T12:58:59.702809Z")]}
    d = _store(tmp_path, b"\x00cowork:cse_abc", vfx.dumps(doc))
    got = desktop_idb.seven_day_reset(d)
    assert got is not None
    assert got[0] == WED_0600Z
    # 2026-09-05T12:58:59.702809Z, not the document's own 2026-08-01.
    assert got[1] == 1788613139.702809


# --- created_at's shape


def test_a_created_at_with_a_trailing_z_is_read(tmp_path):
    d = _store(tmp_path, b"\x00cowork:cse_abc", vfx.dumps(_record()))
    assert desktop_idb.seven_day_reset(d)[1] == 1788613139.702809


def test_a_created_at_with_an_explicit_offset_is_read(tmp_path):
    """The same instant, written the way datetime.isoformat() writes it.

    The shared parser accepted a trailing Z only, so this form used to make
    the record unusable -- and there is no second timestamp to fall back to
    here. Pinned so the extension to iso_to_epoch cannot quietly regress.
    """
    d = _store(tmp_path, b"\x00cowork:cse_abc",
               vfx.dumps(_record(created="2026-09-05T12:58:59.702809+00:00")))
    got = desktop_idb.seven_day_reset(d)
    assert got is not None and got[1] == 1788613139.702809


def test_a_created_at_with_a_nonzero_offset_is_read(tmp_path):
    """+03:00 is three hours EARLIER in UTC, not later."""
    d = _store(tmp_path, b"\x00cowork:cse_abc",
               vfx.dumps(_record(created="2026-09-05T15:58:59.702809+03:00")))
    got = desktop_idb.seven_day_reset(d)
    assert got is not None and got[1] == 1788613139.702809


# --- The store's own shape


def test_the_key_decides_what_is_read_not_the_value(tmp_path):
    """Provenance comes from the IndexedDB KEY. A full rate_limit_event
    under a plain conversation key is not a Cowork record and is not read --
    reading it would be the trap docs/research describes, reintroduced."""
    d = _store(tmp_path, b"\x00conv:plain_01", vfx.dumps(_record()))
    assert desktop_idb.seven_day_reset(d) is None


def test_a_utf16_key_is_matched_too(tmp_path):
    """Keys are UTF-16 on a real machine and ASCII in these fixtures."""
    key = b"\x00" + "cowork:cse_abc".encode("utf-16-le")
    d = _store(tmp_path, key, vfx.dumps(_record()))
    got = desktop_idb.seven_day_reset(d)
    assert got is not None and got[0] == WED_0600Z


def test_a_deleted_record_does_not_survive(tmp_path):
    (tmp_path / "000103.log").write_bytes(fx.build_log([
        [("put", b"\x00cowork:cse_abc", vfx.dumps(_record()))],
        [("del", b"\x00cowork:cse_abc", b"")],
    ]))
    assert desktop_idb.seven_day_reset(str(tmp_path)) is None


def test_an_unparseable_value_is_not_an_error(tmp_path):
    d = _store(tmp_path, b"\x00cowork:cse_abc", b"\xff\x0f\x9e\x7f garbage")
    assert desktop_idb.seven_day_reset(d) is None


def test_a_default_store_path_is_absolute():
    p = desktop_idb.store_path()
    assert p.endswith("https_claude.ai_0.indexeddb.leveldb")
    assert "~" not in p
