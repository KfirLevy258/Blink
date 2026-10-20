"""OpenCode's own token counts against a budget the person sets.

The desk this is for runs OpenCode against a self-hosted model: no GLM plan,
no quota anywhere, and until this a page of blank rings. What is guarded
hardest is what the reader may touch -- token counts and provider ids, never
a message's text, and never a -wal file that would make a look read as
OpenCode activity -- and that a desk with no budget is exactly what it was.

Every database here is built by tests/support/opencode_fixture.py; nothing
was captured from a real machine.
"""
import json
import os
import sqlite3
import sys

import pytest

from pc import cli, ingest, protocol
from pc.providers import base, claude_state, opencode, opencode_budget as ob
from tests.support import opencode_fixture as fx

NOW = 1_787_700_000.0
NOW_MS = int(NOW * 1000)
H = 3600 * 1000
D = 24 * H


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    claude_state.reset_pid_liveness()
    opencode.reset_pid_memory()
    yield
    claude_state.reset_pid_liveness()
    opencode.reset_pid_memory()


@pytest.fixture
def dirs(tmp_path):
    data = tmp_path / "data" / "opencode"
    state = tmp_path / "state-opencode"
    data.mkdir(parents=True)
    state.mkdir()
    return str(data), str(state)


def tok(n):
    """A reply worth `n` tokens, spread over every field the total sums."""
    return fx.tokens(input=n - 4, output=1, reasoning=1, read=1, write=1)


def provider(dirs, tmp_path, session=None, weekly=None, reader=None,
             **kw):
    data, state = dirs
    path = None
    if session is not None or weekly is not None:
        path = fx.write_budget(str(tmp_path), session=session, weekly=weekly)
    counter = ob.TokenCounter(reader=reader) if reader else None
    return opencode.OpenCodeProvider(
        state_dir=state, data_path=data, sweep=False, fetch=False,
        budget_path=path or os.path.join(str(tmp_path), "no-budget.json"),
        tokens=counter, **kw)


def usage(frames):
    return [f for f in frames if f.src != opencode.STATE_SRC_ID]


# --- the plan providers ----------------------------------------------------


def test_the_plan_providers_are_the_quota_hosts():
    """Kept in step by this test rather than by an import: opencode imports
    opencode_budget, so the budget module cannot import it back at load."""
    assert set(ob.PLAN_PROVIDERS) == {p for p, _h in opencode.QUOTA_HOSTS}


# --- amounts -----------------------------------------------------------------


@pytest.mark.parametrize("text,n", [
    ("2M", 2_000_000), ("2m", 2_000_000), ("500K", 500_000),
    ("1.5M", 1_500_000), ("2,000,000", 2_000_000), ("1_000", 1000),
    ("3G", 3_000_000_000), ("1B", 1_000_000_000), (" 20M ", 20_000_000),
])
def test_amounts_people_type(text, n):
    assert ob.parse_amount(text) == n


@pytest.mark.parametrize("text", [
    "", "M", "two", "2X", "-5M", "2", "999", "inf", "nan", "1e20",
])
def test_amounts_that_are_not_a_budget(text):
    with pytest.raises(ValueError) as e:
        ob.parse_amount(text)
    assert str(e.value)


@pytest.mark.parametrize("n,text", [
    (0, "0"), (999, "999"), (1000, "1K"), (1290, "1.3K"),
    (2_000_000, "2M"), (20_000_000, "20M"), (150_000_000, "150M"),
    (2_500_000_000, "2.5G"),
])
def test_format_tokens(n, text):
    assert ob.format_tokens(n) == text


# --- the budget file ---------------------------------------------------------


def test_no_file_is_no_budget(tmp_path):
    assert ob.read_budget(str(tmp_path / "nope.json")) is None


@pytest.mark.parametrize("raw", [
    b"", b"not json", b"\xff\xfe", b"[1, 2]", b"null", b"{}",
    b'{"session_tokens": "2M"}', b'{"session_tokens": true}',
    b'{"session_tokens": -5}', b'{"session_tokens": 10}',
    b'{"session_tokens": 1e300}', b'{"weekly_tokens": NaN}',
    pytest.param(b"[" * 100000, id="deep"),
    pytest.param(b'{"session_tokens": 2000000, "x": "' + b"a" * 5000 + b'"}',
                 id="oversized"),
])
def test_a_garbage_file_is_no_budget(tmp_path, raw):
    path = fx.write_budget(str(tmp_path), raw=raw)
    assert ob.read_budget(path) is None


def test_one_window_alone_is_a_budget(tmp_path):
    path = fx.write_budget(str(tmp_path), weekly=20_000_000)
    assert ob.read_budget(path) == ob.Budget(None, 20_000_000)
    path = fx.write_budget(str(tmp_path), session=2_000_000,
                           weekly="garbage")
    assert ob.read_budget(path) == ob.Budget(2_000_000, None)


def test_the_default_path_is_resolved_under_home_at_call_time(tmp_path):
    # conftest moved HOME to tmp_path after import; the path follows it.
    assert ob.budget_path() == os.path.normpath(
        str(tmp_path / ".blink" / "opencode-budget.json"))


