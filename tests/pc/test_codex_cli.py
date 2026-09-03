"""What the Codex rollout reader must get right about a file it does not own.

The shape here was taken from a real rollout file (2026-08-27), and the tests
that matter are not the happy path -- they are the ways this source can be
confidently wrong rather than silent: the two windows swapped, a millisecond
epoch read as seconds, a percentage that has stopped meaning a percentage.
"""
import json
import os

import pytest

from pc.providers import base
from pc.providers import codex_cli


NOW = 1_787_800_000.0


def rate_limits(s_pct=12.0, w_pct=34.0, s_reset=NOW + 3600,
                w_reset=NOW + 86400, s_min=300, w_min=10080):
    """A `rate_limits` object shaped like the one Codex actually writes."""
    primary = {"used_percent": s_pct, "resets_at": s_reset}
    secondary = {"used_percent": w_pct, "resets_at": w_reset}
    if s_min is not None:
        primary["window_minutes"] = s_min
    if w_min is not None:
        secondary["window_minutes"] = w_min
    return {"limit_id": "codex", "primary": primary, "secondary": secondary}


def token_count_line(limits, stamp="2026-08-27T03:00:00.000Z"):
    return json.dumps({
        "timestamp": stamp,
        "type": "event_msg",
        "payload": {"type": "token_count",
                    "info": {"model_context_window": 258400},
                    "rate_limits": limits},
    })


