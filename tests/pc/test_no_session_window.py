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

The rule is positive evidence (pc/session_verdict). The account is marked
only on proof it has no window -- a Claude Desktop sample with a weekly
figure and no `fh` key, or a Claude Code reply that came back without a
five-hour window -- and the mark goes on proof it has one. Quiet data is
never proof: the latch first, and then a rule that read five hours of
weekly-only status line as Ultra, held "--%" on Pro and Max desks after a
reset (review, 2026-10-31, findings 1, 2, 3, 5 and 10). The verdict is
persisted, so a restart and `blink status` say what the daemon says.
normalizer.merge then borrows nothing for the session. The tests below name
the review finding each one pins.

Every payload here is generated. None is captured from a machine. The
payload and write helpers are test_claude_cli's, the Desktop cache is
tests/support/desktop_cache, and the transcripts are tests/support/
transcripts through test_burn_line.
"""
import json
import os

import pytest

from pc import ingest, normalizer, protocol, session_verdict
from pc.bridge import THEME_QUIET_S, Bridge
from pc.providers import base, claude_cli
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import (VERDICT_FRAME,
                                         ClaudeDesktopProvider)
from pc.providers.claude_desktop_ls import ClaudeDesktopLocalStorageProvider
from tests.pc.test_claude_cli import payload, write
from tests.support import desktop_cache as dc
from tests.support.home import inside

NOW = dc.NOW
WEEK_END = NOW + 3 * 86400
# The age past which a stampless session figure is no longer offered.
BOUND = base.SESSION_WINDOW_S + base.SESSION_SLACK_S


def weekly_only(weekly=2.0):
    """The Ultra shape: seven_day, and no five_hour key whatever."""
    return payload(five_hour=None, seven_day=weekly)


def status(five=None, five_resets=None, seven=2.0, seven_resets=WEEK_END,
           session="s", cost=1.0):
    """A status line as Claude Code writes it, with both reset stamps, the
    session it belongs to and that session's running cost."""
    limits = {}
    if seven is not None:
        limits["seven_day"] = {"used_percentage": seven,
                               "resets_at": seven_resets}
    if five is not None:
        limits["five_hour"] = {"used_percentage": five,
                               "resets_at": five_resets}
    return {"session_id": session, "model": {"id": "m"},
            "cost": {"total_cost_usd": cost, "total_duration_ms": 1000,
                     "total_api_duration_ms": 900},
            "rate_limits": limits}


def replied(poll, p, at, seven=2.0, session="s", limits=None):
    """Claude Code's status line through two replies that came back with no
    five-hour window, each render read by `poll(now)` a second after it is
    written: the same session, weekly-only throughout and repainted every
    five minutes, its cost risen at a first reply and again at a second,
    claude_cli.REPLY_APART_S later, written at `at`. One reply is not proof
    (claude_cli._reply_without_window). Returns what the last poll
    returned. `limits` replaces every render's rate_limits."""
    first = at - claude_cli.REPLY_APART_S
    renders = [(1.0, first - 30), (1.25, first)]
    renders += [(1.25, t) for t in range(int(first) + 300, int(at), 300)]
    renders += [(1.50, at)]
    for cost, when in renders:
        doc = status(seven=seven, session=session, cost=cost)
        if limits is not None:
            doc["rate_limits"] = limits
        write(p, doc, when)
        got = poll(when + 1)
    return got


def no_mark_on_disk():
    """True unless the verdict on disk says the account has no window."""
    ev = session_verdict.load(session_verdict.verdict_path())
    return ev is None or ev[0] is True


def ticking(bus, clock):
    """`bus.poll` as a poll(now): sets the bus's clock first."""
    def poll(now):
        clock[0] = now
        return wire(bus)
    return poll


def proved(home, has_window=False, at=NOW - 60,
           kind=session_verdict.KIND_STATUSLINE_REPLY):
    """A verdict on disk where a daemon would have left it, under `home`
    (tests/support/home.py)."""
    session_verdict.save(inside(home, session_verdict.verdict_path()),
                         session_verdict.evidence(has_window, kind, at))


# A render with no rate_limits at all: a session that has just started or
# been resumed, before its first reply.
NO_LIMITS = {"session_id": "s", "model": {"id": "m"}}

# A Claude Desktop sample with no `fh` key at all, which is what this desk's
# verified Ultra account wrote on 2026-10-31 (pc/session_verdict).
ABSENT = "absent"


def desktop_doc(path, samples):
    """samples: [(epoch_s, fh, sd)]; fh ABSENT leaves the key out."""
    rows = []
    for t, fh, sd in samples:
        s = dc.sample(int(t * 1000), fh=fh, sd=sd)
        if fh is ABSENT:
            del s["u"]["fh"]
        rows.append(s)
    path.write_text(dc.doc(rows), encoding="utf-8")
    return path


def desktop_at(tmp_path, age_s, session=1, weekly=2):
    """A real Desktop provider over a generated cache whose newest sample is
    `age_s` old. `session` ABSENT is the measured Ultra shape."""
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
    """The measured wire line, reproduced and then inverted, on the measured
    Desktop history: a sample from before the change with `fh`, two days
    old, and the newest after it with `sd` and no `fh` key."""
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - 136136, 1, 2), (NOW - 3600, ABSENT, 2)])
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   ClaudeDesktopProvider(path=str(f))],
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
    """A Desktop cache with a rising series carries a burn rate, and once a
    reply has proved there is no window, the session it would describe is
    not this account's. The reply is newer than every sample, so it is the
    evidence that stands."""
    f = tmp_path / "plan-usage-history.json"
    # Three samples five minutes apart, the newest still current: the shape
    # that earns a rate (BURN_MAX_GAP_S, BURN_MIN_SPAN_S).
    f.write_text(dc.doc([dc.sample(int((NOW - 900 + i * 300) * 1000),
                                   fh=10 + 5 * i, sd=2) for i in range(3)]),
                 encoding="utf-8")
    desktop = ClaudeDesktopProvider(path=str(f))
    assert desktop.poll(NOW)[0].session_burn_pph is not None, \
        "the fixture must carry a rate or this proves nothing"
    p = tmp_path / "statusline.json"
    clock = [0.0]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)), desktop],
        now=lambda: clock[0])
    msg = replied(ticking(bus, clock), p, NOW - 1)

    assert msg["session_pct"] == -1
    assert msg.get("burn_pph") is None
    assert bus.claude_page().no_session_window is True


