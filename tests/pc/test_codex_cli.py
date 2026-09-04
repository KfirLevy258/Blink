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


def meta_line(cwd, originator="codex-tui"):
    """Line 1 of a real rollout: the record that carries the project's cwd.

    The padding is not decoration. A real session_meta embeds
    base_instructions and measures 18-19 KB, and a fixture of 80 bytes would
    let a head bound far too small to work on a desk pass every test here.
    """
    return json.dumps({
        "timestamp": "2026-08-27T03:00:00.000Z",
        "type": "session_meta",
        "payload": {"cwd": cwd, "originator": originator,
                    "cli_version": "0.150.0",
                    "base_instructions": "i" * 18_000},
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


def test_the_cwd_comes_out_of_a_session_meta_line():
    line = json.dumps({"type": "session_meta", "timestamp": "2026-08-27",
                       "payload": {"cwd": "/Users/K/Projects/LiveClaudeUi",
                                   "originator": "codex-tui"}})
    assert codex_cli.session_meta_cwd(line) == "/Users/K/Projects/LiveClaudeUi"


def test_a_line_that_is_not_session_meta_yields_no_cwd():
    """None rather than "" so the caller can tell a head it could not read
    from a directory it read and then refused."""
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "event_msg", "payload": {"cwd": "/a/b"}})) is None
    assert codex_cli.session_meta_cwd("") is None
    assert codex_cli.session_meta_cwd("session_meta but not json") is None
    assert codex_cli.session_meta_cwd(json.dumps(["session_meta"])) is None
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "session_meta", "payload": "session_meta"})) is None
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "session_meta", "payload": {}})) is None
    assert codex_cli.session_meta_cwd(
        json.dumps({"type": "session_meta", "payload": {"cwd": 7}})) is None
    # ...and on its own `type`, not merely on the substring pre-filter. Every
    # case above is refused before the parse because the words are not in the
    # line at all; this one carries them and must still be turned away, or a
    # rollout that quotes the phrase would name the panel after itself.
    assert codex_cli.session_meta_cwd(json.dumps(
        {"type": "event_msg",
         "payload": {"cwd": "/a/b", "text": "session_meta"}})) is None


def test_the_name_is_the_last_path_component():
    assert codex_cli._project_name("/Users/K/Projects/LiveClaudeUi") == \
        "LiveClaudeUi"
    assert codex_cli._project_name("/private/tmp") == "tmp"


def test_a_trailing_separator_is_not_a_component():
    assert codex_cli._project_name("/Users/K/Blink/") == "Blink"
    assert codex_cli._project_name("/Users/K/Blink///") == "Blink"


def test_a_windows_path_splits_on_the_windows_separator():
    """Both separators, not os.sep: a home directory can be synced between
    machines, and Codex on Windows writes C:\\Users\\...."""
    assert codex_cli._project_name("C:\\Users\\Kfir\\Projects\\Blink") == "Blink"
    assert codex_cli._project_name("C:\\Users\\Kfir\\Projects\\Blink\\") == "Blink"


def test_a_directory_entry_is_not_a_name():
    for cwd in ("/", "", ".", "..", "/a/b/.", "/a/b/.."):
        assert codex_cli._project_name(cwd) == "", cwd


def test_a_control_character_never_reaches_the_wire():
    """This string is JSON-encoded into a line the firmware scans for quotes.
    A newline in a directory name is legal on every platform this runs on."""
    assert codex_cli._project_name("/a/b/pro\nject") == "project"
    assert codex_cli._project_name("/a/b/pro\x7fject") == "project"
    assert codex_cli._project_name("/a/b/\n\n") == ""


def test_a_name_with_no_drawable_ascii_is_refused():
    """firmware/src/fmt.c draws a label through fmt_ascii(), which replaces
    every codepoint it has no ASCII spelling for with "?" -- pinned by
    tests/fmt/host_test.c. A wholly non-Latin name therefore arrives as a row
    of question marks, which is worse than the count the panel falls back to.
    A name with a Latin stem keeps it and loses the rest.
    """
    assert codex_cli._project_name("/Users/K/פרויקט") == ""
    assert codex_cli._project_name("/Users/K/项目") == ""
    assert codex_cli._project_name("/Users/K/café") == "café"
    assert codex_cli._project_name("/Users/K/proj-项目") == "proj-项目"


