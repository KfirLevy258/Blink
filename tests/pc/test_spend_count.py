"""The running count between /usage runs (pc/spend_count.py).

Every figure is generated here; nothing is captured from a machine."""
import json
import os
import sys

import pytest

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


# -- the save on the serial thread (review of 2026-11-04, U7 and U8) ----------

def test_save_does_not_use_mkstemp(tmp_path, monkeypatch):
    """tempfile.mkstemp on Python 3.11, the Windows build's interpreter,
    takes a PermissionError for a taken name and tries the next, up to
    os.TMP_MAX (about two billion): a ~/.blink that refuses new files held
    the serial thread inside the first save and the board went to HOST
    LOST. The temp comes from install_statusline's _open_temp instead."""
    import tempfile

    def no_mkstemp(*a, **k):
        raise AssertionError("mkstemp on the serial thread")
    monkeypatch.setattr(tempfile, "mkstemp", no_mkstemp)
    c = mk(tmp_path)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.save()
    assert json.loads((tmp_path / "spend-count.json").read_text())["fp"] \
        == "fp"
    assert os.listdir(tmp_path) == ["spend-count.json"]


def test_a_refused_save_tries_once_and_backs_off(tmp_path, monkeypatch):
    """A folder that refuses the write costs one attempt, not a retry on
    every two-second poll; the count stays in memory and is written once
    the back-off has passed."""
    from pc import install_statusline
    calls = []
    real = install_statusline._open_temp

    def refuse(path):
        calls.append(path)
        raise PermissionError(13, "refused")
    monkeypatch.setattr(install_statusline, "_open_temp", refuse)
    tick = Tick()
    c = mk(tmp_path, clock=tick)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.save()
    assert len(calls) == 1
    for i in range(20):                  # forty seconds of polls
        tick.t += 2
        c.observe("b", 1.00, 200 + i)    # a new session is urgent
        c.save()
    assert len(calls) == 1
    monkeypatch.setattr(install_statusline, "_open_temp", real)
    tick.t += 30
    c.save()
    doc = json.loads((tmp_path / "spend-count.json").read_text())
    assert set(doc["sessions"]) == {"a", "b"}
    assert [n for n in os.listdir(tmp_path)] == ["spend-count.json"]


# -- what _load survives (review of 2026-11-04, U1) ---------------------------

def _v2(**kw):
    d = {"v": 2, "fp": "fp", "period": 0, "sessions": {"a": [100, 50]},
         "adds": [[10, 20, 5]], "fold": None, "anchor": None}
    d.update(kw)
    return d


@pytest.mark.parametrize("doc", [
    # 309 digits: json reads it, math.isfinite raises OverflowError on it.
    _v2(sessions={"a": [100, 10 ** 400]}),
    _v2(adds=[[10 ** 400, 20, 5]]),
    _v2(adds=[[10, 20, 10 ** 400]]),
    _v2(fold=[0, 1, 2, 10 ** 400, 4]),
    _v2(anchor=10 ** 400),
    _v2(period=10 ** 400),
    # A high-water mark below zero: the next $1.00 would count $1001.
    _v2(sessions={"a": [-100000, 50]}),
    _v2(adds=[[10, 20, -5]]),
    _v2(fold=[0, -1, 2, 3, 4]),
    _v2(fold=[0, 1, -2, 3, 4]),
    _v2(anchor=-1),
    # Absurd but finite: refused like the observe() that could write it.
    _v2(sessions={"a": [10 ** 300, 50]}),
    _v2(adds=[[10, 20, 10 ** 300]]),
    _v2(fold=[0, 1, 10 ** 300, 3, 4]),
    _v2(sessions={"a": [100, 1e300]}),
    _v2(sessions={"a": [100, 50, 7]}),
    _v2(sessions={"a": "xy"}),
    _v2(sessions={"a": []}),
    # One ledger more than _TRACKS (8).
    _v2(sessions={"a": [100, 50] * 9}),
    _v2(sessions={"a": [100, 50, -1, 60]}),
])
def test_a_malformed_file_loads_empty(tmp_path, doc):
    (tmp_path / "spend-count.json").write_text(json.dumps(doc))
    c = mk(tmp_path)
    assert c.since(0) == 0
    c.bind("fp", 0)
    # Empty: "a" is a first sighting again, and $1.00 counts nothing.
    assert c.observe("a", 1.00, 60) is True
    assert c.since(0) == 0


