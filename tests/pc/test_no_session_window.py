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

Every payload here is generated. None is captured from a machine.
"""
import json
import os

from pc import ingest, normalizer, protocol
from pc.providers import base
from pc.providers.claude_cli import ClaudeCliProvider

NOW = 1_787_700_000.0
WEEK_END = NOW + 3 * 86400


def write(path, payload, mtime):
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.utime(path, (mtime, mtime))
    return path


def weekly_only(weekly=2.0):
    """The Ultra shape: seven_day, and no five_hour key whatever."""
    return {"rate_limits": {"seven_day": {"used_percentage": weekly,
                                          "resets_at": WEEK_END}}}


def with_window(five=27.0, resets_at=None, weekly=2.0):
    p = weekly_only(weekly)
    w = {"used_percentage": five}
    if resets_at is not None:
        w["resets_at"] = resets_at
    p["rate_limits"]["five_hour"] = w
    return p


class Desktop(base.ProviderParser):
    """A Claude Desktop sample with a session figure, at a chosen age."""

    def __init__(self, at, session=1.0, weekly=2.0):
        self._f = base.NormalizedUsageFrame(
            provider="claude", src="desktop", observed_at=at,
            session_pct=session, weekly_pct=weekly, stale=(NOW - at) > 1800)

    def get_provider_id(self):
        return "claude"

    def poll(self, now_epoch):
        return [self._f]


def bus_for(path, *others, clock=None):
    return ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(path)), *others],
        now=clock or (lambda: NOW))


def wire(bus):
    msg = bus.poll()
    line, why = protocol.encode_checked(msg)
    assert why is None, why
    return msg


def test_a_fresh_weekly_only_payload_is_live_with_an_unknown_session(tmp_path):
    """The measured wire line, reproduced and then inverted."""
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)
    bus = bus_for(p, Desktop(NOW - 136136, session=1.0, weekly=2.0))

    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["session_resets_in_s"] == -1
    assert msg.get("burn_pph") is None
    assert msg["weekly_pct"] == 2.0
    assert msg["src"] == "cli"
    assert msg["stale"] is False
    assert msg["age_s"] == 1
    assert msg["active_age_s"] == 1


def test_no_stale_desktop_sample_or_remembered_window_is_borrowed(tmp_path):
    """Both borrowing routes at once. The daemon saw a five-hour window
    earlier (an unverified account, before Anthropic verified it), that
    window has since ended, and Desktop holds a session sample taken after
    that (so the rollover rule cannot turn it away) but an hour ago."""
    p = tmp_path / "statusline.json"
    ended = NOW - 7200
    write(p, with_window(five=27.0, resets_at=ended), NOW - 6 * 3600)
    bus_clock = [NOW - 6 * 3600 + 1]
    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(p)),
                   Desktop(NOW - 3600, session=1.0)],
        now=lambda: bus_clock[0])
    bus.poll()

    write(p, weekly_only(2.0), NOW - 1)
    bus_clock[0] = NOW
    msg = wire(bus)

    assert msg["session_pct"] == -1
    assert msg["session_resets_in_s"] == -1
    assert msg["src"] == "cli"
    assert msg["stale"] is False
    assert msg["age_s"] == 1


def test_five_hour_coming_back_behaves_as_before(tmp_path):
    """An unverified account, or a policy reversal."""
    p = tmp_path / "statusline.json"
    bus = bus_for(p, Desktop(NOW - 136136, session=1.0))

    write(p, weekly_only(2.0), NOW - 10)
    assert wire(bus)["session_pct"] == -1

    write(p, with_window(five=31.0, resets_at=NOW + 3600), NOW - 1)
    msg = wire(bus)
    assert msg["session_pct"] == 31.0
    assert msg["session_resets_in_s"] == 3600
    assert msg["src"] == "cli"
    assert msg["stale"] is False


def test_a_stale_payload_is_not_called_a_missing_window(tmp_path):
    """Freshness is the bound the rule needs: a file nobody has rendered in
    an hour says nothing about what the account has now. Behaviour is the
    one before this rule, so Desktop still fills the dial -- honestly aged."""
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 3600)
    bus = bus_for(p, Desktop(NOW - 120, session=9.0))

    msg = wire(bus)

    assert msg["session_pct"] == 9.0
    assert msg["src"] == "desktop"


def test_a_desktop_only_desk_is_untouched(tmp_path):
    bus = bus_for(tmp_path / "absent.json", Desktop(NOW - 120, session=9.0))

    msg = wire(bus)

    assert msg["session_pct"] == 9.0
    assert msg["src"] == "desktop"
    assert msg["stale"] is False


def test_a_window_that_has_not_ended_is_not_a_missing_window(tmp_path):
    """The discriminator, from the data. A remembered five_hour whose
    resets_at is still ahead is proof this account has a window, so a render
    without it (Claude Code leaves a window out until a session's first
    response) must not blank the dial."""
    p = tmp_path / "statusline.json"
    clock = [NOW - 600]
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: clock[0])
    write(p, with_window(five=27.0, resets_at=NOW + 3600), NOW - 600)
    assert wire(bus)["session_pct"] == 27.0

    write(p, weekly_only(2.0), NOW - 1)
    clock[0] = NOW
    msg = wire(bus)

    assert msg["session_pct"] == 27.0      # remembered, as today
    assert msg["stale"] is False
    assert msg["age_s"] == 600             # the remembered reading's own age


def test_a_present_window_without_a_percentage_is_not_a_missing_window(
        tmp_path):
    """Only an absent KEY means no window. five_hour present with no
    used_percentage is the likeliest shape of a payload change
    (statusline_source._window), and not worth concluding anything from."""
    p = write(tmp_path / "statusline.json",
              {"rate_limits": {"five_hour": {"resets_at": NOW + 600},
                               "seven_day": {"used_percentage": 2.0}}},
              NOW - 1)
    bus = bus_for(p, Desktop(NOW - 120, session=9.0))

    msg = wire(bus)

    assert msg["session_pct"] == 9.0


def test_a_payload_without_a_usable_weekly_figure_is_not_flagged(tmp_path):
    p = write(tmp_path / "statusline.json", {"rate_limits": {}}, NOW - 1)
    frames = ClaudeCliProvider(path=str(p)).poll(NOW)

    assert [f.no_session_window for f in frames] == [False]


def test_the_provider_marks_the_frame(tmp_path):
    p = write(tmp_path / "statusline.json", weekly_only(2.0), NOW - 1)

    frames = ClaudeCliProvider(path=str(p)).poll(NOW)

    assert frames[0].no_session_window is True


def test_the_flag_survives_a_second_merge():
    cli = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW - 1, weekly_pct=2.0,
        no_session_window=True)
    desktop = base.NormalizedUsageFrame(
        provider="claude", src="desktop", observed_at=NOW - 500,
        session_pct=40.0, weekly_pct=2.0)

    once = normalizer.merge([cli, desktop])
    twice = normalizer.merge([once, desktop])

    assert once.session_pct == base.UNKNOWN
    assert twice.session_pct == base.UNKNOWN
    assert twice.src == "cli"


def test_the_flag_does_not_cross_providers():
    claude = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW - 1, weekly_pct=2.0,
        no_session_window=True)
    codex = base.NormalizedUsageFrame(
        provider="codex", src="cli", observed_at=NOW - 1, session_pct=33.0,
        weekly_pct=10.0)

    codex_merged = normalizer.merge([codex])
    assert codex_merged.session_pct == 33.0
    assert normalizer.merge([claude]).session_pct == base.UNKNOWN