def test_write_is_atomic_and_private(tmp_path):
    path = str(tmp_path / ".blink" / "opencode-budget.json")
    ob.write_budget(ob.Budget(2_000_000, 20_000_000), path)
    with open(path) as f:
        assert json.load(f) == {"session_tokens": 2_000_000,
                                "weekly_tokens": 20_000_000}
    if sys.platform != "win32":
        assert os.stat(path).st_mode & 0o777 == 0o600
    assert [n for n in os.listdir(os.path.dirname(path))
            if n.endswith(".tmp")] == []
    assert ob.clear_budget(path) is True
    assert not os.path.exists(path)
    assert ob.clear_budget(path) is False


def test_the_daemon_sees_a_new_budget_without_a_restart(tmp_path):
    path = str(tmp_path / ".blink" / "opencode-budget.json")
    bf = ob.BudgetFile(path)
    assert bf.read() is None
    ob.write_budget(ob.Budget(2_000_000, None), path)
    assert bf.read() == ob.Budget(2_000_000, None)
    # Same size, same second: os.replace's new inode still says "changed".
    ob.write_budget(ob.Budget(3_000_000, None), path)
    assert bf.read() == ob.Budget(3_000_000, None)
    ob.clear_budget(path)
    assert bf.read() is None


def test_an_unchanged_file_is_not_reread(tmp_path, monkeypatch):
    path = str(tmp_path / ".blink" / "opencode-budget.json")
    ob.write_budget(ob.Budget(2_000_000, None), path)
    bf = ob.BudgetFile(path)
    bf.read()
    calls = []
    monkeypatch.setattr(ob, "read_budget",
                        lambda p=None: calls.append(p) or None)
    assert bf.read() == ob.Budget(2_000_000, None)
    assert calls == []


# --- what is counted ---------------------------------------------------------


def rows_v1(data, msgs, **kw):
    path = fx.make_message_db(data, v1=msgs)
    return ob.read_rows(path, NOW_MS - 7 * D, **kw)


@pytest.mark.parametrize("json1", [None, False])
def test_assistant_messages_of_other_providers_are_counted(dirs, json1):
    data, _ = dirs
    rows = rows_v1(data, [
        fx.v1_message(NOW_MS - H, tok=tok(1000)),
        fx.v1_message(NOW_MS - H + 1, role="user"),
        fx.v1_message(NOW_MS - 2 * H, provider="ollama", tok=tok(500)),
    ], json1=json1)
    assert sorted(rows) == [(NOW_MS - 2 * H, 500), (NOW_MS - H, 1000)]


@pytest.mark.parametrize("json1", [None, False])
@pytest.mark.parametrize("plan", ["zai-coding-plan", "zhipuai-coding-plan"])
def test_the_plan_providers_are_left_out(dirs, json1, plan):
    """They have a real quota of their own; counting them here as well
    would charge a plan user's paid traffic against their own server."""
    data, _ = dirs
    rows = rows_v1(data, [
        fx.v1_message(NOW_MS - H, provider=plan, tok=tok(9000)),
        fx.v1_message(NOW_MS - H + 5, tok=tok(1000)),
    ], json1=json1)
    assert rows == [(NOW_MS - H + 5, 1000)]


def test_the_total_is_the_five_fields_not_the_stored_total(dirs):
    """OpenCode's own figure is the sum (`opencode stats`); a stored
    `total` that disagrees, or none at all, changes nothing."""
    data, _ = dirs
    t = fx.tokens(input=1034, output=49, reasoning=7, read=200, write=0)
    assert t["total"] == 1290                     # as measured on 1.19.3
    t2 = dict(t, total=5)
    t3 = fx.tokens(input=1034, output=49, reasoning=7, read=200, total=False)
    rows = rows_v1(data, [fx.v1_message(NOW_MS - 3, tok=t),
                          fx.v1_message(NOW_MS - 2, tok=t2),
                          fx.v1_message(NOW_MS - 1, tok=t3)])
    assert [n for _t, n in rows] == [1290, 1290, 1290]


@pytest.mark.parametrize("json1", [None, False])
def test_odd_token_fields_count_as_zero(dirs, json1):
    data, _ = dirs
    bad = {"input": "100", "output": None, "reasoning": True,
           "cache": {"read": -5, "write": 7.0}}
    rows = rows_v1(data, [fx.v1_message(NOW_MS - 1, tok=bad),
                          fx.v1_message(NOW_MS - 2, tok={"cache": "x"})],
                   json1=json1)
    assert sorted(n for _t, n in rows) == [0, 7]


def test_rows_older_than_the_week_are_not_read(dirs):
    data, _ = dirs
    rows = rows_v1(data, [fx.v1_message(NOW_MS - 7 * D - 1, tok=tok(1000)),
                          fx.v1_message(NOW_MS - 7 * D, tok=tok(2000))])
    assert rows == [(NOW_MS - 7 * D, 2000)]


# --- OpenCode 2.0 --------------------------------------------------------------