def test_a_file_nested_past_the_recursion_limit_loads_empty(tmp_path):
    (tmp_path / "spend-count.json").write_text("[" * 100000 + "]" * 100000)
    c = mk(tmp_path)
    assert c.since(0) == 0


def test_a_good_v2_file_still_loads(tmp_path):
    (tmp_path / "spend-count.json").write_text(json.dumps(_v2()))
    c = mk(tmp_path)
    c.bind("fp", 0)
    assert c.since(0) == 5
    c.observe("a", 1.10, 60)
    assert c.since(0) == 15


@pytest.mark.parametrize("cost", [10 ** 307, 1e307, 1.8e306, 10 ** 400,
                                  2e7])
def test_an_absurd_cost_is_refused_not_saved(tmp_path, cost):
    """A status-line total of 10**307 passed observe(), was saved as a
    309-digit cent figure a minute later, and every start after that died
    reading it (1.8e306 * 100 rounds to infinity and raised there and
    then). No session costs ten million dollars."""
    c = mk(tmp_path)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    assert c.observe("a", cost, 200) is False
    assert c.observe("b", cost, 200) is False
    c.save()
    d = mk(tmp_path)
    d.bind("fp", 0)
    assert d.observe("a", 1.25, 300) is True
    assert d.since(0) == 25


# -- a restart after a closed session (U12) -----------------------------------

def test_a_paced_rise_is_written_at_exit(tmp_path):
    """A rise held back by the one-a-minute pacing used to be recovered
    only from the session's next render after a restart; a session that
    had closed never renders again, so it was lost. The exit flush writes
    it, to the file this counter last wrote and nowhere else."""
    import spend_count
    tick = Tick()
    c = mk(tmp_path, clock=tick)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.save()
    c.observe("a", 1.70, 110)
    tick.t += 5
    c.save()                                 # paced: not written
    spend_count._flush_at_exit()
    d = mk(tmp_path)
    d.bind("fp", 0)
    assert d.since(0) == 70


def test_the_exit_flush_writes_only_where_the_counter_wrote(tmp_path,
                                                            monkeypatch):
    """One that never saved writes nothing at exit: by then the test
    sandbox has handed HOME back, and the default path would name the real
    ~/.blink."""
    import spend_count
    monkeypatch.setenv("HOME", str(tmp_path))
    c = SpendCounter()
    c.observe("a", 1.00, 100)
    spend_count._flush_at_exit()
    assert not (tmp_path / ".blink").exists()



def test_the_file_at_the_end_of_a_busy_month(tmp_path):
    """docs/README-full.md said the file "stays a few kilobytes all month".
    The rises do, but every session's high-water mark is kept until the
    month turns, so it grows by about 2 KB a day at thirty sessions a day:
    66 KB on the 31st, measured with this generator (U13b, final review of
    2026-11-04). The README now gives that figure; this keeps it true."""
    import random
    import uuid
    r = random.Random(1)
    t0 = 1_800_000_000.0
    c = mk(tmp_path)
    c.bind("f" * 64, t0)
    t, sizes = t0, []
    for day in range(31):
        for _s in range(30):
            sid = str(uuid.UUID(int=r.getrandbits(128)))
            for k in range(20):
                t += r.uniform(10, 200) + 0.123456
                c.observe(sid, 0.37 * (k + 1), t)
                c.since(t0)
        c._urgent = True
        c.save()
        sizes.append((tmp_path / "spend-count.json").stat().st_size)
    assert 50_000 < sizes[-1] < 72_000
    # Roughly linear: a day's sessions, not the month's rises.
    assert 1_500 < (sizes[-1] - sizes[0]) / 30 < 2_500