def write_rollout(root, day="2026/08/27", name="rollout-a.jsonl", lines=()):
    path = os.path.join(root, *day.split("/"), name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        for line in lines:
            f.write(line + "\n")
    return path


def poll(root, now=NOW):
    return codex_cli.CodexCliProvider(root=str(root)).poll(now)


# --- the happy path, once ---------------------------------------------------


def test_a_real_shaped_rollout_yields_both_windows(tmp_path):
    write_rollout(tmp_path, lines=[token_count_line(rate_limits())])
    frame, = poll(tmp_path)
    assert frame.provider == "codex"
    assert frame.src == "cli"
    assert frame.session_pct == 12.0
    assert frame.weekly_pct == 34.0
    assert frame.session_resets_at == int(NOW + 3600)
    assert frame.weekly_resets_at == int(NOW + 86400)
    assert frame.stale is False


# --- which window is which --------------------------------------------------


def test_windows_are_matched_by_declared_length_not_by_position(tmp_path):
    """The two entries arriving the other way round must not swap the dials.

    `primary` and `secondary` are positions in a file we do not control. The
    five-hour figure belongs on the session dial because it says it covers
    300 minutes, not because it came first.
    """
    limits = rate_limits()
    limits["primary"], limits["secondary"] = (limits["secondary"],
                                              limits["primary"])
    write_rollout(tmp_path, lines=[token_count_line(limits)])
    frame, = poll(tmp_path)
    assert frame.session_pct == 12.0     # still the 300-minute one
    assert frame.weekly_pct == 34.0


def test_position_is_the_fallback_when_no_length_is_declared(tmp_path):
    limits = rate_limits(s_min=None, w_min=None)
    write_rollout(tmp_path, lines=[token_count_line(limits)])
    frame, = poll(tmp_path)
    assert frame.session_pct == 12.0
    assert frame.weekly_pct == 34.0


# --- refusing to be confidently wrong ---------------------------------------


def test_a_millisecond_epoch_is_refused_rather_than_believed(tmp_path):
    """The unit changing must cost the reset time, not produce the year 58000.

    Claude Desktop's sample timestamps really are milliseconds, so this is
    not a hypothetical difference between two files in this codebase.
    """
    limits = rate_limits(s_reset=int((NOW + 3600) * 1000))
    write_rollout(tmp_path, lines=[token_count_line(limits)])
    frame, = poll(tmp_path)
    assert frame.session_pct == 12.0        # the percentage is still good
    assert frame.session_resets_at is None  # the timestamp is not


def test_a_percentage_far_outside_the_range_is_unknown_not_clamped(tmp_path):
    """The bound is 1000 now; 140 is a plausible overage, 4000 is not."""
    write_rollout(tmp_path, lines=[token_count_line(rate_limits(s_pct=4000.0))])
    frame, = poll(tmp_path)
    assert frame.session_pct == base.UNKNOWN
    assert frame.weekly_pct == 34.0


def test_an_overage_percentage_survives(tmp_path):
    """A Codex user in extra usage must not get an empty ring either."""
    write_rollout(tmp_path, lines=[token_count_line(rate_limits(s_pct=102.0))])
    frame, = poll(tmp_path)
    assert frame.session_pct == 102.0


def test_a_non_numeric_percentage_is_unknown(tmp_path):
    write_rollout(tmp_path, lines=[token_count_line(rate_limits(s_pct="12%"))])
    frame, = poll(tmp_path)
    assert frame.session_pct == base.UNKNOWN


def test_a_frame_with_neither_window_is_not_produced(tmp_path):
    """No numbers means no frame, so it cannot win a recency contest."""
    limits = rate_limits(s_pct=None, w_pct=None)
    write_rollout(tmp_path, lines=[token_count_line(limits)])
    assert poll(tmp_path) == []


# --- freshness --------------------------------------------------------------


def test_observed_at_is_the_events_own_timestamp(tmp_path):
    """Not the file's mtime, which moves when nothing was read or written."""
    write_rollout(tmp_path, lines=[
        token_count_line(rate_limits(), stamp="2026-08-27T03:00:00.000Z")])
    frame, = poll(tmp_path)
    assert frame.observed_at == pytest.approx(1_787_799_600.0)


def test_mtime_is_the_fallback_when_the_timestamp_is_unusable(tmp_path):
    line = json.loads(token_count_line(rate_limits()))
    line["timestamp"] = "not a date"
    path = write_rollout(tmp_path, lines=[json.dumps(line)])
    os.utime(path, (NOW - 10, NOW - 10))
    frame, = poll(tmp_path)
    assert frame.observed_at == pytest.approx(NOW - 10)


def test_an_old_reading_is_marked_stale_rather_than_dropped(tmp_path):
    """Codex only writes this while it runs, so age measures when you last
    used it -- worth showing as stale, not worth throwing away."""
    old = "2026-08-26T03:00:00.000Z"
    write_rollout(tmp_path, lines=[token_count_line(rate_limits(), stamp=old)])
    frame, = poll(tmp_path)
    assert frame.stale is True
    assert frame.session_pct == 12.0


# --- which file, out of several ---------------------------------------------


def test_the_freshest_reading_wins_even_from_an_older_file(tmp_path):
    """A terminal left open writes a rollout with no reading in it at all.

    Its mtime still moves, so "newest file" is the wrong question; the newest
    EVENT is the right one.
    """
    quiet = write_rollout(tmp_path, name="rollout-quiet.jsonl", lines=[
        json.dumps({"type": "session_meta", "payload": {}})])
    used = write_rollout(tmp_path, name="rollout-used.jsonl", lines=[
        token_count_line(rate_limits(s_pct=77.0),
                         stamp="2026-08-27T03:00:00.000Z")])
    os.utime(quiet, (NOW, NOW))             # the quiet one is newer on disk
    os.utime(used, (NOW - 600, NOW - 600))
    frame, = poll(tmp_path)
    assert frame.session_pct == 77.0


def test_only_one_frame_is_produced_however_many_terminals_are_open(tmp_path):
    """The percentages are account-wide: six copies is six ways to say one
    thing, and the normalizer has no more information than we do here."""
    for i in range(4):
        write_rollout(tmp_path, name=f"rollout-{i}.jsonl",
                      lines=[token_count_line(rate_limits())])
    assert len(poll(tmp_path)) == 1


def test_the_last_reading_in_a_file_is_the_one_used(tmp_path):
    write_rollout(tmp_path, lines=[
        token_count_line(rate_limits(s_pct=5.0)),
        json.dumps({"type": "response_item", "payload": {"type": "message"}}),
        token_count_line(rate_limits(s_pct=9.0)),
    ])
    frame, = poll(tmp_path)
    assert frame.session_pct == 9.0


# --- absence and damage are ordinary ----------------------------------------


def test_no_codex_installed_is_silence_not_an_error(tmp_path):
    assert poll(tmp_path / "nothing-here") == []


def test_a_rollout_with_no_reading_yet_is_silence(tmp_path):
    write_rollout(tmp_path, lines=[
        json.dumps({"type": "session_meta", "payload": {"id": "x"}})])
    assert poll(tmp_path) == []


def test_a_truncated_line_is_skipped_rather_than_fatal(tmp_path):
    """Only the tail of a long rollout is read, so the first line in hand is
    routinely half a line. That must cost nothing.

    Order matters, and it used to be wrong: the parser scans REVERSED and
    returns on the first match, so a malformed line placed before the good one
    was never reached and this passed without exercising anything. The
    malformed line has to be NEWER than the reading it must not break.
    """
    good = token_count_line(rate_limits(s_pct=42.0))
    write_rollout(tmp_path, lines=[good, '{"timestamp": "20'])
    frame, = poll(tmp_path)
    assert frame.session_pct == 42.0


def test_a_line_that_is_not_an_object_is_skipped(tmp_path):
    # Newest-first again: a non-object AFTER the reading is the one the
    # reversed scan actually has to survive.
    write_rollout(tmp_path, lines=[token_count_line(rate_limits()),
                                   '["rate_limits"]'])
    frame, = poll(tmp_path)
    assert frame.session_pct == 12.0


def test_only_the_tail_of_a_huge_file_is_read(tmp_path):
    """A megabyte of conversation must not become a megabyte of parsing."""
    filler = json.dumps({"type": "response_item",
                         "payload": {"type": "message", "pad": "x" * 400}})
    lines = [token_count_line(rate_limits(s_pct=1.0))]
    lines += [filler] * 2000
    lines += [token_count_line(rate_limits(s_pct=99.0))]
    write_rollout(tmp_path, lines=lines)
    frame, = poll(tmp_path)
    assert frame.session_pct == 99.0

    # ...and the seek is what makes that true. The old reading sits in the
    # first 1 KB of a ~800 KB file, well outside TAIL_BYTES: if the whole file
    # were read, the assertion above would still pass (99 is newest either
    # way), so it proved nothing about the tail. This does -- it is the
    # reading the seek must have skipped past entirely.
    assert os.path.getsize(next(iter(
        (tmp_path).rglob("rollout-*.jsonl")))) > codex_cli.TAIL_BYTES
    tail = codex_cli._tail_lines(str(next(iter(
        (tmp_path).rglob("rollout-*.jsonl")))))
    assert not any('"s_pct": 1.0' in ln or '"used_percent": 1.0' in ln
                   for ln in tail), "the first reading must be outside the tail"


# --- the head of the file: where the project name lives ----------------------


def test_the_first_line_is_read_whatever_follows_it(tmp_path):
    """The name is on line 1 and the tail read cannot reach it: the biggest
    rollout on the machine this was written against is 51 MB. So the head
    gets its own read, and it must not care how much comes after."""
    path = str(tmp_path / "rollout-a.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"cwd":"/a/b"}}\n')
        f.write("x" * (codex_cli.HEAD_BYTES * 3) + "\n")
    assert codex_cli._head_line(path) == \
        '{"type":"session_meta","payload":{"cwd":"/a/b"}}'


def test_a_first_line_longer_than_the_head_bound_is_refused_not_halved(tmp_path):
    """A JSON object cut in half parses as nothing anyway, and returning the
    fragment would only move the failure into json.loads. The cost of a first
    line that does not fit is the name, not the read."""
    path = str(tmp_path / "rollout-b.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"pad":"'
                + "p" * (codex_cli.HEAD_BYTES + 10) + '"}}\n')
    assert codex_cli._head_line(path) == ""


def test_the_head_bound_clears_a_real_session_meta_line():
    """18-19 KB is what the four real rollouts on this machine measure, and
    that length is upstream's to change -- base_instructions is embedded in
    the record. The bound has to have room over the observation, not equal
    it."""
    assert codex_cli.HEAD_BYTES >= 4 * 19 * 1024


def test_a_missing_file_is_silence_not_an_error(tmp_path):
    assert codex_cli._head_line(str(tmp_path / "nope.jsonl")) == ""


def test_a_file_with_no_newline_at_all_is_refused(tmp_path):
    """A rollout being written right now can have a partial first line. It
    is not a name yet, and it will be on the next poll."""
    path = str(tmp_path / "rollout-c.jsonl")
    with open(path, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"cwd":"/a')
    assert codex_cli._head_line(path) == ""


def test_the_provider_never_raises_on_a_damaged_tree(tmp_path, monkeypatch):
    """Contract from base.ProviderParser: a parser for an app we do not
    control must not be able to stop the daemon."""
    def boom(*a, **k):
        raise OSError("gone")
    write_rollout(tmp_path, lines=[token_count_line(rate_limits())])
    monkeypatch.setattr(codex_cli.os.path, "getsize", boom)
    assert poll(tmp_path) == []


# --- the seam into the rest of the daemon -----------------------------------


def test_codex_ships_in_the_default_provider_list():
    """Without this the board's own 'codex' preference can never be honoured:
    set_preferred refuses a provider that is not reporting."""
    from pc import ingest
    ids = [p.get_provider_id() for p in ingest.default_providers()]
    assert "codex" in ids


def test_codex_home_is_honoured(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert codex_cli.sessions_root() == os.path.join(str(tmp_path), "sessions")


def test_a_rate_limits_object_of_the_wrong_shape_is_skipped_not_raised(tmp_path):
    """Verified raising AttributeError before: a string under `primary` fell
    through _classify's positional fallback to `.get("resets_at")`, and the
    bus then skipped Codex for the rest of the process."""
    for bad in ({"primary": "x"}, {"primary": [], "secondary": 3},
                {"primary": None}, "not even an object"):
        write_rollout(tmp_path, lines=[token_count_line(bad)])
        assert poll(tmp_path) == [], bad


def test_a_timestamp_from_a_wrong_clock_does_not_read_as_fresh(tmp_path):
    """A far-future stamp would otherwise be fresh forever and win every
    recency contest; a pre-1970 one makes .timestamp() raise OSError on
    Windows. Both fall back to the file's mtime."""
    line = token_count_line(rate_limits())
    for stamp in ("2150-01-01T00:00:00Z", "1900-01-01T00:00:00Z", "junk"):
        obj = json.loads(line)
        obj["timestamp"] = stamp
        write_rollout(tmp_path, lines=[json.dumps(obj)])
        frames = poll(tmp_path)
        assert len(frames) == 1, stamp
        # The file's mtime (now), not the year 2099 and not 1900.
        assert codex_cli.RESET_EPOCH_MIN < frames[0].observed_at < 4.0e9, stamp


# --- a real file --------------------------------------------------------------


FIXTURE = os.path.join(os.path.dirname(os.path.dirname(__file__)), "fixtures",
                       "codex_rollout_tail.jsonl")


def _fixture_root(tmp_path):
    """The fixture laid out the way Codex lays its logs out."""
    d = tmp_path / "sessions" / "2026" / "08" / "28"
    d.mkdir(parents=True)
    with open(FIXTURE) as src, open(d / "rollout-2026-08-28T01-57-10-x.jsonl", "w") as dst:
        for line in src:
            if not line.startswith('{"_comment"'):
                dst.write(line)
    return str(tmp_path / "sessions")


def test_the_real_rollout_tail_parses():
    """tests/fixtures/codex_rollout_tail.jsonl is the tail of a real Codex CLI
    log (2026-08-28), content redacted and every token_count event verbatim.
    The synthesized lines above pin the parser to what we THINK the format
    is; this pins it to what the format was."""
    import datetime
    root = _fixture_root(__import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    seen_at = datetime.datetime(2026, 8, 28, 0, 3, 51, 383000,
                                tzinfo=datetime.timezone.utc).timestamp()
    frames = codex_cli.CodexCliProvider(root=root).poll(seen_at + 60)
    # Two frames: the reading, and the execution state the same file implies.
    assert [f.src for f in frames] == ["cli", "cli-state"]
    st = frames[1]
    # The tail ends task_started -> task_complete, 60 s before "now": a
    # finished turn, i.e. the person's move.
    assert (st.state, st.n_idle, st.n_run, st.has_usage()) == ("idle", 1, 0, False)
    f = frames[0]
    assert (f.provider, f.src) == ("codex", "cli")
    assert f.session_pct == 0.0
    assert f.weekly_pct == 9.0
    assert f.session_resets_at == 1787893426
    assert f.weekly_resets_at == 1788460425
    assert abs(f.observed_at - seen_at) < 1.0     # the event's own stamp
    assert f.stale is False


def test_the_real_rollout_tail_is_mostly_not_rate_limits():
    """The fixture must stay representative: a log is mostly conversation
    events, and the reader has to find the one line that matters among them.
    A fixture of only token_count lines would pass while proving little."""
    lines = [ln for ln in open(FIXTURE) if not ln.startswith('{"_comment"')]
    with_limits = [ln for ln in lines if "rate_limits" in ln]
    assert len(lines) >= 30
    assert len(with_limits) >= 3
    assert len(with_limits) < len(lines) / 2


# --- execution state ---------------------------------------------------------

def turn_line(kind, stamp):
    return json.dumps({"timestamp": stamp, "type": "event_msg",
                       "payload": {"type": kind}})


def _stamp(epoch):
    import datetime
    return datetime.datetime.fromtimestamp(
        epoch, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def test_a_started_turn_is_running_and_a_finished_one_is_idle():
    assert codex_cli.parse_rollout_state(
        [turn_line("task_started", _stamp(NOW - 5))], NOW) == base.STATE_RUNNING
    assert codex_cli.parse_rollout_state(
        [turn_line("task_started", _stamp(NOW - 30)),
         turn_line("task_complete", _stamp(NOW - 5))], NOW) == base.STATE_IDLE
    assert codex_cli.parse_rollout_state(
        [turn_line("task_started", _stamp(NOW - 30)),
         turn_line("turn_aborted", _stamp(NOW - 5))], NOW) == base.STATE_IDLE


def test_the_newest_turn_event_wins_whatever_follows_it():
    lines = [turn_line("task_complete", _stamp(NOW - 40)),
             turn_line("task_started", _stamp(NOW - 5)),
             token_count_line(rate_limits(), stamp=_stamp(NOW - 1))]
    assert codex_cli.parse_rollout_state(lines, NOW) == base.STATE_RUNNING


def test_a_long_silent_turn_is_still_running_and_a_finished_one_is_idle():
    late = 40 * 60.0
    assert codex_cli.parse_rollout_state(
        [turn_line("task_started", _stamp(NOW - late))], NOW) == base.STATE_RUNNING
    assert codex_cli.parse_rollout_state(
        [turn_line("task_complete", _stamp(NOW - late))], NOW) == base.STATE_IDLE


def test_an_abandoned_rollout_claims_nothing():
    gone = codex_cli.ABANDONED_AFTER_S + 1
    for kind in ("task_started", "task_complete"):
        assert codex_cli.parse_rollout_state(
            [turn_line(kind, _stamp(NOW - gone))], NOW) == base.STATE_UNKNOWN


def test_a_session_with_no_turn_yet_claims_nothing():
    """Opened, not typed into: the same silence a Claude SessionStart keeps."""
    lines = [json.dumps({"type": "session_meta", "payload": {}}),
             token_count_line(rate_limits())]
    assert codex_cli.parse_rollout_state(lines, NOW) == base.STATE_UNKNOWN
    assert codex_cli.parse_rollout_state([], NOW) == base.STATE_UNKNOWN


def test_a_turn_event_without_a_usable_timestamp_claims_nothing():
    line = json.dumps({"type": "event_msg", "payload": {"type": "task_started"}})
    assert codex_cli.parse_rollout_state([line], NOW) == base.STATE_UNKNOWN
    line = json.dumps({"timestamp": "not a date", "type": "event_msg",
                       "payload": {"type": "task_started"}})
    assert codex_cli.parse_rollout_state([line], NOW) == base.STATE_UNKNOWN


def test_every_rollout_votes_and_the_worst_state_is_reported(tmp_path):
    """Percentages are account-wide (one frame), but each rollout is one
    session and the light must show the worst of them: one finished session
    shows through two that are still working."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[token_count_line(rate_limits()),
                         turn_line("task_started", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[turn_line("task_started", _stamp(NOW - 9))])
    write_rollout(root, name="rollout-c.jsonl",
                  lines=[turn_line("task_complete", _stamp(NOW - 3))])
    frames = codex_cli.CodexCliProvider(root=root).poll(NOW)
    usage = [f for f in frames if f.src == "cli"]
    state = [f for f in frames if f.src == "cli-state"]
    assert len(usage) == 1 and len(state) == 1
    st = state[0]
    assert (st.state, st.n_run, st.n_idle, st.n_stuck) == ("idle", 2, 1, 0)
    assert st.n_sessions() == 3
    assert not st.has_usage()       # can never win a contest for numbers


def test_no_state_frame_when_no_rollout_has_a_turn(tmp_path):
    root = str(tmp_path / "sessions")
    write_rollout(root, lines=[token_count_line(rate_limits())])
    frames = codex_cli.CodexCliProvider(root=root).poll(NOW)
    assert [f.src for f in frames] == ["cli"]
