"""The one thing this provider remembers, and why it may.

The status line is rewritten only when Claude Code renders. When the
five-hour window expires and nobody is rendering, the payload stops carrying
a percentage, the frame stops being a candidate for the session dial, and the
panel falls to whatever else is on the bus -- in the field, a Claude Desktop
sample 57 hours old, whose age was then drawn as "56h ago" over a machine
that had used Claude Code six hours earlier.
"""
import json

from pc import statusline_source as ss
from pc.providers.claude_cli import ClaudeCliProvider

NOW = 1_787_700_000.0


def write(path, payload, mtime):
    path.write_text(json.dumps(payload), encoding="utf-8")
    import os
    os.utime(path, (mtime, mtime))
    return path


def payload(five_hour=27.0, resets_at=None, seven_day=12.0):
    limits = {"seven_day": {"used_percentage": seven_day}}
    if five_hour is not None:
        limits["five_hour"] = {"used_percentage": five_hour,
                               "resets_at": resets_at}
    return {"rate_limits": limits}


def test_a_live_reading_is_returned_alone(tmp_path):
    p = write(tmp_path / "statusline.json",
              payload(five_hour=27.0, resets_at=NOW + 900), NOW - 60)
    prov = ClaudeCliProvider(path=str(p))

    frames = prov.poll(NOW)

    assert len(frames) == 1
    assert frames[0].session_pct == 27.0


# The remembered payloads below carry NO five-hour reset stamp, which is what
# `payload()` writes by default and is the shape this memory is for. A stamp
# the clock has passed is proof the window ended, and a reading of a window
# that has ended is not a reading anyone should be offered -- see
# test_a_remembered_window_that_has_since_ended_is_not_re_offered below, and
# pc/providers/base.settle_window for the argument.


def test_the_last_reading_with_a_percentage_is_offered_when_the_file_loses_one(tmp_path):
    """The field case: the file was rewritten without the five-hour window,
    so the only session figure left in the world is the one we read five
    hours ago, and nothing says the window it describes has rolled.

    Five hours, not the six this test used to use: an unstamped figure
    fetched more than five hours and ten minutes ago describes a window that
    has certainly ended, and is no longer lent at all
    (test_an_unstamped_window_is_lent_no_longer_than_a_window_lasts)."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0), NOW - 5 * 3600)
    prov.poll(NOW - 5 * 3600 + 1)

    write(p, {"rate_limits": {"seven_day": {"used_percentage": 12.0}}}, NOW - 60)
    frames = prov.poll(NOW)

    remembered = [f for f in frames if f.session_pct >= 0]
    assert len(remembered) == 1
    assert remembered[0].session_pct == 27.0
    assert remembered[0].observed_at == NOW - 5 * 3600
    assert remembered[0].src == "cli"


def test_a_remembered_window_that_has_since_ended_is_not_re_offered(tmp_path):
    """The same memory, with the one fact that disqualifies it.

    The payload's five-hour window rolled two hours ago, and re-offering its
    27% would put usage that has already been forgiven on the dial -- the
    failure pc/normalizer's docstring says the whole design exists to
    prevent. What the memory still carries is the evidence: the epoch that
    window emptied, which is what stops a Claude Desktop sample taken before
    the same reset from inheriting the dial (pc/normalizer._survives_rollover).
    """
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW - 7200), NOW - 6 * 3600)
    prov.poll(NOW - 6 * 3600 + 1)

    write(p, {"rate_limits": {"seven_day": {"used_percentage": 12.0}}}, NOW - 60)
    frames = prov.poll(NOW)

    assert [f.session_pct for f in frames if f.session_pct >= 0] == []
    assert any(f.session_rolled_at == NOW - 7200 for f in frames)


def test_the_remembered_reading_is_marked_stale_by_its_own_age(tmp_path):
    """Not frozen at the staleness it had when captured. A five-hour-old
    number under a green dot is the confident-wrong-number failure
    pc/normalizer's docstring exists to prevent."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0), NOW - 5 * 3600)
    prov.poll(NOW - 5 * 3600 + 1)
    assert prov.poll(NOW - 5 * 3600 + 1)[0].stale is False

    write(p, {"rate_limits": {}}, NOW - 60)
    remembered = [f for f in prov.poll(NOW) if f.session_pct >= 0][0]
    assert remembered.stale is True


