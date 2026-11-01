"""An account with no five-hour window: Ultra, from 2026-10-30.

Anthropic removed the five-hour limit for verified Ultra accounts (formerly
Max 20x). Claude Code's status line then carries rate_limits.seven_day and no
five_hour key at all. Measured on a real
Ultra desk, `blink status --wire` one second after Claude Code wrote the
file printed

    "session_pct":1.0, "src":"desktop", "stale":true, "age_s":136136,
    "weekly_pct":2.0, "active_age_s":0

-- live data, told it was stale and 37 hours old. The dial had been filled by
a borrowed session reading (a Claude Desktop sample two days old), and the
borrowed source then also named and dated the frame, because merge() takes
`primary = session_src or weekly_src`.

The rule is positive evidence (pc/session_verdict). The account is marked
only on proof it has no window -- Claude Desktop's weekly figure risen
between two samples one poll apart, neither with an `fh` key (owner's
decision, 2026-11-01), or Claude Code replying for over five hours with no
five-hour window rendered (the re-review of 2026-11-01) -- and the mark
goes on proof it has one: any five_hour rendered after it. Quiet data is
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


def sustained(at):
    """(cost, mtime) of the renders of one Ultra session that prove no
    window, the last written at `at`: a reply every twenty minutes, the
    first more than claude_cli.CHAIN_SPAN_S before the last, and a repaint
    every four minutes in between. Two replies, or one, are not proof
    (claude_cli._reply_without_window)."""
    spacing = 20 * 60
    n = -(-claude_cli.CHAIN_SPAN_S // spacing)          # ceil
    first = at - n * spacing
    renders, cost = [(1.0, first - 60)], 1.0
    for k in range(n + 1):
        t = first + k * spacing
        cost += 0.25
        renders.append((cost, t))
        if k < n:
            renders += [(cost, t + r) for r in range(240, spacing, 240)]
    return renders


def replied(poll, p, at, seven=2.0, session="s", limits=None):
    """Claude Code's status line through the replies that prove no window
    (sustained()), each render read by `poll(now)` a second after it is
    written: the same session, weekly-only throughout. Returns what the
    last poll returned. `limits` replaces every render's rate_limits."""
    for cost, when in sustained(at):
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
           kind=session_verdict.KIND_STATUSLINE_SUSTAINED):
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


def rose(at, weekly=2):
    """The Desktop shape that proves no window (owner's decision,
    2026-11-01): two samples one 15-minute idle poll apart, weekly usage
    risen between them, neither with an `fh` key, the newer at `at`. A
    sample with no `fh` key alone proves nothing
    (claude_desktop._no_window_rise)."""
    return [(at - 900, ABSENT, weekly - 1), (at, ABSENT, weekly)]


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


