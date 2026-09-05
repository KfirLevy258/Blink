"""Conflict and recency resolution across sources that each see a slice."""
from pc import normalizer
from pc import statusline_source as ss
from pc.providers import base

NOW = 1_787_700_000.0


def cli(at, session=base.UNKNOWN, weekly=base.UNKNOWN, s_reset=None,
        w_reset=None, state="", stale=False, provider="claude"):
    return base.NormalizedUsageFrame(
        provider=provider, src="cli", observed_at=at, session_pct=session,
        weekly_pct=weekly, session_resets_at=s_reset, weekly_resets_at=w_reset,
        state=state, stale=stale)


def desktop(at, session=base.UNKNOWN, weekly=base.UNKNOWN, stale=False,
            provider="claude", burn=None):
    return base.NormalizedUsageFrame(
        provider=provider, src="desktop", observed_at=at, session_pct=session,
        weekly_pct=weekly, stale=stale, session_burn_pph=burn)


def test_nothing_in_nothing_out():
    assert normalizer.merge([]) is None
    assert normalizer.merge([None]) is None


def test_the_fresher_percentage_wins():
    m = normalizer.merge([
        cli(NOW - 3600, session=40.0),
        desktop(NOW - 60, session=55.0),
    ])
    assert m.session_pct == 55.0
    assert m.src == "desktop"


def test_a_source_without_the_field_never_wins_it():
    """The desktop cache has no reset timestamps. Being newest must not let
    it blank the only reset time anybody has."""
    m = normalizer.merge([
        cli(NOW - 3600, session=40.0, s_reset=NOW + 900),
        desktop(NOW - 60, session=55.0),
    ])
    assert m.session_pct == 55.0          # desktop is fresher
    assert m.session_resets_at == NOW + 900   # and CLI still supplies this


def test_higher_does_not_beat_fresher_across_a_reset():
    """The rejected rule, pinned as a test.

    A window that rolled over reads 0% fresh and 90% from just before the
    reset. Preferring the higher number would show 90% for as long as the old
    reading survived -- inventing usage that has already been forgiven.
    """
    m = normalizer.merge([
        desktop(NOW - 600, session=90.0),
        cli(NOW - 10, session=0.0),
    ])
    assert m.session_pct == 0.0


def test_staleness_follows_the_primary_dial_not_the_whole_set():
    """A fresh desktop percentage next to an hours-old CLI reset time is a
    live panel, not a stale one."""
    m = normalizer.merge([
        cli(NOW - 7200, session=40.0, s_reset=NOW + 900, stale=True),
        desktop(NOW - 30, session=55.0, stale=False),
    ])
    assert m.stale is False
    assert m.src == "desktop"


def test_a_stale_winner_keeps_its_amber():
    m = normalizer.merge([desktop(NOW - 90_000, session=55.0, stale=True)])
    assert m.stale is True


def test_frames_with_no_percentage_at_all_produce_nothing():
    """Two blank dials are worse than the board keeping what it has."""
    assert normalizer.merge([cli(NOW, state="running")]) is None


def test_each_window_resolves_independently():
    m = normalizer.merge([
        cli(NOW - 10, weekly=12.0),
        desktop(NOW - 400, session=88.0),
    ])
    assert m.session_pct == 88.0
    assert m.weekly_pct == 12.0


def test_providers_are_never_merged_into_each_other():
    """Two providers are two accounts with two separate limits."""
    groups = normalizer.group_by_provider([
        cli(NOW, session=10.0),
        cli(NOW, session=90.0, provider="codex"),
    ])
    assert set(groups) == {"claude", "codex"}
    assert len(groups["claude"]) == 1


def test_select_prefers_the_configured_provider_even_when_older():
    chosen = normalizer.select([
        cli(NOW - 3600, session=10.0),
        cli(NOW, session=90.0, provider="codex"),
    ], preferred="claude")
    assert chosen.provider == "claude"


def test_select_falls_back_to_the_freshest_provider():
    chosen = normalizer.select([
        cli(NOW - 3600, session=10.0, provider="gemini"),
        cli(NOW, session=90.0, provider="codex"),
    ], preferred="claude")
    assert chosen.provider == "codex"