# -- a cost that starts again from zero (round 2 of 2026-11-04, K1) ----------
#
# Claude Code keeps a session's id across /login and starts its cost again
# from zero (2.1.318's costLedger.reset). A sign-out and sign-in to the same
# seat leaves the fingerprint as it was, so nothing upstream sees it, and a
# single high-water mark swallowed every rise until the session passed its
# old total: $14.34 shown for $16.84 in U9. Each process keeps its own
# ledger, so every figure a session shows continues one of a few ledgers:
# the counter keeps a mark per ledger and counts a figure against the
# highest mark at or below it.

def test_a_same_seat_relogin_counts_from_the_new_base():
    c = SpendCounter(persist=False)
    c.observe("a", 16.84, 100)
    c.observe("a", 17.00, 200)          # +16
    c.observe("a", 0.00, 300)           # /login: a new base, adds nothing
    c.observe("a", 0.50, 400)           # +50
    c.observe("a", 2.50, 500)           # +200
    assert c.since(0) == 16 + 50 + 200


def test_a_restarted_ledger_crossing_its_old_mark_undercounts_once():
    """The one figure that passes the dead ledger's mark is counted from
    that mark, not from its own last figure: the two cannot be told apart
    (a live first process rising 20 cents looks the same), so the smaller
    rise is the one counted. 16.50 to 17.20 is 70 cents; 20 are counted,
    once, and every rise after it is whole."""
    c = SpendCounter(persist=False)
    c.observe("a", 17.00, 100)
    c.observe("a", 0.00, 200)
    c.observe("a", 16.50, 300)          # +1650
    c.observe("a", 17.20, 400)          # +20, not +70
    c.observe("a", 17.90, 500)          # +70
    assert c.since(0) == 1650 + 20 + 70


def test_two_processes_alternating_count_only_their_own_rises():
    """`claude --continue` in a second terminal: a second process on the
    same session id, restored to an earlier total, rendering in turn with
    the first. Each figure is counted against its own process's last one,
    never against the other's, so taking turns adds nothing: A 10.00 ->
    10.50 is 50 cents, B 4.00 -> 4.60 is 60, and the same two figures
    shown again and again add none."""
    c = SpendCounter(persist=False)
    seq = [10.00, 4.00, 10.30, 4.20, 10.50, 4.60]
    for i, v in enumerate(seq):
        c.observe("a", v, 100 + i)
    assert c.since(0) == 50 + 60
    for i in range(20):
        c.observe("a", (10.50, 4.60)[i % 2], 200 + i)
    assert c.since(0) == 50 + 60


def test_a_resume_restoring_an_earlier_total_adds_nothing():
    c = SpendCounter(persist=False)
    c.observe("a", 10.00, 100)
    c.observe("a", 10.40, 200)          # +40
    c.observe("a", 8.00, 300)           # resumed at an older total: +0
    c.observe("a", 10.40, 400)          # the first process again: +0
    assert c.since(0) == 40


def test_a_rise_on_a_second_ledger_straddling_the_anchor_is_dropped():
    """Each ledger keeps its own last sighting for the straddling rule: a
    rise on B whose previous B figure predates /usage is dropped even
    though A was seen after it."""
    c = SpendCounter(persist=False)
    c.observe("a", 10.00, 100)
    c.observe("a", 4.00, 110)
    c.observe("a", 10.20, 200)          # prev 100: straddles 150
    c.observe("a", 4.50, 300)           # prev 110: straddles 150
    c.observe("a", 10.40, 400)          # prev 200: counts
    assert c.since(150) == 20
    assert c.since(0) == 20 + 50 + 20


def test_ledgers_past_the_cap_are_not_opened():
    """A session keeps at most spend_count._TRACKS ledgers. A figure below
    all of them on a full session adds nothing and opens nothing: the
    safe direction, an undercount until the next /usage."""
    import spend_count
    c = SpendCounter(persist=False)
    n = spend_count._TRACKS
    for i in range(n):
        c.observe("a", 10.0 * (n - i), 100 + i)
    assert c.observe("a", 1.00, 200) is False
    c.observe("a", 1.50, 300)
    assert c.since(0) == 0