def test_a_name_with_spaces_is_kept():
    """Unlike the Claude hook shim, which is one sed and refuses them. There
    is no filename being built here and no shell quoting to get wrong, so the
    reason that rule exists there does not exist here."""
    assert codex_cli._project_name("/Users/K/My Project") == "My Project"


def test_a_cwd_that_is_not_a_string_is_refused_not_raised():
    for cwd in (None, 7, [], {}, True):
        assert codex_cli._project_name(cwd) == ""


def test_the_name_is_not_capped_here():
    """protocol.session is the one place that knows the byte bound and the
    one place that truncates on a UTF-8 boundary. A second cap here would be
    a second thing to keep in step with the firmware."""
    long = "n" * 200
    assert codex_cli._project_name("/a/" + long) == long


def test_the_name_is_read_once_per_file(tmp_path):
    """`session_meta` is line 1 of an append-only file whose name carries a
    UUID: the path is never reused and the answer never changes. Re-deriving
    it from 19 KB of embedded system prompt on every tick is waste that grows
    with whatever upstream puts in that record next."""
    root = str(tmp_path / "sessions")
    path = write_rollout(root, lines=[meta_line("/Users/K/Blink"),
                                      token_count_line(rate_limits())])
    p = codex_cli.CodexCliProvider(root=root)
    reads = []
    real = codex_cli._head_line
    codex_cli._head_line = lambda q: (reads.append(q), real(q))[1]
    try:
        assert p._name_for(path) == "Blink"
        assert p._name_for(path) == "Blink"
        assert p._name_for(path) == "Blink"
    finally:
        codex_cli._head_line = real
    assert reads == [path], reads


def test_a_file_with_no_usable_name_is_not_re_read_either(tmp_path):
    """The negative answer is as fixed as the positive one, and a rollout
    with no session_meta is the common case for a file being written right
    now -- exactly the file this would otherwise re-read every minute."""
    root = str(tmp_path / "sessions")
    path = write_rollout(root, lines=[token_count_line(rate_limits())])
    p = codex_cli.CodexCliProvider(root=root)
    reads = []
    real = codex_cli._head_line
    codex_cli._head_line = lambda q: (reads.append(q), real(q))[1]
    try:
        assert p._name_for(path) == ""
        assert p._name_for(path) == ""
    finally:
        codex_cli._head_line = real
    assert reads == [path], reads


def test_names_are_pruned_to_the_files_still_being_read(tmp_path):
    """Bounded by RECENT_FILES rather than by how long the daemon has been
    up. A path that has fallen out of the recent set will never be read
    again, so holding its name is holding a string for nothing."""
    p = codex_cli.CodexCliProvider(root=str(tmp_path))
    p._names = {"/a": "A", "/b": "B", "/c": "C"}
    p._prune_names({"/b", "/c", "/d"})
    assert p._names == {"/b": "B", "/c": "C"}


def test_two_providers_do_not_share_a_name_cache(tmp_path):
    """A mutable default on __init__ would make the cache class-wide, which
    is a bug that only shows up on a desk running two of these."""
    a = codex_cli.CodexCliProvider(root=str(tmp_path))
    b = codex_cli.CodexCliProvider(root=str(tmp_path))
    a._names["/a"] = "A"
    assert b._names == {}


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


# --- naming the session, when there is exactly one to name -------------------


def _state_frame(root, now=NOW):
    frames = codex_cli.CodexCliProvider(root=root).poll(now)
    held = [f for f in frames if f.src == codex_cli.STATE_SRC_ID]
    return held[0] if held else None


def test_the_one_session_in_the_winning_state_is_named(tmp_path):
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Projects/LiveClaudeUi"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    st = _state_frame(root)
    assert (st.state, st.label) == ("idle", "LiveClaudeUi")


