"""Claude Desktop's five-hour record, read out of its Local Storage.

This store carries no conversation content -- its keys are onboarding flags,
experiment toggles and tip cooldowns -- which is the property that makes it
the one we build on. See docs/research/claude-desktop-window-sources.md.
"""
import json

from pc import desktop_local_storage as ls
from tests.support import leveldb_fixture as fx

KEY = (b"_https://claude.ai\x00\x01"
       b"claudeai.ochre_heron_tide.3d2fe603-510b-4256-bd4e-2f2b1b689bef")


def _record(resets_at=1788628200, utilization=0.06, observed_at=1788613507.7):
    return json.dumps({
        "resetsAt": resets_at, "utilization": utilization,
        "prevUtilization": 0.05, "observedAt": observed_at,
        "atWall": False, "fired": [], "shown": False, "shownCowork": False})


def _value(text, utf16=False):
    if utf16:
        return b"\x00" + text.encode("utf-16-le")
    return b"\x01" + text.encode("utf-8")


def test_decodes_a_utf8_value():
    assert ls.decode_value(_value('{"a":1}')) == '{"a":1}'


def test_decodes_a_utf16_value():
    assert ls.decode_value(_value('{"a":1}', utf16=True)) == '{"a":1}'


def test_finds_the_usage_record(tmp_path):
    (tmp_path / "000001.log").write_bytes(
        fx.build_log([[("put", KEY, _value(_record()))]]))
    rec = ls.newest_record(str(tmp_path))
    assert rec["resetsAt"] == 1788628200
    assert rec["utilization"] == 0.06


def test_ignores_every_other_key(tmp_path):
    other = b"_https://claude.ai\x00\x01LSS-cowork-loading-tip-cooldown"
    (tmp_path / "000001.log").write_bytes(
        fx.build_log([[("put", other, _value("whatever"))]]))
    assert ls.newest_record(str(tmp_path)) is None


def test_the_newest_record_wins_on_its_own_timestamp(tmp_path):
    """Two surviving copies must be resolved by observedAt, not by which file
    the reader happened to reach last -- pc.leveldb.scan says so explicitly."""
    old = _record(utilization=0.19, observed_at=1788600021.1)
    new = _record(utilization=0.06, observed_at=1788613507.7)
    (tmp_path / "000005.ldb").write_bytes(
        fx.build_table([("put", KEY, _value(new))]))
    (tmp_path / "000006.log").write_bytes(
        fx.build_log([[("put", KEY, _value(old))]]))
    assert ls.newest_record(str(tmp_path))["utilization"] == 0.06


def test_a_record_with_an_absurd_timestamp_is_refused(tmp_path):
    (tmp_path / "000001.log").write_bytes(fx.build_log(
        [[("put", KEY, _value(_record(observed_at=99_999_999_999)))]]))
    assert ls.newest_record(str(tmp_path)) is None


def test_unparseable_json_is_not_an_exception(tmp_path):
    (tmp_path / "000001.log").write_bytes(
        fx.build_log([[("put", KEY, _value("{not json"))]]))
    assert ls.newest_record(str(tmp_path)) is None


def test_a_boolean_utilization_is_not_a_record(tmp_path):
    """bool is an int in Python. Left unguarded, {"utilization": true} would
    pass the isinstance check, become float(True) * 100.0 in the provider,
    and reach the panel as a confident 100% -- the one input on this path
    that produces a WRONG NUMBER rather than an absence."""
    doc = json.dumps({"resetsAt": 1788628200, "utilization": True,
                      "observedAt": 1788613507.7})
    (tmp_path / "000001.log").write_bytes(
        fx.build_log([[("put", KEY, _value(doc))]]))
    assert ls.newest_record(str(tmp_path)) is None


def test_a_boolean_utilization_does_not_hide_a_real_record(tmp_path):
    """Rejecting the bad copy must not reject the store. A real reading in
    the same file is still returned."""
    bad = json.dumps({"resetsAt": 1788628200, "utilization": False,
                      "observedAt": 1788613600.0})
    (tmp_path / "000001.log").write_bytes(fx.build_log([
        [("put", KEY, _value(_record(utilization=0.42)))],
        [("put", KEY + b".2", _value(bad))]]))
    rec = ls.newest_record(str(tmp_path))
    assert rec is not None
    assert rec["utilization"] == 0.42