def test_ledgers_survive_a_restart(tmp_path):
    c = mk(tmp_path)
    c.bind("fp", 0)
    c.observe("a", 17.00, 100)
    c.observe("a", 0.00, 200)
    c._urgent = True
    c.save()
    d = mk(tmp_path)
    d.bind("fp", 0)
    d.observe("a", 0.40, 300)
    d.observe("a", 17.10, 400)
    assert d.since(0) == 40 + 10


def _sessions_life(seed, steps=300):
    """One session id's processes, at random: each spends, renders (some
    renders lost to other sessions writing over them), takes a /login
    (cost back to zero), exits (Claude Code saves its total as the
    session's lastCost) or is joined by `claude --continue`, which
    restores that saved total -- or starts at zero when none is saved.
    One step a second. Returns the counter and the spend as (second,
    cents) pairs, so a /usage at any moment can be checked against it."""
    import random
    r = random.Random(seed)
    live, saved = [0], None
    spend, t = [], 1000
    c = SpendCounter(persist=False)
    for _ in range(steps):
        t += 1
        roll = r.random()
        if not live or (roll < 0.03 and len(live) < 3):
            # A restore of the saved total revives the ledger that exited
            # with it. Once revived it is not handed out again: two live
            # processes from one saved total are the residual pinned in
            # test_two_resumes_from_one_saved_total_can_overcount.
            live.append(saved if saved is not None else 0)
            saved = None
            i = len(live) - 1
        elif roll < 0.06 and len(live) > 1:
            i = r.randrange(len(live))
            saved = live.pop(i)
            continue
        else:
            i = r.randrange(len(live))
            if roll < 0.07:
                live[i] = 0                          # /login
            else:
                d = r.randrange(0, 120)
                live[i] += d
                spend.append((t, d))
        if r.random() < 0.7:                         # else lost
            c.observe("a", live[i] / 100.0, t)
    return c, spend


def _anchors(steps=300):
    """A /usage between every two steps of a life. The cross-ledger case
    R1 found came up about once in 13,000 (life, anchor) pairs here, so a
    handful of anchors a life never met it."""
    return [1000 + k + 0.5 for k in range(steps)]


@pytest.mark.parametrize("seed", range(500))
def test_never_counts_more_than_was_spent(seed):
    """Whatever the interleaving of processes, /logins, exits, resumes and
    lost renders, the count never exceeds the spend (the module
    docstring's argument, tested): since the period start, and since a
    /usage at any moment, which measured everything spent before it.
    The second is the straddling rule's promise; round 2's review (R1)
    found the first cut of the ledgers breaking it, 460 cents counted for
    425, through a mark one ledger had taken over from another."""
    c, spend = _sessions_life(seed)
    assert c.since(0) <= sum(d for _t, d in spend)
    for anchor in _anchors():
        assert c.since(anchor) <= sum(d for t, d in spend if t > anchor)


def test_the_count_stays_close_to_the_spend():
    """And it is not safe by counting nothing: over these lives the count
    since the period start is most of the spend. What it misses is each
    /login's first figure, a restarted ledger passing a dead mark, and
    rises behind lost renders on a ledger that is passed meanwhile."""
    counted = spent = 0
    for seed in range(500):
        c, spend = _sessions_life(seed)
        counted += c.since(0)
        spent += sum(d for _t, d in spend)
    assert counted > 0.85 * spent


def test_a_rise_carried_past_the_anchor_by_another_ledger_is_dropped():
    """R1, round 2 of the 2026-11-04 review. B at 4.00 before a /usage at
    100, really at 4.50 by then (its render lost). A, after a /login, is
    seen at 0.10 and then 4.05 -- the figure lands on B's mark, and the
    mark took A's time. B's 4.60 was then counted from 4.05 as a rise
    after the /usage: 55 cents, of which 45 were spent before it. A rise
    is now stamped with the oldest sighting among the marks it could
    continue, and those marks keep the oldest stamp they could carry."""
    c = SpendCounter(persist=False)
    c.observe("s", 12.00, 95)
    c.observe("s", 4.00, 96)
    c.observe("s", 0.10, 105)           # A, after /login
    c.observe("s", 4.05, 107)
    c.observe("s", 4.60, 110)           # B
    c.observe("s", 4.15, 115)           # A
    # Spent after 100: A's 0.10 and 405 after it, B's 10.
    assert c.since(100) <= 10 + 405 + 10