def desktop_at(tmp_path, age_s, session=1, weekly=2, rising=False):
    """A real Desktop provider over a generated cache whose newest sample is
    `age_s` old. `session` ABSENT is the measured Ultra shape, and with
    `rising` it is proof: a sample one poll earlier, a point lower, comes
    before it (rose())."""
    samples = [(NOW - age_s, session, weekly)]
    if rising:
        samples.insert(0, (NOW - age_s - 900, session, weekly - 1))
    f = desktop_doc(tmp_path / "plan-usage-history.json", samples)
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
    """The measured wire line, reproduced and then inverted, on a Desktop
    history like the measured one: a sample from before the change with
    `fh`, two days old, and after it `sd` with no `fh` key -- here risen
    between two polls, which is what proves no window (rose()). The
    measured pair alone proves nothing: scenario A0 below."""
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(NOW - 136136, 1, 2)] + rose(NOW - 3600))
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
    window, rendered after the Desktop sample that set the mark, is newer
    evidence. A block is dated by its render (claude_cli._frame)."""
    p = tmp_path / "statusline.json"
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   desktop_at(tmp_path, 3600, session=ABSENT, rising=True)],
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
    Claude Code repaints the line on every event; aged by the figure,
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
                                  weekly=24, rising=weekly is ABSENT)],
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
    line the dial and the burn view waited for five more hours of replies.
    The first bus learns it from its replies and writes it; the second, a
    restart, reads a 40-minute-old weekly-only payload and needs no
    evidence of its own."""
    p = tmp_path / "statusline.json"
    clock = [0.0]
    first = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                                now=lambda: clock[0])
    assert replied(ticking(first, clock), p, NOW - 2400)["session_pct"] == -1
    assert first.claude_page().no_session_window is True
    on_disk = session_verdict.load(session_verdict.verdict_path())
    assert on_disk == (False, session_verdict.KIND_STATUSLINE_SUSTAINED,
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
        False, session_verdict.KIND_STATUSLINE_SUSTAINED, NOW - 1)


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
    to its next reply, repainted on Claude Code's events for as long as it
    stays open."""
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
    (ABSENT, None), (0, True), (37, True), (None, None), (5000, None)],
    ids=["no fh key", "fh 0", "fh 37", "fh null", "fh out of range"])
def test_desktop_proves_by_its_fh_key(tmp_path, fh, want):
    """A number under `fh`, 0 included, proves a window. A sample alone
    with no `fh` key proves nothing (owner's decision, 2026-11-01): a Pro
    or Max account with no window open may write that too. Null and out of
    range prove nothing either way."""
    frame = desktop_at(tmp_path, 600, session=fh, weekly=2).poll(NOW)[0]

    if want is None:
        assert frame.window_evidence is None
    else:
        assert frame.window_evidence == (
            True, session_verdict.KIND_DESKTOP_FH, NOW - 600)


# Desktop's no-window evidence is a RISE (owner's decision after the
# 2026-11-01 review): weekly usage that went up between two consecutive
# samples, the later of which has no `fh` key. The gap is Desktop's own
# sampling cadence bound, claude_desktop.DESKTOP_RISE_MAX_GAP_S.
def _rise_gap():
    from pc.providers import claude_desktop
    return claude_desktop.DESKTOP_RISE_MAX_GAP_S


def desktop_evidence(tmp_path, samples):
    f = desktop_doc(tmp_path / "plan-usage-history.json", samples)
    return ClaudeDesktopProvider(path=str(f)).poll(NOW)[0].window_evidence


def test_desktop_weekly_rising_with_no_fh_proves_no_window(tmp_path):
    """Weekly usage went up between two samples Desktop took one poll
    apart, and neither has an `fh` key. With no window open at the first,
    usage after it opens one on any plan that has them, and the second
    would carry fh; only an account with no window can show this."""
    ev = desktop_evidence(tmp_path, [(NOW - 1500, ABSENT, 11),
                                     (NOW - 600, ABSENT, 12)])

    assert ev == (False, session_verdict.KIND_DESKTOP_RISE, NOW - 600)


@pytest.mark.parametrize("fh", [40, 0, None],
                         ids=["fh 40 before", "fh 0 before", "fh null before"])
def test_a_rise_from_a_sample_with_fh_proves_nothing(tmp_path, fh):
    """The re-review of 2026-11-01, finding 1. Only the later sample's `fh`
    was checked, so a rise measured from a sample that itself carried one
    -- a window open a poll ago -- was proof of none. A null `fh` is not an
    absent one either."""
    ev = desktop_evidence(tmp_path, [(NOW - 1500, fh, 11),
                                     (NOW - 600, ABSENT, 12)])

    assert ev is None


def test_a_pro_window_closing_between_two_samples_is_not_marked(tmp_path):
    """Finding 1's scenario, end to end on a bus. Pro, Desktop open: a
    sample {fh: 85, sd: 40} four minutes before the reset at R, the window's
    last usage lifting sd to 41, the reset, and the next sample {sd: 41} a
    minute after it, with no fh because no window is open. That marked the
    desk and saved the verdict."""
    R = NOW - 60
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(R - 240, 85, 40), (R + 60, ABSENT, 41)])
    bus = ingest.IngestionBus(providers=[ClaudeDesktopProvider(path=str(f))],
                              now=lambda: NOW)

    wire(bus)

    assert bus.claude_page().no_session_window is False
    assert no_mark_on_disk()


def test_an_idle_pro_desk_with_no_fh_is_not_marked(tmp_path):
    """The case the rule exists for. A Pro or Max account idle for over
    five hours has no window open, and may write a sample with `sd` and no
    `fh` -- which the single-sample rule read as Ultra, and showed the burn
    view: the worst outcome. Its weekly figure did not move."""
    ev = desktop_evidence(tmp_path, [(NOW - 6 * 3600, 30, 12),
                                     (NOW - 600, ABSENT, 12)])

    assert ev is None


def test_a_rise_across_a_gap_longer_than_a_poll_is_not_proof(tmp_path):
    """Found while writing this rule, 2026-11-01. A rise does not always
    open a window: usage can land at the end of one already open, which
    then closes. A Pro window opened at T-4h, Desktop sampled at T-0.1h,
    the user chatted at T and closed the app, the window closed at T+1h,
    and the app reopened at T+4h: sd rose within five hours, and there is
    no window now. So only consecutive poll samples are trusted."""
    t = NOW - 600
    assert desktop_evidence(tmp_path, [(t - 4.1 * 3600, 80, 10),
                                       (t, ABSENT, 11)]) is None


@pytest.mark.parametrize("gap_extra, proof", [(-1, True), (0, False),
                                               (1, False)],
                         ids=["inside", "at the bound", "past"])
def test_the_gap_must_be_less_than_the_bound(tmp_path, gap_extra, proof):
    t = NOW - 600
    ev = desktop_evidence(tmp_path, [(t - _rise_gap() - gap_extra, ABSENT, 10),
                                     (t, ABSENT, 11)])
    assert (ev is not None) is proof


@pytest.mark.parametrize("sd", [12, 11], ids=["equal", "falling"])
def test_weekly_usage_that_did_not_rise_is_not_proof(tmp_path, sd):
    assert desktop_evidence(tmp_path, [(NOW - 1500, ABSENT, 12),
                                       (NOW - 600, ABSENT, sd)]) is None


def test_the_measured_owner_desk_is_not_desktop_proof(tmp_path):
    """This desk's verified Ultra account, read-only, 2026-10-31: samples
    10-29 21:29 {fh: 1, sd: 23} and 10-31 18:29 {sd: 19}. Forty-five hours
    apart and falling (the weekly window rolled between): not a rise one
    poll apart, so not evidence. Claude Code's sustained replies are what
    mark that desk."""
    t = NOW - 600
    assert desktop_evidence(tmp_path, [(t - 45 * 3600, 1, 23),
                                       (t, ABSENT, 19)]) is None


def test_the_previous_sample_is_the_previous_in_time(tmp_path):
    """By timestamp, not by position: the file is sorted today, and that is
    an observation about one file (_newest_sample). Here the sample before
    the newest in the list is hours old, and the one just before it in time
    is lower."""
    ev = desktop_evidence(tmp_path, [(NOW - 600, ABSENT, 12),
                                     (NOW - 6 * 3600, 30, 12),
                                     (NOW - 1500, ABSENT, 11)])
    assert ev == (False, session_verdict.KIND_DESKTOP_RISE, NOW - 600)

    assert desktop_evidence(tmp_path, [(NOW - 600, ABSENT, 12),
                                       (NOW - 1500, ABSENT, 12),
                                       (NOW - 2400, ABSENT, 11)]) is None


def test_a_previous_sample_with_no_weekly_figure_proves_nothing(tmp_path):
    assert desktop_evidence(tmp_path, [(NOW - 1500, 3, None),
                                       (NOW - 600, ABSENT, 12)]) is None


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


def no(at, kind=session_verdict.KIND_STATUSLINE_SUSTAINED):
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
                                 "evidence": "statusline_sustained",
                                 "at": NOW - 60}

    os.utime(path, (NOW - 9999, NOW - 9999))
    v.observe([framed(no(NOW - 10, session_verdict.KIND_DESKTOP_RISE))],
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
    "", "{", "[]", '{"state": "maybe", "evidence": "statusline_sustained",'
    ' "at": 1}', '{"state": "no_window", "evidence": "rumour", "at": 1}',
    '{"state": "no_window", "evidence": "statusline_sustained", "at": true}',
    '{"state": "no_window", "evidence": "statusline_sustained", "at": NaN}'],
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


def _desktop_only_bus(home, fh, transcripts):
    """A Desktop-only desk's fetch, under the caller's `home`. The bus saves
    the account's verdict, so it is handed a SessionVerdict whose path has
    been through inside() first: with none, it took pc/ingest's default,
    which finds home on its own, and a script that imported this file
    wrote a no-window verdict into a real desk's ~/.blink (the re-review of
    2026-11-01, finding 10; CLAUDE.md, test helpers)."""
    from pc import burn_reader
    from tests.pc import test_burn_line as bl
    from tests.support import transcripts as tx
    tmp_path = home
    verdict = session_verdict.SessionVerdict(
        path=inside(home, session_verdict.verdict_path()))
    if transcripts:
        tx.write(tmp_path, tmp_path / ".claude" / "projects" / "-work"
                 / "code-tab.jsonl",
                 [dict(tx.assistant("msg_%d" % i, "req_%d" % i,
                                    bl.start(i), 1000 * (i + 1)),
                       entrypoint="claude-desktop") for i in range(20)],
                 mtime=bl.NOW - 2)
    f = desktop_doc(tmp_path / "plan-usage-history.json",
                    [(bl.NOW - 1200, fh, 11), (bl.NOW - 300, fh, 12)])
    return ingest.IngestionBus(
        providers=[ClaudeDesktopProvider(path=str(f))],
        now=lambda: bl.NOW,
        burn=burn_reader.BurnReader(
            root=str(tmp_path / ".claude" / "projects")),
        verdict=verdict).fetch()


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
# now, so those rows carry sd and no fh key, and keep the lines they drew.
# Since the owner's decision of 2026-11-01 that shape proves no window only
# as a rise between two samples one poll apart (rose()), so the rows carry
# that; A1 is the measured 10-31 pair, which no longer proves anything. "A0" keeps the 10-30 inputs, a 37-hour-old pre-change
# sample and a status line that has not replied, and shows the rule's (c):
# no proof either way, no mark, the session unknown on its own. G2 is the
# row the rule rightly changes: a fresh process after a Pro or Max reset
# keeps Desktop's 8%.

W = status(seven=2.0)
DK_PRE = [(NOW - 136136, 1, 2)]                 # the 10-30 measured sample
# Not the 10-31 measured sample beside it, which proves nothing on its own
# (owner's decision, 2026-11-01; A0 and A1): the proving shape, a rise one
# poll apart with no fh.
DK_ROSE = DK_PRE + rose(NOW - 3600)
TORN, GONE = "torn", "gone"
REPLIED = session_verdict.evidence(
    False, session_verdict.KIND_STATUSLINE_SUSTAINED, NOW - 7200)
# replied()'s renders as scenario steps, the last written at NOW - 1: the
# sustained replies that prove no window (sustained()).
REPLY_STEPS = [(status(seven=2.0, cost=c), t, t + 1)
               for c, t in sustained(NOW - 1)]

SCENARIOS = {
    "A0 the 10-30 inputs, nothing proved": dict(
        steps=[(W, NOW - 1, NOW)], desktop=DK_PRE,
        want=[(-1, "cli", False, 1)], marked=False),
    # The 10-31 measured pair, {fh: 1, sd: 23} then {sd: 19} 45 h later:
    # no rise one poll apart, so Desktop proves nothing, and with no reply
    # yet nothing is marked. On the owner's desk it is the replies that do.
    "A1 the 10-31 measured pair, nothing replied": dict(
        steps=[(W, NOW - 1, NOW)],
        desktop=[(NOW - 3600 - 45 * 3600, 1, 23), (NOW - 3600, ABSENT, 19)],
        want=[(-1, "cli", False, 1)], marked=False),
    # Ultra, the measured inputs.
    "A measured": dict(
        steps=[(W, NOW - 1, NOW)], desktop=DK_ROSE,
        want=[(-1, "cli", False, 1)], marked=True),
    "B restart, payload 40 min old": dict(
        steps=[(W, NOW - 2400, NOW)], desktop=DK_ROSE,
        want=[(-1, "cli", True, 2400)], marked=True),
    "C two hours idle": dict(
        steps=[(W, NOW - 1, NOW), (None, None, NOW + 7200)],
        desktop=DK_ROSE,
        want=[(-1, "cli", False, 1), (-1, "cli", True, 7201)], marked=True),
    # Ultra, a Desktop sample inside its own bound.
    "D1 Desktop 2 h old": dict(
        steps=[(W, NOW - 1, NOW)], desktop=rose(NOW - 7200),
        want=[(-1, "cli", False, 1)], marked=True),
    "D2 Desktop 5 h old": dict(
        steps=[(W, NOW - 1, NOW)], desktop=rose(NOW - 18000),
        want=[(-1, "cli", False, 1)], marked=True),
    "D3 restart, Desktop 2 h old": dict(
        steps=[(W, NOW - 2400, NOW)], desktop=rose(NOW - 7200),
        want=[(-1, "cli", True, 2400)], marked=True),
    # Desktop's weekly figure is the newest, so it names the frame.
    "D4 Desktop sampled between two polls": dict(
        steps=[(W, NOW - 1, NOW, DK_ROSE),
               (None, None, NOW + 7200,
                DK_ROSE + rose(NOW + 3600, weekly=3))],
        desktop=DK_ROSE,
        want=[(-1, "cli", False, 1), (-1, "desktop", True, 3600)],
        marked=True),
    "D5 Desktop newer than the render": dict(
        steps=[(W, NOW - 600, NOW)], desktop=rose(NOW - 60),
        want=[(-1, "desktop", False, 60)], marked=True),
    # A rising series from before the last reply proved there is no window:
    # the reply is the newer evidence, and the rate goes with the figure.
    # Only the last line is pinned: the cache is written once, so the polls
    # early in the chain see samples ahead of their clock.
    "D6 a rising Desktop series, then a reply": dict(
        steps=REPLY_STEPS,
        desktop=[(NOW - 900 + i * 300, 10 + 5 * i, 2) for i in range(3)],
        want_last=(-1, "cli", False, 1), marked=True),
    # Renders with no rate_limits.
    "E alternating renders, Desktop 2 h old": dict(
        steps=[(W, NOW - 7, NOW - 6), (NO_LIMITS, NOW - 5, NOW - 4),
               (W, NOW - 3, NOW - 2), (NO_LIMITS, NOW - 1, NOW)],
        desktop=rose(NOW - 7200),
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
        desktop=rose(NOW - 7200),
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

    if "want_last" in case:
        assert got[-1] == case["want_last"]
    else:
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
    bus, burns = _burn_desk(tmp_path, [
        (status(seven=12.0, cost=c), t) for c, t in sustained(bl.NOW - 1)])

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
    costs, each repainted on its own events -- the earliest reset, the
    prompt cache expiring. Alternating, every other render is a rise, and
    over a day of an idle Pro desk the rises spanned more than a window.
    A fall starts the whole proof over."""
    p = tmp_path / "statusline.json"
    v = session_verdict.SessionVerdict()
    renders = [(status(cost=(1.0, 1.30)[i % 2]), NOW - 8 * 3600 + i * 120)
               for i in range(8 * 3600 // 120)]

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


def test_finding_4_an_old_terminals_block_rendered_after_the_mark_lifts_it(
        tmp_path):
    """Ultra, verified: the account was proved to have no window at
    T-1000 s and the verdict saved. Terminal X still holds a five_hour block
    it fetched at T-2h, before the change, and repaints it at T-30 s. The
    2026-11-01 build dated such a block from its window's start so that it
    could not lift the mark; the re-review found that machinery could hold
    a wrong mark over a Pro or Max dial for a window (its findings 5, 6
    and 11), and it went. A five_hour rendered after the mark lifts it, this
    one included: the Ultra desk shows X's old figure until a chain proves
    no window again, which is the safe direction."""
    T = NOW
    proved(tmp_path, has_window=False, at=T - 1000)
    p = tmp_path / "statusline.json"
    x = status(five=40.0, five_resets=T - 2 * 3600 + base.SESSION_WINDOW_S,
               seven=20.0, session="x", cost=7.0)
    v = session_verdict.SessionVerdict()
    write(p, x, T - 30)

    assert v.observe(ClaudeCliProvider(path=str(p)).poll(T), T) is False
    assert session_verdict.load(session_verdict.verdict_path()) == (
        True, session_verdict.KIND_STATUSLINE_WINDOW, T - 30)


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
    '{"state": "no_window", "evidence": "statusline_sustained", "at": 1%s}'
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


# --- the re-review of 2026-11-01 ---------------------------------------------
#
# A second /code-review max, of 592efa7...feat/ultra-burn, found the
# two-reply proof unsound. Claude Code 2.1.316's bundle, read and not run:
# the status line renders on events, not every few seconds; API_TIMEOUT_MS
# bounds only the wait for headers, so a body streams for half an hour and
# more; five_hour leaves the line by the LOCAL clock. The proof is now
# sustained absence (claude_cli._reply_without_window). The desks below are
# simulated from those three facts, on generated status lines.

HOUR = 3600


def desk_run(tmp_path, renders, poll_every=60, start=None, end=None,
             polled=None):
    """Drive a real provider and a real verdict over `renders`, [(local
    mtime, doc)] in any order, as the daemon would: a poll every
    `poll_every` seconds of the desk's clock and one a second after each
    render, each poll seeing whatever was rendered last. `polled(now, doc)`,
    given the clock and the render in the file, says whether that poll
    happens at all: the board unplugged, the lid closed. Returns (the times
    of the no-window proofs, the verdict on disk)."""
    renders = sorted(renders, key=lambda r: r[0])
    start = renders[0][0] if start is None else start
    end = renders[-1][0] + 2 if end is None else end
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    v = session_verdict.SessionVerdict(
        path=inside(tmp_path, session_verdict.verdict_path()))
    ticks = sorted({t + 1 for t, _ in renders}
                   | set(range(int(start), int(end), poll_every)))
    proofs, i, shown = [], 0, None
    for now in ticks:
        latest = None
        while i < len(renders) and renders[i][0] <= now:
            latest = renders[i]
            i += 1
        if latest is not None:
            write(p, latest[1], latest[0])
            shown = latest[1]
        if polled is not None and not polled(now, shown):
            continue
        frames = prov.poll(now)
        v.observe(frames, now)
        proofs += [f.window_evidence[2] for f in frames
                   if f.window_evidence is not None
                   and f.window_evidence[0] is False]
    return proofs, session_verdict.load(v.path())


def costed(renders):
    """[(local mtime, five-hour block or None, session)] in time order,
    each a reply: the session's cost rises by a cent and a quarter."""
    out, costs = [], {}
    for at, five, session in sorted(renders, key=lambda r: r[0]):
        costs[session] = costs.get(session, 1.0) + 0.0125
        doc = status(seven=30.0, session=session, cost=costs[session])
        if five is not None:
            doc["rate_limits"]["five_hour"] = {"used_percentage": 20.0,
                                               "resets_at": five}
        out.append((at, doc))
    return out


def pro_desk(skew_s, hours, start, stream_s=45 * 60, every_s=120):
    """A Pro or Max desk in use from `start` (server time) for `hours`: a
    reply every `every_s`, and one long request accepted a minute before
    each reset that streams for `stream_s`, rendering as it goes. The
    window opens at the first request after a reset and ends five hours
    later. Every render carries the headers of its request's acceptance,
    and shows five_hour only while that window's resets_at is ahead of the
    desk's LOCAL clock, `skew_s` fast. Returns costed() renders."""
    renders, reset = [], None
    accepted = [start + k * every_s
                for k in range(int(hours * HOUR // every_s))]
    streams = []
    for a in accepted:
        if reset is None or a >= reset:
            if reset is not None:
                streams.append((reset - 60, reset))
            reset = a + base.SESSION_WINDOW_S
        renders.append((a + 60, reset))
    for a, r in streams:
        renders += [(a + d, r) for d in range(300, stream_s + 1, 300)]
    return costed([(srv + skew_s, r if r > srv + skew_s else None, "s")
                   for srv, r in renders])


def weekly_only_seen(now, doc):
    """A `polled` for desk_run under which no five_hour is ever seen: the
    board is unplugged whenever the file holds one. Since a poll gap no
    longer starts the chain over (the 3rd review of 2026-11-01, finding 1),
    a Pro or Max desk must stay unmarked with no five_hour sighting to
    reset it."""
    return doc is None or not ss_five(doc)


def ss_five(doc):
    return isinstance(doc.get("rate_limits", {}).get("five_hour"), dict)


@pytest.mark.parametrize("unseen", [False, True],
                         ids=["polled throughout", "five_hour never seen"])
@pytest.mark.parametrize("skew_s", [0, 30 * 60, 3 * HOUR],
                         ids=["true clock", "30 min fast", "3 h fast"])
def test_a_pro_desk_with_a_fast_clock_is_never_marked(tmp_path, skew_s,
                                                      unseen):
    """The re-review's finding 3. Claude Code drops five_hour once its
    resets_at is behind the LOCAL clock, so on a desk 30 minutes fast every
    reply in the last half hour of each window renders weekly-only, and two
    of them 25 minutes apart were proof; three hours fast, the last three
    hours of every window. In use through four window ends, with a long
    stream across each one, nothing is proved. Polled throughout, each
    window's first replies render five_hour and start the chain over; with
    every one of those unseen, what holds is that the weekly-only stretches
    are an hour and a quarter apart at the least (3 h fast, a 45-minute
    stream), more than claude_cli.CHAIN_MAX_GAP_S."""
    start = NOW - 24 * HOUR
    proofs, on_disk = desk_run(tmp_path, pro_desk(skew_s, 21, start),
                               polled=weekly_only_seen if unseen else None)

    assert proofs == []
    assert on_disk is None or on_disk[0] is True


@pytest.mark.parametrize("unplugged", [False, True],
                         ids=["polled throughout", "unplugged between"])
def test_a_long_stream_across_each_reset_never_marks(tmp_path, unplugged):
    """The re-review's finding 4. API_TIMEOUT_MS bounds only the wait for
    headers, so a reply accepted before a reset can render weekly-only for
    half an hour after it, each content block a render. Here the desk uses
    Claude Code only across resets -- the windows between are opened from
    claude.ai -- with one 50-minute stream straddling each, the next window
    ending 5 h 20 min after the last, three times over. No five_hour is
    ever rendered after the first, and each stretch is a chain of its own;
    what keeps them from joining is claude_cli.CHAIN_MAX_GAP_S, polled or
    not in between."""
    R = NOW - 20 * HOUR
    renders = [(R - 120, R, "s")]
    stretches = []
    for k in range(3):
        r = R + k * (base.SESSION_WINDOW_S + 20 * 60)
        stretches.append((r, r + 50 * 60))
        renders += [(r + d, None, "s") for d in range(60, 50 * 60, 240)]
    polled = None
    if unplugged:
        def polled(now, doc):
            return any(a <= now <= b + 60 for a, b in stretches)
    proofs, on_disk = desk_run(tmp_path, costed(renders), end=NOW,
                               polled=polled)

    assert proofs == []
    assert on_disk is None or on_disk[0] is True


def ultra_desk(start, hours, turn_s=20 * 60, pauses=(11, 12, 13, 14, 15),
               sessions=("s",)):
    """An Ultra desk at work: turns of `turn_s` with a reply every two
    minutes, then a pause of 11 to 15 minutes with nothing rendered at
    all, turn after turn, the turns taken by `sessions` in turn."""
    renders, t, k = [], start, 0
    while t < start + hours * HOUR:
        session = sessions[k % len(sessions)]
        renders += [(t + d, None, session) for d in range(0, turn_s, 120)]
        t += turn_s + pauses[k % len(pauses)] * 60
        k += 1
    return costed(renders)


def test_an_ultra_desk_in_turns_with_pauses_is_marked(tmp_path):
    """The re-review's finding 2. The two-reply rule took a render more than
    600 s after the last as a gap, on the premise that Claude Code renders
    every few seconds, and a user who paused over ten minutes between turns
    was never marked. Renders are events: here twenty-minute turns and
    pauses of 11 to 15 minutes, six hours of them, the daemon polling every
    minute throughout. Marked once more than CHAIN_SPAN_S has passed with
    no five_hour, and not before."""
    start = NOW - 6 * HOUR
    proofs, on_disk = desk_run(tmp_path, ultra_desk(start, 6))

    assert proofs
    assert proofs[0] - start >= claude_cli.CHAIN_SPAN_S
    assert proofs[0] - start < claude_cli.CHAIN_SPAN_S + 40 * 60
    # Written once, when it changed: dated by the first proof.
    assert on_disk == (False, session_verdict.KIND_STATUSLINE_SUSTAINED,
                       proofs[0])


def test_replies_from_several_sessions_make_one_chain(tmp_path):
    """The account is one, so its replies are counted across the desk; a
    rise is still recognised within a session."""
    start = NOW - 6 * HOUR
    proofs, _ = desk_run(tmp_path,
                         ultra_desk(start, 6, sessions=("a", "b", "c")))

    assert proofs


def test_a_five_hour_anywhere_starts_the_chain_over(tmp_path):
    """Any five_hour in the file, from any session, means the account has a
    window. One render of one in the middle of six hours of Ultra-shaped
    replies leaves less than a chain after it."""
    start = NOW - 6 * HOUR
    renders = ultra_desk(start, 6)
    mid = start + 3 * HOUR
    other = status(five=5.0, five_resets=mid + base.SESSION_WINDOW_S,
                   seven=30.0, session="other", cost=1.0)
    proofs, on_disk = desk_run(tmp_path, renders + [(mid, other)])

    assert proofs == []
    assert on_disk == (True, session_verdict.KIND_STATUSLINE_WINDOW, mid)


def workday(start, end, every_s=120, silent=()):
    """An Ultra desk replying every `every_s` from `start` to `end`, one
    session, nothing rendered inside any (from, to) of `silent`."""
    return costed([(t, None, "s") for t in range(int(start), int(end), every_s)
                   if not any(a <= t < b for a, b in silent)])


NINE = NOW - 10 * HOUR          # "09:00" of a desk's day, 17:30 in the past


def at(hh, mm):
    """The desk's clock at hh:mm on the day that starts at NINE."""
    return NINE + (hh - 9) * HOUR + mm * 60


@pytest.mark.parametrize("dark", [None, "lid", "unplugged"],
                         ids=["no break", "lid closed 13:00-13:10",
                              "unplugged 13:00-13:10"])
def test_an_ultra_workday_with_a_break_in_polling_is_marked_that_day(
        tmp_path, dark):
    """The 3rd review of 2026-11-01, finding 1. Polls run only while the
    board is alive, and any gap in them over five minutes used to throw the
    chain away: an Ultra desk replying every two minutes from 09:00 to 17:30
    was never marked with the lid closed from 13:00 to 13:10 (nothing
    rendered, nothing polled) or the board unplugged for those ten minutes
    while Claude Code kept replying. A poll gap is no longer a reset: the
    first rise after it is measured against a baseline twelve minutes old,
    inside claude_cli.BASELINE_MAX_S, and the chain goes on. Marked at
    14:17, five and a quarter hours after the first reply that rose
    against a baseline (09:02), all three ways."""
    lid = (at(13, 0), at(13, 10))
    renders = workday(at(9, 0), at(17, 30),
                      silent=(lid,) if dark == "lid" else ())
    polled = None
    if dark is not None:
        def polled(now, doc):
            return not lid[0] <= now < lid[1]
    proofs, on_disk = desk_run(tmp_path, renders, polled=polled)

    assert proofs
    assert at(14, 17) <= proofs[0] <= at(14, 18)
    assert on_disk[0] is False


def test_an_ultra_workday_with_a_36_minute_silence_is_marked_that_day(
        tmp_path):
    """The 3rd review's finding 2. The longest gap, CHAIN_MAX_GAP_S, was
    measured from the last reply the chain had TALLIED, and replies within
    25 minutes of it were not tallied, so a 36-minute silence from 12:02 to
    12:38 left the reply at 12:40 more than an hour after the tally's 11:38
    and the chain started over: not marked that day. It is measured from
    the latest weekly-only reply. The reply at 12:40 rises against a
    baseline 38 minutes old and is not counted (BASELINE_MAX_S); the one at
    12:42 is, 40 minutes after 12:02, and the day is marked at 14:17."""
    renders = workday(at(9, 0), at(17, 30), silent=((at(12, 3), at(12, 40)),))
    proofs, on_disk = desk_run(tmp_path, renders)

    assert proofs
    assert at(14, 17) <= proofs[0] <= at(14, 18)


def test_random_ultra_days_with_breaks_of_up_to_55_minutes_are_marked(
        tmp_path):
    """The 3rd review's finding 2, at random: forty generated ten-hour days
    of turns of 5 to 40 minutes, a reply every 20 s to 2 min, and breaks of
    5 to 55 minutes with nothing rendered. With the gap measured from the
    last tallied reply, 38% of such days with breaks of 20 to 55 minutes
    were marked; from the latest reply, all of them, and none sooner than
    CHAIN_SPAN_S after the first reply. Fed straight to the provider, one
    poll a second after each render, since gaps in polling decide nothing."""
    import random
    rng = random.Random(20261101)
    p = tmp_path / "statusline.json"
    for day in range(40):
        prov = ClaudeCliProvider(path=str(p))
        t0 = NOW - 11 * HOUR
        t, cost, first, proof = t0, 1.0, None, None
        while t < t0 + 10 * HOUR and proof is None:
            turn_end = t + rng.uniform(5, 40) * 60
            while t < turn_end and proof is None:
                cost += 0.05
                write(p, status(seven=30.0, cost=cost), t)
                first = t if first is None else first
                if any(f.window_evidence and f.window_evidence[0] is False
                       for f in prov.poll(t + 1)):
                    proof = t
                t += rng.uniform(20, 120)
            t += rng.uniform(5, 55) * 60
        assert proof is not None, "day %d not marked" % day
        assert proof - first >= claude_cli.CHAIN_SPAN_S


# --- the guards, each pinned (the 3rd review of 2026-11-01, finding 8) ------
#
# Five mutations of claude_cli.py, each shown to change behavior, had left
# every test green. Each test below fails with its guard deleted.


def fed(p, prov, steps):
    """Drive `prov` by hand: for each (doc or None, mtime, now) write the doc
    at that mtime if there is one, then poll at `now`. Returns the times
    of the no-window proofs. The poll clock and the file's mtime are given
    apart, so either can be made to go back without the other."""
    proofs = []
    for doc, mtime, now in steps:
        if doc is not None:
            write(p, doc, mtime)
        proofs += [f.window_evidence[2] for f in prov.poll(now)
                   if f.window_evidence is not None
                   and f.window_evidence[0] is False]
    return proofs


class Desk:
    """One Ultra session's replies, every two minutes, each polled a
    second after it is written; `cost` runs on across calls."""

    def __init__(self, session="a"):
        self.session, self.cost = session, 1.0

    def reply(self, t, now=None, rise=0.05):
        self.cost += rise
        return (status(seven=30.0, session=self.session, cost=self.cost), t,
                t + 1 if now is None else now)

    def replies(self, start, end, every_s=120):
        return [self.reply(t) for t in range(int(start), int(end), every_s)]


T0 = NOW - 12 * HOUR
# Five hours and ten minutes into a chain: one more short stretch of
# replies would complete it.
ALMOST = T0 + claude_cli.CHAIN_SPAN_S - 5 * 60


def test_a_rise_against_a_baseline_older_than_the_bound_is_no_reply(
        tmp_path):
    """BASELINE_MAX_S. Pro or Max: eight terminals render a window that
    resets at R, the board is unplugged, each replies inside the window
    (rendering five_hour, none of it seen), and after R each repaints
    weekly-only on its own event -- the earliest reset, the prompt cache
    expiring -- fifty minutes apart. Each repaint is a cost rise against
    that terminal's last render seen, hours before, and with no bound on
    that baseline's age the eight make a chain of six hours with no
    five_hour seen in it: a Pro desk marked. A rise may be a reply long
    before the render that shows it, so it counts only against a baseline
    at most half an hour old. Since a poll gap no longer starts the chain
    over, this bound is what stands between an unwatched window and the
    proof."""
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    R = T0 + 4 * HOUR
    steps = [(status(five=20.0, five_resets=R, seven=30.0, session="t%d" % i,
                     cost=1.0), T0 + 10 * i, T0 + 10 * i + 1)
             for i in range(8)]
    steps += [(status(seven=30.0, session="t%d" % i, cost=2.0),
               R + 60 + i * 50 * 60, R + 61 + i * 50 * 60) for i in range(8)]

    assert fed(p, prov, steps) == []


def test_a_session_whose_mtime_goes_back_starts_the_chain_over(tmp_path):
    """The per-session mtime check. Session a's last render was a repaint
    at L+5 min; then the file carries a rise from a dated L+3 min -- a file
    restored, a clock stepped back with the polls' clock running on. It is
    after the chain's latest reply, so only the session's own baseline
    shows the clock went back, and the spacing means nothing. The chain
    starts over; without the check it went on and was proved at L+15."""
    p = tmp_path / "statusline.json"
    a = Desk()
    L = ALMOST
    steps = a.replies(T0, L + 1)
    steps.append((status(seven=30.0, session="a", cost=a.cost), L + 300,
                  L + 301))
    steps.append(a.reply(L + 180, now=L + 303))
    steps += a.replies(L + 600, L + 30 * 60)

    assert fed(p, ClaudeCliProvider(path=str(p)), steps) == []


def test_a_reply_dated_before_the_chains_latest_starts_it_over(tmp_path):
    """The chain's own mtime check. Session b's rise is dated two minutes
    before session a's latest reply, against a baseline of b's from before
    that, so b's own history looks in order; the desk's does not. Without
    the check the chain went on and was proved at L+15."""
    p = tmp_path / "statusline.json"
    a, b = Desk("a"), Desk("b")
    L = ALMOST
    steps = [s for s in a.replies(T0, L + 1) if s[1] != L - 300]
    steps.insert(-3, (status(seven=30.0, session="b", cost=b.cost), L - 300,
                      L - 299))
    steps.append(b.reply(L - 120, now=L + 3))
    steps += a.replies(L + 120, L + 30 * 60)

    assert fed(p, ClaudeCliProvider(path=str(p)), steps) == []


def test_the_polls_clock_going_back_starts_the_proof_over(tmp_path):
    """The `0 <=` arm of poll(). The desk's clock steps back ten minutes
    between two polls. The next render is dated after the last one seen,
    so no mtime goes backwards, but every interval measured across the
    step is short by ten minutes, which is the unsafe direction for
    CHAIN_MAX_GAP_S. Without the arm the chain went on and was proved at
    L+15."""
    p = tmp_path / "statusline.json"
    a = Desk()
    L = ALMOST
    steps = a.replies(T0, L + 1)
    steps.append((None, None, L - 9 * 60))           # the clock back
    steps += a.replies(L + 120, L + 30 * 60)

    assert fed(p, ClaudeCliProvider(path=str(p)), steps) == []


@pytest.mark.parametrize("who", ["the file's", "another session's"])
def test_a_clock_gone_back_leaves_no_baseline_before_it(tmp_path, who):
    """_forget_replies. A baseline taken by a clock that has since gone back
    is no baseline: neither the render the file holds at the step, which
    every poll reads again, nor any other session's last render. After the
    step, one reply, forty minutes of silence, then replies every two
    minutes. Against a pre-step baseline that first reply began a new chain
    at once, which the silence (under CHAIN_MAX_GAP_S) did not break:
    proved 42 minutes early. Clearing the baselines, as the code did until
    the 3rd review of 2026-11-01, stopped it for the other session only:
    the poll that saw the clock go back read the file's render straight
    back in as one."""
    p = tmp_path / "statusline.json"
    a, b = Desk("a"), Desk("b")
    L = T0 + 2 * HOUR
    steps = a.replies(T0, L + 1)
    steps.insert(-2, b.reply(L - 3 * 60))
    steps.append((None, None, L - 9 * 60))           # the clock back
    d = a if who == "the file's" else b
    steps.append(d.reply(L + 120))
    restart = L + 120 + 42 * 60
    steps += d.replies(restart - 120, restart + claude_cli.CHAIN_SPAN_S
                       + 10 * 60)
    proofs = fed(p, ClaudeCliProvider(path=str(p)), steps)

    assert proofs
    assert proofs[0] - restart >= claude_cli.CHAIN_SPAN_S


def test_a_repaint_of_a_block_seen_hours_ago_is_dated_by_its_render(
        tmp_path):
    """Five_hour evidence is dated by the payload's mtime, the render, not
    by the block's first sighting (first_seen_at). Terminal X's block was
    first seen three hours ago, before the account was proved to have no
    window at T-1000; X repaints it at T-30. That is a five_hour rendered
    after the mark, and lifts it. finding 4's test sees its block once,
    where the two dates agree and cannot tell which one was used."""
    T = NOW
    proved(tmp_path, has_window=False, at=T - 1000)
    p = tmp_path / "statusline.json"
    prov = ClaudeCliProvider(path=str(p))
    x = status(five=40.0, five_resets=T - 2 * 3600 + base.SESSION_WINDOW_S,
               seven=20.0, session="x", cost=7.0)
    write(p, x, T - 3 * 3600)
    first = prov.poll(T - 3 * 3600 + 1)
    assert [f.first_seen_at for f in first] == [T - 3 * 3600]
    write(p, x, T - 30)
    v = session_verdict.SessionVerdict()
    frames = prov.poll(T)

    assert [f.first_seen_at for f in frames] == [T - 3 * 3600]
    assert v.observe(frames, T) is False
    assert session_verdict.load(session_verdict.verdict_path()) == (
        True, session_verdict.KIND_STATUSLINE_WINDOW, T - 30)


def test_a_break_longer_than_the_chain_gap_starts_it_over(tmp_path):
    """claude_cli.CHAIN_MAX_GAP_S. Five hours of replies with a lunch of an
    hour and a half in the middle are two chains, neither long enough."""
    start = NOW - 7 * HOUR
    renders = [r for r in ultra_desk(start, 7)
               if not start + 3 * HOUR <= r[0] < start + 4.5 * HOUR]
    proofs, _ = desk_run(tmp_path, renders)

    assert proofs == []


def test_a_cost_falling_in_any_session_starts_the_chain_over(tmp_path):
    """Two processes on one session_id cannot be told from one, so a fall
    anywhere puts the whole proof in doubt."""
    start = NOW - 6 * HOUR
    renders = ultra_desk(start, 6)
    mid = start + 3 * HOUR
    fell = status(seven=30.0, session="s", cost=0.5)
    proofs, _ = desk_run(tmp_path, renders + [(mid, fell)])

    assert all(t - mid >= claude_cli.CHAIN_SPAN_S for t in proofs)


def test_two_replies_far_apart_are_no_longer_proof(tmp_path):
    """What the rule of 2026-11-01 accepted, 25 minutes apart and more."""
    t0 = NOW - 2 * HOUR
    renders = costed([(t0, None, "s"), (t0 + 60, None, "s"),
                      (t0 + 60 + 1800, None, "s")])
    proofs, on_disk = desk_run(tmp_path, renders, end=NOW)

    assert proofs == [] and on_disk is None


@pytest.mark.parametrize("fresh", [False, True],
                         ids=["running daemon", "fresh process"])
def test_any_five_hour_rendered_after_the_mark_clears_it(tmp_path, fresh):
    """Decision B of the re-review: a five_hour block lifts the mark when it
    was RENDERED after it, whenever its window opened -- here four hours
    before the mark, which the build of 2026-11-01 dated it by, so that a
    fresh process or a reply nobody watched could not lift a mark made
    inside the window (the re-review's finding 5)."""
    p = tmp_path / "statusline.json"
    start = NOW - 6 * HOUR
    renders = ultra_desk(start, 6)
    prov = ClaudeCliProvider(path=str(p))
    v = session_verdict.SessionVerdict()
    ticks = sorted({t + 1 for t, _ in renders}
                   | set(range(int(start), int(NOW - 2), 60)))
    i = 0
    for now in ticks:
        while i < len(renders) and renders[i][0] <= now:
            write(p, renders[i][1], renders[i][0])
            i += 1
        v.observe(prov.poll(now), now)
    assert session_verdict.load(session_verdict.verdict_path())[0] is False
    assert v.current()[2] > NOW - 4 * HOUR

    opened = NOW - 4 * HOUR
    write(p, status(five=31.0, five_resets=opened + base.SESSION_WINDOW_S,
                    seven=30.0, session="s", cost=renders[-1][1]["cost"]
                    ["total_cost_usd"]), NOW - 1)
    if fresh:
        prov, v = ClaudeCliProvider(path=str(p)), session_verdict.SessionVerdict()
    assert v.observe(prov.poll(NOW), NOW) is False
    assert session_verdict.load(session_verdict.verdict_path())[0] is True


@pytest.mark.parametrize("kind", ["statusline_reply", "desktop_no_fh"])
def test_a_verdict_saved_under_a_rejected_rule_is_decided_again(tmp_path,
                                                                kind):
    """Decision D. The owner's own desk had {"evidence": "desktop_no_fh"} on
    disk, and builds of 592efa7 saved statusline_reply from one reply
    streaming across a reset: both are rules a Pro or Max desk could meet.
    The kinds were renamed with the rules, so the old names read as no
    verdict, and a weekly-only status line with nothing proved is not
    marked."""
    path = inside(tmp_path, session_verdict.verdict_path())
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"state": "no_window", "evidence": kind, "at": NOW - 60}, f)

    assert session_verdict.load(path) is None
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: NOW)
    assert wire(bus)["session_pct"] == -1
    assert bus.claude_page().no_session_window is False


def test_a_failed_save_is_one_attempt(tmp_path, monkeypatch):
    """The re-review's finding 14. save() went through _write_json's
    os.replace, retried ten times 0.3 s apart on Windows' WinError 5 and 32,
    on the serial thread, every SAVE_RETRY_S: 2.7 s of stalled pongs a
    minute on a desk whose verdict file was read-only."""
    from pc import update
    calls, slept = [], []

    def held(src, dst):
        calls.append(dst)
        e = PermissionError(13, "held")
        e.winerror = 32
        raise e
    monkeypatch.setattr(os, "replace", held)
    monkeypatch.setattr(update.time, "sleep", slept.append)

    assert session_verdict.save(inside(tmp_path,
                                       session_verdict.verdict_path()),
                                no(NOW - 60)) is False
    assert len(calls) == 1 and slept == []


def test_a_failed_save_is_retried_at_once_when_the_clock_goes_back(
        tmp_path, monkeypatch):
    """The re-review's finding 15. With only `now >= failed + 60`, an NTP
    step back of an hour held the retry off for that hour."""
    path = inside(tmp_path, session_verdict.verdict_path())
    real, calls = os.replace, []

    def refuse_once(src, dst):
        calls.append(dst)
        if len(calls) == 1:
            raise PermissionError(13, "refused")
        return real(src, dst)
    monkeypatch.setattr(os, "replace", refuse_once)
    v = session_verdict.SessionVerdict()
    v.observe([framed(no(NOW - 60))], NOW)
    assert not os.path.exists(path)

    v.observe([], NOW + 10)                 # inside the minute: not yet
    assert len(calls) == 1
    v.observe([], NOW - HOUR)               # the clock stepped back
    assert session_verdict.load(path)[0] is False


HUGE = int("1" + "0" * 400)


@pytest.mark.parametrize("where", ["five resets_at", "seven resets_at",
                                   "five used_percentage"])
def test_an_int_too_big_for_a_float_in_the_status_line_is_no_number(
        tmp_path, where):
    """The re-review's finding 12. _block_key's float(resets_at), now the
    first thing every poll() does, and statusline_source._window's
    float(used_percentage) raised OverflowError on such an int, and the
    source was benched. Not a number, like any other."""
    doc = status(five=20.0, five_resets=NOW + 600, seven=12.0)
    window, field = (("five_hour" if where.startswith("five")
                      else "seven_day"), where.split(" ")[1])
    doc["rate_limits"][window][field] = HUGE
    p = write(tmp_path / "statusline.json", doc, NOW - 1)

    frames = ClaudeCliProvider(path=str(p)).poll(NOW)

    assert frames[0].weekly_pct == 12.0
    if field == "used_percentage":
        assert frames[0].session_pct == base.UNKNOWN


@pytest.mark.parametrize("field", ["t", "fh", "sd"])
def test_an_int_too_big_for_a_float_in_desktop_is_no_number(tmp_path,
                                                            field):
    """The same in Claude Desktop's samples: one dropped the whole file as
    a changed format. The sample, or the figure, is refused, and the rest
    of the file is read."""
    rows = [dc.sample(int((NOW - 900) * 1000), fh=7, sd=3),
            dc.sample(int((NOW - 600) * 1000), fh=8, sd=4)]
    if field == "t":
        rows[1]["t"] = HUGE
    else:
        rows[1]["u"][field] = HUGE
    f = tmp_path / "plan-usage-history.json"
    f.write_text(json.dumps({"version": 2, "samples": rows}),
                 encoding="utf-8")

    verdict, frame = ClaudeDesktopProvider(path=str(f)).verdict(NOW)

    assert verdict == VERDICT_FRAME
    want = {"t": (7.0, 3.0), "fh": (base.UNKNOWN, 4.0), "sd": (8.0,
                                                             base.UNKNOWN)}
    assert (frame.session_pct, frame.weekly_pct) == want[field]


def test_the_desktop_only_bus_writes_only_inside_the_home_it_is_given(
        tmp_path):
    """The re-review's finding 10: _desktop_only_bus saved its verdict
    through pc/ingest's default SessionVerdict, which finds home itself."""
    with pytest.raises(AssertionError, match="outside the caller's home"):
        _desktop_only_bus(tmp_path / "elsewhere", ABSENT, transcripts=False)
    assert not os.path.exists(session_verdict.verdict_path())