def test_select_with_nothing_returns_none():
    assert normalizer.select([], preferred="claude") is None


# --- two providers on one pair of gauges ----------------------------------


def test_select_pair_returns_preferred_first_then_the_other():
    primary, secondary = normalizer.select_pair([
        cli(NOW - 3600, session=10.0),
        cli(NOW, session=90.0, provider="codex"),
    ], preferred="claude")
    assert primary.provider == "claude"
    assert secondary.provider == "codex"


def test_select_pair_with_one_provider_has_no_secondary():
    primary, secondary = normalizer.select_pair([cli(NOW, session=10.0)],
                                                preferred="claude")
    assert primary.provider == "claude"
    assert secondary is None


def test_select_pair_with_nothing():
    assert normalizer.select_pair([], preferred="claude") == (None, None)


def test_a_third_provider_is_dropped_not_rotated():
    """A ring that silently changes whose number it shows is worse than one
    that never shows it."""
    primary, secondary = normalizer.select_pair([
        cli(NOW, session=10.0),
        cli(NOW - 10, session=20.0, provider="codex"),
        cli(NOW - 5, session=30.0, provider="gemini"),
    ], preferred="claude")
    assert primary.provider == "claude"
    assert secondary.provider == "gemini"   # the fresher of the other two


def test_the_secondary_reaches_the_wire():
    from pc import protocol
    primary, secondary = normalizer.select_pair([
        cli(NOW, session=10.0, weekly=20.0),
        cli(NOW, session=34.0, weekly=61.0, provider="codex"),
    ], preferred="claude")
    msg = protocol.frame_to_usage(primary, NOW, secondary)
    assert msg["p2"] == "codex"
    assert msg["p2_session_pct"] == 34.0
    assert msg["p2_weekly_pct"] == 61.0


def test_one_provider_costs_nothing_on_the_wire():
    from pc import protocol
    primary, secondary = normalizer.select_pair([cli(NOW, session=10.0)],
                                                preferred="claude")
    msg = protocol.frame_to_usage(primary, NOW, secondary)
    for k in ("p2", "p2_session_pct", "p2_weekly_pct"):
        assert k not in msg


def test_the_name_travels_with_the_state_it_names():
    """label lives on the same source frame as n_run/n_wait/etc. and rides
    along by the same field-by-field rule -- see the block comment above
    n_run in merge(). Dropped here, the project name never reaches the
    board on any machine running more than one source."""
    f = cli(NOW, session=40.0, state=base.STATE_WAITING)
    f.label = "LiveClaudeUi"
    m = normalizer.merge([f])
    assert m.label == "LiveClaudeUi"


def test_two_providers_still_fit_the_board_line_limit():
    from pc import protocol
    primary, secondary = normalizer.select_pair([
        cli(NOW, session=88.0, weekly=99.0, s_reset=NOW + 900,
            w_reset=NOW + 90000,
            state="stuck"),
        cli(NOW, session=100.0, weekly=100.0, provider="codex"),
    ], preferred="claude")
    primary.n_run, primary.n_wait, primary.n_stuck = 3, 2, 4
    primary.n_agents = 9
    raw, why = protocol.encode_checked(
        protocol.frame_to_usage(primary, NOW, secondary))
    assert why is None, why
    assert len(raw) <= protocol.MAX_LINE_BYTES


def test_the_secondary_countdowns_reach_the_wire():
    """'Under the gauge should be the time left for each one' -- so each
    provider's own countdown has to travel, not just its percentage."""
    from pc import protocol
    primary, secondary = normalizer.select_pair([
        cli(NOW, session=10.0, s_reset=NOW + 1800),
        cli(NOW, session=34.0, s_reset=NOW + 4320, w_reset=NOW + 259200,
            provider="codex"),
    ], preferred="claude")
    msg = protocol.frame_to_usage(primary, NOW, secondary)
    assert msg["session_resets_in_s"] == 1800     # claude's
    assert msg["p2_s_in_s"] == 4320               # codex's
    assert msg["p2_w_in_s"] == 259200