def test_the_remembered_reading_ages(tmp_path):
    """The whole point: the age must answer "when did you last use Claude
    Code", so it grows with the wall clock instead of resetting each poll."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0), NOW - 3600)
    prov.poll(NOW - 3600 + 1)
    write(p, {"rate_limits": {}}, NOW - 60)

    a = [f for f in prov.poll(NOW) if f.session_pct >= 0][0]
    b = [f for f in prov.poll(NOW + 60) if f.session_pct >= 0][0]
    assert a.observed_at == b.observed_at == NOW - 3600


def test_a_newer_reading_with_a_percentage_replaces_the_remembered_one(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW + 900), NOW - 3600)
    prov.poll(NOW - 3599)
    write(p, payload(five_hour=61.0, resets_at=NOW + 900), NOW - 60)
    prov.poll(NOW)

    write(p, {"rate_limits": {}}, NOW - 30)
    remembered = [f for f in prov.poll(NOW) if f.session_pct >= 0][0]
    assert remembered.session_pct == 61.0


def test_a_vanished_file_still_leaves_the_memory(tmp_path):
    """read_payload returns nothing for an absent or malformed file, and that
    is not evidence the last reading never happened."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))

    write(p, payload(five_hour=27.0, resets_at=NOW + 900), NOW - 3600)
    prov.poll(NOW - 3599)
    p.unlink()

    frames = prov.poll(NOW)
    assert len(frames) == 1
    assert frames[0].session_pct == 27.0


def test_nothing_is_invented_before_the_first_good_reading(tmp_path):
    p = write(tmp_path / "statusline.json", {"rate_limits": {}}, NOW - 60)
    prov = ClaudeCliProvider(path=str(p))

    frames = prov.poll(NOW)
    assert len(frames) == 1  # the all() below is vacuously true over an empty list
    assert all(f.session_pct < 0 for f in frames)


def test_the_memory_does_not_survive_a_new_daemon(tmp_path):
    """Deliberately in-memory: a fresh process starts with no history. Pinned
    so the decision is visible rather than accidental."""
    p = tmp_path / "statusline.json"
    write(p, payload(five_hour=27.0, resets_at=NOW + 900), NOW - 3600)
    ClaudeCliProvider(path=str(p)).poll(NOW - 3599)

    write(p, {"rate_limits": {}}, NOW - 60)
    frames = ClaudeCliProvider(path=str(p)).poll(NOW)
    assert len(frames) == 1  # the all() below is vacuously true over an empty list
    assert all(f.session_pct < 0 for f in frames)


# --- context per session, for the sessions card ------------------------------

def ctx_render(sid, pct):
    body = payload(five_hour=27.0, resets_at=NOW + 900)
    body["session_id"] = sid
    body["context_window"] = {"used_percentage": pct}
    return body