@pytest.mark.parametrize("json1", [None, False])
def test_2_0s_session_message_is_read(dirs, json1):
    data, _ = dirs
    path = fx.make_message_db(data, v2=[
        fx.v2_message(NOW_MS - H, tok=tok(1000)),
        fx.v2_message(NOW_MS - H + 1, kind="user"),
        fx.v2_message(NOW_MS - H + 2, kind="compaction"),
        fx.v2_message(NOW_MS - H + 3, provider="zai-coding-plan",
                      tok=tok(9000)),
    ], sessions=[("ses_fixture", None, NOW_MS - D)])
    assert ob.read_rows(path, NOW_MS - 7 * D, json1=json1) == [
        (NOW_MS - H, 1000)]


@pytest.mark.parametrize("json1", [None, False])
def test_2_0_rows_older_than_the_week_are_not_read(dirs, json1):
    """The bound on the work, in SQL: sum_rows would drop them anyway, but
    a read that pulled every message ever written would grow without end."""
    data, _ = dirs
    path = fx.make_message_db(data, v2=[
        fx.v2_message(NOW_MS - 7 * D - 1, tok=tok(1000), mid="old"),
        fx.v2_message(NOW_MS - 7 * D, tok=tok(2000), mid="edge"),
    ], sessions=[("ses_fixture", None, NOW_MS - 30 * D)])
    assert ob.read_rows(path, NOW_MS - 7 * D, json1=json1) == [
        (NOW_MS - 7 * D, 2000)]


@pytest.mark.parametrize("json1", [None, False])
def test_1_19_3s_empty_session_message_table_hides_nothing(dirs, json1):
    """The shape of a real OpenCode 1.19.3 database: its messages in
    `message`, and 2.0's session_message already created and EMPTY. Found
    end to end, 2026-10-19: a reader that preferred session_message
    wherever it existed counted zero on every 1.x desk."""
    data, _ = dirs
    path = fx.make_message_db(
        data, v1=[fx.v1_message(NOW_MS - H, tok=tok(1290))], v2=[])
    assert ob.read_rows(path, NOW_MS - 7 * D, json1=json1) == [
        (NOW_MS - H, 1290)]


@pytest.mark.parametrize("json1", [None, False])
def test_both_tables_are_summed_and_one_id_counts_once(dirs, json1):
    """A desk upgraded mid-week: 1.x replies in `message`, 2.0's in
    session_message. A message id in both (nothing seen writes one, but a
    projection could) counts once, as 2.0 has it."""
    data, _ = dirs
    path = fx.make_message_db(
        data,
        v1=[fx.v1_message(NOW_MS - 2 * H, tok=tok(5000), mid="msg_old"),
            fx.v1_message(NOW_MS - H, tok=tok(7000), mid="msg_both")],
        v2=[fx.v2_message(NOW_MS - 30_000, tok=tok(1000), mid="msg_new"),
            fx.v2_message(NOW_MS - H, tok=tok(3000), mid="msg_both")],
        sessions=[("ses_fixture", None, NOW_MS - D)])
    assert sorted(ob.read_rows(path, NOW_MS - 7 * D, json1=json1)) == [
        (NOW_MS - 2 * H, 5000), (NOW_MS - H, 3000), (NOW_MS - 30_000, 1000)]


@pytest.mark.parametrize("json1", [None, False])
def test_a_forks_copied_messages_are_not_counted_twice(dirs, json1):
    """2.0's own stats leave out what a forked session copied from its
    parent: the messages older than the fork itself."""
    data, _ = dirs
    path = fx.make_message_db(data, v2=[
        fx.v2_message(NOW_MS - 3 * H, tok=tok(1000), session="parent",
                      mid="a"),
        fx.v2_message(NOW_MS - 3 * H, tok=tok(1000), session="fork",
                      mid="a-copy"),
        fx.v2_message(NOW_MS - H, tok=tok(2000), session="fork", mid="b"),
    ], sessions=[("parent", None, NOW_MS - D),
                 ("fork", "parent", NOW_MS - 2 * H)])
    assert sorted(ob.read_rows(path, NOW_MS - 7 * D, json1=json1)) == [
        (NOW_MS - 3 * H, 1000), (NOW_MS - H, 2000)]


def test_a_database_with_no_message_table_counts_nothing(dirs):
    data, _ = dirs
    path = fx.make_credential_db(data)
    assert ob.read_rows(path, 0) == []


# --- the windows -------------------------------------------------------------


def test_the_windows_edges():
    s0, w0 = NOW_MS - 5 * H, NOW_MS - 7 * D
    rows = [(s0, 1), (s0 - 1, 10), (w0, 100), (w0 - 1, 1000),
            (NOW_MS + 60_000, 10000)]
    use = ob.sum_rows(rows, NOW)
    # The session window holds its first millisecond and the future row
    # (a clock that stepped) -- not the millisecond before.
    assert use.session_tokens == 10001
    assert use.weekly_tokens == 10111
    assert use.messages == 4


def test_a_message_ages_out_with_nothing_reread(dirs):
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - 5 * H + 1000,
                                               tok=tok(1000))])
    calls = []

    def reader(path, since):
        calls.append(since)
        return ob.read_rows(path, since)
    c = ob.TokenCounter(reader=reader)
    paths = opencode.db_paths(data)
    assert c.use(paths, NOW).session_tokens == 1000
    assert c.use(paths, NOW + 2).session_tokens == 0
    assert c.use(paths, NOW + 2).weekly_tokens == 1000
    assert len(calls) == 1