def test_two_sessions_in_the_winning_state_are_not_named(tmp_path):
    """The rule claude_state.poll applies, applied here for the same reason:
    a name picked from two says something true about one and implies it about
    the other. The count is what is true of both."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/Other"),
                         turn_line("task_complete", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.n_idle, st.label) == ("idle", 2, "")


def test_a_session_in_a_lesser_state_does_not_lend_its_name(tmp_path):
    """Two sessions, two states. The frame's `state` is the worse of them, so
    only the session actually holding that state may be named -- the other's
    name under the other's status would be a wrong sentence."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Finished"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/Working"),
                         turn_line("task_started", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.label) == ("idle", "Finished")


def test_an_unnamed_session_leaves_a_named_one_alone(tmp_path):
    """A rollout whose session_meta could not be read is still a session and
    still votes on the state -- it just has nothing to add to the name. Two
    holders of the state is still two, named or not."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_complete", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[turn_line("task_complete", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.n_idle, st.label) == ("idle", 2, "")


def test_a_rollout_with_no_turn_yet_lends_no_name(tmp_path):
    """It makes no claim on the state, so it must make none on the name --
    otherwise an opened-and-untyped-into terminal would rename the panel."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_started", _stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/JustOpened")])
    st = _state_frame(root)
    assert (st.state, st.n_run, st.label) == ("running", 1, "Blink")


def test_a_poll_prunes_the_names_of_files_it_no_longer_reads(tmp_path):
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         turn_line("task_started", _stamp(NOW - 5))])
    p = codex_cli.CodexCliProvider(root=root)
    p._names["/gone/rollout-z.jsonl"] = "Ghost"
    p.poll(NOW)
    assert "/gone/rollout-z.jsonl" not in p._names
    assert list(p._names.values()) == ["Blink"]


# --- a turn that died -------------------------------------------------------


def failed_line(stamp, info="usage_limit_exceeded", message="You have hit"):
    """A `task_complete` that carries an error, shaped as upstream writes it.

    ErrorEvent is {message, codex_error_info}; codex_error_info is a
    CodexErrorInfo, whose unit variants are bare snake_case strings.
    """
    error = {"message": message}
    if info is not None:
        error["codex_error_info"] = info
    return json.dumps({"timestamp": stamp, "type": "event_msg",
                       "payload": {"type": "task_complete",
                                   "turn_id": "t1", "error": error}})


def test_a_turn_that_died_on_a_usage_limit_is_failed():
    """The single case this product exists to warn about, and -- folded in
    here rather than kept as a parallel test -- every other terminal-error
    string upstream can send. No branch on which error: there is nowhere on
    the wire to put the distinction -- base.VALID_STATES is fixed -- and the
    panel already draws "Session used up" off the percentage dial this same
    file feeds. All six exercise the identical str-branch of
    _is_turn_failure, so a mutation that reddens one reddens them all; a
    second test making the same claim over more strings would not be a
    second data point."""
    for info in ("usage_limit_exceeded", "context_window_exceeded",
                 "unauthorized", "internal_server_error", "sandbox_error",
                 "other"):
        assert codex_cli.parse_rollout_state(
            [failed_line(_stamp(NOW - 5), info=info)], NOW) \
            == base.STATE_FAILED, info


def test_a_struct_shaped_error_is_failed():
    """CodexErrorInfo::HttpConnectionFailed carries a field, so it
    serialises as a single-key object rather than a bare string -- and the
    struct branch has to actually read that key, not merely agree with the
    fallback. A non-deny-listed struct proves nothing on its own: the
    fallback alone would also call it failed. Pairing it with a deny-listed
    struct is what forces the branch to exist -- only a real inspection of
    the key can turn one of these idle while the other stays failed."""
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5),
                     info={"http_connection_failed": {"http_status_code": 503}})],
        NOW) == base.STATE_FAILED
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5),
                     info={"active_turn_not_steerable": {"turn_kind": "review"}})],
        NOW) == base.STATE_IDLE


