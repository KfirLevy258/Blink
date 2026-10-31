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

The rule is evidence, not a latch (ClaudeCliProvider._has_no_window): a
payload with a usable weekly figure and no five-hour window marks its frames
when this provider has seen no window within five hours and ten minutes of
the figures being fetched. normalizer.merge then borrows nothing for the
session, unless a source carries a reset time that is still ahead. The latch
this replaced held "--%" on every Pro and Max desk at every five-hour reset
(review, 2026-10-31); the tests below name the review finding each one pins,
and every one fails on a mutant that removes what it pins (listed in the
commit that wrote them).

Every payload here is generated. None is captured from a machine. The
payload and write helpers are test_claude_cli's, the Desktop cache is
tests/support/desktop_cache, and the transcripts are tests/support/
transcripts through test_burn_line.
"""
import json
import os

import pytest

from pc import ingest, normalizer, protocol
from pc.bridge import THEME_QUIET_S, Bridge
from pc.providers import base
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import (VERDICT_FRAME,
                                         ClaudeDesktopProvider)
from pc.providers.claude_desktop_ls import ClaudeDesktopLocalStorageProvider
from tests.pc.test_claude_cli import payload, write
from tests.support import desktop_cache as dc

NOW = dc.NOW
WEEK_END = NOW + 3 * 86400
# The age past which a stampless session figure is no longer offered.
BOUND = base.SESSION_WINDOW_S + base.SESSION_SLACK_S


def weekly_only(weekly=2.0):
    """The Ultra shape: seven_day, and no five_hour key whatever."""
    return payload(five_hour=None, seven_day=weekly)


def status(five=None, five_resets=None, seven=2.0, seven_resets=WEEK_END):
    """A status line as Claude Code writes it, with both reset stamps."""
    limits = {}
    if seven is not None:
        limits["seven_day"] = {"used_percentage": seven,
                               "resets_at": seven_resets}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five,
                               "resets_at": five_resets}
    return {"session_id": "s", "model": {"id": "m"}, "rate_limits": limits}


# A render with no rate_limits at all: a session that has just started or
# been resumed, before its first reply.
NO_LIMITS = {"session_id": "s", "model": {"id": "m"}}


def desktop_doc(path, samples):
    """samples: [(epoch_s, fh, sd)]."""
    path.write_text(dc.doc([dc.sample(int(t * 1000), fh=fh, sd=sd)
                            for t, fh, sd in samples]), encoding="utf-8")
    return path


def desktop_at(tmp_path, age_s, session=1, weekly=2):
    """A real Desktop provider over a generated cache whose newest sample is
    `age_s` old."""
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - age_s, session, weekly)])
    return ClaudeDesktopProvider(path=str(f))


class Fixed(base.ProviderParser):
    def __init__(self, *frames):
        self._frames = list(frames)

    def get_provider_id(self):
        return self._frames[0].provider

    def poll(self, now_epoch):
        return list(self._frames)


class LocalStorage(base.ProviderParser):
    """Claude Desktop's Local Storage, mapped by the real record_to_frame at
    each poll's clock. The real store is a LevelDB, which nothing here
    should build; the record is what its reader hands on."""

    def __init__(self, tmp_path, rec):
        self._real = ClaudeDesktopLocalStorageProvider(
            dir_path=str(tmp_path / "no-local-storage"))
        self.rec = rec

    def get_provider_id(self):
        return "claude"

    def poll(self, now_epoch):
        f = self._real.record_to_frame(dict(self.rec), now_epoch)
        return [f] if f is not None else []


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


def brief(msg):
    return (msg["session_pct"], msg["src"], msg["stale"], msg["age_s"])


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
    assert bus.claude_page().no_session_window is True


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
    assert bus.claude_page().no_session_window is True

    write(p, payload(five_hour=31.0, resets_at=NOW + 3600), NOW - 1)
    msg = wire(bus)
    assert msg["session_pct"] == 31.0
    assert msg["session_resets_in_s"] == 3600
    assert msg["src"] == "cli"
    assert msg["stale"] is False
    assert bus.claude_page().no_session_window is False


def test_a_desktop_only_desk_is_untouched(tmp_path):
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(tmp_path / "absent.json")),
                   desktop_at(tmp_path, 120, session=9)],
        now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == 9.0
    assert msg["src"] == "desktop"
    assert msg["stale"] is False


# --- Ultra in its steady state ----------------------------------------------


@pytest.mark.parametrize("doc", [
    status(seven=24.0),
    status(five=40.0, five_resets=NOW + 3600, seven=24.0)],
    ids=["no window", "pro or max, session held still"])
def test_the_wire_ages_the_status_line_not_the_figure(tmp_path, doc):
    """The mark is judged by when the figures were FETCHED (first_seen_at),
    and that must not leak into the reading's age. A weekly percentage holds
    still for many minutes on an Ultra desk -- one point of a weekly cap is a
    lot of work -- while Claude Code repaints the line every few seconds; aged
    by the figure, the board would draw "20m ago" over live data from
    AGE_CAPTION_MIN_S on. The age is the file's, as it is for a Pro or Max
    desk whose session percentage holds still, on a running daemon and on a
    fresh process alike (questioned on the owner's desk, 2026-10-31)."""
    p = tmp_path / "statusline.json"
    clock = [NOW - 1200]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 4000, session=1, weekly=24)],
        now=lambda: clock[0])
    for at in (NOW - 1200, NOW - 600, NOW - 60, NOW - 1):
        write(p, doc, at)                       # the same block, repainted
        clock[0] = at + 0.5
        wire(bus)
    clock[0] = NOW

    running = wire(bus)
    fresh = wire(ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 4000, session=1, weekly=24)],
        now=lambda: NOW))

    for msg in (running, fresh):
        assert msg["src"] == "cli"
        assert msg["stale"] is False
        assert msg["age_s"] <= 2


