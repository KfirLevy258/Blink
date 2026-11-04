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


def test_a_render_clock_ahead_of_the_usage_clock_does_not_count_twice():
    """Round 4 of the 2026-11-04 review. On a WSL desk the render's time is
    the drop file's mtime, from the Windows clock, and the /usage time is
    Date.now() inside the distribution, a clock that runs behind it: 30
    seconds measured, more after the host sleeps. A render made before the
    /usage then carries a time after it, and the rise from it was counted
    whole although part of it was in the /usage figure. A rise counts
    toward a /usage only from a sighting SKEW_S or more after it
    (since_usage, which claude_cli asks with the /usage time)."""
    import spend_count
    skew = spend_count.SKEW_S
    assert 60 <= skew <= 120
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 1030)          # before the /usage, 30 s ahead
    c.observe("a", 1.50, 1100)
    assert c.since_usage(1000) == 0
    c.observe("a", 1.70, 1000 + skew + 50)
    c.observe("a", 2.00, 1000 + skew + 100)
    assert c.since_usage(1000) == 30


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


# -- several processes on one session id (rounds 2 and 3 of 2026-11-04) ------
#
# Each Claude Code process keeps its own cost ledger, and one session id can
# carry several (a second terminal's `claude --continue`, a /login). Round 2
# tried a mark per ledger (K1, R1); round 3 found each cut counting too much
# across a /usage, or holding a session at zero after one, and the counter
# went back to one mark a session (the module docstring). These hold the
# acceptance bar round 3 set: no overcount in random lives, since the period
# start or since a /usage at any step; an idle second terminal holds nothing
# back; coverage no worse than the single mark of before.

def _life(seed, steps=300, procs=4):
    """One session id's processes, at random, one step a second: each
    spends, renders (30% of renders lost to other sessions writing over
    them), takes a /login (cost back to zero), exits (Claude Code saves its
    total as the session's lastCost) or is joined by `claude --continue`,
    which restores that saved total -- or zero when none is saved -- as
    often as it is asked, so two live processes can start from one saved
    total. Returns the counter, the spend as (second, cents) and whether
    the session ever had more than one ledger."""
    import random
    r = random.Random(seed)
    live, saved, spend, t = [0], None, [], 1000
    multi = False
    c = SpendCounter(persist=False)
    for _ in range(steps):
        t += 1
        roll = r.random()
        if not live or (roll < 0.03 and len(live) < procs):
            live.append(saved if saved is not None else 0)
            multi = multi or len(live) > 1
            i = len(live) - 1
        elif roll < 0.06 and len(live) > 1:
            saved = live.pop(r.randrange(len(live)))
            continue
        else:
            i = r.randrange(len(live))
            if roll < 0.07:
                live[i] = 0                          # /login
                multi = True
            else:
                d = r.randrange(0, 120)
                live[i] += d
                spend.append((t, d))
        if r.random() < 0.7:                         # else lost
            c.observe("a", live[i] / 100.0, t)
    return c, spend, multi


def _after(spend, anchor):
    return sum(d for t, d in spend if t > anchor)


# 150 lives and a /usage between every two of their 300 steps: 45,000
# (life, anchor) pairs, the bar round 3 set. Run at 10,000 lives (3,000,000
# pairs) it finds the 3 the module docstring keeps; these 150 find none.
_LIVES = 150
_ANCHORS = [1000 + k + 0.5 for k in range(300)]


def test_random_lives_never_count_more_than_was_spent():
    over = []
    for seed in range(_LIVES):
        c, spend, _multi = _life(seed)
        if c.since(0) > _after(spend, 0):
            over.append((seed, 0))
        for anchor in _ANCHORS:
            if c.since(anchor) > _after(spend, anchor):
                over.append((seed, anchor))
    assert over == []


def test_random_lives_cover_what_the_single_mark_always_did():
    """Since a /usage, in lives with several ledgers: 21.3% of the spend
    after it at 3,000 lives, against 21.9% for the rule before round 3,
    whose restamping on an equal or lower figure also counted too much
    139 times in 900,000 pairs. The gap is the first rise after a render
    that showed no change, now dropped when a /usage falls before it."""
    counted = spent = 0
    for seed in range(_LIVES):
        c, spend, multi = _life(seed)
        if not multi:
            continue
        for anchor in _ANCHORS:
            counted += c.since(anchor)
            spent += _after(spend, anchor)
    assert counted > 0.17 * spent


def _one_process(seed, steps=300):
    import random
    r = random.Random(seed)
    v, t, seen = 0, 1000, []
    c = SpendCounter(persist=False)
    for _ in range(steps):
        t += 1
        v += r.randrange(0, 120)
        if r.random() < 0.7:
            c.observe("a", v / 100.0, t)
            seen.append((t, v))
    return c, seen


@pytest.mark.parametrize("seed", range(20))
def test_one_process_is_counted_whole_but_its_first_rise_after_a_usage(seed):
    """A session with one ledger, the usual one: since the period start,
    everything from its first figure to its last; since a /usage,
    everything from the first figure after it that moved the total -- the
    straddling rise before that is the one the rule drops."""
    c, seen = _one_process(seed)
    assert c.since(0) == seen[-1][1] - seen[0][1]
    for anchor in _ANCHORS:
        later = [v for t, v in seen if t > anchor]
        before = [v for t, v in seen if t < anchor]
        mark = max(before, default=None)
        base = next((v for v in later if mark is None or v > mark), None)
        want = 0 if base is None else seen[-1][1] - base
        assert c.since(anchor) == want