def test_five_hour_coming_back_behaves_as_before(tmp_path):
    """An unverified account, or a policy reversal: a status line with a
    window is newer evidence than the Desktop sample that set the mark. The
    window is one that opened after that sample, as a reversal's would: a
    block is dated by its window's start (claude_cli._window_dated)."""
    p = tmp_path / "statusline.json"
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=ABSENT)],
        now=lambda: NOW)

    write(p, weekly_only(2.0), NOW - 10)
    assert wire(bus)["session_pct"] == -1
    assert bus.claude_page().no_session_window is True

    opened = NOW - 600
    write(p, payload(five_hour=31.0,
                     resets_at=opened + base.SESSION_WINDOW_S), NOW - 1)
    msg = wire(bus)
    assert msg["session_pct"] == 31.0
    assert msg["session_resets_in_s"] == base.SESSION_WINDOW_S - 600
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
    assert bus.claude_page().no_session_window is False


# --- Ultra in its steady state ----------------------------------------------


@pytest.mark.parametrize("doc", [
    status(seven=24.0),
    status(five=40.0, five_resets=NOW + 3600, seven=24.0)],
    ids=["no window", "pro or max, session held still"])
def test_the_wire_ages_the_status_line_not_the_figure(tmp_path, doc):
    """first_seen_at dates a window's evidence, and that must not leak into
    the reading's age. A weekly percentage holds still for many minutes on
    an Ultra desk -- one point of a weekly cap is a lot of work -- while
    Claude Code repaints the line every few seconds; aged by the figure,
    the board would draw "20m ago" over live data from AGE_CAPTION_MIN_S
    on. The age is the file's, as it is for a Pro or Max desk whose session
    percentage holds still, on a running daemon and on a fresh process
    alike (questioned on the owner's desk, 2026-10-31)."""
    p = tmp_path / "statusline.json"
    clock = [NOW - 1200]
    weekly = ABSENT if "five_hour" not in doc["rate_limits"] else 1

    def bus():
        return ingest.IngestionBus(
            providers=[ClaudeCliProvider(path=str(p)),
                       desktop_at(tmp_path, 4000, session=weekly,
                                  weekly=24)],
            now=lambda: clock[0])
    running = bus()
    for at in (NOW - 1200, NOW - 600, NOW - 60, NOW - 1):
        write(p, doc, at)                       # the same block, repainted
        clock[0] = at + 0.5
        wire(running)
    clock[0] = NOW

    for msg in (wire(running), wire(bus())):
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
                   desktop_at(tmp_path, 136136, session=1)],
        now=lambda: clock[0])
    assert replied(ticking(bus, clock), p, NOW - 1)["session_pct"] == -1
    assert bus.claude_page().no_session_window is True

    clock[0] = NOW + 2 * 3600
    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["src"] == "cli"
    assert msg["stale"] is True
    assert msg["age_s"] == 2 * 3600 + 1


def test_a_restarted_daemon_reads_its_verdict_back(tmp_path):
    """Finding 5's daemon half. Before the verdict was persisted, a fresh
    process had no memory, and on an Ultra desk with nothing but a status
    line the dial and the burn view waited for the next reply. The first
    bus learns it from a reply and writes it; the second, a restart, reads
    a 40-minute-old weekly-only payload and needs no evidence of its own."""
    p = tmp_path / "statusline.json"
    clock = [0.0]
    first = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                                now=lambda: clock[0])
    assert replied(ticking(first, clock), p, NOW - 2400)["session_pct"] == -1
    assert first.claude_page().no_session_window is True
    on_disk = session_verdict.load(session_verdict.verdict_path())
    assert on_disk == (False, session_verdict.KIND_STATUSLINE_REPLY,
                       NOW - 2400)

    restarted = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 136136, session=1)],
        now=lambda: NOW)
    msg = wire(restarted)

    assert msg["session_pct"] == -1
    assert msg["src"] == "cli"
    assert msg["age_s"] == 2400
    assert restarted.claude_page().no_session_window is True


def test_a_render_without_rate_limits_keeps_the_mark(tmp_path):
    """Finding 2's first half. A new or resumed session writes a status line
    with no rate_limits at all, and the last payload with a figure is offered
    beside it, so the weekly figure and the mark both stand."""
    proved(tmp_path)
    p = tmp_path / "statusline.json"
    clock = [NOW - 2]
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: clock[0])
    write(p, weekly_only(2.0), NOW - 3)
    assert wire(bus)["session_pct"] == -1

    write(p, NO_LIMITS, NOW - 1)
    clock[0] = NOW
    msg = wire(bus)

    assert (msg["session_pct"], msg["weekly_pct"]) == (-1, 2.0)
    assert bus.claude_page().no_session_window is True


def test_a_render_without_rate_limits_does_not_hand_the_frame_to_desktop(
        tmp_path):
    """Finding 2. The primary is whatever dates the weekly figure, and with
    no figure in the render a Desktop sample 37 hours old dated it: the
    measured line came back on every such render and went on the next, and
    each change was a push."""
    proved(tmp_path)
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
    """Finding 6. A torn write and a deleted file read as nothing, and an
    earlier rule then dropped the mark for that poll: the borrowed Desktop
    figure came back until the next render."""
    proved(tmp_path)
    p = tmp_path / "statusline.json"
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 7200, session=1)],
        now=lambda: NOW)
    write(p, weekly_only(2.0), NOW - 5)
    assert brief(wire(bus)) == (-1, "cli", False, 5)

    p.write_text("{", encoding="utf-8")          # a torn write
    assert brief(wire(bus)) == (-1, "cli", False, 5)

    p.unlink()
    assert brief(wire(bus)) == (-1, "cli", False, 5)
    assert bus.claude_page().no_session_window is True


# --- the evidence: what proves no window, and what proves one --------------


def evidence_of(frames):
    return [f.window_evidence for f in frames]


def test_a_reply_that_comes_back_without_a_window_is_proof(tmp_path):
    """On Pro and Max every API response carries the five-hour headers, so a
    reply brings five_hour back into the status line. The same session's
    cost rising between two weekly-only renders is that reply."""
    p = tmp_path / "statusline.json"
    frames = replied(ClaudeCliProvider(path=str(p)).poll, p, NOW - 1)

    assert evidence_of(frames)[0] == (
        False, session_verdict.KIND_STATUSLINE_REPLY, NOW - 1)


def test_the_weekly_figure_moving_alone_is_not_a_reply(tmp_path):
    """The seven-day figure is the account's, not the session's. Tested on
    it as well as on cost, a generated day of three terminals in which it
    moved for all of them at once marked a Pro desk on 6.4% of polls
    (2026-10-31); cost, which only the session's own responses move, marked
    none."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, status(seven=2.0, cost=1.0), NOW - 30)
    prov.poll(NOW - 29)
    write(p, status(seven=3.0, cost=1.0), NOW - 1)

    assert evidence_of(prov.poll(NOW)) == [None]


@pytest.mark.parametrize("second", [
    dict(seven=2.0, cost=1.0),                  # a repaint: nothing moved
    dict(seven=2.0, cost=0.5),                  # cost went down: a new count
    dict(seven=2.0, cost=1.25, session="other"),  # another session's reply
], ids=["repaint", "cost fell", "another session"])
def test_what_is_not_a_reply_proves_nothing(tmp_path, second):
    """A weekly-only status line is also every Pro and Max desk from a reset
    to its next reply, repainted every few seconds for as long as Claude
    Code stays open."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, status(seven=2.0, cost=1.0), NOW - 30)
    assert evidence_of(prov.poll(NOW - 29)) == [None]
    write(p, status(**second), NOW - 1)

    assert evidence_of(prov.poll(NOW)) == [None]