def test_a_stale_payload_stays_marked_and_nothing_is_borrowed(tmp_path):
    """Idle. At 30 minutes the first version stopped marking and the 37
    hours came back, until the board slept at four. The frame is stale,
    honestly aged by the status line, and the dial is still unknown."""
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


def test_a_restarted_daemon_reads_a_stale_payload_as_marked(tmp_path):
    """The same for a fresh process, which is also `blink status --wire`:
    no memory, and a payload that is 40 minutes old."""
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 2400)
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=1)],
        now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["src"] == "cli"
    assert msg["age_s"] == 2400


def test_a_render_without_rate_limits_keeps_the_mark(tmp_path):
    """Finding 2's first half. A new or resumed session writes a status line
    with no rate_limits at all, and the last payload with a figure is offered
    beside it with its own mark."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, weekly_only(2.0), NOW - 3)
    assert [f.no_session_window for f in prov.poll(NOW - 2)] == [True]

    write(p, NO_LIMITS, NOW - 1)
    frames = prov.poll(NOW)

    assert [(f.observed_at, f.no_session_window) for f in frames] == [
        (NOW - 1, False), (NOW - 3, True)]
    assert frames[1].weekly_pct == 2.0


def test_a_render_without_rate_limits_does_not_hand_the_frame_to_desktop(
        tmp_path):
    """Finding 2. The primary is whatever dates the weekly figure, and with
    no figure in the render a Desktop sample 37 hours old dated it: the
    measured line came back on every such render and went on the next, and
    each change was a push."""
    p = tmp_path / "statusline.json"
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 136136, session=1, weekly=2)],
        now=lambda: NOW)
    seen = []
    for i, doc in enumerate((weekly_only(2.0), NO_LIMITS, weekly_only(2.0),
                             NO_LIMITS)):
        write(p, doc, NOW - 4 + i)
        msg = wire(bus)
        seen.append((msg["session_pct"], msg["src"], msg["stale"]))

    assert seen == [(-1, "cli", False)] * 4


def test_an_unreadable_file_keeps_the_mark_and_the_figure(tmp_path):
    """Finding 6. A torn write and a deleted file read as nothing, and the
    previous rule then dropped the mark for that poll: the borrowed Desktop
    figure came back until the next render."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    bus = ingest.IngestionBus(
        providers=[prov, desktop_at(tmp_path, 7200, session=1)],
        now=lambda: NOW)
    write(p, weekly_only(2.0), NOW - 5)
    assert brief(wire(bus)) == (-1, "cli", False, 5)

    p.write_text("{", encoding="utf-8")          # a torn write
    assert [f.no_session_window for f in prov.poll(NOW)] == [True]
    assert brief(wire(bus)) == (-1, "cli", False, 5)

    p.unlink()
    assert brief(wire(bus)) == (-1, "cli", False, 5)
    assert bus.claude_page().no_session_window is True


# --- the evidence: what tells Ultra from a Pro or Max reset ------------------


def test_a_pro_or_max_reset_keeps_its_zero_on_a_running_daemon(tmp_path):
    """Finding 1, the worst. Claude Code 2.1.316 drops an expired five_hour
    and re-renders a second after the reset, so every Pro and Max desk goes
    weekly-only at every reset. The latch read that as "no window" and held
    "--%" until the next reply; before it, the desk showed 0%. The window
    that just reset is the evidence the account has one."""
    t1 = NOW
    p = tmp_path / "statusline.json"
    clock = [t1 - 59]
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: clock[0])
    write(p, status(five=50.0, five_resets=t1, seven=30.0), t1 - 60)
    assert wire(bus)["session_pct"] == 50.0

    write(p, status(seven=30.0), t1 + 60)
    clock[0] = t1 + 70
    msg = wire(bus)

    assert msg["session_pct"] == 0.0
    assert msg["src"] == "cli"
    assert bus.claude_page().no_session_window is False