def test_an_error_upstream_does_not_call_a_turn_failure_is_still_idle():
    """CodexErrorInfo::affects_turn_status returns false for exactly two
    variants, both of them failures of a client operation rather than of the
    turn. Painting the panel red for a failed thread rollback would cry wolf
    with the one colour that must not."""
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5), info="thread_rollback_failed")],
        NOW) == base.STATE_IDLE
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5),
                     info={"active_turn_not_steerable": {"turn_kind": "review"}})],
        NOW) == base.STATE_IDLE


def test_an_error_with_no_info_is_failed():
    """ErrorEvent::affects_turn_status is is_none_or(...) -- upstream's own
    answer for a missing CodexErrorInfo is that the turn failed. An error
    object with nothing legible in it is still an error object."""
    assert codex_cli.parse_rollout_state(
        [failed_line(_stamp(NOW - 5), info=None)], NOW) == base.STATE_FAILED
    for info in (None, 7, [], ["thread_rollback_failed"],
                 {"a": 1, "b": 2}, {}):
        line = json.dumps({"timestamp": _stamp(NOW - 5), "type": "event_msg",
                           "payload": {"type": "task_complete",
                                       "error": {"message": "boom",
                                                 "codex_error_info": info}}})
        assert codex_cli.parse_rollout_state([line], NOW) \
            == base.STATE_FAILED, info


def test_an_error_of_an_unexpected_shape_degrades_to_idle_not_to_red():
    """Never observed in a real file, so the shape is upstream's to change.
    Red is the loudest thing this panel does and must not be reachable by a
    field that merely stopped being an object."""
    for bad in ("boom", 7, [], None, True):
        line = json.dumps({"timestamp": _stamp(NOW - 5), "type": "event_msg",
                           "payload": {"type": "task_complete", "error": bad}})
        assert codex_cli.parse_rollout_state([line], NOW) == base.STATE_IDLE, bad


def test_a_task_complete_without_an_error_is_still_idle():
    """The ordinary case, and every rollout ever captured on this machine."""
    assert codex_cli.parse_rollout_state(
        [turn_line("task_complete", _stamp(NOW - 5))], NOW) == base.STATE_IDLE


def test_an_aborted_turn_is_still_idle_whatever_its_reason():
    """All four TurnAbortReason values are things the person did: Esc, a new
    message typed over the turn, a review closing, a budget stopping it.
    Idle is the right colour for all four, and this mapping is frozen."""
    for reason in ("interrupted", "replaced", "review_ended", "budget_limited"):
        line = json.dumps({"timestamp": _stamp(NOW - 5), "type": "event_msg",
                           "payload": {"type": "turn_aborted",
                                       "reason": reason}})
        assert codex_cli.parse_rollout_state([line], NOW) == base.STATE_IDLE, \
            reason


# A failure an hour ago is a session that is gone, not a red light that
# stays on until the daemon restarts -- but that guarantee comes from the
# age gate in parse_rollout_state, which runs unconditionally AFTER the
# state (of any kind) is decided and does not read what the state is. That
# is already exercised by test_an_abandoned_rollout_claims_nothing above;
# a `failed`-flavoured copy of it cannot fail independently of that one, so
# it was removed rather than kept as coverage that cannot be lost.


def test_a_failed_session_is_the_worst_state_and_counted_with_the_stuck(tmp_path):
    """The wire has one count for "not working and not finished", and
    claude_state.poll folds failed into it for the same reason: `state`
    already carries which of the two it is."""
    root = str(tmp_path / "sessions")
    write_rollout(root, name="rollout-a.jsonl",
                  lines=[meta_line("/Users/K/Blink"),
                         token_count_line(rate_limits()),
                         failed_line(_stamp(NOW - 5))])
    write_rollout(root, name="rollout-b.jsonl",
                  lines=[meta_line("/Users/K/Other"),
                         turn_line("task_started", _stamp(NOW - 9))])
    st = _state_frame(root)
    assert (st.state, st.n_stuck, st.n_run, st.n_idle) == ("failed", 1, 1, 0)
    assert st.n_sessions() == 2
    assert st.label == "Blink"      # the only session holding the state