def test_two_sessions_rendering_in_turn_each_keep_their_own_context(tmp_path):
    """The file is shared: one terminal's render overwrites the other's. Each
    figure is keyed by the id in the payload that carried it, so a row never
    shows another session's number."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    for k in range(4):
        sid, pct = (("sess-a", 30), ("sess-b", 70))[k % 2]
        write(p, ctx_render(sid, pct + k), NOW - 60 + k)
        prov.poll(NOW + k)
    assert prov.context_by_session() == {"sess-a": 32, "sess-b": 73}


def test_each_row_shows_its_own_context_when_two_sessions_differ_between_polls(
        tmp_path):
    """Reviewer point: two sessions report different percentages between
    daemon polls, and each ROW (not just the provider's map) carries its own
    value, a session never rendered carries none."""
    from pc import session_rows
    from pc.providers import base
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("sess-a", 20), NOW - 60)
    prov.poll(NOW)
    write(p, ctx_render("sess-b", 85), NOW - 50)
    prov.poll(NOW + 1)
    write(p, ctx_render("sess-a", 64), NOW - 40)
    prov.poll(NOW + 2)
    det = {sid: (base.STATE_RUNNING, name, 5, "", "UserPromptSubmit")
           for sid, name in (("sess-a", "A"), ("sess-b", "B"),
                             ("sess-c", "C"))}
    rows = session_rows.build_rows(det, {}, prov.context_by_session())
    assert {r.name: r.ctx for r in rows} == {"A": 64, "B": 85, "C": None}


def test_context_is_rounded_to_a_whole_percent(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 41.6), NOW - 60)
    prov.poll(NOW)
    assert prov.context_by_session() == {"s": 42}


def test_a_bad_context_value_is_not_stored_and_the_last_good_one_stays(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 55), NOW - 60)
    prov.poll(NOW)
    for k, bad in enumerate((True, float("nan"), 150, -3, "40", None,
                             [50])):
        write(p, ctx_render("s", bad), NOW - 50 + k)
        prov.poll(NOW + 1 + k)
        assert prov.context_by_session() == {"s": 55}, bad
    # And a session whose only render was bad is absent, never 0.
    write(p, ctx_render("t", float("nan")), NOW - 30)
    prov.poll(NOW + 20)
    assert "t" not in prov.context_by_session()


def test_the_context_memory_is_bounded_oldest_first(tmp_path):
    from pc.providers import claude_cli
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    n = claude_cli.REPLY_MEMORY_LIMIT + 3
    for k in range(n):
        write(p, ctx_render(f"s{k}", 10), NOW - 600 + k)
        prov.poll(NOW + k)
    held = prov.context_by_session()
    assert len(held) == claude_cli.REPLY_MEMORY_LIMIT
    assert "s0" not in held and "s2" not in held
    assert f"s{n - 1}" in held


# --- the context sampler, between polls (ClaudeCliProvider.sample_context) ----
#
# The file is one file for every terminal. These are the review's case: two
# sessions each write it BETWEEN two polls, B's render replacing A's, with only
# the sampler (one per serial-loop pass in the daemon) running in between.

def test_two_sessions_reporting_between_polls_each_keep_their_own_context(
        tmp_path):
    from pc import session_rows
    from pc.providers import base
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("sess-a", 10), NOW - 120)
    prov.poll(NOW)
    assert prov.context_by_session() == {"sess-a": 10}
    # Between this poll and the next: A renders, a pass samples it, then B's
    # render replaces A's in the same file before the poll comes round.
    write(p, ctx_render("sess-a", 31), NOW - 60)
    assert prov.sample_context() is True
    write(p, ctx_render("sess-b", 64), NOW - 59)
    assert prov.sample_context() is True
    prov.poll(NOW + 2)
    assert prov.context_by_session() == {"sess-a": 31, "sess-b": 64}
    det = {sid: (base.STATE_RUNNING, name, 5, "", "UserPromptSubmit")
           for sid, name in (("sess-a", "A"), ("sess-b", "B"))}
    rows = session_rows.build_rows(det, {}, prov.context_by_session())
    assert {r.name: r.ctx for r in rows} == {"A": 31, "B": 64}


def test_without_a_sample_between_the_two_renders_only_the_last_is_known(
        tmp_path):
    """The limit, pinned: two renders inside one sampling interval are the
    old failure shape, and the first is lost until that session's next
    render. What a row never gets is the other session's figure."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("sess-a", 10), NOW - 120)
    prov.poll(NOW)
    write(p, ctx_render("sess-a", 31), NOW - 60)
    write(p, ctx_render("sess-b", 64), NOW - 59)
    prov.sample_context()
    prov.poll(NOW + 2)
    assert prov.context_by_session() == {"sess-a": 10, "sess-b": 64}


def test_the_sampler_does_not_re_read_an_unchanged_file(tmp_path, monkeypatch):
    from pc.providers import claude_cli
    reads = []
    real = claude_cli.ss.read_payload

    def counting(path):
        reads.append(path)
        return real(path)

    monkeypatch.setattr(claude_cli.ss, "read_payload", counting)
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 40), NOW - 60)
    assert prov.sample_context() is True
    for _ in range(5):
        assert prov.sample_context() is False
    assert len(reads) == 1
    # A new render (by rename, as the shim writes) is read once more.
    tmp = tmp_path / "statusline.json.tmp"
    write(tmp, ctx_render("s", 41), NOW - 50)
    tmp.replace(p)
    assert prov.sample_context() is True
    assert prov.sample_context() is False
    assert len(reads) == 2
    assert prov.context_by_session() == {"s": 41}