def test_two_resumes_from_one_saved_total_can_overcount():
    """The residual (module docstring): two processes resumed from the
    same saved total, in a session that already holds a lower ledger. The
    first rises past the saved total before the second renders, so the
    second's first figure lies above the lower ledger's mark and is
    counted from it. Here 10.00 was saved; a /login ledger stands at 2.00;
    A resumes and reaches 10.50; B resumes, spends 20 cents and renders
    10.20, counted as 820 cents. Real spend: 50 + 20 + 20 (the lower
    ledger's own 1.80 -> 2.00)."""
    c = SpendCounter(persist=False)
    c.observe("a", 10.00, 100)          # the first process, then exits
    c.observe("a", 1.80, 110)           # a second, after /login
    c.observe("a", 2.00, 120)           # +20
    c.observe("a", 10.00, 130)          # A, resumed from the saved 10.00
    c.observe("a", 10.50, 140)          # +50
    c.observe("a", 10.20, 150)          # B, resumed from 10.00 too: +820
    assert c.since(0) == 20 + 50 + 820


def test_one_process_is_counted_exactly():
    import random
    r = random.Random(7)
    v, t = 0, 1000
    c = SpendCounter(persist=False)
    c.observe("a", 0.0, t)
    for _ in range(500):
        v += r.randrange(0, 80)
        t += 1
        c.observe("a", v / 100.0, t)
    assert c.since(0) == v


# -- an exit inside observe() (round 2 of 2026-11-04, K4) ---------------------

class _ExitAfterAppend(list):
    """The rise list, raising the daemon's SIGTERM exit (SystemExit, from
    claude_usage_bridge._on_sigterm, which can land between any two
    bytecodes of the main thread) right after a rise is appended."""

    def append(self, x):
        super().append(x)
        raise SystemExit(0)


class _ExitBeforeAppend(list):
    """The same, landing just before the rise is appended."""

    def append(self, x):
        raise SystemExit(0)


def _paced(tmp_path):
    """A counter whose file holds a at 1.00 and b at 2.00, with a rise on
    each since -- a to 1.20, b to 2.40 -- held back by the pacing."""
    tick = Tick()
    c = mk(tmp_path, clock=tick)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.observe("b", 2.00, 101)
    c.save()
    c.observe("a", 1.20, 105)
    c.observe("b", 2.40, 106)
    tick.t += 5
    c.save()
    return c


def _restart(tmp_path):
    import spend_count
    spend_count._flush_at_exit()
    d = mk(tmp_path)
    d.bind("fp", 0)
    return d


def test_an_exit_inside_observe_does_not_save_a_torn_count(tmp_path):
    """K4, round 2 of the 2026-11-04 review: the exit flush wrote whatever
    was in memory, and a SIGTERM between a rise's append and its mark's
    move saved a rise of 30 cents against the old mark; the restart
    counted it again from there. The mark now moves first, in one store,
    and the rise is appended after it, so wherever the exit lands the
    flush saves no rise its mark does not cover: here the whole update."""
    c = _paced(tmp_path)
    c._adds = _ExitAfterAppend(c._adds)
    with pytest.raises(SystemExit):
        c.observe("a", 1.50, 110)
    d = _restart(tmp_path)
    d.observe("a", 1.50, 110)
    assert d.since(0) == 50 + 40


def test_an_exit_before_the_rise_is_appended_loses_only_that_rise(tmp_path):
    """Landing between the mark's move and the rise: the 30 cents are lost,
    the safe direction, and nothing else is."""
    c = _paced(tmp_path)
    c._adds = _ExitBeforeAppend(c._adds)
    with pytest.raises(SystemExit):
        c.observe("a", 1.50, 110)
    d = _restart(tmp_path)
    d.observe("a", 1.50, 110)
    assert d.since(0) == 20 + 40