def test_two_terminals_painting_different_blocks_prove_nothing(tmp_path):
    """Finding 10. The status line is one file for every Claude Code on the
    desk. A terminal left open repaints its own pre-reset weekly-only block
    between renders of another whose reply carried a window; compared
    across sessions, every alternation looked like news. Compared within a
    session, neither terminal's own figures move."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    a = status(seven=10.0, session="a", cost=3.0)
    b = status(five=7.0, five_resets=NOW + 3600, seven=11.0, session="b",
               cost=9.0)
    got = []
    for i in range(8):
        write(p, (a, b)[i % 2], NOW - 40 + i * 5)
        got += [ev for ev in evidence_of(prov.poll(NOW - 39 + i * 5))
                if ev is not None and ev[0] is False]

    assert got == []


def test_a_pro_or_max_reply_brings_its_window_back_and_proves_it(tmp_path):
    """The other direction, on a desk that has just reset: the weekly-only
    render a second after the reset is no evidence, and the reply after it
    carries five_hour, which is."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, status(seven=30.0, cost=1.0), NOW - 60)
    assert evidence_of(prov.poll(NOW - 59)) == [None]

    write(p, status(five=1.0, five_resets=NOW + 5 * 3600, seven=30.0,
                    cost=1.25), NOW - 1)
    frames = prov.poll(NOW)

    assert frames[0].window_evidence == (
        True, session_verdict.KIND_STATUSLINE_WINDOW, NOW - 1)


@pytest.mark.parametrize("five_hour, proof", [
    (None, False),                # "five_hour": null
    ("soon", False),              # not an object
    ([1, 2], False),
    ({}, True),                   # an object: a window with no figure
    ({"resets_at": NOW + 600}, True),
], ids=["null", "string", "list", "empty object", "no percentage"])
def test_a_window_is_an_object_under_the_key_and_nothing_else(
        tmp_path, five_hour, proof):
    """Finding 7. A rule once asked `"five_hour" in limits` while the mapping
    treats null and non-objects as no window, so `"five_hour": null` was a
    window to one and not to the other. One definition
    (statusline_source.window_present). An object with no percentage is the
    likeliest shape of a payload change, and still a window."""
    p = tmp_path / "statusline.json"
    frames = replied(ClaudeCliProvider(path=str(p)).poll, p, NOW - 1,
                     limits={
        "five_hour": five_hour, "seven_day": {"used_percentage": 2.0}})

    assert frames[0].window_evidence[0] is proof


def test_a_payload_without_a_usable_weekly_figure_proves_nothing(tmp_path):
    p = tmp_path / "statusline.json"
    for limits in ({}, {"seven_day": {"used_percentage": float("nan")}}):
        frames = replied(ClaudeCliProvider(path=str(p)).poll, p, NOW - 1,
                         limits=limits)
        assert all(ev is None for ev in evidence_of(frames)), limits