# --- the cache ---------------------------------------------------------------


def test_an_unchanged_database_is_read_once(dirs):
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H, tok=tok(1000))])
    calls = []

    def reader(path, since):
        calls.append(path)
        return ob.read_rows(path, since)
    c = ob.TokenCounter(reader=reader)
    paths = opencode.db_paths(data)
    for i in range(5):
        assert c.use(paths, NOW + 60 * i).weekly_tokens == 1000
    assert len(calls) == 1


def test_a_change_is_read_but_not_more_often_than_min_requery(dirs):
    data, _ = dirs
    path = fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H,
                                                      tok=tok(1000))])
    calls = []

    def reader(p, since):
        calls.append(p)
        return ob.read_rows(p, since)
    c = ob.TokenCounter(reader=reader)
    paths = [path]
    c.use(paths, NOW)
    con = sqlite3.connect(path)
    m, s, t, d = fx.v1_message(NOW_MS - H + 10, tok=tok(500))
    con.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (m, s, t, t, d))
    con.commit()
    con.close()
    # Moved, but a second later: the cached rows stand.
    assert c.use(paths, NOW + 1).weekly_tokens == 1000
    assert len(calls) == 1
    # Moved, and MIN_REQUERY_S on: read again.
    assert c.use(paths, NOW + ob.MIN_REQUERY_S).weekly_tokens == 1500
    assert len(calls) == 2


def test_the_wal_moving_is_a_change(dirs, tmp_path):
    data, _ = dirs
    path = fx.make_message_db(data, wal=False)
    st1 = ob._stamp(path)
    with open(path + "-wal", "wb") as f:
        f.write(b"x")
    assert ob._stamp(path) != st1


# --- what the read leaves behind ---------------------------------------------


def test_reading_creates_no_wal_or_shm(dirs):
    """A -wal stamped now is OpenCode activity to last_activity(); a look
    that created one would make every budget read look like somebody at
    the desk (opencode.connect_readonly says how this was measured)."""
    data, _ = dirs
    path = fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H,
                                                      tok=tok(1000))])
    assert not os.path.exists(path + "-wal")
    before = os.path.getmtime(path)
    for json1 in (None, False):
        assert ob.read_rows(path, 0, json1=json1) == [(NOW_MS - H, 1000)]
    assert not os.path.exists(path + "-wal")
    assert not os.path.exists(path + "-shm")
    assert os.path.getmtime(path) == before


def test_reading_beside_a_live_wal_leaves_its_mtime_alone(dirs):
    data, _ = dirs
    path = fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H,
                                                      tok=tok(1000))])
    writer = sqlite3.connect(path)          # OpenCode, open
    try:
        writer.execute("PRAGMA journal_mode=WAL")
        m, s, t, d = fx.v1_message(NOW_MS - 10, tok=tok(7000))
        writer.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)",
                       (m, s, t, t, d))
        writer.commit()
        assert os.path.exists(path + "-wal")
        os.utime(path + "-wal", (NOW - 3600, NOW - 3600))
        rows = ob.read_rows(path, 0)
        # The row still sitting in the WAL is seen: mode=ro, not immutable.
        assert sorted(rows) == [(NOW_MS - H, 1000), (NOW_MS - 10, 7000)]
        assert os.path.getmtime(path + "-wal") == NOW - 3600
    finally:
        writer.close()


def test_the_read_cannot_write(dirs, monkeypatch):
    data, _ = dirs
    path = fx.make_message_db(data)
    con = opencode.connect_readonly(path)
    try:
        with pytest.raises(sqlite3.Error):
            con.execute("DELETE FROM message")
    finally:
        con.close()


# --- failure -----------------------------------------------------------------


def test_a_broken_database_is_no_number_and_one_quiet_line(dirs, capsys):
    data, _ = dirs
    path = os.path.join(data, "opencode.db")
    with open(path, "wb") as f:
        f.write(fx.SECRET_TEXT.encode() * 100)
    c = ob.TokenCounter()
    for i in range(3):
        assert c.use([path], NOW + 60 * i) is None
    err = capsys.readouterr().err
    assert err.count("[opencode] token count:") == 1
    assert ob.ERR_DB in err
    assert fx.SECRET_TEXT not in err


def test_a_reader_that_raises_never_reaches_the_bus(dirs, tmp_path):
    def boom(path, since):
        raise RuntimeError(fx.SECRET_TEXT)
    data, _ = dirs
    fx.make_message_db(data)
    p = provider(dirs, tmp_path, session=2_000_000, reader=boom)
    assert usage(p.poll(NOW)) == []


# --- back at the budget ------------------------------------------------------


def test_under_or_at_budget_there_is_nothing_to_count_down_to():
    rows = [(NOW_MS - 4 * H, 600), (NOW_MS - H, 400)]
    assert ob.clear_at(rows, NOW, ob.SESSION_WINDOW_S, 1000) is None
    assert ob.clear_at(rows, NOW, ob.SESSION_WINDOW_S, 5000) is None
    assert ob.clear_at(rows, NOW, ob.SESSION_WINDOW_S, None) is None


