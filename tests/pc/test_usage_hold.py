"""The dial must not step backwards by a rounding difference.

Claude Code's status line and Claude Desktop's cache report the same
five-hour window and can disagree by a fraction of a point. The logged trace
was cli 39, desktop 40, cli 39, cli 41, and the panel drew 39, 40, 39, 41:
the third reading was Claude Code REDRAWING the same rate_limits block it
had already shown, and the file's fresh mtime let it take the dial back from
a newer desktop sample. See docs/open-bugs/usage-merge-jitter.md.

Every scenario here is built from code: a status line written to tmp_path
with a chosen mtime and read back through the real provider, and desktop
frames constructed directly.
"""
import json
import os

import pytest

from pc import normalizer
from pc import protocol
from pc.providers import base
from pc.providers import claude_cli
from pc.providers.claude_cli import ClaudeCliProvider

T0 = 1_787_700_000.0
RESET_5H = T0 + 3 * 3600
RESET_7D = T0 + 4 * 86400
STALE = 1800          # the bound every Claude source uses


def block(five_hour, seven_day=20.0, s_reset=RESET_5H, w_reset=RESET_7D):
    return {"rate_limits": {
        "five_hour": {"used_percentage": five_hour, "resets_at": s_reset},
        "seven_day": {"used_percentage": seven_day, "resets_at": w_reset},
    }}


class StatusLine:
    """A status-line file on disk and the provider that reads it."""

    def __init__(self, tmp_path):
        self.path = tmp_path / "statusline.json"
        self.provider = ClaudeCliProvider(path=str(self.path))

    def write(self, payload, mtime):
        self.path.write_text(json.dumps(payload), encoding="utf-8")
        os.utime(self.path, (mtime, mtime))

    def poll(self, now):
        return self.provider.poll(now)


def desktop(at, now, session=base.UNKNOWN, weekly=base.UNKNOWN, src="desktop"):
    return base.NormalizedUsageFrame(
        provider="claude", src=src, observed_at=at, session_pct=session,
        weekly_pct=weekly, stale=(now - at) > STALE)


def draw(frames, now):
    m = normalizer.merge(frames)
    return m, protocol.frame_to_usage(m, now)


# --- the logged trace -------------------------------------------------------

# (seconds after T0, which source spoke, its five-hour figure, what the dial
# must read afterwards). The status line's second 39 is the same rate_limits
# block written again on a redraw -- the case the old merge drew as 39.
TRACE = (
    (0, "cli", 39.0, 39.0),
    (60, "desktop", 40.0, 40.0),
    (120, "cli", 39.0, 40.0),
    (180, "cli", 41.0, 41.0),
)


def replay(tmp_path, rows):
    """Drive rows through the real status-line provider plus a Desktop frame
    and return what the merged dial read after each one."""
    sl = StatusLine(tmp_path)
    desktop_sample = None
    readings = []
    for offset, source, value, _ in rows:
        now = T0 + offset
        if source == "cli":
            sl.write(block(value), now)
        else:
            desktop_sample = (now, value)
        extra = ([] if desktop_sample is None else
                 [desktop(desktop_sample[0], now, session=desktop_sample[1])])
        readings.append(normalizer.merge(sl.poll(now) + extra).session_pct)
    return readings


def test_the_condensed_field_trace_draws_39_40_40_41(tmp_path):
    assert replay(tmp_path, TRACE) == [row[3] for row in TRACE]


def test_a_new_block_a_fraction_below_keeps_the_dial(tmp_path):
    """Not a redraw this time: Claude Code fetched again and its rounding
    put the same window 0.6 below Desktop. That is the hold, not the
    first-seen memory, and it is what keeps the dial from stepping back."""
    sl = StatusLine(tmp_path)
    sl.write(block(39.0), T0)
    sl.poll(T0)
    d = desktop(T0 + 60, T0 + 120, session=40.0)
    sl.write(block(39.4, seven_day=21.0), T0 + 120)

    m = normalizer.merge(sl.poll(T0 + 120) + [d])

    assert m.session_pct == 40.0
    assert m.weekly_pct == 21.0