def test_the_weekly_figure_is_judged_at_the_payloads_own_time(tmp_path):
    """Finding 8. A weekly-only reply an hour old whose seven-day reset has
    passed since it was written was a weekly figure when it was written,
    and still proves the account had no five-hour window then."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    frames = replied(lambda now: prov.poll(NOW), p, NOW - 3600, limits={
        "seven_day": {"used_percentage": 2.0, "resets_at": NOW - 1800}})

    assert frames[0].weekly_pct == base.UNKNOWN     # settled at the poll
    assert frames[0].window_evidence[0] is False    # judged at the payload


@pytest.mark.parametrize("fh, want", [
    (ABSENT, False), (0, True), (37, True), (None, None), (5000, None)],
    ids=["no fh key", "fh 0", "fh 37", "fh null", "fh out of range"])
def test_desktop_proves_by_its_fh_key(tmp_path, fh, want):
    """Measured on this desk's verified Ultra account, 2026-10-31: the
    newest sample after the change had `sd` and no `fh` key, and the samples
    before it had fh 0 and 1. Absent is the claim; null proves nothing."""
    frame = desktop_at(tmp_path, 600, session=fh, weekly=2).poll(NOW)[0]

    if want is None:
        assert frame.window_evidence is None
    else:
        assert frame.window_evidence == (
            want, session_verdict.KIND_DESKTOP_NO_FH if want is False
            else session_verdict.KIND_DESKTOP_FH, NOW - 600)


def test_desktop_with_no_weekly_figure_and_no_fh_proves_nothing(tmp_path):
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - 600, ABSENT, None), (NOW - 900, 3, 2)])

    frame = ClaudeDesktopProvider(path=str(f)).poll(NOW)[0]

    assert frame.window_evidence == (
        True, session_verdict.KIND_DESKTOP_FH, NOW - 900)


def test_local_storage_proves_a_window_by_a_reading_or_a_stamp(tmp_path):
    ls = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path / "none"))
    for rec in ({"utilization": 0.1, "observedAt": NOW - 60},
                {"utilization": 99.0, "resetsAt": NOW + 600,
                 "observedAt": NOW - 60}):
        assert ls.record_to_frame(rec, NOW).window_evidence == (
            True, session_verdict.KIND_LOCAL_STORAGE, NOW - 60)
    assert ls.record_to_frame({"utilization": 99.0, "observedAt": NOW - 60},
                              NOW).window_evidence is None


# --- the verdict: newest evidence wins, and it is kept ---------------------


def framed(ev):
    return base.NormalizedUsageFrame(provider="claude", src="cli",
                                     observed_at=NOW, window_evidence=ev)


def no(at, kind=session_verdict.KIND_STATUSLINE_REPLY):
    return session_verdict.evidence(False, kind, at)


def yes(at, kind=session_verdict.KIND_STATUSLINE_WINDOW):
    return session_verdict.evidence(True, kind, at)


def test_the_newest_evidence_wins_either_way(tmp_path):
    v = session_verdict.SessionVerdict(path=str(tmp_path / "v.json"))

    assert v.observe([], NOW) is False                  # nothing: no mark
    assert v.observe([framed(no(NOW - 60))], NOW) is True
    assert v.observe([framed(yes(NOW - 120))], NOW) is True   # older
    assert v.observe([framed(yes(NOW - 30))], NOW) is False   # newer
    assert v.observe([framed(no(NOW - 30))], NOW) is False    # a tie: window
    assert v.observe([framed(no(NOW - 10)), framed(yes(NOW - 20))],
                     NOW) is True


def test_the_verdict_is_written_when_it_changes_and_only_then(tmp_path):
    path = tmp_path / "v.json"
    v = session_verdict.SessionVerdict(path=str(path))
    v.observe([framed(no(NOW - 60))], NOW)
    first = path.read_bytes()
    assert json.loads(first) == {"state": "no_window",
                                 "evidence": "statusline_reply",
                                 "at": NOW - 60}

    os.utime(path, (NOW - 9999, NOW - 9999))
    v.observe([framed(no(NOW - 10, session_verdict.KIND_DESKTOP_NO_FH))],
              NOW)
    assert os.path.getmtime(path) == NOW - 9999         # same verdict

    v.observe([framed(yes(NOW - 5))], NOW)
    assert json.loads(path.read_text())["state"] == "window"
    assert not (tmp_path / "v.json.tmp").exists()


def test_a_stale_verdict_on_disk_is_overruled_by_fresh_evidence(tmp_path):
    proved(tmp_path, has_window=False, at=NOW - 3600)
    v = session_verdict.SessionVerdict()
    assert v.observe([], NOW) is True
    assert v.observe([framed(yes(NOW - 7200))], NOW) is True   # older still
    assert v.observe([framed(yes(NOW - 60))], NOW) is False

    proved(tmp_path, has_window=True, at=NOW - 3600,
           kind=session_verdict.KIND_DESKTOP_FH)
    assert session_verdict.SessionVerdict().observe(
        [framed(no(NOW - 60))], NOW) is True


def test_a_verdict_dated_ahead_of_the_clock_cannot_hold_off_the_evidence(
        tmp_path):
    proved(tmp_path, has_window=False, at=NOW + 30 * 86400)

    assert session_verdict.SessionVerdict().observe(
        [framed(yes(NOW - 60))], NOW) is False


@pytest.mark.parametrize("text", [
    "", "{", "[]", '{"state": "maybe", "evidence": "statusline_reply",'
    ' "at": 1}', '{"state": "no_window", "evidence": "rumour", "at": 1}',
    '{"state": "no_window", "evidence": "statusline_reply", "at": true}',
    '{"state": "no_window", "evidence": "statusline_reply", "at": NaN}'],
    ids=["empty", "torn", "a list", "bad state", "bad kind", "bool time",
         "nan time"])
def test_a_verdict_file_that_does_not_read_is_no_verdict(tmp_path, text):
    path = inside(tmp_path, session_verdict.verdict_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    assert session_verdict.SessionVerdict().observe([], NOW) is False


def test_status_reads_the_verdict_and_never_writes_it(tmp_path):
    v = session_verdict.SessionVerdict(persist=False)
    v.observe([framed(no(NOW - 60))], NOW)

    assert not os.path.exists(session_verdict.verdict_path())


def test_the_verdict_path_is_under_home_resolved_when_asked(tmp_path,
                                                           move_home):
    later = tmp_path / "later"
    move_home(later)
    assert session_verdict.verdict_path() == str(
        later / ".blink" / "session-window.json")


def test_a_scenario_bus_is_not_marked_by_the_desk_verdict(tmp_path,
                                                         monkeypatch):
    """The bench walks a board through a step where the account has its
    window back. On this Ultra desk the persisted verdict would hold "no
    window" over it, so build_bus gives a scenario no verdict at all."""
    import claude_usage_bridge     # here, under the sandbox's BLINK_SKIP_SERVICE
    proved(tmp_path, has_window=False, at=NOW - 60)
    scenario = tmp_path / "scenario.json"
    scenario.write_text(json.dumps({"name": "t", "steps": [
        {"at": 0, "provider": "claude", "session_pct": 37.0,
         "weekly_pct": 12.0}]}), encoding="utf-8")
    monkeypatch.setenv("BLINK_SCENARIO", str(scenario))
    bus = claude_usage_bridge.build_bus()

    assert bus.verdict() is None
    assert bus.poll()["session_pct"] == 37.0


# --- the findings the evidence rule closes (2, 3, 5, 10) -------------------


def test_finding_2_a_laptop_asleep_across_a_reset(tmp_path):
    """Pro or Max. Claude Code repaints 23:00-23:30 with a window, the lid
    closes, the reset passes at 02:00, and at 08:00 Claude Code repaints the
    expired block weekly-only and Claude Desktop has a fresh post-reset
    sample with fh 5. The time rule read nine hours without a window as
    Ultra and blanked the 5%."""
    reset = NOW - 6 * 3600
    p = tmp_path / "statusline.json"
    desk = tmp_path / "plan-usage-history.json"
    clock = [0.0]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   ClaudeDesktopProvider(path=str(desk))],
        now=lambda: clock[0])
    desktop_doc(desk, [(reset - 3 * 3600, 27, 26)])
    for m in range(0, 31, 5):
        at = reset - 3 * 3600 + m * 60
        write(p, status(five=27.0, five_resets=reset, seven=26.0), at)
        clock[0] = at + 1
        wire(bus)

    wake = NOW
    write(p, status(seven=26.0), wake)
    desktop_doc(desk, [(reset - 3 * 3600, 27, 26), (wake - 120, 5, 26)])
    got = []
    for t in (wake + 2, wake + 600, wake + 3 * 3600):
        clock[0] = t
        msg = wire(bus)
        got.append((msg["session_pct"], msg["src"],
                    bus.claude_page().no_session_window))

    assert got[:2] == [(5.0, "desktop", False)] * 2
    assert got[2][2] is False


def test_finding_3_an_old_weekly_only_status_line_at_start(tmp_path):
    """A status line forty days old, weekly-only -- Claude Code left open
    across a reset and then not used again -- read when the daemon starts,
    beside a live Claude Desktop. It proves nothing, and Desktop's fh is
    proof of a window."""
    old = NOW - 40 * 86400
    p = write(tmp_path / "statusline.json",
              status(seven=61.0, seven_resets=old + 3 * 86400), old)
    for fh in (12, 0):
        bus = ingest.IngestionBus(
            providers=[ClaudeCliProvider(path=str(p)),
                       desktop_at(tmp_path, 60, session=fh, weekly=10)],
            now=lambda: NOW)
        msg = wire(bus)
        assert (msg["session_pct"], msg["src"]) == (float(fh), "desktop"), fh
        assert bus.claude_page().no_session_window is False


def test_finding_10_two_terminals_and_desktop(tmp_path):
    """Two terminals sharing one status line: each repaints its own
    weekly-only block, and neither block moves. The time rule flapped
    between Desktop's 7% and "--%" on every alternation (the reviewer's
    two-terminal probe)."""
    p = tmp_path / "statusline.json"
    clock = [0.0]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 60, session=7, weekly=11)],
        now=lambda: clock[0])
    a = status(seven=10.0, session="a", cost=2.0)
    b = status(seven=11.0, session="b", cost=5.0)
    seen = set()
    for i in range(8):
        write(p, (a, b)[i % 2], NOW - 40 + i * 5)
        clock[0] = NOW - 39 + i * 5
        msg = wire(bus)
        seen.add((msg["session_pct"], bus.claude_page().no_session_window))

    assert seen == {(7.0, False)}