def test_over_budget_counts_down_to_the_reply_whose_leaving_is_enough():
    """1320 of 1000 used: the oldest reply (100) leaving is not enough,
    the next (300) is -- so the moment is that one's, five hours on."""
    rows = [(NOW_MS - 4 * H, 100), (NOW_MS - 3 * H, 300),
            (NOW_MS - H, 900), (NOW_MS - 2 * H, 20)]
    want = (NOW_MS - 3 * H) / 1000 + ob.SESSION_WINDOW_S
    assert ob.clear_at(rows, NOW, ob.SESSION_WINDOW_S, 1000) == want
    # Exactly at the budget is back: 1320 - 320 = 1000.
    assert ob.clear_at([(NOW_MS - 4 * H, 320), (NOW_MS - H, 1000)], NOW,
                       ob.SESSION_WINDOW_S, 1000) == \
        (NOW_MS - 4 * H) / 1000 + ob.SESSION_WINDOW_S


def test_a_reply_already_out_of_the_window_is_not_waited_for():
    rows = [(NOW_MS - 6 * H, 5000), (NOW_MS - 2 * H, 800),
            (NOW_MS - H, 700)]
    assert ob.clear_at(rows, NOW, ob.SESSION_WINDOW_S, 1000) == \
        (NOW_MS - 2 * H) / 1000 + ob.SESSION_WINDOW_S


def test_one_reply_over_the_budget_by_itself_waits_a_whole_window():
    rows = [(NOW_MS - H, 5000), (NOW_MS + 60_000, 10)]
    # The future row (a clock that stepped) counts as made now.
    assert ob.clear_at(rows, NOW, ob.SESSION_WINDOW_S, 1000) == \
        (NOW_MS - H) / 1000 + ob.SESSION_WINDOW_S
    assert ob.clear_at(rows, NOW, ob.SESSION_WINDOW_S, 4) == \
        NOW + ob.SESSION_WINDOW_S


def test_sum_rows_carries_both_moments_only_with_a_budget():
    rows = [(NOW_MS - 2 * D, 3000), (NOW_MS - H, 1500)]
    plain = ob.sum_rows(rows, NOW)
    assert plain.session_clear_at is None and plain.weekly_clear_at is None
    use = ob.sum_rows(rows, NOW, ob.Budget(1000, 4000))
    assert use.session_clear_at == (NOW_MS - H) / 1000 + ob.SESSION_WINDOW_S
    assert use.weekly_clear_at == (NOW_MS - 2 * D) / 1000 + ob.WEEKLY_WINDOW_S


# --- with no budget: the person's own record ---------------------------------


def test_the_record_is_the_busiest_finished_window():
    rows = [(NOW_MS - 3 * D, 300), (NOW_MS - 3 * D + 4 * H, 200),   # 500
            (NOW_MS - 10 * D, 450),
            (NOW_MS - 2 * H, 9999)]          # the current window: not a record
    assert ob.peak(rows, NOW, ob.SESSION_WINDOW_S) == 500


def test_a_window_reaching_into_the_current_one_is_not_a_record():
    """The last window allowed ends exactly where the current one starts."""
    end = NOW_MS - 5 * H
    rows = [(end, 700), (end + 1, 50)]
    assert ob.peak(rows, NOW, ob.SESSION_WINDOW_S) == 700


def test_the_record_forgets_what_is_older_than_the_history():
    rows = [(NOW_MS - 31 * D, 5000), (NOW_MS - 20 * D, 100)]
    assert ob.peak(rows, NOW, ob.SESSION_WINDOW_S) == 100
    assert ob.peak(rows, NOW, ob.WEEKLY_WINDOW_S) == 100


def test_no_finished_window_with_tokens_is_no_record():
    assert ob.peak([], NOW, ob.SESSION_WINDOW_S) is None
    assert ob.peak([(NOW_MS - H, 800)], NOW, ob.SESSION_WINDOW_S) is None
    assert ob.peak([(NOW_MS - 2 * D, 0)], NOW, ob.SESSION_WINDOW_S) is None


def test_the_record_agrees_with_trying_every_window():
    """Against brute force: every window end, minute by minute."""
    import random
    rnd = random.Random(7)
    rows = [(NOW_MS - rnd.randrange(0, 30 * D), rnd.randrange(1, 100))
            for _ in range(60)]
    first, w = NOW_MS - 30 * D, 5 * H
    best = 0
    for end in range(int(first + w), int(NOW_MS - w) + 1, 60_000):
        best = max(best, sum(n for t, n in rows if end - w <= t <= end))
    got = ob.peak(rows, NOW, ob.SESSION_WINDOW_S)
    assert got >= best
    # ...and it is a sum some window really held (ends on a reply).
    ends = [t for t, _n in rows if first + w <= t <= NOW_MS - w]
    assert got in {sum(n for t, n in rows if e - w <= t <= e)
                   for e in ends + [NOW_MS - w, first + w]}


