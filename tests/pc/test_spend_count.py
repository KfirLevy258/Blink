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


class Tick:
    """A monotonic clock for the save throttle, moved by hand."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _month(c, start, renders, step, rise_cents, anchors=()):
    """`renders` rises of one session, `step` seconds apart from `start`,
    each `rise_cents`; the page's since() asked after each render the way
    claude_cli._spend_frame asks it, with the newest anchor at or before
    the render (or the period start, 0, when there is none yet)."""
    c.observe("a", 0.0, start)
    for i in range(1, renders + 1):
        at = start + i * step
        c.observe("a", i * rise_cents / 100.0, at)
        seen = [x for x in anchors if x <= at]
        c.since(max(seen) if seen else 0)


def test_a_month_of_renders_keeps_the_file_small(tmp_path):
    """The file held every rise of the month until the final review of
    2026-11-04, and grew all month. Rises older than five hours are folded
    into one carried entry; only the last five hours are kept one by one,
    for the quarter-hours of the burn view."""
    t = 1_800_000_000
    c = mk(tmp_path)
    c.bind("fp", 0)
    _month(c, t, 30 * 24 * 12, 300, 3)          # every 5 min for 30 days
    c.save()
    doc = json.loads((tmp_path / "spend-count.json").read_text())
    assert len(doc["adds"]) <= 5 * 12 + 1
    assert (tmp_path / "spend-count.json").stat().st_size < 4096
    assert c.since(0) == 30 * 24 * 12 * 3


def test_since_stays_exact_after_a_fold():
    """An anchor at or after the fold boundary -- the newest one since()
    was asked about -- counts exactly what it did before the fold, and so
    does the period start; a /usage newer than everything folded counts
    nothing folded."""
    t = 1_800_000_000
    a1 = t + 3600 + 150
    c = SpendCounter(persist=False)
    _month(c, t, 10 * 12, 300, 5, anchors=(a1,))   # ten hours
    # Rises whose previous sighting is at or after a1: every one stamped
    # from t+3600+300 on, i.e. renders 13..120 minus the straddling one.
    want = sum(5 for i in range(1, 121) if t + (i - 1) * 300 >= a1)
    assert c.since(a1) == want
    assert c.since(0) == 120 * 5
    a2 = t + 10 * 3600 + 1                        # after the last render
    assert c.since(a2) == 0
    # The first rise after it straddles it and is dropped (the module's
    # rule); the next is counted.
    for i, at in enumerate((t + 10 * 3600 + 300, t + 10 * 3600 + 600)):
        c.observe("a", (121 + i) * 5 / 100.0, at)
    assert c.since(a2) == 5
    assert c.since(0) == 122 * 5


def test_a_fold_survives_a_restart(tmp_path):
    t = 1_800_000_000
    a1 = t + 3600 + 150
    c = mk(tmp_path)
    c.bind("fp", 0)
    _month(c, t, 10 * 12, 300, 5, anchors=(a1,))
    c.save()
    d = mk(tmp_path)
    d.bind("fp", 0)
    assert d.since(a1) == c.since(a1)
    assert d.since(0) == c.since(0) == 600
    assert d.buckets(t + 10 * 3600) == c.buckets(t + 10 * 3600)


def test_the_burn_view_is_unchanged_by_a_fold():
    t = 1_800_000_000
    c = SpendCounter(persist=False)
    _month(c, t, 10 * 12, 300, 5)
    now = t + 10 * 3600 - 1                     # the last full quarter
    b = c.buckets(now)
    # Twenty quarter-hours of three renders at 5 cents each.
    assert sum(b) == 20 * 3 * 5 and b == [15] * 20


def test_saves_are_paced_to_one_a_minute(tmp_path):
    """The file was rewritten on nearly every render (each carries a cost
    rise). Now at most once a minute; what was not written is not lost,
    because a restart re-derives a rise from the saved high-water mark."""
    tick = Tick()
    p = tmp_path / "spend-count.json"
    c = mk(tmp_path, clock=tick)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.save()
    first = p.read_text()
    c.observe("a", 1.20, 110)
    tick.t += 30
    c.save()
    assert p.read_text() == first
    tick.t += 30
    c.save()
    assert p.read_text() != first
    # The unwritten rise is counted after a restart all the same.
    c.observe("a", 1.50, 120)
    tick.t += 1
    c.save()
    d = mk(tmp_path)
    d.bind("fp", 0)
    d.observe("a", 1.50, 120)
    assert d.since(0) == 50


def test_a_new_session_and_an_emptied_count_are_written_at_once(tmp_path):
    """Pacing them lost spend: a restart that missed a session's first
    sighting took its next render for the first and counted nothing, and
    one that missed bind()'s emptying kept the last login's count."""
    tick = Tick()
    p = tmp_path / "spend-count.json"
    c = mk(tmp_path, clock=tick)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.save()
    tick.t += 1
    c.observe("b", 5.00, 110)
    c.save()
    assert "b" in json.loads(p.read_text())["sessions"]
    tick.t += 1
    c.bind("other", 0)
    c.save()
    assert json.loads(p.read_text())["fp"] == "other"


def test_a_negative_cost_changes_nothing():
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 100)
    assert c.observe("a", -0.50, 200) is False
    assert c.observe("b", -0.50, 200) is False
    assert c.since(0) == 0
    c.observe("a", 1.10, 300)
    assert c.since(0) == 10