def test_codex_alone_becomes_the_primary_not_the_second_ring():
    """The default is one provider, whichever it is. A Codex-only machine
    puts Codex on the outer ring -- which is why the firmware colours by the
    provider's NAME rather than by ring position."""
    primary, secondary = normalizer.select_pair(
        [cli(NOW, session=52.0, weekly=18.0, provider="codex")],
        preferred="claude")
    assert primary.provider == "codex"
    assert secondary is None


def test_a_second_provider_costs_nothing_until_there_is_one():
    from pc import protocol
    primary, secondary = normalizer.select_pair([cli(NOW, session=10.0)],
                                                preferred="claude")
    msg = protocol.frame_to_usage(primary, NOW, secondary)
    for k in ("p2", "p2_session_pct", "p2_weekly_pct", "p2_s_in_s",
              "p2_w_in_s"):
        assert k not in msg


# --- the burn rate never competes with a real countdown --------------------
#
# The rate is a poor substitute for the server's own reset time and a good
# substitute for nothing at all. These two tests are the whole rule: a merged
# frame carries one or the other, never both, so nothing downstream -- the
# protocol layer, the firmware, a future second panel -- ever has to decide
# between them or invent a precedence.


def test_the_rate_survives_when_no_source_has_a_reset_time():
    """Claude Desktop with no Claude Code: percentages, no reset, a rate."""
    out = normalizer.merge([desktop(NOW, session=40, weekly=20, burn=14.0)])
    assert out.session_resets_at is None
    assert out.session_burn_pph == 14.0


def test_a_real_reset_time_drops_the_rate():
    """The instant Claude Code reports, the rate stops being carried -- even
    though the desktop frame is FRESHER, which under plain recency would have
    let it through. This is not a recency question."""
    out = normalizer.merge([
        cli(NOW - 600, session=38, s_reset=NOW + 3600),
        desktop(NOW, session=40, burn=14.0),
    ])
    assert out.session_resets_at == NOW + 3600
    assert out.session_burn_pph is None


def test_a_stale_cli_reset_still_beats_the_rate():
    """Even an hours-old reset time is the server's answer, and the rate is
    ours. Age does not promote a measurement into a fact."""
    out = normalizer.merge([
        cli(NOW - 7200, session=38, s_reset=NOW + 60),
        desktop(NOW, session=40, burn=14.0),
    ])
    assert out.session_burn_pph is None


# --- a reading taken before a reset must not outlive it ---------------------
#
# The failure this prevents is the one the module docstring names: inventing
# usage that has already been forgiven. Strict recency alone got it wrong,
# because the status line is rewritten only when Claude Code renders, so its
# post-reset 0% is routinely OLDER than a desktop sample from before the same
# reset -- and the desktop cache cannot see reset times at all.


def rolled(at, session=base.UNKNOWN, s_rolled=None, provider="claude"):
    return base.NormalizedUsageFrame(
        provider=provider, src="cli", observed_at=at, session_pct=session,
        session_rolled_at=s_rolled)


def test_a_pre_reset_reading_loses_even_though_it_is_fresher():
    out = normalizer.merge([
        rolled(NOW - 1200, session=0.0, s_rolled=NOW - 60),   # older, correct
        desktop(NOW - 600, session=78.0),                     # fresher, stale truth
    ])
    assert out.session_pct == 0.0
    assert out.src == "cli"


def test_the_frame_that_saw_the_reset_is_not_excluded_by_its_own_evidence():
    """Its observed_at is the payload's mtime, which predates the rollover it
    is reporting. A naive `observed_at >= rolled_at` would discard it and
    leave the panel with nothing."""
    out = normalizer.merge([rolled(NOW - 1200, session=0.0, s_rolled=NOW - 60)])
    assert out is not None and out.session_pct == 0.0