def test_an_exit_inside_observe_still_writes_the_rest(tmp_path):
    """R3, round 2's review of K4's first cut: it skipped the flush whole
    while an update was in flight, so b's 40 cents, held back by the
    pacing and untouched by the update, were lost with it."""
    c = _paced(tmp_path)
    c._adds = _ExitAfterAppend(c._adds)
    with pytest.raises(SystemExit):
        c.observe("a", 1.50, 110)
    d = _restart(tmp_path)
    # b has closed and does not render again.
    d.observe("a", 1.50, 110)
    assert d.since(0) == 50 + 40


def test_an_error_inside_observe_does_not_stop_the_exit_flush(tmp_path):
    """R2, the same review: K4's first cut set a flag around observe() and
    cleared it after, not in a finally, so an ordinary exception -- one
    the provider catches and goes on from -- left it set, and every exit
    flush after it was skipped without a word. There is no flag now."""
    c = _paced(tmp_path)

    def boom():
        raise RuntimeError("x")
    c._compact = boom
    with pytest.raises(RuntimeError):
        c.observe("a", 1.50, 110)
    del c._compact
    d = _restart(tmp_path)
    # b has closed and does not render again: its 40 cents are in the
    # file or nowhere.
    d.observe("a", 1.50, 110)
    assert d.since(0) == 50 + 40


def test_an_exit_inside_bind_does_not_save_a_torn_count(tmp_path):
    """bind() names the new login before it empties the count; an exit
    between the two would save the old login's count under the new one."""
    import spend_count
    tick = Tick()
    c = mk(tmp_path, clock=tick)
    c.bind("fp", 0)
    c.observe("a", 1.00, 100)
    c.save()
    c.observe("a", 1.50, 110)           # held back by the pacing
    tick.t += 5
    c.save()

    def boom():
        raise SystemExit(0)
    c._clear = boom
    with pytest.raises(SystemExit):
        c.bind("other", 0)
    spend_count._flush_at_exit()
    d = mk(tmp_path)
    d.bind("other", 0)
    assert d.since(0) == 0



def test_two_live_ledgers_count_again_once_each_is_seen_after_the_anchor():
    """What R1's rule costs two processes taking turns: after a /usage, a
    figure counts toward it only once every mark at or below it has been
    seen after the /usage. A at 10.00 and B at 4.00 before 100. A's first
    rise after it is dropped, as one ledger's would be, and B's first one
    too: its mark took A's older stamp when A passed it. From then on both
    count, every cent spent after 100 that the straddling rule allows."""
    c = SpendCounter(persist=False)
    c.observe("a", 10.00, 90)
    c.observe("a", 4.00, 91)
    c.observe("a", 10.20, 101)          # dropped: straddles 100
    c.observe("a", 4.10, 102)           # dropped: straddles 100
    c.observe("a", 10.50, 103)          # +30
    c.observe("a", 4.30, 104)           # +20
    c.observe("a", 10.70, 105)          # +20
    c.observe("a", 4.40, 106)           # +10
    assert c.since(100) == 30 + 20 + 20 + 10
    assert c.since(0) == 20 + 10 + 30 + 20 + 20 + 10


def test_a_stranded_mark_before_the_anchor_holds_the_count_back():
    """The price of R1's rule, kept: a mark no ledger comes back to -- a
    process that exited, or the old total a /login left behind once the
    new ledger passed it -- stays under every later figure with its old
    stamp, and after a /usage those figures count nothing toward it. It
    could still be a live process whose renders were lost, spending
    before the /usage. Here B exited at 4.00 before the /usage at 100;
    A's rises after it count toward the period, not toward the /usage,
    until the next /usage's figure takes them in exactly."""
    c = SpendCounter(persist=False)
    c.observe("a", 10.00, 90)
    c.observe("a", 4.00, 91)            # B, never seen again
    for i, at in enumerate(range(101, 106)):
        c.observe("a", 10.00 + (i + 1) / 10, at)
    assert c.since(100) == 0
    assert c.since(0) == 50