def test_with_no_budget_the_rings_measure_against_the_record(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[
        fx.v1_message(NOW_MS - 3 * D, tok=tok(400_000)),      # the record
        fx.v1_message(NOW_MS - H, tok=tok(100_000)),          # now
    ])
    p = provider(dirs, tmp_path)
    [f] = usage(p.poll(NOW))
    assert f.src == opencode.TOKENS_SRC_ID
    assert f.session_pct == pytest.approx(25.0)
    # The weekly window holds both; the only finished week holds nothing,
    # so the weekly ring has no record yet.
    assert f.weekly_pct == base.UNKNOWN
    assert f.session_resets_at is None and f.weekly_resets_at is None


def test_harder_than_ever_goes_past_100_with_no_countdown(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[
        fx.v1_message(NOW_MS - 3 * D, tok=tok(100_000)),
        fx.v1_message(NOW_MS - H, tok=tok(250_000)),
    ])
    [f] = usage(provider(dirs, tmp_path).poll(NOW))
    assert f.session_pct == pytest.approx(250.0)
    assert f.session_resets_at is None


def test_a_budget_wins_for_its_window_and_the_record_fills_the_other(
        dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[
        fx.v1_message(NOW_MS - 20 * D, tok=tok(1_000_000)),
        fx.v1_message(NOW_MS - 3 * D, tok=tok(400_000)),
        fx.v1_message(NOW_MS - H, tok=tok(100_000)),
    ])
    p = provider(dirs, tmp_path, session=200_000)
    [f] = usage(p.poll(NOW))
    assert f.session_pct == pytest.approx(50.0)                  # the budget
    assert f.weekly_pct == pytest.approx(50.0)     # 500K of the 1M record


# --- the frame and the bus -----------------------------------------------------


def test_no_opencode_database_reads_nothing(dirs, tmp_path):
    calls = []
    p = provider(dirs, tmp_path,
                 reader=lambda path, since: calls.append(path) or [])
    assert usage(p.poll(NOW)) == []
    assert p.token_use(NOW) == (None, None)
    assert calls == []


def test_the_table_is_read_back_as_far_as_the_record_looks(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H, tok=tok(1000))])
    calls = []
    p = provider(dirs, tmp_path,
                 reader=lambda path, since: calls.append(since) or [])
    p.token_use(NOW)
    assert calls == [(NOW - ob.HISTORY_S) * 1000.0]


def test_no_budget_is_the_blank_page_it_always_was(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H, tok=tok(1000))])
    bus = ingest.IngestionBus(providers=[provider(dirs, tmp_path)],
                              now=lambda: NOW)
    msg = bus.poll()
    assert msg["src"] == protocol.BLANK_SRC
    assert msg["session_pct"] < 0 and msg["weekly_pct"] < 0


def test_a_budget_puts_token_use_on_the_rings(dirs, tmp_path):
    data, state = dirs
    path = fx.make_message_db(data, v1=[
        fx.v1_message(NOW_MS - H, tok=tok(500_000)),
        fx.v1_message(NOW_MS - 2 * D, tok=tok(4_500_000)),
        fx.v1_message(NOW_MS - 30 * 60_000, provider="zai-coding-plan",
                      tok=tok(9_000_000)),
    ])
    fx.stamp(path, NOW - 120)
    p = provider(dirs, tmp_path, session=2_000_000, weekly=20_000_000)
    [f] = usage(p.poll(NOW))
    assert f.provider == "opencode" and f.src == opencode.TOKENS_SRC_ID
    assert f.session_pct == pytest.approx(25.0)
    assert f.weekly_pct == pytest.approx(25.0)
    assert f.session_resets_at is None and f.weekly_resets_at is None
    assert f.observed_at == NOW
    assert f.active_at == NOW - 120              # OpenCode's, not the poll's
    assert fx.SECRET_TEXT not in repr(f)

    bus = ingest.IngestionBus(providers=[p], now=lambda: NOW)
    msg = bus.poll()
    assert msg["src"] == "tokens"
    assert msg["session_pct"] == pytest.approx(25.0)
    assert msg["weekly_pct"] == pytest.approx(25.0)
    assert msg["session_resets_in_s"] == -1
    assert msg["weekly_resets_in_s"] == -1
    line = protocol.encode_checked(msg)
    assert fx.SECRET_TEXT.encode() not in line


def test_over_budget_the_rings_count_down_to_being_back_at_it(dirs, tmp_path):
    """The owner's 132%: over the five-hour budget, the session ring counts
    down to the moment it is back at 100%, plus the margin that keeps the
    board's own zero from landing first. The weekly ring, under budget,
    keeps `--`."""
    data, _ = dirs
    fx.make_message_db(data, v1=[
        fx.v1_message(NOW_MS - 4 * H, tok=tok(1_000_000)),
        fx.v1_message(NOW_MS - H, tok=tok(1_640_000)),
    ])
    p = provider(dirs, tmp_path, session=2_000_000, weekly=20_000_000)
    [f] = usage(p.poll(NOW))
    assert f.session_pct == pytest.approx(132.0)
    back = (NOW_MS - 4 * H) / 1000 + ob.SESSION_WINDOW_S
    assert f.session_resets_at == back + opencode.CLEAR_MARGIN_S
    assert f.weekly_resets_at is None

    msg = ingest.IngestionBus(providers=[p], now=lambda: NOW).poll()
    assert msg["session_resets_in_s"] == H // 1000 + opencode.CLEAR_MARGIN_S
    assert msg["weekly_resets_in_s"] == -1