def test_rollout_session_id_comes_from_the_meta_line(tmp_path):
    """Line 1 of every rollout is its session_meta record, and it carries the
    id the hooks also report. That id is the only thing the two state sources
    share, so it is what lets them describe one session instead of two."""
    p = tmp_path / "rollout-x.jsonl"
    p.write_text(
        '{"type":"session_meta","payload":{"session_id":"cx-1",'
        '"cwd":"/Users/k/Projects/Blink","cli_version":"0.150.0"}}\n'
        '{"type":"event_msg","payload":{"type":"task_started"}}\n')
    assert codex_cli.rollout_session_id(str(p)) == "cx-1"


def test_rollout_session_id_reads_only_the_head(tmp_path):
    """A real rollout on this desk is 51 MB, and the tail reader would not
    reach line 1 at all. This asserts the head read does not depend on the
    rest of the file being small -- or even parseable."""
    p = tmp_path / "rollout-big.jsonl"
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"type":"session_meta","payload":{"session_id":"cx-2"}}\n')
        f.write("x" * (codex_cli.HEAD_BYTES * 4) + "\n")
    assert codex_cli.rollout_session_id(str(p)) == "cx-2"


@pytest.mark.parametrize("first_line", [
    '{"type":"event_msg","payload":{"type":"task_started"}}',   # not meta
    '{"type":"session_meta","payload":{}}',                     # meta, no id
    '{"type":"session_meta","payload":{"session_id":7}}',       # id not a string
    '{"type":"session_meta","payload":[]}',                     # payload not an object
    'not json at all',
])
def test_rollout_session_id_refuses_rather_than_guesses(tmp_path, first_line):
    """An empty string, never a guess. The caller treats "" as "this session
    cannot be matched to a hook slot", which is the safe answer -- it counts
    once from the rollout and is never merged with the wrong slot."""
    p = tmp_path / "rollout-odd.jsonl"
    p.write_text(first_line + "\n")
    assert codex_cli.rollout_session_id(str(p)) == ""


def test_rollout_session_id_refuses_an_unterminated_first_line(tmp_path):
    """A first line longer than the bound is not a rollout we understand."""
    p = tmp_path / "rollout-long.jsonl"
    p.write_text("{" + "a" * (codex_cli.HEAD_BYTES + 10))
    assert codex_cli.rollout_session_id(str(p)) == ""


def test_rollout_session_id_on_a_missing_file_is_empty(tmp_path):
    assert codex_cli.rollout_session_id(str(tmp_path / "nope.jsonl")) == ""


# --- the union: two sources, one census --------------------------------------
#
# From here down every test is about the join between the rollout reader above
# and the Codex hook slots (codex_state). The failure being guarded is not a
# wrong state, it is a DOUBLE COUNT: a session both sources can see appearing
# twice in the pip row, which is the arithmetic the panel shows a human.


def sid_meta_line(sid, cwd="/Users/K/Blink"):
    """A `session_meta` carrying a session_id, padded like a real one.

    Same padding argument as `meta_line`: an 80-byte fixture would let a head
    bound far too small to survive a real 18 KB session_meta pass every test.
    """
    return json.dumps({
        "timestamp": "2026-08-27T03:00:00.000Z",
        "type": "session_meta",
        "payload": {"session_id": sid, "cwd": cwd,
                    "originator": "codex-tui",
                    "cli_version": "0.150.0",
                    "base_instructions": "i" * 18_000},
    })


def slot(slots, sid, event, t):
    """One hook slot, in the shape tools/blink-hook.sh writes."""
    p = slots / (sid + ".state")
    p.write_text(json.dumps({"event": event, "t": float(t)}))
    return p


def union_frame(sessions, slots, now=NOW):
    """The single state frame a poll over both sources produces, or None.

    `state_dir` is passed on every call and `sweep` is off here for the reason
    codex_state spells out: the scan behind that directory DELETES files, so a
    test that let it fall back to the real default would be one HOME fixture
    away from collecting the slots driving the board on the desk.
    """
    frames = codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(now)
    held = [f for f in frames if f.src == codex_cli.STATE_SRC_ID]
    assert len(held) <= 1, "one census, or the pip row double-counts"
    return held[0] if held else None