# --- desks with Claude Desktop alone ---------------------------------------
#
# Claude Desktop's Code tab writes transcripts into ~/.claude/projects (with
# "entrypoint":"claude-desktop") and never runs the status line (measured
# 2026-10-31, counts only), so a Desktop-only Ultra desk has no fresh status
# line, and its proof is Desktop's history alone.


def _desktop_only_bus(tmp_path, fh, transcripts):
    from pc import burn_reader
    from tests.pc import test_burn_line as bl
    from tests.support import transcripts as tx
    if transcripts:
        tx.write(tmp_path, tmp_path / ".claude" / "projects" / "-work"
                 / "code-tab.jsonl",
                 [dict(tx.assistant("msg_%d" % i, "req_%d" % i,
                                    bl.start(i), 1000 * (i + 1)),
                       entrypoint="claude-desktop") for i in range(20)],
                 mtime=bl.NOW - 2)
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(bl.NOW - 300, fh, 12)])
    return ingest.IngestionBus(
        providers=[ClaudeDesktopProvider(path=str(f))],
        now=lambda: bl.NOW,
        burn=burn_reader.BurnReader()).fetch()


def test_a_desktop_only_ultra_desk_is_marked_and_sent_its_burn(tmp_path):
    from tests.pc import test_burn_line as bl
    fetch = _desktop_only_bus(tmp_path, ABSENT, transcripts=True)

    usage = fetch()
    line = fetch.burn()

    assert usage["session_pct"] == -1 and usage["src"] == "desktop"
    assert line == protocol.burn("claude", bl.Q,
                                 [1000 * (i + 1) for i in range(20)])


def test_a_desktop_only_ultra_desk_with_no_transcripts_sends_no_zeros(
        tmp_path):
    """Desktop's chat leaves no tokens on disk. With no projects directory
    the reader has nothing honest to say, so there is no burn line and the
    board shows "--%", not a confident idle."""
    fetch = _desktop_only_bus(tmp_path, ABSENT, transcripts=False)
    assert not (tmp_path / ".claude" / "projects").exists()

    assert fetch()["session_pct"] == -1
    assert fetch.burn() is None


def test_a_desktop_only_pro_desk_is_as_it_was(tmp_path):
    fetch = _desktop_only_bus(tmp_path, 4, transcripts=True)

    usage = fetch()

    assert (usage["session_pct"], usage["src"]) == (4.0, "desktop")
    assert fetch.burn() is None


def test_a_desktop_only_ultra_desk_gets_its_line_through_the_bridge(
        tmp_path):
    """The bridge asks the fetch, never a status-line frame."""
    fetch = _desktop_only_bus(tmp_path, ABSENT, transcripts=True)
    sent = []
    clock = [1000.0]
    bridge = Bridge(write_msg=sent.append, fetch_usage=fetch,
                    now=lambda: clock[0], wall=lambda: (1752444000, 0),
                    fetch_manifest=lambda: None,
                    fetch_signed_manifest=lambda: None)
    bridge.on_message({"t": "hello", "v": 2, "board_id": "ab", "fw": "1.4.0",
                       "caps": ["svc", "theme", "edit_done", "burn"]})
    clock[0] += THEME_QUIET_S + 1
    bridge.poll_if_changed()

    assert [m["t"] for m in sent].count("burn") == 1


def test_the_reader_does_not_filter_on_the_entrypoint(tmp_path):
    from pc import burn_reader
    from tests.pc import test_burn_line as bl
    from tests.support import transcripts as tx
    tx.write(tmp_path, tmp_path / ".claude" / "projects" / "-w" / "s.jsonl",
             [dict(tx.assistant("m1", "r1", bl.start(19), 40),
                   entrypoint="claude-desktop"),
              dict(tx.assistant("m2", "r2", bl.start(19), 2),
                   entrypoint="cli")], mtime=bl.NOW)

    assert sum(burn_reader.BurnReader().poll(bl.NOW)[1]) == 42


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


# --- merge: the mark is the verdict's, and nothing in the merge lifts it ----


def test_a_desktop_window_with_a_reset_ahead_proves_the_account_has_one(
        tmp_path):
    """Scenario G3. Claude Desktop's Local Storage holding a window with its
    reset still ahead is proof of a window, newer than the reply that set
    the mark, so the verdict clears it and the merge goes on as if no mark
    had been set. The merge used to lift the mark itself on any stamp, which
    is the verdict's business now: it knows which proof is the newer."""
    ls = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path / "none"))
    live_window = ls.record_to_frame(
        {"resetsAt": NOW + 3000, "utilization": 0.40,
         "observedAt": NOW - 300}, NOW)
    assert live_window.session_resets_at == NOW + 3000
    proved(tmp_path, has_window=False, at=NOW - 600)
    frames = [cli_frame(at=NOW - 1, flagged=False), live_window]

    session_verdict.SessionVerdict().mark(frames, NOW)
    merged = normalizer.merge(frames)

    assert merged.session_pct == 40.0
    assert merged.session_resets_at == NOW + 3000
    assert merged.no_session_window is False


def test_proof_older_than_the_mark_does_not_lift_it(tmp_path):
    """The other way round: a Local Storage record written before the reply
    that came back without a window describes a window the account has
    since lost, and the merge borrows nothing from it."""
    ls = ClaudeDesktopLocalStorageProvider(dir_path=str(tmp_path / "none"))
    old_window = ls.record_to_frame(
        {"resetsAt": NOW + 3000, "utilization": 0.40,
         "observedAt": NOW - 3000}, NOW)
    proved(tmp_path, has_window=False, at=NOW - 600)
    frames = [cli_frame(at=NOW - 1, flagged=False), old_window]

    session_verdict.SessionVerdict().mark(frames, NOW)
    merged = normalizer.merge(frames)

    assert merged.session_pct == base.UNKNOWN
    assert merged.session_resets_at is None
    assert merged.no_session_window is True