def test_a_pro_or_max_reset_seen_by_a_fresh_process_reads_as_ultra(tmp_path):
    """The one shape the rule cannot tell apart, accepted and pinned so it
    is a decision rather than an accident: with no memory, the post-reset
    payload is byte for byte an Ultra one. The next reply puts it right."""
    p = write(tmp_path / "statusline.json", status(seven=30.0), NOW + 60)
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: NOW + 70)

    assert wire(bus)["session_pct"] == -1
    assert bus.claude_page().no_session_window is True

    write(p, status(five=1.0, five_resets=NOW + 5 * 3600, seven=30.0),
          NOW + 65)
    assert wire(bus)["session_pct"] == 1.0
    assert bus.claude_page().no_session_window is False


def test_an_idle_repaint_after_a_reset_is_still_the_same_fetch(tmp_path):
    """An idle Claude Code repaints the same post-reset block for as long as
    it stays open. Judged by the file's mtime, a Pro or Max desk working in
    Claude Desktop would lose Desktop's figure five hours and ten minutes
    after its last Claude Code reply; judged by when the block was fetched,
    it never does."""
    t1 = NOW - 8 * 3600
    p = tmp_path / "statusline.json"
    desk = tmp_path / "plan-usage-history.json"
    clock = [t1 - 59]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   ClaudeDesktopProvider(path=str(desk))],
        now=lambda: clock[0])
    desktop_doc(desk, [(t1 - 300, 49, 30)])
    write(p, status(five=50.0, five_resets=t1, seven=30.0), t1 - 60)
    wire(bus)

    for hour in range(1, 9):
        at = t1 + hour * 3600 - 2
        write(p, status(seven=30.0), at)            # the same block, repainted
        desktop_doc(desk, [(t1 - 300, 49, 30), (at - 60, 3 * hour, 31)])
        clock[0] = at + 1
        msg = wire(bus)
        assert (msg["session_pct"], msg["src"]) == (3.0 * hour, "desktop"), \
            hour


def test_a_window_seen_longer_ago_than_a_window_lasts_is_no_evidence(
        tmp_path):
    """The bound, at its edge. The window existed until its reset; a
    weekly-only block fetched within five hours and ten minutes of that is
    a Pro or Max rollover, and one fetched after it is an account that no
    longer has a window."""
    for gap, marked in ((BOUND - 5, False), (BOUND + 5, True)):
        p = tmp_path / f"statusline-{gap}.json"
        prov = ClaudeCliProvider(path=str(p))
        write(p, status(five=40.0, five_resets=NOW - 3600), NOW - 7200)
        prov.poll(NOW - 7199)

        write(p, status(seven=3.0), NOW - 3600 + gap)
        frames = prov.poll(NOW - 3600 + gap + 1)

        assert frames[0].no_session_window is marked, gap


def test_the_last_sighting_is_the_windows_reset_not_the_payloads_mtime(
        tmp_path):
    """Scenario G of the review's altitude probe. A Pro or Max window read
    once when it opened and never again: its reset is five hours after the
    only payload that carried it, and measured from that payload the
    weekly-only render after the reset read as "nothing seen for six hours",
    which blanked Desktop's correct post-reset 8%."""
    p = tmp_path / "statusline.json"
    clock = [NOW - 6 * 3600 + 1]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 1200, session=8, weekly=31)],
        now=lambda: clock[0])
    write(p, status(five=60.0, five_resets=NOW - 3600, seven=30.0),
          NOW - 6 * 3600)
    wire(bus)

    write(p, status(seven=31.0), NOW - 1)
    clock[0] = NOW
    msg = wire(bus)

    assert (msg["session_pct"], msg["src"]) == (8.0, "desktop")
    assert bus.claude_page().no_session_window is False


