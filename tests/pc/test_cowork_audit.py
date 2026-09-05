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