def test_the_weekly_dial_is_held_on_its_own():
    cli = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0 + 100, weekly_pct=67.5,
        session_pct=12.0)
    d = desktop(T0 + 50, T0 + 100, session=30.0, weekly=68.0)

    m = normalizer.merge([cli, d])

    assert m.weekly_pct == 68.0          # held, 0.5 above
    assert m.session_pct == 12.0         # not held, 18 below is a real fall


def test_a_redraw_does_not_undo_a_newer_sample_from_elsewhere(tmp_path):
    """The first-seen rule on its own, with the gap too wide for the hold to
    hide it: Claude Code fetched 38, then Desktop sampled 40 (it sees
    claude.ai, the status line does not), then Claude Code redrew its 38.
    The 38 is the OLDER reading however new its file is."""
    sl = StatusLine(tmp_path)
    sl.write(block(38.0), T0)
    sl.poll(T0)
    d = desktop(T0 + 60, T0 + 120, session=40.0)
    sl.write(block(38.0), T0 + 120)

    m = normalizer.merge(sl.poll(T0 + 120) + [d])

    assert (m.session_pct, m.src) == (40.0, "desktop")

    # Until Desktop's sample goes stale; then the redrawn 38 is all there
    # is. Polled each minute, that is 1920 s (see the open-bugs note).
    dial = {}
    for minute in range(3, 34):
        now = T0 + 60 * minute
        sl.write(block(38.0), now)
        frames = sl.poll(now) + [desktop(T0 + 60, now, session=40.0)]
        dial[60 * minute] = normalizer.merge(frames).session_pct
    assert dial[1860] == 40.0
    assert dial[1920] == 38.0


# --- a real change is shown at once -----------------------------------------

def test_a_reset_to_zero_is_shown_at_once(tmp_path):
    sl = StatusLine(tmp_path)
    reset = T0 + 600
    sl.write(block(40.0, s_reset=reset), T0)
    d = desktop(T0 + 300, T0 + 300, session=40.0)
    assert normalizer.merge(sl.poll(T0 + 300) + [d]).session_pct == 40.0

    m = normalizer.merge(sl.poll(reset + 30) + [d])

    assert m.session_pct == 0.0


def test_a_fall_of_two_points_is_shown_at_once():
    d = desktop(T0, T0 + 60, session=40.0)
    cli = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0 + 60, session_pct=38.0)

    assert normalizer.merge([d, cli]).session_pct == 38.0


def test_a_rise_is_shown_at_once():
    d = desktop(T0, T0 + 60, session=40.0)
    cli = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0 + 60, session_pct=40.5)

    assert normalizer.merge([d, cli]).session_pct == 40.5


def test_a_pre_reset_reading_is_never_held_even_within_the_band():
    """A frame from before an observed rollover is out of the contest
    entirely, so it cannot hold the dial either -- even when its number
    happens to sit inside the band."""
    rolled = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0 - 600, session_pct=0.0,
        session_rolled_at=T0)
    old = desktop(T0 - 30, T0 + 60, session=0.8)

    assert normalizer.merge([rolled, old]).session_pct == 0.0


def test_two_different_windows_never_hold_each_other():
    """A reading from another five-hour window is across a reset whether or
    not anybody watched that reset happen."""
    old_window = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0, session_pct=1.0,
        session_resets_at=T0 + 3600)
    new_window = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0 + 60, session_pct=0.5,
        session_resets_at=T0 + 3600 + 5 * 3600)

    assert normalizer.merge([old_window, new_window]).session_pct == 0.5


def test_a_stampless_sample_from_before_the_window_opened_never_holds():
    """Claude Code fetched the new window directly -- a fresh reset stamp
    and no rollover reported, because it never saw the old one end. Desktop
    sampled 0.8 a minute before the reset. Desktop carries no stamp, so only
    its sample time can place it, and that time is before the new window
    began: the 0.8 belongs to the window that was forgiven."""
    reset = T0
    pre_reset = desktop(reset - 60, reset + 30, session=0.8)
    post_reset = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=reset + 30,
        session_pct=0.3, session_resets_at=reset + 5 * 3600)

    assert normalizer.merge([pre_reset, post_reset]).session_pct == 0.3