def test_a_far_future_reset_cannot_hold_the_rule_off(tmp_path):
    """A window cannot outlast its own length, so a stamp years ahead counts
    as five hours past the payload that carried it and no more."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, status(five=1.0, five_resets=NOW + 20 * 86400), NOW - 2 * 86400)
    prov.poll(NOW - 2 * 86400 + 1)

    write(p, weekly_only(2.0), NOW - 1)
    assert prov.poll(NOW)[0].no_session_window is True


def test_a_window_that_has_not_ended_is_not_a_missing_window(tmp_path):
    """Scenario I. A remembered five_hour whose resets_at is still ahead is
    proof this account has a window, so a render without it (Claude Code
    leaves a window out until a session's first response) must not blank
    the dial."""
    p = tmp_path / "statusline.json"
    clock = [NOW - 600]
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: clock[0])
    write(p, payload(five_hour=27.0, resets_at=NOW + 3600), NOW - 600)
    assert wire(bus)["session_pct"] == 27.0

    write(p, weekly_only(2.0), NOW - 1)
    clock[0] = NOW
    msg = wire(bus)

    assert msg["session_pct"] == 27.0      # remembered, as before
    assert msg["stale"] is False
    assert msg["age_s"] == 600             # the remembered reading's own age


@pytest.mark.parametrize("five_hour, marked", [
    (None, True),                 # "five_hour": null
    ("soon", True),               # not an object
    ([1, 2], True),
    ({}, False),                  # an object: a window with no figure
    ({"resets_at": NOW + 600}, False),
], ids=["null", "string", "list", "empty object", "no percentage"])
def test_a_window_is_an_object_under_the_key_and_nothing_else(
        tmp_path, five_hour, marked):
    """Finding 7. The rule asked `"five_hour" in limits` while the mapping
    treats null and non-objects as no window, so `"five_hour": null` was a
    window to the rule and not to the dial. One definition now
    (statusline_source.window_present). An object with no percentage is the
    likeliest shape of a payload change, and still a window."""
    p = write(tmp_path / "statusline.json",
              {"rate_limits": {"five_hour": five_hour,
                               "seven_day": {"used_percentage": 2.0}}},
              NOW - 1)

    assert ClaudeCliProvider(path=str(p)).poll(NOW)[0].no_session_window \
        is marked


def test_a_payload_without_a_usable_weekly_figure_is_not_marked(tmp_path):
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, {"rate_limits": {}}, NOW - 2)
    assert [f.no_session_window for f in prov.poll(NOW - 1)] == [False]

    nan = float("nan")
    write(p, {"rate_limits": {"seven_day": {"used_percentage": nan}}}, NOW - 1)
    assert [f.no_session_window for f in prov.poll(NOW)] == [False]


def test_the_weekly_figure_is_judged_at_the_payloads_own_time(tmp_path):
    """Finding 8. The weekly figure was judged usable at the poll's clock
    while the window was judged at the payload's. A weekly-only payload an
    hour old whose seven-day reset has passed since it was written was a
    weekly figure when it was written, and still says the account has no
    five-hour window; at the poll's clock it settles to unknown, and the
    previous rule then did not mark it."""
    p = write(tmp_path / "statusline.json",
              status(seven=2.0, seven_resets=NOW - 1800), NOW - 3600)

    frames = ClaudeCliProvider(path=str(p)).poll(NOW)

    assert frames[0].weekly_pct == base.UNKNOWN     # settled at the poll
    assert frames[0].no_session_window is True      # judged at the payload


@pytest.mark.parametrize("resets_at", [
    None, "2026-08-25T12:00:00Z", int((NOW - BOUND + 3600) * 1000)],
    ids=["null", "string", "milliseconds"])
def test_an_unstamped_window_is_lent_no_longer_than_a_window_lasts(
        tmp_path, resets_at):
    """Finding 9. A five_hour whose resets_at will not read never ends by
    settle_window, so the remembered payload lent its figure to the dial for
    as long as the daemon ran, with an age that only grew. Bounded as Claude
    Desktop's stampless samples are."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    fetched = NOW - BOUND
    write(p, {"rate_limits": {"five_hour": {"used_percentage": 27.0,
                                            "resets_at": resets_at},
                              "seven_day": {"used_percentage": 2.0}}},
          fetched)
    prov.poll(fetched + 1)
    write(p, weekly_only(2.0), fetched + 60)

    inside = prov.poll(NOW - 5)
    assert [f.session_pct for f in inside if f.session_pct >= 0] == [27.0]

    outside = prov.poll(NOW + 5)
    assert [f.session_pct for f in outside if f.session_pct >= 0] == []
    assert not any(f.no_session_window for f in outside)


# --- Claude Desktop's bound (findings 10, 14) --------------------------------


def test_a_desktop_session_older_than_a_window_is_retired(tmp_path):
    """A window open when the sample was taken has ended five hours later,
    and the cache has no stamps to say so. This is also the restarted
    daemon, which has no memory of the account."""
    inside = desktop_at(tmp_path, BOUND - 5, session=24).poll(NOW)[0]
    assert inside.session_pct == 24.0
    assert inside.stale is True

    outside = desktop_at(tmp_path, BOUND + 5, session=24).poll(NOW)[0]
    assert outside.session_pct == base.UNKNOWN
    assert outside.weekly_pct == 2.0               # lasts seven days


def test_a_retired_desktop_session_carries_no_rate(tmp_path):
    """A rising series all of it past the bound. The rate goes because
    session_burn_pph refuses a series whose newest sample is older than
    BURN_MAX_GAP_S; with one sample, as this test first had it, there was
    no rate to refuse and the assertion could not fail."""
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - BOUND - 610 + i * 300, 10 + 5 * i, 2)
                     for i in range(3)])

    frame = ClaudeDesktopProvider(path=str(f)).poll(NOW)[0]

    assert frame.session_pct == base.UNKNOWN
    assert frame.session_burn_pph is None


