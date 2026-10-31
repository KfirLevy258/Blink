"""An account with no five-hour window: Ultra, from 2026-10-30.

Anthropic removed the five-hour limit for verified Ultra accounts (formerly
Max 20x). Claude Code's status line then carries rate_limits.seven_day and no
five_hour key at all, while it renders every few seconds. Measured on a real
Ultra desk, `blink status --wire` one second after Claude Code wrote the
file printed

    "session_pct":1.0, "src":"desktop", "stale":true, "age_s":136136,
    "weekly_pct":2.0, "active_age_s":0

-- live data, told it was stale and 37 hours old. The dial had been filled by
a borrowed session reading (a Claude Desktop sample two days old), and the
borrowed source then also named and dated the frame, because merge() takes
`primary = session_src or weekly_src`.

The rule is a latch in ClaudeCliProvider (set by a payload with a weekly
figure and no five_hour key, cleared only by a payload that carries the key)
and a veto in normalizer.merge. Review of the first version found it
fresh-only, recomputed per poll, and blind to a Pro or Max desk whose Desktop
still sees a window; each of those has a test below.

Every payload here is generated. None is captured from a machine. The
payload and write helpers are test_claude_cli's, and the Desktop cache is
tests/support/desktop_cache.
"""
import os

from pc import ingest, normalizer, protocol
from pc.providers import base
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider
from pc.providers.claude_desktop_ls import ClaudeDesktopLocalStorageProvider
from tests.pc.test_claude_cli import payload, write
from tests.support import desktop_cache as dc

NOW = dc.NOW


def weekly_only(weekly=2.0):
    """The Ultra shape: seven_day, and no five_hour key whatever."""
    return payload(five_hour=None, seven_day=weekly)


def desktop_at(tmp_path, age_s, session=1, weekly=2):
    """A real Desktop provider over a generated cache whose newest sample is
    `age_s` old."""
    f = tmp_path / "plan-usage-history.json"
    f.write_text(dc.doc([dc.sample(int((NOW - age_s) * 1000), fh=session,
                                   sd=weekly)]), encoding="utf-8")
    return ClaudeDesktopProvider(path=str(f))


class Fixed(base.ProviderParser):
    def __init__(self, *frames):
        self._frames = list(frames)

    def get_provider_id(self):
        return self._frames[0].provider

    def poll(self, now_epoch):
        return list(self._frames)


def cli_frame(at=NOW - 1, weekly=2.0, session=base.UNKNOWN, flagged=True,
              **kw):
    return base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=at, weekly_pct=weekly,
        session_pct=session, no_session_window=flagged, **kw)


def desktop_frame(at, session=40.0, weekly=2.0, **kw):
    return base.NormalizedUsageFrame(
        provider="claude", src="desktop", observed_at=at,
        session_pct=session, weekly_pct=weekly, **kw)


def wire(bus):
    msg = bus.poll()
    line, why = protocol.encode_checked(msg)
    assert why is None, why
    return msg


# --- the measured desk ------------------------------------------------------


def test_a_fresh_weekly_only_payload_is_live_with_an_unknown_session(tmp_path):
    """The measured wire line, reproduced and then inverted. The Desktop
    sample is an hour old, inside the age at which Desktop retires its own
    session reading, so it is the vetoed borrower and not retired first."""
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=1)],
        now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["session_resets_in_s"] == -1
    assert msg.get("burn_pph") is None
    assert msg["weekly_pct"] == 2.0
    assert msg["src"] == "cli"
    assert msg["stale"] is False
    assert msg["age_s"] == 1
    assert msg["active_age_s"] == 1


def test_the_borrowed_burn_rate_and_countdown_are_not_sent(tmp_path):
    """A Desktop cache with a rising series carries a burn rate, and the
    session it would describe is not this account's."""
    f = tmp_path / "plan-usage-history.json"
    # Three samples five minutes apart, the newest still current: the shape
    # that earns a rate (BURN_MAX_GAP_S, BURN_MIN_SPAN_S), and older than the
    # status line's payload below, so it is the vetoed borrower.
    f.write_text(dc.doc([dc.sample(int((NOW - 900 + i * 300) * 1000),
                                   fh=10 + 5 * i, sd=2) for i in range(3)]),
                 encoding="utf-8")
    desktop = ClaudeDesktopProvider(path=str(f))
    assert desktop.poll(NOW)[0].session_burn_pph is not None, \
        "the fixture must carry a rate or this proves nothing"
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)), desktop], now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg.get("burn_pph") is None


def test_five_hour_coming_back_behaves_as_before(tmp_path):
    """An unverified account, or a policy reversal."""
    p = tmp_path / "statusline.json"
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=1)],
        now=lambda: NOW)

    write(p, weekly_only(2.0), NOW - 10)
    assert wire(bus)["session_pct"] == -1

    write(p, payload(five_hour=31.0, resets_at=NOW + 3600), NOW - 1)
    msg = wire(bus)
    assert msg["session_pct"] == 31.0
    assert msg["session_resets_in_s"] == 3600
    assert msg["src"] == "cli"
    assert msg["stale"] is False