def test_one_window_budgeted_leaves_the_other_ring_blank(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H, tok=tok(10_000))])
    p = provider(dirs, tmp_path, weekly=1_000_000)
    [f] = usage(p.poll(NOW))
    assert f.session_pct == base.UNKNOWN
    assert f.weekly_pct == pytest.approx(1.0)


def test_an_overrun_is_held_at_the_boards_ceiling(dirs, tmp_path):
    """proto.c ignores a percentage above PCT_MAX outright and keeps the
    ring it last drew; held at the ceiling, it reads "over"."""
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H,
                                               tok=tok(50_000_000))])
    p = provider(dirs, tmp_path, session=1_000_000)
    [f] = usage(p.poll(NOW))
    assert f.session_pct == ob.PCT_CEILING == 1000.0


def test_a_server_that_reports_no_usage_leaves_the_rings_blank(dirs,
                                                               tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[
        fx.v1_message(NOW_MS - H, tok=fx.tokens()),
        fx.v1_message(NOW_MS - 2 * H, tok=fx.tokens(total=False)),
    ])
    p = provider(dirs, tmp_path, session=2_000_000, weekly=20_000_000)
    assert usage(p.poll(NOW)) == []
    _b, use = p.token_use(NOW)
    assert ob.no_counts(use)


def test_an_idle_week_is_zero_percent_not_blank(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data)
    p = provider(dirs, tmp_path, session=2_000_000, weekly=20_000_000)
    [f] = usage(p.poll(NOW))
    assert f.session_pct == 0.0 and f.weekly_pct == 0.0
    assert not ob.no_counts(p.token_use(NOW)[1])


def test_the_plan_reading_wins_over_the_budget(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H,
                                               tok=tok(1_000_000))])
    p = provider(dirs, tmp_path, session=2_000_000, weekly=20_000_000)
    [f] = usage(p.poll(NOW))
    assert f.src == opencode.TOKENS_SRC_ID
    p.remember(opencode.Quota(34.0, NOW + 3600, 12.0, NOW + 86400), NOW - 5)
    [f] = usage(p.poll(NOW))
    assert f.src == opencode.SRC_ID
    assert f.session_pct == 34.0


def test_the_budget_is_summed_over_every_channels_database(dirs, tmp_path):
    data, _ = dirs
    fx.make_message_db(data, v1=[fx.v1_message(NOW_MS - H, tok=tok(1000))])
    fx.make_message_db(data, name="opencode-beta.db",
                       v1=[fx.v1_message(NOW_MS - H, tok=tok(3000))])
    p = provider(dirs, tmp_path, session=100_000)
    [f] = usage(p.poll(NOW))
    assert f.session_pct == pytest.approx(4.0)


# --- the command ---------------------------------------------------------------


def _oc_data(tmp_path):
    return str(tmp_path / ".local" / "share" / "opencode")


def _budget_file(tmp_path):
    return tmp_path / ".blink" / "opencode-budget.json"


def test_cli_sets_shows_and_clears(tmp_path, capsys):
    assert cli.main(["opencode-budget"]) == 0
    assert "No OpenCode token budget set" in capsys.readouterr().out

    assert cli.main(["opencode-budget", "5h=2M", "7d=20M"]) == 0
    out = capsys.readouterr().out
    assert "2M per 5 hours, 20M per 7 days" in out
    assert json.loads(_budget_file(tmp_path).read_text()) == {
        "session_tokens": 2_000_000, "weekly_tokens": 20_000_000}
    if sys.platform != "win32":
        assert _budget_file(tmp_path).stat().st_mode & 0o777 == 0o600

    # One window changes, the other is kept.
    assert cli.main(["opencode-budget", "5h=500K"]) == 0
    assert json.loads(_budget_file(tmp_path).read_text()) == {
        "session_tokens": 500_000, "weekly_tokens": 20_000_000}
    assert cli.main(["opencode-budget", "7d=off"]) == 0
    assert json.loads(_budget_file(tmp_path).read_text()) == {
        "session_tokens": 500_000}
    capsys.readouterr()

    assert cli.main(["opencode-budget"]) == 0
    assert "500K per 5 hours" in capsys.readouterr().out

    assert cli.main(["opencode-budget", "off"]) == 0
    assert "cleared" in capsys.readouterr().out
    assert not _budget_file(tmp_path).exists()


@pytest.mark.parametrize("args", [
    ["5h"], ["1h=2M"], ["5h=2"], ["5h=lots"], ["7d=-1M"], ["off", "5h=2M"],
])
def test_cli_refuses_a_bad_setting_and_writes_nothing(tmp_path, capsys,
                                                      args):
    assert cli.main(["opencode-budget"] + args) == 2
    err = capsys.readouterr().err
    assert err.startswith("blink: ")
    assert not _budget_file(tmp_path).exists()