def test_a_retired_desktop_sample_does_not_reach_the_wire(tmp_path):
    """The measured 37 hours, with no status line on the bus at all."""
    bus = ingest.IngestionBus(
        providers=[desktop_at(tmp_path, 136136, session=1, weekly=2)],
        now=lambda: NOW)

    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["weekly_pct"] == 2.0


def test_a_sample_whose_only_figure_retired_gives_way_to_an_older_one(
        tmp_path):
    """Finding 10. The retire ran after the test for a sample with neither
    percentage, so a newest sample carrying fh and no sd was chosen and then
    emptied: no usage at all, the weekly figure in the sample before it was
    never offered, and a Desktop-only bus sent nothing."""
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - 7 * 3600, 20, 30), (NOW - 6 * 3600, 30, None)])
    desktop = ClaudeDesktopProvider(path=str(f))

    frame = desktop.poll(NOW)[0]
    assert (frame.session_pct, frame.weekly_pct) == (base.UNKNOWN, 30.0)
    assert frame.observed_at == NOW - 7 * 3600

    msg = wire(ingest.IngestionBus(providers=[desktop], now=lambda: NOW))
    assert (msg["session_pct"], msg["weekly_pct"]) == (-1, 30.0)


def test_a_cache_whose_only_figures_retired_is_still_read(tmp_path):
    """Nothing in it is offered, but it is the app's own file in its own
    layout: `blink status` must say it was read, not that its format may
    have changed."""
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - 6 * 3600, 30, None)])

    verdict, frame = ClaudeDesktopProvider(path=str(f)).verdict(NOW)

    assert verdict == VERDICT_FRAME
    assert frame.observed_at == NOW - 6 * 3600
    assert not frame.has_usage()


def test_a_cache_whose_only_figures_retired_is_reduced_once(tmp_path,
                                                           monkeypatch):
    """It was parsed a second time, without the clock, for a result that
    could only ever be both figures unknown at the newest sample's time
    (review, 2026-10-31). Each sample is now read once for the reading and
    once for the rate's series, which judges age its own way."""
    from pc.providers import claude_desktop
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - 7 * 3600, 20, None), (NOW - 6 * 3600, 30, None)])
    calls = []
    real = claude_desktop._sample_to_frame

    def counting(sample, *args):
        calls.append(sample)
        return real(sample, *args)
    monkeypatch.setattr(claude_desktop, "_sample_to_frame", counting)

    frame = ClaudeDesktopProvider(path=str(f)).poll(NOW)[0]

    assert (frame.session_pct, frame.weekly_pct) == (base.UNKNOWN,
                                                     base.UNKNOWN)
    assert frame.observed_at == NOW - 6 * 3600
    assert len(calls) == 2 * 2


# --- merge: what voids the mark (findings 3, 4, 13) --------------------------


def test_a_desktop_window_with_a_reset_ahead_proves_the_account_has_one(
        tmp_path):
    """Scenario G3. The rule cannot tell an Ultra account from a Pro or Max
    one read by a fresh process after a reset. Claude Desktop's Local
    Storage can: resetsAt in the future is the server saying a window is
    open, and that voids the mark."""
    ls = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path / "none"))
    live_window = ls.record_to_frame(
        {"resetsAt": NOW + 3000, "utilization": 0.40,
         "observedAt": NOW - 300}, NOW)
    assert live_window.session_resets_at == NOW + 3000

    merged = normalizer.merge([cli_frame(at=NOW - 1), live_window])

    assert merged.session_pct == 40.0
    assert merged.session_resets_at == NOW + 3000
    assert merged.src == "desktop_ls"
    assert merged.no_session_window is False


def test_proof_of_a_window_lets_newer_readings_compete_as_usual(tmp_path):
    """Finding 3. A stamp voided the mark but only the stamped source could
    fill the dial, so a Local Storage record from fifty minutes ago took it
    back from a Desktop sample taken a minute ago. Proven, the merge is the
    one it would have been with no mark at all."""
    ls = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path / "none"))
    old_window = ls.record_to_frame(
        {"resetsAt": NOW + 3000, "utilization": 0.40,
         "observedAt": NOW - 3000}, NOW)
    newer = desktop_frame(NOW - 60, session=46.0)
    frames = [cli_frame(at=NOW - 1), old_window, newer]

    merged = normalizer.merge(frames)
    unmarked = normalizer.merge([cli_frame(at=NOW - 1, flagged=False),
                                 old_window, newer])

    assert merged.session_pct == 46.0
    assert merged.session_resets_at == NOW + 3000
    assert merged.no_session_window is False
    assert (merged.session_pct, merged.src, merged.observed_at,
            merged.session_resets_at) == (
        unmarked.session_pct, unmarked.src, unmarked.observed_at,
        unmarked.session_resets_at)