def test_a_waiting_hook_slot_beats_a_running_rollout(tmp_path):
    """The rollout cannot see a permission prompt -- Codex never persists the
    approval events -- so `running` from it is the older, blinder answer for a
    session whose hook has since said `waiting`. One session, one row."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    write_rollout(str(sessions), name="rollout-cx-1.jsonl",
                  lines=[sid_meta_line("cx-1"),
                         turn_line("task_started", _stamp(NOW - 30))])
    slot(slots, "cx-1", "PermissionRequest", NOW - 5)

    f = union_frame(sessions, slots)

    assert f.provider == "codex"
    assert f.state == base.STATE_WAITING
    assert (f.n_run, f.n_wait, f.n_idle) == (0, 1, 0)
    assert f.n_sessions() == 1


def test_one_session_seen_by_both_sources_is_counted_once(tmp_path):
    """The failure this whole union exists to prevent. Both sources agree the
    session is running; the census must still say one."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    write_rollout(str(sessions), name="rollout-cx-1.jsonl",
                  lines=[sid_meta_line("cx-1"),
                         turn_line("task_started", _stamp(NOW - 30))])
    slot(slots, "cx-1", "PreToolUse", NOW - 5)

    f = union_frame(sessions, slots)

    assert (f.n_run, f.n_wait, f.n_idle) == (1, 0, 0)


def test_the_rollout_alone_would_have_counted_that_session_too(tmp_path):
    """The control for the test above, and the whole reason it means anything:
    with the slot removed the rollout still reports one running session. So
    the `1` there is a session both sources saw and one of them dropped, not a
    rollout that was never counted in the first place."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    write_rollout(str(sessions), name="rollout-cx-1.jsonl",
                  lines=[sid_meta_line("cx-1"),
                         turn_line("task_started", _stamp(NOW - 30))])

    f = union_frame(sessions, slots)

    assert (f.n_run, f.n_wait, f.n_idle) == (1, 0, 0)


def test_a_session_only_the_rollout_can_see_still_counts(tmp_path):
    """A Codex session already open when the hooks were installed has no slot
    and never will -- the hooks only fire from its next turn on. Dropping it
    would make a running terminal vanish from the panel, which is worse than
    not knowing it is waiting. So the two sources are unioned, not swapped."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    write_rollout(str(sessions), name="rollout-old-1.jsonl",
                  lines=[sid_meta_line("old-1"),
                         turn_line("task_started", _stamp(NOW - 30))])
    slot(slots, "cx-2", "PermissionRequest", NOW - 5)

    f = union_frame(sessions, slots)

    assert (f.n_run, f.n_wait) == (1, 1)
    assert f.n_sessions() == 2
    assert f.state == base.STATE_WAITING, "waiting outranks running"


def test_a_rollout_with_no_readable_id_still_counts_once(tmp_path):
    """The degradation path for rollout_session_id's refusals: an
    unidentifiable rollout is keyed by its own path, so it can never collide
    with a hook slot and can never be merged into the wrong one. It counts,
    beside the slot rather than instead of it."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    write_rollout(str(sessions), name="rollout-anon.jsonl",
                  lines=["not json at all",
                         turn_line("task_started", _stamp(NOW - 30))])
    slot(slots, "cx-9", "PreToolUse", NOW - 5)

    f = union_frame(sessions, slots)

    assert (f.n_run, f.n_sessions()) == (2, 2)


def test_two_unidentifiable_rollouts_do_not_collapse_into_one(tmp_path):
    """The path fallback has to be per FILE. Keying both anonymous rollouts on
    a single shared sentinel -- "" among them -- would silently merge two
    terminals into one row, an under-count as wrong as the double count."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    for name in ("rollout-anon-a.jsonl", "rollout-anon-b.jsonl"):
        write_rollout(str(sessions), name=name,
                      lines=["not json at all",
                             turn_line("task_started", _stamp(NOW - 30))])

    f = union_frame(sessions, slots)

    assert (f.n_run, f.n_sessions()) == (2, 2)