def test_the_same_figure_sampled_again_is_no_change(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 40), NOW - 60)
    assert prov.sample_context() is True
    write(p, ctx_render("s", 40), NOW - 50)
    assert prov.sample_context() is False
    assert prov.context_by_session() == {"s": 40}


def test_a_missing_or_unreadable_file_or_bad_value_samples_nothing(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    assert prov.sample_context() is False          # no file at all
    p.write_text("{not json", encoding="utf-8")
    assert prov.sample_context() is False          # half-written / garbage
    p.write_text("[1, 2]", encoding="utf-8")
    assert prov.sample_context() is False          # not an object
    for k, bad in enumerate((True, float("nan"), 150, -3, "40", None, [50])):
        write(p, ctx_render("s", bad), NOW - 50 + k)
        assert prov.sample_context() is False, bad
    body = ctx_render("s", 20)
    del body["session_id"]
    write(p, body, NOW - 10)
    assert prov.sample_context() is False
    assert prov.context_by_session() == {}
    # A directory where the file should be cannot be read: still nothing.
    p.unlink()
    p.mkdir()
    assert prov.sample_context() is False
    assert prov.context_by_session() == {}


def test_a_path_that_cannot_be_resolved_does_not_raise(tmp_path):
    def broken_home():
        raise RuntimeError("no home")

    prov = ClaudeCliProvider(home=broken_home)
    assert prov.sample_context() is False
    assert prov.context_by_session() == {}


def test_the_sampler_keeps_the_bound(tmp_path):
    from pc.providers import claude_cli
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    n = claude_cli.REPLY_MEMORY_LIMIT + 3
    for k in range(n):
        write(p, ctx_render(f"s{k}", 10), NOW - 600 + k)
        prov.sample_context()
    held = prov.context_by_session()
    assert len(held) == claude_cli.REPLY_MEMORY_LIMIT
    assert "s0" not in held and "s2" not in held
    assert f"s{n - 1}" in held


def test_the_sampler_changes_no_usage_figure(tmp_path):
    """Context only: a sample between polls leaves the frames, the memories
    and the spend inputs to the poll that reads the file itself."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 40), NOW - 60)
    prov.sample_context()
    assert prov._remembered is None and prov._any is None
    assert prov._read == (None, None) and prov._render_at is None
    assert prov.poll(NOW)[0].session_pct == 27.0


def test_the_sampler_refuses_the_old_logins_render_as_the_poll_does(tmp_path):
    """While a /login switch marks the file (_note_login), the render in it
    is the old login's: the poll does not take it, nor may the sampler. The
    first render after it is taken, and the mark is the poll's to clear."""
    from pc.providers import claude_cli
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 40), NOW - 60)
    prov._old_render = claude_cli._file_sig(str(p))
    assert prov.sample_context() is False
    assert prov.context_by_session() == {}
    prov._old_render = claude_cli._UNSTATED
    assert prov.sample_context() is False
    assert prov.context_by_session() == {}
    prov._old_render = claude_cli._file_sig(str(p))
    tmp = tmp_path / "statusline.json.tmp"
    write(tmp, ctx_render("s", 45), NOW - 50)
    tmp.replace(p)
    assert prov.sample_context() is True
    assert prov.context_by_session() == {"s": 45}
    assert prov._old_render is not None


# --- the sampler's edges: races, identity, path, containment -------------------