def test_a_post_reset_reading_still_wins_normally():
    """The rule must not freeze the dial at 0 -- once another source has
    sampled after the reset, recency resumes."""
    out = normalizer.merge([
        rolled(NOW - 1200, session=0.0, s_rolled=NOW - 60),
        desktop(NOW - 30, session=4.0),
    ])
    assert out.session_pct == 4.0
    assert out.src == "desktop"


def test_the_burn_rate_is_dropped_with_the_reading_it_describes():
    """A slope measured before the window emptied describes the old window --
    and a reset is exactly the state that lets a rate through at all, since it
    leaves session_resets_at None."""
    out = normalizer.merge([
        rolled(NOW - 1200, session=0.0, s_rolled=NOW - 60),
        desktop(NOW - 600, session=78.0, burn=12.0),
    ])
    assert out.session_burn_pph is None


def test_no_rollover_reported_changes_nothing():
    out = normalizer.merge([cli(NOW - 1200, session=10.0),
                            desktop(NOW - 600, session=78.0)])
    assert out.session_pct == 78.0


def test_a_stale_reading_that_has_a_number_beats_a_fresher_one_that_does_not():
    """The invariant pc/providers/claude_cli's memory is built on.

    A CLI reading six hours old still carries a five-hour percentage; a
    desktop sample two days older carries one too. Staleness does not remove
    a frame from the contest -- recency ranks it -- so the CLI figure wins
    and `src` follows it. If this ever inverts, the panel goes back to
    reporting the age of a desktop sample nobody asked about (field report,
    2026-09-02).
    """
    m = normalizer.merge([
        cli(NOW - 6 * 3600, session=27.0, stale=True),
        desktop(NOW - 57 * 3600, session=0.0, stale=True),
    ])
    assert m.session_pct == 27.0
    assert m.src == "cli"
    assert m.observed_at == NOW - 6 * 3600
    assert m.stale is True


# --- how long since we heard a voice, as opposed to how old the dial is ----
#
# Two questions the merge used to answer with one number. `observed_at` is
# the age of the number on the dial and belongs to whichever source won it;
# `active_at` is the age of the freshest CONTACT and belongs to the whole
# group, losers included. They only come apart because
# pc/providers/claude_cli re-offers a remembered payload at its original
# mtime -- and when they do, a board that dozes on the first one closes its
# eyes on somebody who is sitting at the desk (field review 2026-09-02).


def test_a_frame_that_won_nothing_still_proves_somebody_is_here():
    """The exact field shape: Claude Code rewrote its status line five
    seconds ago, the five-hour window expired overnight so the rewrite
    carries no session percentage, and the remembered reading from this
    morning wins the dial. The dial is six hours old. The desk is not."""
    m = normalizer.merge([
        cli(NOW - 5, weekly=26.0),                      # live, no session pct
        cli(NOW - 6 * 3600, session=27.0, stale=True),  # remembered
    ])
    assert m.session_pct == 27.0                        # dial unchanged
    assert m.observed_at == NOW - 6 * 3600              # and honestly old
    assert m.active_at == NOW - 5                       # somebody is here


def test_a_frame_with_no_numbers_at_all_still_counts_as_contact():
    """A source that lost EVERY field, not just the session dial. It is not
    a candidate for anything the panel draws, and it is still proof that a
    tool on this machine wrote a file a minute ago."""
    m = normalizer.merge([
        cli(NOW - 3600, session=40.0, weekly=20.0),
        desktop(NOW - 60),                              # no percentages
    ])
    assert m.session_pct == 40.0
    assert m.observed_at == NOW - 3600
    assert m.active_at == NOW - 60


def test_a_single_reading_dates_itself_by_its_own_clock():
    """Nothing to be fresher than: the two questions have the same answer,
    which is what makes an absent active age exactly recoverable from the
    reading age on the wire."""
    m = normalizer.merge([cli(NOW - 900, session=40.0)])
    assert m.active_at == m.observed_at == NOW - 900


def test_the_freshest_contact_survives_a_second_merge():
    """A merged frame is a valid input to another merge (the same property
    session_rolled_at is carried for). Re-merging must not narrow the
    freshest contact back down to the winning reading's own age."""
    once = normalizer.merge([cli(NOW - 5, weekly=26.0),
                             cli(NOW - 6 * 3600, session=27.0)])
    twice = normalizer.merge([once])
    assert twice.active_at == NOW - 5