def test_cli_shows_current_use_on_a_desk_with_opencode(tmp_path, capsys):
    fx.make_message_db(_oc_data(tmp_path),
                       v1=[fx.v1_message(int(__import__("time").time() * 1000)
                                         - 60_000, tok=tok(500_000))])
    assert cli.main(["opencode-budget", "5h=2M", "7d=20M"]) == 0
    out = capsys.readouterr().out
    assert "used 500K of 2M (25%) in the last 5 hours" in out
    assert "500K of 20M (2%) in the last 7 days" in out
    assert fx.SECRET_TEXT not in out


def test_cli_says_when_an_overrun_window_is_back_at_its_budget(tmp_path,
                                                                capsys):
    import time
    now = time.time()
    made = now - 3600
    fx.make_message_db(_oc_data(tmp_path),
                       v1=[fx.v1_message(int(made * 1000), tok=tok(2_640_000))])
    assert cli.main(["opencode-budget", "5h=2M", "7d=20M"]) == 0
    out = capsys.readouterr().out
    assert "(132%) in the last 5 hours" in out
    back = time.strftime("%H:%M", time.localtime(made + ob.SESSION_WINDOW_S))
    assert f"over the 5-hour budget -- back at it at {back}" in out \
        or f"over the 5-hour budget -- back at it on " in out
    assert "7-day" not in out


def test_the_clock_time_names_the_day_when_it_is_not_today():
    now = time_at(2026, 10, 20, 10, 0)
    assert cli._clock_at(time_at(2026, 10, 20, 14, 5), now) == "at 14:05"
    assert cli._clock_at(time_at(2026, 10, 22, 9, 30), now) == "on Thu 09:30"


def time_at(*ymdhm):
    import time
    return time.mktime(tuple(ymdhm) + (0, 0, 0, -1))


def test_cli_with_no_budget_names_the_record(tmp_path, capsys):
    import time
    now_ms = int(time.time() * 1000)
    fx.make_message_db(_oc_data(tmp_path), v1=[
        fx.v1_message(now_ms - 3 * D, tok=tok(400_000)),
        fx.v1_message(now_ms - H, tok=tok(100_000))])
    assert cli.main(["opencode-budget"]) == 0
    out = capsys.readouterr().out
    assert "No OpenCode token budget set." in out
    assert "against your busiest stretch in the last 30 days" in out
    assert "100K in the last 5 hours, against your record of 400K (25%)" in out
    assert "sets a fixed limit instead" in out
    assert fx.SECRET_TEXT not in out


def test_cli_with_no_history_says_the_record_comes_later(tmp_path, capsys):
    import time
    fx.make_message_db(_oc_data(tmp_path), v1=[
        fx.v1_message(int(time.time() * 1000) - H, tok=tok(100_000))])
    assert cli.main(["opencode-budget"]) == 0
    out = capsys.readouterr().out
    assert "once OpenCode has some history here" in out


def test_status_says_when_the_server_reports_no_usage(tmp_path, capsys,
                                                      monkeypatch):
    import time
    now_ms = int(time.time() * 1000)
    fx.make_message_db(_oc_data(tmp_path),
                       v1=[fx.v1_message(now_ms - 60_000, tok=fx.tokens())])
    fx.write_budget(str(tmp_path), session=2_000_000, weekly=20_000_000)
    lines, _q = cli._opencode_lines(now=time.time())
    text = "\n".join(lines)
    assert ("OpenCode recorded no token counts -- your model server may not"
            " return usage") in text
    assert "the rings show its token use against your budget" in text
    assert fx.SECRET_TEXT not in text


def test_status_offers_a_budget_only_to_a_keyless_desk(tmp_path, monkeypatch):
    import time
    fx.make_message_db(_oc_data(tmp_path))
    lines, _q = cli._opencode_lines(now=time.time())
    assert any("no token budget set" in ln for ln in lines)

    now = time.time()
    quota = opencode.Quota(34.0, now + 3600, 12.0, now + 86400)
    monkeypatch.setattr(cli.opencode, "fetch_once",
                        lambda *a, **k: (quota, None))
    fx.write_auth(_oc_data(tmp_path), fx.plan_key())
    lines, _q = cli._opencode_lines(now=now)
    assert any("GLM plan quota: 34%" in ln for ln in lines)
    assert not any("token budget" in ln for ln in lines)

    # With a budget set as well, it is shown, and which one wins is said.
    fx.write_budget(str(tmp_path), session=2_000_000)
    lines, _q = cli._opencode_lines(now=now)
    assert any("token budget 2M per 5 hours" in ln for ln in lines)
    assert any("the GLM plan quota is on the rings" in ln for ln in lines)


def test_status_wire_carries_the_budget_page(tmp_path, capsys, monkeypatch):
    import time
    now_ms = int(time.time() * 1000)
    fx.make_message_db(_oc_data(tmp_path),
                       v1=[fx.v1_message(now_ms - 60_000, tok=tok(500_000))])
    fx.write_budget(str(tmp_path), session=2_000_000, weekly=20_000_000)
    cli._wire_line()
    out = capsys.readouterr().out
    msg = json.loads(out.strip().splitlines()[-1])
    assert msg["provider"] == "opencode" and msg["src"] == "tokens"
    assert msg["session_pct"] == pytest.approx(25.0)
    assert msg["weekly_pct"] == pytest.approx(2.5)