def _counting_reads(monkeypatch, after=None, before=None):
    from pc.providers import claude_cli
    reads = []
    real = claude_cli.ss.read_payload

    def wrapper(path):
        reads.append(path)
        if before:
            before()
        out = real(path)
        if after:
            after()
        return out

    monkeypatch.setattr(claude_cli.ss, "read_payload", wrapper)
    return reads


def _render_by_rename(tmp_path, p, body, mtime):
    tmp = tmp_path / "statusline.json.tmp"
    write(tmp, body, mtime)
    tmp.replace(p)


def test_a_file_replaced_between_the_stat_and_the_read_is_still_picked_up(
        tmp_path, monkeypatch):
    """The signature kept is the one statted BEFORE the read, so a render
    that lands in between leaves a stale signature: the next pass reads again
    (the same figure, no change) and then settles. It can cost a read; it can
    never lose the newer render."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 10), NOW - 60)
    first = [True]

    def swap():
        if first[0]:
            first[0] = False
            _render_by_rename(tmp_path, p, ctx_render("s", 20), NOW - 50)

    reads = _counting_reads(monkeypatch, before=swap)
    assert prov.sample_context() is True           # read the NEW render
    assert prov.context_by_session() == {"s": 20}
    assert prov.sample_context() is False          # stale sig: one more read
    assert len(reads) == 2
    assert prov.sample_context() is False          # and now settled
    assert len(reads) == 2


def test_a_render_landing_just_after_the_read_is_read_on_the_next_pass(
        tmp_path, monkeypatch):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 10), NOW - 60)
    first = [True]

    def swap():
        if first[0]:
            first[0] = False
            _render_by_rename(tmp_path, p, ctx_render("s", 20), NOW - 50)

    _counting_reads(monkeypatch, after=swap)
    assert prov.sample_context() is True
    assert prov.context_by_session() == {"s": 10}
    assert prov.sample_context() is True
    assert prov.context_by_session() == {"s": 20}


def test_a_rewrite_with_the_same_mtime_and_size_on_another_inode_is_read(
        tmp_path):
    import os
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 11), NOW - 60)
    assert prov.sample_context() is True
    keep = tmp_path / "keep-the-old-inode-alive"
    os.link(p, keep)                    # so the new file cannot reuse its inode
    st = os.stat(p)
    tmp = tmp_path / "statusline.json.tmp"
    write(tmp, ctx_render("s", 22), NOW - 60)   # same length, "11" -> "22"
    os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))
    tmp.replace(p)
    new = os.stat(p)
    assert (new.st_size, new.st_mtime_ns) == (st.st_size, st.st_mtime_ns)
    assert new.st_ino != st.st_ino
    assert prov.sample_context() is True
    assert prov.context_by_session() == {"s": 22}


def test_a_poll_and_a_sample_do_not_hide_a_render_from_each_other(tmp_path):
    """The two readers keep separate memory of what they have seen. A sample
    that took the render leaves the poll to read it itself, to the byte the
    frames it would have made alone; a poll that took it leaves the sampler
    to read it too (the same figure, so no change)."""
    import dataclasses
    pa = tmp_path / "a" / "statusline.json"
    pb = tmp_path / "b" / "statusline.json"
    for q in (pa, pb):
        q.parent.mkdir()
        write(q, ctx_render("s", 40), NOW - 60)
    alone = ClaudeCliProvider(path=str(pa))
    sampled = ClaudeCliProvider(path=str(pb))
    assert sampled.sample_context() is True
    fa, fb = alone.poll(NOW), sampled.poll(NOW)
    assert ([dataclasses.asdict(f) for f in fa]
            == [dataclasses.asdict(f) for f in fb])
    assert alone._render_at == sampled._render_at
    assert alone._read == sampled._read
    assert alone._costs == sampled._costs
    assert alone.sample_context() is False
    assert alone.context_by_session() == {"s": 40}


def test_the_poll_still_clears_the_old_login_mark_after_a_sample(tmp_path):
    from pc.providers import claude_cli
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 40), NOW - 60)
    prov.poll(NOW)
    prov._old_render = claude_cli._file_sig(str(p))
    assert prov.sample_context() is False
    _render_by_rename(tmp_path, p, ctx_render("s", 45), NOW - 50)
    assert prov.sample_context() is True
    prov.poll(NOW + 2)
    assert prov._old_render is None            # the poll's to clear, still
    assert prov._read[0] is not None


def test_a_moved_out_folder_is_followed_and_its_first_file_is_read(tmp_path):
    import os
    from pc import claude_plan
    one = tmp_path / "one" / "statusline.json"
    two = tmp_path / "two" / "statusline.json"
    one.parent.mkdir()
    two.parent.mkdir()
    prov = ClaudeCliProvider(path=str(one))
    write(one, ctx_render("s", 10), NOW - 60)
    prov.poll(NOW)
    assert prov.context_by_session() == {"s": 10}
    # The new folder's file shares the old one's mtime and size; the inode is
    # forced to collide below, so only the path change can tell the sampler
    # it is news.
    write(two, ctx_render("s", 77), NOW - 60)
    st = os.stat(one)
    os.utime(two, ns=(st.st_atime_ns, st.st_mtime_ns))
    prov.sample_context()
    prov._ctx_sig = claude_plan.signature(os.stat(two))   # the collision
    prov._path = str(two)                                 # `out` moved
    prov.poll(NOW + 2)
    assert prov.context_by_session() == {"s": 77}
    write(two, ctx_render("t", 5), NOW - 10)
    assert prov.sample_context() is True
    assert prov.context_by_session() == {"s": 77, "t": 5}


def test_a_sampler_that_fails_in_any_way_says_nothing_and_leaks_nothing(
        tmp_path, monkeypatch, capsys):
    import os
    from pc.providers import claude_cli
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 40), NOW - 60)
    before = sorted(os.listdir(tmp_path))

    def boom(*a, **k):
        raise RuntimeError("secret-figure-73")

    for target, name in ((claude_cli.ss, "read_payload"),
                         (claude_cli, "_file_sig")):
        with monkeypatch.context() as m:
            m.setattr(target, name, boom)
            assert prov.sample_context() is False
    for exc in (OSError("x"), ValueError("x"), OverflowError("x"),
                RecursionError("x"), MemoryError()):
        def raiser(*a, _e=exc, **k):
            raise _e
        with monkeypatch.context() as m:
            m.setattr(claude_cli.ss, "read_payload", raiser)
            assert prov.sample_context() is False
        prov._ctx_sig = None
    cap = capsys.readouterr()
    assert cap.out == "" and cap.err == ""
    assert sorted(os.listdir(tmp_path)) == before       # nothing saved
    assert prov.context_by_session() == {}


def test_a_quiet_pass_is_one_stat_and_no_read(tmp_path, monkeypatch):
    import os
    from pc.providers import claude_cli
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", 40), NOW - 60)
    prov.sample_context()
    stats, opens = [], []
    real_stat, real_open = os.stat, open

    def stat(path, *a, **k):
        if str(path) == str(p):
            stats.append(path)
        return real_stat(path, *a, **k)

    def opened(path, *a, **k):
        if str(path) == str(p):
            opens.append(path)
        return real_open(path, *a, **k)

    monkeypatch.setattr(claude_cli.os, "stat", stat)
    monkeypatch.setattr("builtins.open", opened)
    for _ in range(10):
        assert prov.sample_context() is False
    assert len(stats) == 10 and opens == []


def test_an_unknown_context_is_never_a_zero(tmp_path):
    from pc import session_rows
    from pc.providers import base
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, ctx_render("s", None), NOW - 60)
    assert prov.sample_context() is False
    assert prov.context_by_session() == {}
    det = {"s": (base.STATE_RUNNING, "S", 5, "", "UserPromptSubmit")}
    rows = session_rows.build_rows(det, {}, prov.context_by_session())
    assert [r.ctx for r in rows] == [None]