def test_a_desktop_only_desk_is_untouched(tmp_path):
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(tmp_path / "absent.json")),
                   desktop_at(tmp_path, 120, session=9)],
        now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == 9.0
    assert msg["src"] == "desktop"
    assert msg["stale"] is False


# --- the latch --------------------------------------------------------------


def test_a_stale_payload_stays_flagged_and_nothing_is_borrowed(tmp_path):
    """Review finding 1. At 30 minutes the first version stopped flagging and
    the 37 hours came back, until the board slept at four. The frame is
    stale, honestly aged by the status line, and the dial is still unknown."""
    p = tmp_path / "statusline.json"
    clock = [NOW]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=1)],
        now=lambda: clock[0])
    write(p, weekly_only(2.0), NOW - 1)
    assert wire(bus)["session_pct"] == -1

    clock[0] = NOW + 2 * 3600
    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["src"] == "cli"
    assert msg["stale"] is True
    assert msg["age_s"] == 2 * 3600 + 1


def test_a_restarted_daemon_reads_a_stale_payload_as_flagged(tmp_path):
    """The same finding for a fresh process, which is also `blink status
    --wire`: no memory, and a payload that is 40 minutes old."""
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 2400)
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=1)],
        now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["src"] == "cli"
    assert msg["age_s"] == 2400


def test_a_render_without_rate_limits_does_not_unflag_the_account(tmp_path):
    """Review finding 3. A new or resumed session writes a status line with
    no rate_limits at all. The first version recomputed per poll, so the wire
    alternated between "--%" and the borrowed number and the pushes doubled."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, weekly_only(2.0), NOW - 3)
    assert [f.no_session_window for f in prov.poll(NOW - 2)] == [True]

    write(p, {"session_id": "s", "model": {"id": "m"}}, NOW - 1)
    assert [f.no_session_window for f in prov.poll(NOW)] == [True]


def test_the_wire_does_not_alternate_across_a_render_without_limits(tmp_path):
    p = tmp_path / "statusline.json"
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=1)],
        now=lambda: NOW)
    sessions = []
    for doc in (weekly_only(2.0), {"session_id": "s"}, weekly_only(2.0),
                {"session_id": "s"}):
        write(p, doc, NOW - 1)
        sessions.append(wire(bus)["session_pct"])

    # The dial. (A render with no figures at all leaves the weekly to the
    # Desktop sample, as it always has for any desk; that is not a session.)
    assert sessions == [-1, -1, -1, -1]


def test_an_unreadable_file_does_not_unflag_the_account(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, weekly_only(2.0), NOW - 5)
    prov.poll(NOW - 4)

    p.write_text("{", encoding="utf-8")          # a torn write
    assert prov.poll(NOW - 3) == []
    p.unlink()
    assert prov.poll(NOW - 2) == []

    write(p, {"session_id": "s"}, NOW - 1)
    assert [f.no_session_window for f in prov.poll(NOW)] == [True]


def test_a_five_hour_key_clears_the_latch(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, weekly_only(2.0), NOW - 5)
    assert prov.poll(NOW - 4)[0].no_session_window is True

    write(p, payload(five_hour=31.0, resets_at=NOW + 3600), NOW - 3)
    assert [f.no_session_window for f in prov.poll(NOW - 2)] == [False]

    # and it stays clear through a render that leaves the window out, which
    # is what Claude Code does until a session's first response.
    write(p, {"session_id": "s"}, NOW - 1)
    assert not any(f.no_session_window for f in prov.poll(NOW))


def test_a_present_five_hour_without_a_percentage_clears_it_too(tmp_path):
    """Only the KEY decides, in both directions: five_hour present with no
    used_percentage is the likeliest shape of a payload change, and neither
    sets the latch nor holds it."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, weekly_only(2.0), NOW - 5)
    prov.poll(NOW - 4)

    write(p, {"rate_limits": {"five_hour": {"resets_at": NOW + 600},
                              "seven_day": {"used_percentage": 2.0}}},
          NOW - 1)
    assert [f.no_session_window for f in prov.poll(NOW)] == [False]


def test_a_payload_without_a_usable_weekly_figure_does_not_set_it(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, {"rate_limits": {}}, NOW - 2)
    assert [f.no_session_window for f in prov.poll(NOW - 1)] == [False]

    nan = float("nan")
    write(p, {"rate_limits": {"seven_day": {"used_percentage": nan}}}, NOW - 1)
    assert [f.no_session_window for f in prov.poll(NOW)] == [False]