def test_the_same_rule_places_a_weekly_sample_by_the_seven_day_window():
    reset = T0
    pre_reset = desktop(reset - 60, reset + 30, weekly=0.9, session=10.0)
    post_reset = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=reset + 30,
        session_pct=10.0, weekly_pct=0.2, weekly_resets_at=reset + 7 * 86400)

    assert normalizer.merge([pre_reset, post_reset]).weekly_pct == 0.2


def test_a_stampless_sample_inside_the_window_still_holds():
    reset_next = T0 + 5 * 3600 - 600        # the window opened 600 s ago
    inside = desktop(T0 - 300, T0, session=1.0)
    cli_frame = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0, session_pct=0.4,
        session_resets_at=reset_next)

    assert normalizer.merge([inside, cli_frame]).session_pct == 1.0


# --- staleness ----------------------------------------------------------------

def test_a_stale_frame_never_holds():
    old = desktop(T0, T0 + STALE + 60, session=40.0)
    assert old.stale
    cli = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0 + STALE + 30,
        session_pct=39.5)

    m = normalizer.merge([old, cli])

    assert m.session_pct == 39.5
    assert (m.src, m.stale) == ("cli", False)


def test_a_fresh_frame_outranks_a_stale_one_whatever_their_first_seen():
    stale_new = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=T0, first_seen_at=T0,
        session_pct=55.0, stale=True)
    fresh_old = desktop(T0 - 100, T0, session=50.0)

    m = normalizer.merge([stale_new, fresh_old])

    assert (m.session_pct, m.src, m.stale) == (50.0, "desktop", False)


# --- the caption ----------------------------------------------------------------

def test_a_redrawn_status_line_dates_the_caption_for_forty_minutes(tmp_path):
    """Claude Code redraws an unchanged 39 every minute; Desktop sampled 39
    once, 300 s in. Desktop wins the ranking (its 39 is the newer FETCH) but
    the number is the same, so the caption must be the status line's: live,
    age 0, the whole time -- not "5 min ago" growing toward a stale mark."""
    sl = StatusLine(tmp_path)
    for minute in range(0, 41):
        now = T0 + 60 * minute
        sl.write(block(39.0), now)
        frames = sl.poll(now)
        if now >= T0 + 300:
            frames = frames + [desktop(T0 + 300, now, session=39.0)]
        m, wire = draw(frames, now)
        assert m.session_pct == 39.0
        assert m.src == "cli", minute
        assert wire["age_s"] == 0, minute
        assert wire["stale"] is False, minute