def test_a_stamped_reading_does_not_lift_a_mark_in_the_merge():
    """Finding 13's other half: what the merge reports is what it decided.
    A marked set stays marked whatever stamp one of its frames carries."""
    stamped = desktop_frame(NOW - 300, session=8.0,
                            session_resets_at=NOW + 3000)

    merged = normalizer.merge([cli_frame(at=NOW - 1), stamped])

    assert merged.no_session_window is True
    assert merged.session_pct == base.UNKNOWN


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
    proved(tmp_path, has_window=False, at=NOW - 600)
    frames = [cli_frame(at=NOW - 1, flagged=False), unreadable]

    session_verdict.SessionVerdict().mark(frames, NOW)
    merged = normalizer.merge(frames)

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
# Desks, each a sequence of (status line, its mtime, poll at) with a Claude
# Desktop cache or Local Storage record beside it, and optionally a verdict a
# daemon left on disk. The expected line is what this rule draws.
#
# The evidence rule changed the inputs of the Ultra rows, A to E and U: their
# Desktop samples carried `fh`, which a verified Ultra account's Desktop no
# longer writes (measured 2026-10-31). A sample with fh is proof of a window
# now, so those rows carry the measured shape, sd and no fh key, and keep
# the lines they drew. "A0" keeps the 10-30 inputs, a 37-hour-old pre-change
# sample and a status line that has not replied, and shows the rule's (c):
# no proof either way, no mark, the session unknown on its own. G2 is the
# row the rule rightly changes: a fresh process after a Pro or Max reset
# keeps Desktop's 8%.

W = status(seven=2.0)
DK_PRE = [(NOW - 136136, 1, 2)]                 # the 10-30 measured sample
DK_MEASURED = DK_PRE + [(NOW - 3600, ABSENT, 2)]
TORN, GONE = "torn", "gone"
REPLIED = session_verdict.evidence(
    False, session_verdict.KIND_STATUSLINE_REPLY, NOW - 7200)
# replied()'s renders as scenario steps, the last written at NOW - 1: two
# replies far enough apart to be proof.
_FIRST = NOW - 1 - claude_cli.REPLY_APART_S
REPLY_STEPS = (
    [(status(seven=2.0, cost=1.0), _FIRST - 30, _FIRST - 29),
     (status(seven=2.0, cost=1.25), _FIRST, _FIRST + 1)]
    + [(status(seven=2.0, cost=1.25), t, t + 1)
       for t in range(int(_FIRST) + 300, int(NOW - 1), 300)]
    + [(status(seven=2.0, cost=1.50), NOW - 1, NOW)])

SCENARIOS = {
    "A0 the 10-30 inputs, nothing proved": dict(
        steps=[(W, NOW - 1, NOW)], desktop=DK_PRE,
        want=[(-1, "cli", False, 1)], marked=False),
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
        steps=[(W, NOW - 1, NOW)], desktop=[(NOW - 7200, ABSENT, 2)],
        want=[(-1, "cli", False, 1)], marked=True),
    "D2 Desktop 5 h old": dict(
        steps=[(W, NOW - 1, NOW)], desktop=[(NOW - 18000, ABSENT, 2)],
        want=[(-1, "cli", False, 1)], marked=True),
    "D3 restart, Desktop 2 h old": dict(
        steps=[(W, NOW - 2400, NOW)], desktop=[(NOW - 7200, ABSENT, 2)],
        want=[(-1, "cli", True, 2400)], marked=True),
    # Desktop's weekly figure is the newest, so it names the frame.
    "D4 Desktop sampled between two polls": dict(
        steps=[(W, NOW - 1, NOW, DK_MEASURED),
               (None, None, NOW + 7200,
                DK_MEASURED + [(NOW + 3600, ABSENT, 2)])],
        desktop=DK_MEASURED,
        want=[(-1, "cli", False, 1), (-1, "desktop", True, 3600)],
        marked=True),
    "D5 Desktop newer than the render": dict(
        steps=[(W, NOW - 600, NOW)], desktop=[(NOW - 60, ABSENT, 2)],
        want=[(-1, "desktop", False, 60)], marked=True),
    # A rising series from before a reply proved there is no window: the
    # reply is the newer evidence, and the rate goes with the figure.
    "D6 a rising Desktop series, then a reply": dict(
        steps=REPLY_STEPS,
        desktop=[(NOW - 2700 + i * 300, 10 + 5 * i, 2) for i in range(3)],
        want=[(20.0, "desktop", False, t + 1 - (NOW - 2100))
              for _, t, _ in REPLY_STEPS[:-1]] + [(-1, "cli", False, 1)],
        marked=True),
    # Renders with no rate_limits.
    "E alternating renders, Desktop 2 h old": dict(
        steps=[(W, NOW - 7, NOW - 6), (NO_LIMITS, NOW - 5, NOW - 4),
               (W, NOW - 3, NOW - 2), (NO_LIMITS, NOW - 1, NOW)],
        desktop=[(NOW - 7200, ABSENT, 2)],
        want=[(-1, "cli", False, 1), (-1, "cli", False, 3)] * 2,
        marked=True, pushes=1),
    "E2 alternating renders, Desktop 37 h old, verdict on disk": dict(
        steps=[(W, NOW - 7, NOW - 6), (NO_LIMITS, NOW - 5, NOW - 4),
               (W, NOW - 3, NOW - 2), (NO_LIMITS, NOW - 1, NOW)],
        desktop=DK_PRE, verdict=REPLIED,
        want=[(-1, "cli", False, 1), (-1, "cli", False, 3)] * 2,
        marked=True, pushes=1),
    # Ultra proved by a reply alone, and a restart that reads it back.
    "R a reply without a window": dict(
        steps=REPLY_STEPS, want=[(-1, "cli", False, 1)] * len(REPLY_STEPS),
        marked=True),
    "R2 restart, verdict on disk, nothing else": dict(
        steps=[(W, NOW - 1, NOW)], verdict=REPLIED,
        want=[(-1, "cli", False, 1)], marked=True),
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
    # No memory, and nothing proved: Desktop's figure stands.
    "G2 fresh process after a reset": dict(
        steps=[(status(seven=31.0), NOW - 1, NOW)],
        desktop=[(NOW - 1200, 8, 31)],
        want=[(8.0, "desktop", False, 1200)], marked=False),
    "G3 fresh process, Local Storage window open": dict(
        steps=[(status(seven=31.0), NOW - 1, NOW)],
        ls={"resetsAt": NOW + 4 * 3600, "utilization": 0.08,
            "observedAt": NOW - 300},
        want=[(8.0, "desktop_ls", False, 300)], marked=False),
    "G4 fresh process, a stale Ultra verdict, Desktop's fh after it": dict(
        steps=[(status(seven=31.0), NOW - 1, NOW)],
        desktop=[(NOW - 1200, 8, 31)], verdict=REPLIED,
        want=[(8.0, "desktop", False, 1200)], marked=False),
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
        desktop=[(NOW - 7200, ABSENT, 2)],
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
    if "verdict" in case:
        proved(tmp_path, *case["verdict"][:1], at=case["verdict"][2],
               kind=case["verdict"][1])
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
    if "verdict" not in case:
        on_disk = session_verdict.load(session_verdict.verdict_path())
        assert (on_disk is not None and not on_disk[0]) is case["marked"]