def test_a_reading_newer_than_the_mark_is_not_proof():
    """Finding 4. A reading observed after the newest marked frame lifted
    the veto, so any Desktop sample taken after the last repaint put a
    window the account does not have on the dial (scenario D5)."""
    merged = normalizer.merge([cli_frame(at=NOW - 600),
                               desktop_frame(NOW - 60, session=1.0)])

    assert merged.session_pct == base.UNKNOWN
    assert merged.no_session_window is True


def test_a_desktop_reading_without_a_reset_is_not_proof():
    """The plan-usage-history cache has no stamps, and a stamp is the proof."""
    merged = normalizer.merge([cli_frame(at=NOW - 1),
                               desktop_frame(NOW - 600, session=40.0)])

    assert merged.session_pct == base.UNKNOWN
    assert merged.session_resets_at is None
    assert merged.no_session_window is True


def test_a_window_that_ended_is_not_proof(tmp_path):
    """A record whose resetsAt has passed publishes no number and no stamp
    (it is evidence of a rollover), so it cannot lift the mark."""
    ls = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path / "none"))
    ended = ls.record_to_frame(
        {"resetsAt": NOW - 60, "utilization": 0.40,
         "observedAt": NOW - 3000}, NOW)

    merged = normalizer.merge([cli_frame(at=NOW - 1), ended])

    assert merged.session_pct == base.UNKNOWN
    assert merged.no_session_window is True


def test_the_merged_mark_is_what_the_merge_decided(tmp_path):
    """Finding 13. The merged frame recorded whether anything passed the
    veto, not what was picked. A proven window with no readable figure is
    still a window: the dial is unknown, and the burn view must not take its
    place."""
    ls = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path / "none"))
    unreadable = ls.record_to_frame(
        {"resetsAt": NOW + 3000, "utilization": 99.0,     # 9900%: refused
         "observedAt": NOW - 300}, NOW)
    assert unreadable.session_pct == base.UNKNOWN
    assert unreadable.session_resets_at == NOW + 3000

    merged = normalizer.merge([cli_frame(at=NOW - 1), unreadable])

    assert merged.session_pct == base.UNKNOWN
    assert merged.session_resets_at == NOW + 3000
    assert merged.no_session_window is False


def test_a_marked_frame_cannot_fill_the_dial_itself():
    """A marked frame cannot vouch against its own mark. The provider cannot
    produce this frame today (a payload with a window is never marked), and
    merge() is the contract regardless of who calls it."""
    remembered = cli_frame(at=NOW - 3600, session=40.0, weekly=2.0,
                           session_resets_at=NOW + 600)

    merged = normalizer.merge([cli_frame(at=NOW - 1), remembered])

    assert merged.session_pct == base.UNKNOWN
    assert merged.session_resets_at is None
    assert merged.src == "cli"
    assert protocol.frame_to_usage(merged, NOW)["session_resets_in_s"] == -1


def test_the_burn_rate_is_not_carried_onto_an_unknown_dial():
    """A rate off the vetoed Desktop sample would describe a window the
    account does not have."""
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


def test_the_mark_does_not_cross_providers_on_the_wire():
    """Codex has its own account and its own five-hour window."""
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


def test_the_mark_survives_a_second_merge():
    cli = cli_frame(at=NOW - 1)
    older = desktop_frame(NOW - 500, session=40.0)

    once = normalizer.merge([cli, older])
    twice = normalizer.merge([once, older])

    assert once.session_pct == base.UNKNOWN
    assert twice.session_pct == base.UNKNOWN
    assert twice.no_session_window is True
    assert twice.src == "cli"


# --- the reviewer's altitude probe, every scenario ---------------------------
#
# Nineteen desks, each a sequence of (status line, its mtime, poll at) with a
# Claude Desktop cache or Local Storage record beside it. The expected line
# is what this rule draws; the commit that added the table lists what the
# merge-base and the latch drew for each.

W = status(seven=2.0)
DK_MEASURED = [(NOW - 136136, 1, 2)]
TORN, GONE = "torn", "gone"