# --- the staleness cliff, and the reading it used to resurrect ---------------
#
# The failure this module was written against, reached by a route the design
# did not cover: rollover EVIDENCE used to expire along with the payload that
# carried it. Half an hour after the status line went quiet, the CLI frame
# stopped reporting that it had watched the window empty -- and a Claude
# Desktop sample taken BEFORE that reset became a legal candidate again. It
# was the freshest one, so it took the dial, wearing its own stale=False.
#
# Built through map_statusline_frame rather than the cli() helper on purpose.
# The helper cannot express the bug: it takes session_rolled_at as a given,
# and the defect was in what the real producer decides to set.

T0 = NOW


def _statusline(now, five_hour=80.0, resets_at=T0 + 60, mtime=T0):
    return ss.map_statusline_frame(
        {"rate_limits": {"five_hour": {"used_percentage": five_hour,
                                       "resets_at": resets_at}}},
        now, mtime)


def test_a_forgiven_percentage_cannot_return_when_the_status_line_ages_out():
    """The reproduction, at four clocks either side of the bound.

    A five-hour window at 80% resets sixty seconds after the status line was
    written; Claude Desktop sampled 78% thirty seconds before that reset, and
    cannot see reset times at all, so it has no way to know its reading has
    been superseded. Two seconds apart the panel used to flip from a correct
    0% to a confident green 78% for usage already forgiven, and it stayed
    there for the life of the daemon.
    """
    before = normalizer.merge([_statusline(T0 + 1799),
                               desktop(T0 + 30, session=78.0, weekly=40.0)])
    assert (before.session_pct, before.src, before.stale) == (0.0, "cli", False)

    for elapsed in (1801, 7200, 86400):
        m = normalizer.merge([_statusline(T0 + elapsed),
                              desktop(T0 + 30, session=78.0, weekly=40.0)])
        assert m.session_pct == base.UNKNOWN, elapsed
        assert m.weekly_pct == 40.0, elapsed     # the weekly window is intact


def test_the_rollover_is_still_evidence_after_the_reading_stops_being_one():
    """Why the frames above can refuse the desktop sample at all. A window
    emptied at an epoch; that fact does not rot with the file's mtime, and it
    is the only thing on the bus that can tell a source with no reset times
    that its reading describes a window which no longer exists."""
    assert _statusline(T0 + 86400).session_rolled_at == T0 + 60


def test_a_window_that_has_ended_stops_suppressing_the_burn_rate():
    """merge() sends a measured rate only when NO source could supply a reset
    time, so a long-past stamp out of a remembered payload silenced the rate
    -- while protocol.secs_until refused that same stamp as a countdown. The
    panel got neither. A stamp the window has already passed is not a reset
    time this daemon has; it is one it used to have."""
    m = normalizer.merge([_statusline(T0 + 7200),
                          desktop(T0 + 7080, session=55.0, burn=12.4)])
    assert m.session_resets_at is None
    assert m.session_burn_pph == 12.4
    assert m.session_pct == 55.0


def test_desktop_local_storage_supplies_a_reset_the_history_file_cannot():
    """The two desktop sources are complementary, not competing: the history
    file has both percentages and no reset, this one has the five-hour reset.
    The normalizer merges per field, so the panel gets both."""
    from pc.providers import base as b
    now = 1788613600.0
    history = b.NormalizedUsageFrame(
        provider="claude", src="desktop", observed_at=now - 200,
        session_pct=6.0, weekly_pct=17.0,
        session_resets_at=None, weekly_resets_at=None)
    local_storage = b.NormalizedUsageFrame(
        provider="claude", src="desktop_ls", observed_at=now - 60,
        session_pct=6.0, session_resets_at=now + 3600)
    merged = normalizer.merge([history, local_storage])
    assert merged.session_resets_at == now + 3600
    assert merged.weekly_pct == 17.0