# --- the burn view: sent on Ultra, not on Pro or Max after a reset -----------


def _burn_desk(home, docs):
    """A real bus (status line + transcript reader) feeding a real Bridge
    whose board lists "burn". `docs` is [(status line, mtime)], each polled
    in turn; returns the burn lines the bridge put on the wire.

    The bus saves the account's verdict, so it is handed a SessionVerdict
    whose path has been through inside() for the caller's home: the
    default one finds home on its own (CLAUDE.md, test helpers)."""
    from pc import burn_reader
    from tests.pc import test_burn_line as bl
    verdict = session_verdict.SessionVerdict(
        path=inside(home, session_verdict.verdict_path()))
    bl.transcripts(home, [0] * 18 + [1500, 260_000])
    p = home / "statusline.json"
    clock = [0.0]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p))], now=lambda: clock[0],
        burn=burn_reader.BurnReader(root=str(home / ".claude" / "projects")),
        verdict=verdict)
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
    first = bl.NOW - 1 - claude_cli.REPLY_APART_S
    costs = [(1.0, first - 30), (1.25, first)]
    costs += [(1.25, t) for t in range(int(first) + 300, int(bl.NOW - 1), 300)]
    costs += [(1.50, bl.NOW - 1)]
    bus, burns = _burn_desk(tmp_path, [
        (status(seven=12.0, cost=c), t) for c, t in costs])

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


def test_the_burn_desk_writes_its_verdict_only_inside_the_home_it_is_given(
        tmp_path):
    """_burn_desk's bus saved its verdict through the default SessionVerdict,
    to wherever verdict_path() found home, without asking home.inside()
    first (/code-review max, 2026-11-01). Under pytest that is tmp_path; in a
    script that imported this file it is a real desk's ~/.blink. Handed a
    home that is not where the verdict would go, it stops before writing."""
    from tests.pc import test_burn_line as bl
    elsewhere = tmp_path / "elsewhere"
    with pytest.raises(AssertionError, match="outside the caller's home"):
        _burn_desk(elsewhere, [(status(seven=12.0, cost=1.0), bl.NOW - 31)])
    assert not os.path.exists(session_verdict.verdict_path())


def test_an_ultra_desk_after_a_restart_is_sent_its_burn_line_at_once(
        tmp_path):
    """The persisted verdict: no reply needed, the first tick after the
    quiet window carries the view."""
    from tests.pc import test_burn_line as bl
    proved(tmp_path, has_window=False, at=bl.NOW - 86400)
    bus, burns = _burn_desk(tmp_path, [(weekly_only(12.0), bl.NOW - 1)])

    assert len(burns) == 1 and burns[0]["p"] == "claude"
    assert bus.claude_page().no_session_window is True


# --- the review of 2026-11-01 ------------------------------------------------
#
# /code-review max of feat/ultra-burn. Each test names the finding it pins.


def watched(prov, verdict, p, renders):
    """Write each (doc, mtime) render and poll it a second later, through a
    real provider and verdict. Returns every piece of no-window evidence
    the provider put on a frame, as (has_window, kind, at)."""
    proofs = []
    for doc, mtime in renders:
        write(p, doc, mtime)
        frames = prov.poll(mtime + 1)
        verdict.observe(frames, mtime + 1)
        proofs += [f.window_evidence for f in frames
                   if f.window_evidence is not None
                   and f.window_evidence[0] is False]
    return proofs


def test_finding_1_a_reply_streaming_across_a_reset_proves_nothing(tmp_path):
    """Pro or Max. A request accepted before the reset at R still streams
    after it. Claude Code re-renders at R+1 s and drops the expired
    five_hour; when the stream ends it credits the cost and applies headers
    whose five_hour resets at R, which the render filter drops again. So the
    session's cost rises between two weekly-only renders, and that read as
    proof: the desk was marked and the verdict saved. Two parallel
    subagents straddling the same reset rise it twice."""
    R = NOW - 3600
    p = tmp_path / "statusline.json"
    v = session_verdict.SessionVerdict()
    renders = [(status(five=60.0, five_resets=R, cost=1.0), R - 60)]
    renders += [(status(cost=1.0), R + 1 + i * 5) for i in range(12)]
    renders += [(status(cost=1.30), R + 300), (status(cost=1.30), R + 305),
                (status(cost=1.55), R + 540), (status(cost=1.55), R + 545)]
    renders += [(status(five=1.0, five_resets=R + 5 * 3600 + 600,
                        cost=1.60), R + 600)]

    assert watched(ClaudeCliProvider(path=str(p)), v, p, renders) == []
    assert no_mark_on_disk()


