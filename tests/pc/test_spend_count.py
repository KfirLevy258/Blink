"""The running count between /usage runs (pc/spend_count.py).

Every figure is generated here; nothing is captured from a machine."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "pc"))

import protocol  # noqa: E402
from spend_count import SpendCounter  # noqa: E402


def mk(tmp_path, **kw):
    return SpendCounter(path=str(tmp_path / "spend-count.json"), **kw)


def test_first_sighting_adds_nothing():
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 100)
    assert c.since(0) == 0


def test_rise_is_counted():
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 100)
    assert c.observe("a", 1.25, 200) is True
    assert c.since(0) == 25


def test_reread_of_same_render_changes_nothing():
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 100)
    assert c.observe("a", 1.00, 100) is False


def test_two_processes_count_once():
    c = SpendCounter(persist=False)
    for i, v in enumerate([1.00, 1.30, 1.10, 1.30, 1.40]):
        c.observe("a", v, 100 + i)
    assert c.since(0) == 40


def test_resume_is_not_new_spend():
    c = SpendCounter(persist=False)
    c.observe("a", 37.00, 100)
    assert c.since(0) == 0


def test_clear_starts_a_new_session():
    c = SpendCounter(persist=False)
    c.observe("a", 2.00, 100)
    c.observe("b", 0.00, 200)
    c.observe("b", 0.30, 300)
    assert c.since(0) == 30


def test_rise_straddling_the_anchor_is_dropped():
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 100)
    c.observe("a", 1.50, 200)
    assert c.since(150) == 0
    c.observe("a", 1.70, 300)
    assert c.since(150) == 20


def test_buckets():
    now = 1_800_000_000 + 600
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, now - 1300)
    c.observe("a", 1.30, now - 1200)
    b = c.buckets(now)
    assert len(b) == 20
    assert b[18] == 30 and sum(b) == 30
    c.observe("a", 1.40, now)
    assert c.buckets(now)[19] == 10


def test_old_additions_leave_the_buckets():
    now = 1_800_000_000
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, now - 100000)
    c.observe("a", 2.00, now - 99000)
    assert sum(c.buckets(now)) == 0


def test_restart_keeps_the_count(tmp_path):
    c = mk(tmp_path)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.observe("a", 1.25, 200)
    c.save()
    d = mk(tmp_path)
    d.bind("fp", 0)
    assert d.since(0) == 25
    d.observe("a", 1.30, 300)
    assert d.since(0) == 30


def test_other_login_or_period_starts_empty(tmp_path):
    c = mk(tmp_path)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.observe("a", 1.25, 200)
    c.save()
    d = mk(tmp_path)
    d.bind("other", 0)
    assert d.since(0) == 0
    e = mk(tmp_path)
    e.bind("fp", 5)
    assert e.since(0) == 0


def test_first_bind_keeps_earlier_observations():
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 100)
    c.observe("a", 1.25, 200)
    c.bind("fp", 0)
    assert c.since(0) == 25


def test_corrupt_file_starts_empty(tmp_path):
    (tmp_path / "spend-count.json").write_text("{not json")
    c = mk(tmp_path)
    c.bind("fp", 0)
    assert c.since(0) == 0
    (tmp_path / "spend-count.json").write_text(
        json.dumps({"v": 1, "fp": "fp", "period": 0,
                    "sessions": {"a": "x"}, "adds": [[1, 2]]}))
    assert mk(tmp_path).since(0) == 0


def test_persist_false_writes_nothing(tmp_path):
    p = tmp_path / "spend-count.json"
    c = SpendCounter(path=str(p), persist=False)
    c.observe("a", 1.00, 100)
    c.observe("a", 2.00, 200)
    c.save()
    assert os.listdir(tmp_path) == []


def test_save_is_a_noop_when_unchanged(tmp_path):
    c = mk(tmp_path)
    c.observe("a", 1.00, 100)
    c.save()
    p = tmp_path / "spend-count.json"
    os.remove(p)
    c.save()
    assert not p.exists()
    assert os.listdir(tmp_path) == []


def test_default_path_resolves_at_call_time(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    c = SpendCounter()
    c.observe("a", 1.00, 100)
    c.save()
    assert (tmp_path / ".blink" / "spend-count.json").exists()


def test_damaged_shapes_load_empty(tmp_path):
    f = tmp_path / "spend-count.json"
    for body in ('{"v":1,"fp":"fp","period":0,"sessions":[],"adds":[]}',
                 '{"v":1,"fp":"fp","period":0,"sessions":null,"adds":[]}',
                 '{"v":1,"fp":"fp","period":0,"sessions":{},"adds":{}}',
                 '{"v":1,"fp":"fp","period":0,"sessions":{},'
                 '"adds":[[1,2,Infinity]]}',
                 '{"v":1,"fp":"fp","period":0,'
                 '"sessions":{"a":[NaN,1]},"adds":[]}'):
        f.write_text(body)
        c = mk(tmp_path)
        assert c.since(0) == 0
        assert c.observe("a", 1.0, 5) is True


def test_bad_cost_changes_nothing():
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 100)
    for bad in (float("nan"), float("inf"), "2.0", True, None):
        assert c.observe("a", bad, 200) is False
    assert c.observe("a", 2.0, float("nan")) is False
    assert c.since(0) == 0
    c.observe("a", 1.10, 300)
    assert c.since(0) == 10


def test_additions_older_than_32_days_are_dropped():
    day = 86400
    t = 1_800_000_000
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, t)
    c.observe("a", 1.10, t + day)
    assert c.since(0) == 10
    c.observe("a", 1.25, t + 34 * day)
    assert c.since(0) == 15