def test_hook_slots_alone_produce_a_frame(tmp_path):
    """Codex hooks installed, no rollout recent enough to be read. The census
    still has to reach the board."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    sessions.mkdir()
    slots.mkdir()
    slot(slots, "cx-1", "PermissionRequest", NOW - 5)

    frames = codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(NOW)

    assert [f.state for f in frames] == [base.STATE_WAITING]
    assert frames[0].session_pct == base.UNKNOWN, \
        "a state frame carries no percentage and must never win the dial"


def test_the_state_dir_argument_is_what_is_read(tmp_path):
    """Not "the default happened to work". The slot lives in a directory the
    default would never name, and a decoy sits in the one it would -- so a
    provider that ignored the argument would report the decoy's `idle`."""
    sessions, slots = tmp_path / "sessions", tmp_path / "elsewhere"
    sessions.mkdir()
    slots.mkdir()
    slot(slots, "cx-1", "PermissionRequest", NOW - 5)
    decoy = tmp_path / ".blink" / "state-codex"      # HOME is tmp_path here
    decoy.mkdir(parents=True)
    slot(decoy, "cx-decoy", "Stop", NOW - 5)

    f = union_frame(sessions, slots)

    assert (f.state, f.n_wait, f.n_idle) == (base.STATE_WAITING, 1, 0)


def test_sweep_false_leaves_an_abandoned_slot_on_disk(tmp_path):
    """`blink status` and every test must be able to LOOK without collecting:
    the sweep deletes slots, and a diagnostic that deletes what it is
    diagnosing destroys the evidence somebody ran it to see."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    sessions.mkdir()
    slots.mkdir()
    dead = slot(slots, "cx-old", "PreToolUse", NOW - 7200)   # past the hour

    codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=False).poll(NOW)
    assert dead.exists()

    codex_cli.CodexCliProvider(
        root=str(sessions), state_dir=str(slots), sweep=True).poll(NOW)
    assert not dead.exists(), "the daemon's poll is still the collector"


def test_a_session_the_hook_moved_keeps_the_name_the_rollout_gave_it(tmp_path):
    """The name belongs to the session, not to the state it was in when the
    rollout was read. Looking the holders up in the rollout's own tally would
    lose the label of every session the hook overruled -- the panel would go
    from "Blink - waiting" to a bare "waiting" at the exact moment it has
    something worth saying."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    write_rollout(str(sessions), name="rollout-cx-1.jsonl",
                  lines=[sid_meta_line("cx-1", cwd="/Users/K/Blink"),
                         turn_line("task_started", _stamp(NOW - 30))])
    slot(slots, "cx-1", "PermissionRequest", NOW - 5)

    f = union_frame(sessions, slots)

    assert (f.state, f.label) == (base.STATE_WAITING, "Blink")


def test_a_hook_only_session_is_a_nameless_holder(tmp_path):
    """codex_state drops the names its slots carry on purpose -- naming a
    session on the panel has its own rule and the Codex frame has no label
    story for the hooks yet. So a hook-only holder silences the label exactly
    as a rollout with an unreadable session_meta does, rather than borrowing
    the name of the other session in the census."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    slots.mkdir()
    write_rollout(str(sessions), name="rollout-cx-1.jsonl",
                  lines=[sid_meta_line("cx-1", cwd="/Users/K/Blink"),
                         turn_line("task_complete", _stamp(NOW - 30))])
    slot(slots, "cx-2", "PermissionRequest", NOW - 5)

    f = union_frame(sessions, slots)

    assert (f.state, f.n_wait, f.label) == (base.STATE_WAITING, 1, "")


def test_live_agents_reach_the_frame(tmp_path):
    """n_agents is already on the wire and already drawn. Codex could never
    fill it before because nothing counted subagents for it; the hook slots
    do, and a count left at zero would quietly report "no agents"."""
    sessions, slots = tmp_path / "sessions", tmp_path / "state-codex"
    sessions.mkdir()
    slots.mkdir()
    slot(slots, "cx-1", "PreToolUse", NOW - 5)
    agents = slots / "cx-1"
    agents.mkdir()
    (agents / "a1").write_text("")
    (agents / "a2").write_text("")

    f = union_frame(sessions, slots)

    assert f.n_agents == 2