SCENARIOS = {
    # Ultra, the measured inputs.
    "A measured": dict(
        steps=[(W, NOW - 1, NOW)], desktop=DK_MEASURED,
        want=[(-1, "cli", False, 1)], marked=True),
    "B restart, payload 40 min old": dict(
        steps=[(W, NOW - 2400, NOW)], desktop=DK_MEASURED,
        want=[(-1, "cli", True, 2400)], marked=True),
    "C two hours idle": dict(
        steps=[(W, NOW - 1, NOW), (None, None, NOW + 7200)],
        desktop=DK_MEASURED,
        want=[(-1, "cli", False, 1), (-1, "cli", True, 7201)], marked=True),
    # Ultra, a Desktop sample inside its own bound.
    "D1 Desktop 2 h old": dict(
        steps=[(W, NOW - 1, NOW)], desktop=[(NOW - 7200, 1, 2)],
        want=[(-1, "cli", False, 1)], marked=True),
    "D2 Desktop 5 h old": dict(
        steps=[(W, NOW - 1, NOW)], desktop=[(NOW - 18000, 1, 2)],
        want=[(-1, "cli", False, 1)], marked=True),
    "D3 restart, Desktop 2 h old": dict(
        steps=[(W, NOW - 2400, NOW)], desktop=[(NOW - 7200, 1, 2)],
        want=[(-1, "cli", True, 2400)], marked=True),
    # Desktop's weekly figure is the newest, so it names the frame; its
    # session figure is not borrowed.
    "D4 Desktop sampled between two polls": dict(
        steps=[(W, NOW - 1, NOW, DK_MEASURED),
               (None, None, NOW + 7200, DK_MEASURED + [(NOW + 3600, 1, 2)])],
        desktop=DK_MEASURED,
        want=[(-1, "cli", False, 1), (-1, "desktop", True, 3600)],
        marked=True),
    "D5 Desktop newer than the render": dict(
        steps=[(W, NOW - 600, NOW)], desktop=[(NOW - 60, 1, 2)],
        want=[(-1, "desktop", False, 60)], marked=True),
    "D6 a rising Desktop series": dict(
        steps=[(W, NOW - 1, NOW)],
        desktop=[(NOW - 900 + i * 300, 10 + 5 * i, 2) for i in range(3)],
        want=[(-1, "cli", False, 1)], marked=True),
    # Renders with no rate_limits.
    "E alternating renders, Desktop 2 h old": dict(
        steps=[(W, NOW - 7, NOW - 6), (NO_LIMITS, NOW - 5, NOW - 4),
               (W, NOW - 3, NOW - 2), (NO_LIMITS, NOW - 1, NOW)],
        desktop=[(NOW - 7200, 1, 2)],
        want=[(-1, "cli", False, 1), (-1, "cli", False, 3)] * 2,
        marked=True, pushes=1),
    "E2 alternating renders, Desktop 37 h old": dict(
        steps=[(W, NOW - 7, NOW - 6), (NO_LIMITS, NOW - 5, NOW - 4),
               (W, NOW - 3, NOW - 2), (NO_LIMITS, NOW - 1, NOW)],
        desktop=DK_MEASURED,
        want=[(-1, "cli", False, 1), (-1, "cli", False, 3)] * 2,
        marked=True, pushes=1),
    # Pro and Max across a reset.
    "F reset five minutes ago": dict(
        steps=[(status(five=55.0, five_resets=NOW - 300, seven=30.0),
                NOW - 600, NOW - 599), (status(seven=30.0), NOW - 1, NOW)],
        want=[(55.0, "cli", False, 1), (0.0, "cli", False, 600)],
        marked=False),
    "G reset an hour ago unwatched, Desktop after it": dict(
        steps=[(status(five=60.0, five_resets=NOW - 3600, seven=30.0),
                NOW - 6 * 3600, NOW - 6 * 3600 + 1),
               (status(seven=31.0), NOW - 1, NOW, [(NOW - 1200, 8, 31)])],
        desktop=[],
        want=[(60.0, "cli", False, 1), (8.0, "desktop", False, 1200)],
        marked=False),
    # The accepted ambiguity: no memory.
    "G2 fresh process after a reset": dict(
        steps=[(status(seven=31.0), NOW - 1, NOW)],
        desktop=[(NOW - 1200, 8, 31)],
        want=[(-1, "cli", False, 1)], marked=True),
    "G3 fresh process, Local Storage window open": dict(
        steps=[(status(seven=31.0), NOW - 1, NOW)],
        ls={"resetsAt": NOW + 4 * 3600, "utilization": 0.08,
            "observedAt": NOW - 300},
        want=[(8.0, "desktop_ls", False, 300)], marked=False),
    # An account losing its window inside a running daemon.
    "H remembered window ended ten minutes ago": dict(
        steps=[(status(five=40.0, five_resets=NOW - 600), NOW - 1200,
                NOW - 1199), (W, NOW - 1, NOW)],
        want=[(40.0, "cli", False, 1), (0.0, "cli", False, 1200)],
        marked=False),
    "H2 same, two hours, Desktop after the reset": dict(
        steps=[(status(five=40.0, five_resets=NOW - 3600), NOW - 7200,
                NOW - 7199), (W, NOW - 1, NOW, [(NOW - 1800, 3, 2)])],
        desktop=[],
        want=[(40.0, "cli", False, 1), (3.0, "desktop", False, 1800)],
        marked=False),
    "I remembered window still open": dict(
        steps=[(status(five=27.0, five_resets=NOW + 3600), NOW - 600,
                NOW - 599), (W, NOW - 1, NOW)],
        want=[(27.0, "cli", False, 1), (27.0, "cli", False, 600)],
        marked=False),
    "U torn write, then the file gone": dict(
        steps=[(W, NOW - 5, NOW - 4), (TORN, None, NOW - 2),
               (GONE, None, NOW)],
        desktop=[(NOW - 7200, 1, 2)],
        want=[(-1, "cli", False, 1), (-1, "cli", False, 3),
              (-1, "cli", False, 5)], marked=True, pushes=1),
}