def test_a_held_number_never_ages_sooner_than_before(tmp_path):
    """Desktop at 40 from 30 s in, the status line a fresh 39 redrawn every
    minute. Before this change the redraw owned the dial and its age; now
    Desktop's 40 holds the dial, and the caption must still be no older than
    the old merge would have drawn.

    The hold lasts only as long as Desktop's sample is fresh. At 1860 s it
    is past the half-hour bound, a stale frame never holds, and the dial
    goes to the status line's figure: one step back, half an hour late,
    instead of one every time the two sources take turns. Pinned here so
    nobody mistakes it for a regression -- see the open-bugs note."""
    sl = StatusLine(tmp_path)
    for minute in range(0, 41):
        now = T0 + 60 * minute
        # A fresh fetch every ten minutes, a redraw in between.
        cli_value = 39.0 + 0.01 * (minute // 10)
        sl.write(block(cli_value), now)
        frames = sl.poll(now)
        desktop_fresh = False
        if now >= T0 + 30:
            sample = desktop(T0 + 30, now, session=40.0)
            desktop_fresh = not sample.stale
            frames = frames + [sample]
        m, wire = draw(frames, now)
        if desktop_fresh:
            assert m.session_pct == 40.0, minute
        else:
            assert m.session_pct == cli_value, minute
        assert desktop_fresh == (0 < 60 * minute <= 1830), minute
        old_age = min(int(now - f.observed_at) for f in frames
                      if f.session_pct >= 0)
        assert wire["age_s"] <= old_age, minute
        # The old merge never went stale here (the redraw kept it live),
        # so neither may this one.
        assert wire["stale"] is False, minute


# --- the memory -----------------------------------------------------------------

def test_a_redraw_keeps_the_earliest_time(tmp_path):
    sl = StatusLine(tmp_path)
    sl.write(block(39.0), T0)
    sl.poll(T0)
    sl.write(block(39.0), T0 + 500)

    (f,) = sl.poll(T0 + 500)

    assert f.observed_at == T0 + 500      # the age of the file, as before
    assert f.first_seen_at == T0          # the age of the numbers


def test_a_file_from_before_the_remembered_time_is_dated_by_itself(tmp_path):
    """The provider remembered T0 + 1000 for this block, then the same
    block turned up in a file stamped T0 -- the clock was wound back, or
    the file came out of a backup. The frame takes the file's own stamp:
    a figure cannot have been fetched after the file holding it was
    written."""
    sl = StatusLine(tmp_path)
    sl.write(block(39.0), T0 + 1000)
    sl.poll(T0 + 1000)
    sl.write(block(39.0), T0)

    (f,) = sl.poll(T0 + 1000)

    assert f.first_seen_at == T0 == f.observed_at


def test_the_memory_stays_bounded_while_one_terminal_climbs(tmp_path):
    sl = StatusLine(tmp_path)
    for i in range(300):
        at = T0 + 60 * i
        sl.write(block(round(i * 0.3, 1), seven_day=float(i)), at)
        frames = sl.poll(at)
        assert frames[0].first_seen_at == at
    assert len(sl.provider._fetch_times) <= claude_cli.FETCH_MEMORY_LIMIT
    assert claude_cli.FETCH_MEMORY_LIMIT <= 256

    # And the block it is showing right now was not the one evicted.
    sl.write(block(round(299 * 0.3, 1), seven_day=299.0), T0 + 60 * 400)
    (f,) = sl.poll(T0 + 60 * 400)
    assert f.first_seen_at == T0 + 60 * 299


def test_ended_blocks_are_evicted_before_the_oldest(tmp_path):
    sl = StatusLine(tmp_path)
    limit = claude_cli.FETCH_MEMORY_LIMIT
    # The oldest entry belongs to a window that is still running.
    sl.write(block(1.0, s_reset=T0 + 10 * 86400), T0)
    sl.poll(T0)
    # Then a run of blocks whose windows end almost at once.
    for i in range(limit - 1):
        sl.write(block(50.0 + i * 0.1, s_reset=T0 + 5), T0 + 1 + i)
        sl.poll(T0 + 1 + i)
    later = T0 + 10_000
    sl.write(block(2.0, s_reset=T0 + 10 * 86400), later)
    sl.poll(later)

    assert len(sl.provider._fetch_times) <= limit
    sl.write(block(1.0, s_reset=T0 + 10 * 86400), later + 1)
    (f,) = sl.poll(later + 1)
    assert f.first_seen_at == T0          # the running window kept its time


# --- every other source is untouched ----------------------------------------

@pytest.mark.parametrize("src,provider", [
    ("desktop", "claude"), ("desktop_ls", "claude"), ("scripted", "claude"),
    ("cli", "codex"),
])
def test_other_sources_rank_by_their_own_reading_time(src, provider):
    def f(at, pct):
        return base.NormalizedUsageFrame(
            provider=provider, src=src, observed_at=at, session_pct=pct,
            weekly_pct=pct / 2)

    a, b = f(T0, 40.0), f(T0 + 60, 55.0)
    assert a.first_seen_at == a.observed_at
    m = normalizer.merge([a, b])
    assert (m.session_pct, m.weekly_pct, m.observed_at) == (55.0, 27.5, T0 + 60)
    m = normalizer.merge([f(T0, 40.0), f(T0 + 60, 12.0)])
    assert (m.session_pct, m.observed_at) == (12.0, T0 + 60)


def test_scripted_frames_carry_no_first_seen_of_their_own(tmp_path):
    from pc.providers import scripted
    script = tmp_path / "s.json"
    script.write_text(json.dumps({"steps": [
        {"at": 0, "provider": "codex", "session_pct": 30.0, "age_s": 120}]}),
        encoding="utf-8")
    prov = scripted.ScriptedProvider(str(script), now=lambda: T0)
    (frame,) = prov.poll(T0)
    assert frame.first_seen_at == frame.observed_at == T0 - 120