def test_a_window_that_has_not_ended_is_not_a_missing_window(tmp_path):
    """The discriminator, from the data. A remembered five_hour whose
    resets_at is still ahead is proof this account has a window, so a render
    without it (Claude Code leaves a window out until a session's first
    response) must not blank the dial."""
    p = tmp_path / "statusline.json"
    clock = [NOW - 600]
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: clock[0])
    write(p, payload(five_hour=27.0, resets_at=NOW + 3600), NOW - 600)
    assert wire(bus)["session_pct"] == 27.0

    write(p, weekly_only(2.0), NOW - 1)
    clock[0] = NOW
    msg = wire(bus)

    assert msg["session_pct"] == 27.0      # remembered, as today
    assert msg["stale"] is False
    assert msg["age_s"] == 600             # the remembered reading's own age


def test_the_remembered_window_is_judged_at_the_payloads_own_clock(tmp_path):
    """Review finding 5. The payload was written while the remembered window
    was still open; the daemon only reads it after that window has ended.
    Judged by the poll's clock the window had ended and the account was
    flagged; judged by the payload's it was a render that left the window
    out, which is what it was."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, payload(five_hour=27.0, resets_at=NOW - 600), NOW - 3000)
    prov.poll(NOW - 2900)

    write(p, weekly_only(2.0), NOW - 1200)       # written at a time it was open
    assert not any(f.no_session_window for f in prov.poll(NOW))


def test_a_stamped_window_that_had_ended_when_the_payload_was_written(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, payload(five_hour=27.0, resets_at=NOW - 7200), NOW - 9000)
    prov.poll(NOW - 8900)

    write(p, weekly_only(2.0), NOW - 1)
    frames = prov.poll(NOW)
    assert frames and all(f.no_session_window for f in frames)


def test_an_unstamped_remembered_window_cannot_be_shown_to_have_ended(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, payload(five_hour=27.0), NOW - 9000)
    prov.poll(NOW - 8900)

    write(p, weekly_only(2.0), NOW - 1)
    assert not any(f.no_session_window for f in prov.poll(NOW))


# --- the Desktop age bound (finding 11) -------------------------------------


def test_a_desktop_session_older_than_a_window_is_retired(tmp_path):
    """A window open when the sample was taken has ended five hours later,
    and the cache has no stamps to say so. This is also the restarted
    daemon: with no latch yet, nothing else stops a days-old sample."""
    limit = 5 * 3600 + 600

    inside = desktop_at(tmp_path, limit - 5, session=24).poll(NOW)[0]
    assert inside.session_pct == 24.0
    assert inside.stale is True

    outside = desktop_at(tmp_path, limit + 5, session=24).poll(NOW)[0]
    assert outside.session_pct == base.UNKNOWN
    assert outside.session_burn_pph is None
    assert outside.weekly_pct == 2.0               # lasts seven days


def test_a_retired_desktop_sample_does_not_reach_the_wire(tmp_path):
    """The measured 37 hours, with no status line on the bus at all."""
    bus = ingest.IngestionBus(
        providers=[desktop_at(tmp_path, 136136, session=1, weekly=2)],
        now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["weekly_pct"] == 2.0


# --- finding 2: a window the account demonstrably has -----------------------


def test_a_desktop_window_with_a_reset_ahead_proves_the_account_has_one(
        tmp_path):
    """The flag cannot tell an Ultra account from a Pro or Max one whose
    window expired while nobody rendered. Claude Desktop's Local Storage
    can: resetsAt in the future is the server saying a window is open, and
    that source wins as it would have before the flag existed."""
    ls = ClaudeDesktopLocalStorageProvider()
    live_window = ls.record_to_frame(
        {"resetsAt": NOW + 3000, "utilization": 0.40,
         "observedAt": NOW - 300}, NOW)
    assert live_window.session_resets_at == NOW + 3000

    merged = normalizer.merge([cli_frame(at=NOW - 1), live_window])

    assert merged.session_pct == 40.0
    assert merged.session_resets_at == NOW + 3000
    assert merged.src == "desktop_ls"
    assert merged.no_session_window is False


def test_a_desktop_reading_without_a_reset_is_not_proof():
    """The plan-usage-history cache has no stamps, and a stamp is the proof."""
    merged = normalizer.merge([cli_frame(at=NOW - 1),
                               desktop_frame(NOW - 600, session=40.0)])

    assert merged.session_pct == base.UNKNOWN
    assert merged.session_resets_at is None
    assert merged.no_session_window is True


def test_a_window_that_ended_is_not_proof(tmp_path):
    """A record whose resetsAt has passed publishes no number and no stamp
    (it is evidence of a rollover), so it cannot lift the flag."""
    ls = ClaudeDesktopLocalStorageProvider()
    ended = ls.record_to_frame(
        {"resetsAt": NOW - 60, "utilization": 0.40,
         "observedAt": NOW - 3000}, NOW)

    merged = normalizer.merge([cli_frame(at=NOW - 1), ended])

    assert merged.session_pct == base.UNKNOWN
    assert merged.no_session_window is True


# --- merge: what the flag vetoes (the five mutants of the review) -----------


def test_a_flagged_frame_cannot_fill_the_dial_itself():
    """Mutant: let CLI frames still fill the dial under the flag. The
    provider cannot produce this frame today (a remembered window that
    counts is never one that ended), and merge() is the contract regardless
    of who calls it."""
    remembered = cli_frame(at=NOW - 3600, session=40.0, weekly=2.0,
                           session_resets_at=NOW + 600)

    merged = normalizer.merge([cli_frame(at=NOW - 1), remembered])

    assert merged.session_pct == base.UNKNOWN
    assert merged.session_resets_at is None
    assert merged.src == "cli"


def test_the_countdown_is_not_carried_onto_an_unknown_dial():
    """Mutant: delete the countdown override. A flagged frame's reset stamp
    would ride along on a dial drawn "--%"."""
    stamped = cli_frame(at=NOW - 3600, session=40.0, weekly=2.0,
                        session_resets_at=NOW + 600)

    merged = normalizer.merge([cli_frame(at=NOW - 1), stamped])

    assert merged.session_resets_at is None
    assert protocol.frame_to_usage(merged, NOW)["session_resets_in_s"] == -1


def test_the_burn_rate_is_not_carried_onto_an_unknown_dial():
    """Mutant: delete the burn override. A rate off the vetoed Desktop
    sample would describe a window the account does not have."""
    rated = desktop_frame(NOW - 600, session=40.0, session_burn_pph=12.0)

    merged = normalizer.merge([cli_frame(at=NOW - 1), rated])

    assert merged.session_pct == base.UNKNOWN
    assert merged.session_burn_pph is None
    assert "burn_pph" not in protocol.frame_to_usage(merged, NOW)


def test_the_status_line_names_and_dates_the_frame_not_the_borrower():
    """The primary is the pre-existing expression; with nothing borrowed it
    falls to the weekly source, which is the live status line."""
    stale_desktop = desktop_frame(NOW - 136136, session=1.0, weekly=2.0,
                                  stale=True)

    merged = normalizer.merge([cli_frame(at=NOW - 1), stale_desktop])

    assert (merged.src, merged.stale, merged.observed_at) == (
        "cli", False, NOW - 1)


def test_the_flag_does_not_cross_providers_on_the_wire():
    """Mutant: blank every provider's session in select_pair. Codex has its
    own account and its own five-hour window."""
    claude = cli_frame(at=NOW - 1)
    codex = base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=NOW - 1, session_pct=33.0,
        weekly_pct=10.0)

    primary, secondary = normalizer.select_pair([claude, codex],
                                                preferred="claude")

    assert primary.provider == "claude"
    assert primary.session_pct == base.UNKNOWN
    assert secondary.provider == "codex"
    assert secondary.session_pct == 33.0

    bus = ingest.IngestionBus(
        providers=[Fixed(claude), Fixed(codex)], now=lambda: NOW)
    msg = wire(bus)
    assert msg["session_pct"] == -1
    assert msg["p2"] == "codex"
    assert msg["p2_session_pct"] == 33.0


# --- the merged frame as an input (finding 10) ------------------------------


def test_the_flag_survives_a_second_merge():
    cli = cli_frame(at=NOW - 1)
    older = desktop_frame(NOW - 500, session=40.0)

    once = normalizer.merge([cli, older])
    twice = normalizer.merge([once, older])

    assert once.session_pct == base.UNKNOWN
    assert twice.session_pct == base.UNKNOWN
    assert twice.no_session_window is True
    assert twice.src == "cli"


def test_a_carried_flag_does_not_veto_a_reading_newer_than_itself():
    """The flag is a statement as of the frame that carries it. A merged
    frame handed on to another merge must not blank a session reading taken
    after it."""
    once = normalizer.merge([cli_frame(at=NOW - 1000)])
    assert once.no_session_window is True
    newer = desktop_frame(NOW - 10, session=55.0)

    merged = normalizer.merge([once, newer])

    assert merged.session_pct == 55.0
    assert merged.no_session_window is False


def test_the_flag_does_not_cross_providers():
    claude = cli_frame(at=NOW - 1)
    codex = base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=NOW - 1, session_pct=33.0,
        weekly_pct=10.0)

    assert normalizer.merge([codex]).session_pct == 33.0
    assert normalizer.merge([claude]).session_pct == base.UNKNOWN


def test_the_provider_marks_every_frame_it_offers(tmp_path):
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)

    frames = ClaudeCliProvider(path=str(p)).poll(NOW)

    assert [f.no_session_window for f in frames] == [True]