def test_finding_1_two_processes_on_one_session_prove_nothing(tmp_path):
    """`claude --continue` in two terminals: one session_id, two running
    costs, each repainted while idle. Alternating, every other render is a
    rise, for as long as both stay open."""
    p = tmp_path / "statusline.json"
    v = session_verdict.SessionVerdict()
    renders = [(status(cost=(1.0, 1.30)[i % 2]), NOW - 7200 + i * 5)
               for i in range(7200 // 5)]

    assert watched(ClaudeCliProvider(path=str(p)), v, p, renders) == []
    assert no_mark_on_disk()


def test_finding_2_a_baseline_from_before_a_gap_proves_nothing(tmp_path):
    """Pro or Max. Polls run only while the board is alive. At 09:00, just
    after a reset, a weekly-only render at cost 3.00 was the baseline; the
    board was unplugged, the session replied all morning with five_hour
    (none of it polled), the window ended, and Claude Code re-rendered
    weekly-only at 4.00. On reconnect 3.00 < 4.00 read as a reply."""
    p = tmp_path / "statusline.json"
    v = session_verdict.SessionVerdict()
    t9 = NOW - 6 * 3600
    renders = [(status(cost=3.00), t9 + i * 5) for i in range(6)]
    renders += [(status(cost=4.00), t9 + 5 * 3600 + 300 + i * 5)
                for i in range(6)]
    renders += [(status(cost=4.00), t9 + 5 * 3600 + 300 + i * 600)
                for i in range(1, 4)]

    assert watched(ClaudeCliProvider(path=str(p)), v, p, renders) == []
    assert no_mark_on_disk()


def test_a_single_reply_is_not_yet_proof_and_a_second_is(tmp_path):
    """The measured Ultra case still proves, once a second reply comes far
    enough after the first that no single reset can account for both."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    v = session_verdict.SessionVerdict()
    t0 = NOW - claude_cli.REPLY_APART_S - 120
    first = watched(prov, v, p, [(status(cost=1.0), t0),
                                 (status(cost=1.25), t0 + 30)])
    assert first == []
    t, cost, proofs = t0 + 30, 1.25, []
    while t < NOW:
        t = min(t + 120, NOW)
        cost += 0.05
        proofs += watched(prov, v, p, [(status(cost=cost), t)])

    assert proofs and proofs[0][1] == session_verdict.KIND_STATUSLINE_REPLY
    assert session_verdict.load(session_verdict.verdict_path())[0] is False


def test_finding_4_an_old_terminals_block_does_not_unmark_a_restart(
        tmp_path):
    """Ultra, verified: terminal Y's reply proved no window at T-1000 s and
    the verdict was saved. Terminal X still holds a five_hour block it
    fetched at T-2h, before the change, and repaints it at T-30 s. A
    restarted daemon had never seen that block, dated it by the repaint,
    and the window "outranked" the proof: the saved verdict went to
    window. Dated from the block's own reset stamp, it is from T-2h."""
    T = NOW
    proved(tmp_path, has_window=False, at=T - 1000)
    p = tmp_path / "statusline.json"
    x = status(five=40.0, five_resets=T - 2 * 3600 + base.SESSION_WINDOW_S,
               seven=20.0, session="x", cost=7.0)
    v = session_verdict.SessionVerdict()
    write(p, x, T - 30)

    assert v.observe(ClaudeCliProvider(path=str(p)).poll(T), T) is True
    assert session_verdict.load(session_verdict.verdict_path())[0] is False


def test_finding_8_a_full_fetch_table_does_not_re_date_the_dial(tmp_path):
    """A remembered five_hour block whose window has ended is purged with
    the other ended blocks when the table fills, and re-entered on the next
    lookup under the remembered (later) mtime: its first_seen_at, the
    figure's date in the merge, moved forward by an hour."""
    T = NOW
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    r = status(five=40.0, five_resets=T - 600, seven=20.0, session="a")
    write(p, r, T - 4 * 3600)
    prov.poll(T - 4 * 3600 + 1)
    write(p, r, T - 3 * 3600)
    prov.poll(T - 3 * 3600 + 1)
    for i in range(claude_cli.FETCH_MEMORY_LIMIT + 4):
        write(p, status(seven=21.0 + i / 100.0, session="f%d" % i),
              T - 170 * 60 + i * 30)
        frames = prov.poll(T - 170 * 60 + i * 30 + 1)

    dial = [f for f in frames if f.session_pct >= 0 or f.session_rolled_at]
    assert [f.first_seen_at for f in dial] == [T - 4 * 3600]


def test_finding_14_a_refused_five_hour_does_not_replace_the_last_figure(
        tmp_path):
    """poll() decided "this payload has a figure" from the raw payload,
    while the frame refuses a five_hour whose resets_at will not read once
    it is older than a window. Such a payload overwrote the last payload
    with a figure, and the weekly figure went with it."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    write(p, status(seven=33.0), NOW - 60)
    prov.poll(NOW - 59)
    refused = {"session_id": "s",
               "rate_limits": {"five_hour": {"used_percentage": 40.0,
                                             "resets_at": None}}}
    write(p, refused, NOW - BOUND - 60)

    frames = prov.poll(NOW)
    assert not frames[0].has_usage()
    assert 33.0 in [f.weekly_pct for f in frames]


def test_finding_7_a_failed_save_is_retried_and_leaves_no_temp(
        tmp_path, monkeypatch):
    """The verdict was written only on the poll where it flipped, through a
    fixed temp name, with no retry: one failed os.replace (WinError 32
    while `blink status` held the file) left the disk stale for the
    daemon's life and a .tmp beside it."""
    path = inside(tmp_path, session_verdict.verdict_path())
    real, calls = os.replace, []

    def refuse_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(13, "held by another process")
        return real(src, dst)

    monkeypatch.setattr(os, "replace", refuse_once)
    v = session_verdict.SessionVerdict()
    assert v.observe([framed(no(NOW - 60))], NOW) is True
    assert not os.path.exists(path)
    assert os.listdir(os.path.dirname(path)) == []

    v.observe([], NOW + 2 + session_verdict.SAVE_RETRY_S)
    assert session_verdict.load(path)[0] is False
    assert os.listdir(os.path.dirname(path)) == ["session-window.json"]


@pytest.mark.parametrize("text", [
    '{"state": "no_window", "evidence": "statusline_reply", "at": 1%s}'
    % ("0" * 400),
    "[" * 200000 + "]" * 200000],
    ids=["an int too big for a float", "nested too deep"])
def test_finding_12_a_verdict_file_that_cannot_be_numbered_is_none(
        tmp_path, text):
    path = inside(tmp_path, session_verdict.verdict_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)

    assert session_verdict.load(path) is None


def test_every_described_kind_is_a_kind_evidence_can_carry():
    """KINDS restated DESCRIBE's keys by hand; a kind left out of one would
    have its evidence, and a verdict saved on it, silently dropped."""
    assert session_verdict.KINDS == tuple(session_verdict.DESCRIBE)


def test_a_cost_too_big_for_a_float_is_no_cost(tmp_path):
    """json reads an integer of any length up to 4300 digits, and float()
    of one past 308 raises OverflowError: a status line whose
    cost.total_cost_usd was such an int made every poll() raise, and the
    status-line source was benched with backoff for as long as the file
    said it. Claude Code would never write one; a corrupt or hand-edited
    file can."""
    p = tmp_path / "statusline.json"
    doc = status(seven=12.0)
    text = json.dumps(doc).replace('"total_cost_usd": 1.0',
                                   '"total_cost_usd": 1' + "0" * 400)
    p.write_text(text, encoding="utf-8")
    os.utime(p, (NOW - 1, NOW - 1))

    frames = ClaudeCliProvider(path=str(p)).poll(NOW)
    assert frames[0].weekly_pct == 12.0
    assert frames[0].window_evidence is None


def test_a_window_block_this_process_saw_arrive_is_dated_when_it_did(
        tmp_path):
    """The other side of finding 4's fix. Dated from its window's start, a
    block on a Pro or Max desk loses to any no-window mark from inside that
    window, so a mark that should never have been made would hold the dial
    for up to five hours. A block that arrived with a reply this process
    watched -- the same session, rendered minutes before at a lower cost --
    was fetched between those two renders, and is dated by its arrival:
    the next reply lifts such a mark."""
    p = tmp_path / "statusline.json"
    opened = NOW - 4 * 3600
    proved(tmp_path, has_window=False, at=NOW - 600)
    v = session_verdict.SessionVerdict()
    prov = ClaudeCliProvider(path=str(p))
    before = status(five=30.0, five_resets=opened + base.SESSION_WINDOW_S,
                    seven=20.0, cost=2.0)
    write(p, before, NOW - 120)
    assert v.observe(prov.poll(NOW - 119), NOW - 119) is True   # a repaint

    after = status(five=31.0, five_resets=opened + base.SESSION_WINDOW_S,
                   seven=20.0, cost=2.25)
    write(p, after, NOW - 1)
    assert v.observe(prov.poll(NOW), NOW) is False