@pytest.mark.parametrize("seed", range(20))
def test_an_idle_second_terminal_holds_nothing_back(seed):
    """Round 3's (c): one idle `claude --continue` terminal at 4.00 before
    a /usage pinned every later rise of the session at the oldest stamp,
    and the count since the /usage stayed at zero for the month. Every
    rise of the working terminal after its first rise after the /usage
    is counted."""
    import random
    r = random.Random(seed)
    c = SpendCounter(persist=False)
    c.observe("a", 10.00, 90)
    c.observe("a", 4.00, 91)            # the idle terminal, never again
    anchor, v, t, seen = 100, 1000, 100, []
    for _ in range(200):
        t += 1
        v += r.randrange(1, 120)
        if r.random() < 0.7:
            c.observe("a", v / 100.0, t)
            seen.append(v)
    assert c.since(anchor) == seen[-1] - seen[0]


def test_a_lower_ledger_landing_on_a_mark_across_a_usage():
    """Round 3's (a): B at 4.00 before a /usage at 100, really 4.50 by then
    (its renders lost); a figure of 4.05 from another process after it,
    then B's 4.60. Counted 55 cents for about 15. Below the session's
    mark of 12.00, neither counts now."""
    c = SpendCounter(persist=False)
    c.observe("s", 12.00, 95)
    c.observe("s", 4.00, 96)
    c.observe("s", 4.05, 107)
    c.observe("s", 4.60, 110)
    assert c.since(100) == 0


def test_an_equal_figure_does_not_restamp_the_mark():
    """Round 3's (b), and U10 of the final review: an equal figure from
    another process -- 0.00 from every new process or /login, a resume's
    restored total -- stamped the mark after the /usage, and the next rise
    was counted whole although part of it was spent before. 0.50 at 95;
    a new process at 0.00 at 99 that spends 30 cents before 100, its
    renders lost; another at 0.00 at 105; 0.40 at 110."""
    c = SpendCounter(persist=False)
    c.observe("s", 0.50, 95)
    c.observe("s", 0.00, 99)
    c.observe("s", 0.00, 105)
    c.observe("s", 0.40, 110)
    assert c.since(100) == 0
    # And the U10 shape: the leader at 1.00, its render at 1.20 lost, a
    # lagging process at 1.00 after the /usage, the leader at 1.30.
    c = SpendCounter(persist=False)
    c.observe("s", 1.00, 95)
    c.observe("s", 1.00, 105)
    c.observe("s", 1.30, 110)
    assert c.since(100) == 0
    assert c.since(0) == 30


def test_two_processes_taking_turns_count_the_leader_only():
    c = SpendCounter(persist=False)
    for i, v in enumerate([10.00, 4.00, 10.30, 4.20, 10.50, 4.60]):
        c.observe("a", v, 100 + i)
    assert c.since(0) == 50


def test_a_same_seat_relogin_is_counted_once_past_its_old_total():
    """U9, kept (the module docstring): /logout and /login to the same seat
    keep the session id and the fingerprint, and the cost starts again
    from zero. Nothing counts until the session passes its old total; the
    next /usage takes the rest in."""
    c = SpendCounter(persist=False)
    c.observe("a", 16.84, 100)
    c.observe("a", 17.00, 200)          # +16
    c.observe("a", 0.00, 300)           # /login
    c.observe("a", 2.50, 400)
    c.observe("a", 17.20, 500)          # +20
    assert c.since(0) == 16 + 20


def test_a_file_from_the_per_ledger_builds_loads_empty(tmp_path):
    """Round 2's builds, never released, wrote a session as [mark, seen,
    mark, seen, ...]. Round 5's ruling D: such a file is not converted but
    read as empty, as any file this cannot believe is; the next /usage
    makes the figure exact again. A conversion had to guess which time
    went with the mark, and guessed wrong once already (round 4)."""
    d = _v2(sessions={"a": [400, 96, 1200, 150]}, adds=[[10, 20, 5]])
    (tmp_path / "spend-count.json").write_text(json.dumps(d))
    c = mk(tmp_path)
    c.bind("fp", 0)
    assert c.since(0) == 0
    assert c.observe("a", 12.30, 170) is True       # a first sighting
    assert c.since(0) == 0


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



def test_a_rise_after_the_usage_is_seen_even_inside_the_margin():
    """Round 6 of the 2026-11-04 review, item 6: the /usage margin is back,
    and the page said "exact" while a rise inside it went uncounted, since
    exact was "nothing added". rose_after() says whether any rise was
    seen after a time, counted or not."""
    c = SpendCounter(persist=False)
    c.observe("a", 1.00, 1010)
    assert c.rose_after(1000) is False
    c.observe("a", 1.50, 1030)                  # inside the margin
    assert c.since_usage(1000) == 0
    assert c.rose_after(1000) is True
    assert c.rose_after(1030) is False


def test_the_margin_and_the_fold_agree_on_a_file_this_build_writes(
        tmp_path):
    """Item 6: since_usage moves the anchor the fold is keyed on. On a file
    written by this build the boundary is the anchor since_usage asks, so
    rises folded out of the five hours still count toward it, before and
    after a restart: ten hours of renders after a /usage at t."""
    t = 1_800_000_000
    c = mk(tmp_path)
    c.bind("fp", 0)
    c.observe("a", 0.0, t + 10)
    want = 0
    for i in range(1, 121):
        at = t + 10 + i * 300
        c.observe("a", i * 5 / 100.0, at)
        if at - 300 >= t + SKEW():
            want += 5
        c.since_usage(t)
    c.save()
    assert c._fold is not None and c._fold[0] == t + SKEW()
    assert c.since_usage(t) == want
    d = mk(tmp_path)
    d.bind("fp", 0)
    assert d.since_usage(t) == want


def SKEW():
    import spend_count
    return spend_count.SKEW_S