@pytest.mark.parametrize("name", list(SCENARIOS))
def test_the_altitude_scenarios(tmp_path, name):
    case = SCENARIOS[name]
    p = tmp_path / "statusline.json"
    desk = tmp_path / "plan-usage-history.json"
    providers = [ClaudeCliProvider(path=str(p))]
    if "desktop" in case:
        desktop_doc(desk, case["desktop"])
        providers.append(ClaudeDesktopProvider(path=str(desk)))
    if "ls" in case:
        providers.append(LocalStorage(tmp_path, case["ls"]))
    clock = [0.0]
    bus = ingest.IngestionBus(providers=providers, now=lambda: clock[0])

    got, pushes, last = [], 0, None
    for step in case["steps"]:
        doc, mtime, at = step[:3]
        if len(step) > 3:
            desktop_doc(desk, step[3])
        if doc == TORN:
            p.write_text("{", encoding="utf-8")
        elif doc == GONE:
            p.unlink()
        elif doc is not None:
            write(p, doc, mtime)
        clock[0] = at
        msg = wire(bus)
        got.append(brief(msg))
        meaningful = protocol.meaningful_usage(msg)
        pushes += meaningful != last
        last = meaningful

    assert got == case["want"]
    assert bus.claude_page().no_session_window is case["marked"]
    if "pushes" in case:
        assert pushes == case["pushes"]
    if name.startswith("D6"):
        assert "burn_pph" not in msg


# --- the burn view: sent on Ultra, not on Pro or Max after a reset -----------


def _burn_desk(tmp_path, docs):
    """A real bus (status line + transcript reader) feeding a real Bridge
    whose board lists "burn". `docs` is [(status line, mtime)], each polled
    in turn; returns the burn lines the bridge put on the wire."""
    from pc import burn_reader
    from tests.pc import test_burn_line as bl
    bl.transcripts(tmp_path, [0] * 18 + [1500, 260_000])
    p = tmp_path / "statusline.json"
    clock = [0.0]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p))], now=lambda: clock[0],
        burn=burn_reader.BurnReader(root=str(tmp_path / ".claude"
                                             / "projects")))
    sent = []
    bridge_clock = [1000.0]
    bridge = Bridge(write_msg=sent.append, fetch_usage=bus.fetch(),
                    now=lambda: bridge_clock[0],
                    wall=lambda: (1752444000, 0),
                    fetch_manifest=lambda: None,
                    fetch_signed_manifest=lambda: None)
    bridge.on_message({"t": "hello", "v": 2, "board_id": "ab",
                       "fw": "1.4.0",
                       "caps": ["svc", "theme", "edit_done", "burn"]})
    for doc, mtime in docs:
        write(p, doc, mtime)
        clock[0] = mtime + 1
        bridge_clock[0] += THEME_QUIET_S + 30
        bridge.poll_if_changed()
    return bus, [m for m in sent if m.get("t") == "burn"]


def test_an_ultra_desk_is_sent_its_burn_line(tmp_path):
    from tests.pc import test_burn_line as bl
    bus, burns = _burn_desk(tmp_path, [(weekly_only(12.0), bl.NOW - 1)])

    assert burns and burns[-1]["p"] == "claude"
    assert bus.claude_page().no_session_window is True


def test_a_pro_or_max_desk_after_a_reset_is_sent_no_burn_line(tmp_path):
    """Finding 1 at the board: under the latch the post-reset render turned
    the session gauge into the burn view on every Pro and Max desk."""
    from tests.pc import test_burn_line as bl
    t1 = bl.NOW - 120
    bus, burns = _burn_desk(tmp_path, [
        (status(five=50.0, five_resets=t1, seven=30.0), t1 - 60),
        (status(seven=30.0), t1 + 1),
        (status(seven=30.0), t1 + 61)])

    assert burns == []
    assert bus.claude_page().no_session_window is False
    assert bus.claude_page().session_pct == 0.0
