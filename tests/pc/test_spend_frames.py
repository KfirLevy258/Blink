"""A Claude Enterprise spend cap, read off the status line (task 15a).

Every payload here is built by spend_payload() below. Nothing is captured:
no Enterprise login was available when this was written, and a captured
statusline.json carries somebody's session into a public repository. The
field names are the ones Claude Code documents for
`rate_limits.spend_limit` (code.claude.com/docs/en/statusline), and the
figures are the plan's: a $500 monthly cap with $314.12 of it used.
"""
import math

import pytest

from pc import statusline_source as ss
from pc.providers import base

NOW = 1_790_000_000.0
# Eighteen days and four hours to the reset, which is roughly where a
# monthly period sits on the 13th.
RESETS_AT = NOW + 18 * 86400 + 4 * 3600


def spend_payload(**spend):
    """A status-line payload whose only allowance is a spend cap.

    Keyword arguments replace fields of the spend_limit object; passing
    None for one removes it, which is how Claude Code leaves out the
    dollars it has not fetched yet.
    """
    obj = {"used_percentage": 62.8, "resets_at": RESETS_AT,
           "used_usd": 314.12, "limit_usd": 500, "period": "monthly"}
    for k, v in spend.items():
        if v is None:
            obj.pop(k, None)
        else:
            obj[k] = v
    return {"rate_limits": {"spend_limit": obj}}


def windows_payload():
    return {"rate_limits": {
        "five_hour": {"used_percentage": 41.0, "resets_at": NOW + 3600},
        "seven_day": {"used_percentage": 12.0, "resets_at": NOW + 86400},
    }}


def frame_of(payload, mtime=NOW, now=NOW):
    return ss.map_statusline_frame(payload, now, mtime)


def test_a_spend_limit_becomes_a_spend_frame():
    f = frame_of(spend_payload())
    assert f.plan == base.PLAN_SPEND
    assert f.session_pct == 62.8
    assert f.session_resets_at == RESETS_AT
    # A spend account has no seven-day window, and the frame says so
    # rather than leaving a zero there for the board to draw.
    assert f.weekly_pct == base.UNKNOWN
    assert f.weekly_resets_at is None
    assert f.spend_usd == pytest.approx(314.12)
    assert f.spend_limit_usd == 500.0
    assert f.spend_period == "monthly"
    assert f.stale is False
    assert f.has_usage()


def test_a_payload_with_windows_only_is_exactly_what_it_was():
    """The gate: nothing changes for a desk whose status line carries no
    spend object, which is every desk on a release today."""
    f = frame_of(windows_payload())
    assert f.plan == base.PLAN_WINDOWS
    assert f.spend_usd is None and f.spend_limit_usd is None
    assert f.spend_period == "" and f.account == ""
    assert f.session_pct == 41.0 and f.weekly_pct == 12.0


def test_the_default_plan_is_windows():
    f = base.NormalizedUsageFrame(provider="claude", src="x", observed_at=0)
    assert f.plan == base.PLAN_WINDOWS == "windows"


def test_the_dollars_are_optional_and_the_percentage_is_not():
    # Claude Code fetches the dollars on a separate request minutes behind
    # the percentage, and never under CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC.
    f = frame_of(spend_payload(used_usd=None, limit_usd=None, period=None))
    assert f.plan == base.PLAN_SPEND
    assert f.session_pct == 62.8
    assert f.spend_usd is None and f.spend_limit_usd is None
    assert f.spend_period == ""

    # Without the percentage there is nothing to put on a gauge, so the
    # object is ignored and the payload is read the old way.
    f = frame_of(spend_payload(used_percentage=None))
    assert f.plan == base.PLAN_WINDOWS
    assert f.session_pct == base.UNKNOWN


@pytest.mark.parametrize("bad", [
    {"used_percentage": "62.8%"},
    {"used_percentage": float("nan")},
    {"used_percentage": -3},
    {"used_percentage": True},
])
def test_a_spend_percentage_that_is_not_one_is_not_drawn(bad):
    assert frame_of(spend_payload(**bad)).plan == base.PLAN_WINDOWS


def test_nonsense_in_the_spend_object_is_not_drawn():
    f = frame_of(spend_payload(used_usd=-4, limit_usd=float("inf"),
                               period="fortnightly", resets_at="soon"))
    assert f.plan == base.PLAN_SPEND
    assert f.session_pct == 62.8
    # A negative or infinite amount would reach the panel as "$-4" or
    # "$inf": not reported is the honest reading of either.
    assert f.spend_usd is None
    assert f.spend_limit_usd is None
    # A period this firmware has no caption for is dropped, not shown.
    assert f.spend_period == ""
    assert f.session_resets_at is None

    for junk in ("free", [500], {"usd": 500}, True):
        f = frame_of(spend_payload(used_usd=junk, limit_usd=junk))
        assert f.spend_usd is None and f.spend_limit_usd is None

    # And an object of the wrong type altogether is no spend object.
    for junk in ("62%", [62.8], 62.8, None):
        p = {"rate_limits": {"spend_limit": junk}}
        assert frame_of(p).plan == base.PLAN_WINDOWS


def test_spend_wins_over_windows_on_a_payload_carrying_both():
    p = windows_payload()
    p["rate_limits"].update(spend_payload()["rate_limits"])
    f = frame_of(p)
    assert f.plan == base.PLAN_SPEND
    assert f.session_pct == 62.8
    # The five-hour figure must not ride along in the weekly slot either.
    assert f.weekly_pct == base.UNKNOWN


def test_a_spend_period_that_rolled_over_is_zero_dollars_while_fresh():
    ended = NOW - 60
    f = frame_of(spend_payload(resets_at=ended), mtime=NOW - 120)
    assert f.plan == base.PLAN_SPEND
    assert f.session_pct == 0.0
    assert f.spend_usd == 0.0
    # The cap itself did not reset; only what was spent against it did.
    assert f.spend_limit_usd == 500.0
    assert f.session_rolled_at == ended
    assert f.session_resets_at is None


def test_a_stale_reading_of_an_ended_period_claims_no_dollars():
    ended = NOW - 60
    old = NOW - ss.STALE_AFTER_S - 10
    f = frame_of(spend_payload(resets_at=ended), mtime=old)
    assert f.stale is True
    assert f.session_pct == base.UNKNOWN
    assert f.spend_usd is None
    assert f.session_rolled_at == ended


def test_a_stale_spend_reading_keeps_its_figures_and_says_it_is_old():
    old = NOW - ss.STALE_AFTER_S - 10
    f = frame_of(spend_payload(), mtime=old)
    assert f.stale is True
    assert f.session_pct == 62.8
    assert f.spend_usd == pytest.approx(314.12)


def test_the_spend_frame_helper_takes_rate_limits_directly():
    rl = spend_payload()["rate_limits"]
    f = ss._spend_frame(rl, NOW, NOW)
    assert f is not None and f.plan == base.PLAN_SPEND
    assert ss._spend_frame({}, NOW, NOW) is None
    assert ss._spend_frame(windows_payload()["rate_limits"], NOW, NOW) is None


def test_the_spend_periods_are_the_three_claude_code_documents():
    assert base.SPEND_PERIODS == ("daily", "weekly", "monthly")
    assert base.PLAN_SPEND == "spend"
    assert base.PLAN_UNREPORTED == "unreported"
    for p in base.SPEND_PERIODS:
        assert frame_of(spend_payload(period=p)).spend_period == p


def test_nothing_here_is_a_float_the_wire_cannot_carry():
    f = frame_of(spend_payload())
    for v in (f.session_pct, f.spend_usd, f.spend_limit_usd):
        assert math.isfinite(v)


# -- numbers json reads and nothing else can (final review 2026-11-04, U2) ---

HUGE = 10 ** 400        # 401 digits: json reads it, float() refuses it


@pytest.mark.parametrize("value", [HUGE, 1e300, 10_000_001])
@pytest.mark.parametrize("field", ["used_usd", "limit_usd"])
def test_a_huge_dollar_figure_is_not_reported(field, value):
    """math.isfinite raises OverflowError on an int this long; it came out
    of every poll and the provider was benched, spend page and all. A
    finite one past ten million dollars is no developer's figure either."""
    f = frame_of(spend_payload(**{field: value}))
    assert f.plan == base.PLAN_SPEND
    assert f.session_pct == 62.8
    assert getattr(f, {"used_usd": "spend_usd",
                       "limit_usd": "spend_limit_usd"}[field]) is None


@pytest.mark.parametrize("pct", [HUGE, 5000, 1000.5, 1e300])
def test_a_spend_share_past_the_bound_is_held_at_it(pct):
    """Round 2 of the 2026-11-04 review, K2. 5000 went to the board as it
    was until U2, and U2's bound then dropped the whole spend page for it:
    $300 against a cap cut to $20 is 1500%, a real gateway figure, and the
    desk fell back to the windows page. It is held at 1000, as
    protocol._share and proto.c's num_pct hold one: far over the cap still
    reads as over it."""
    f = frame_of(spend_payload(used_percentage=pct))
    assert f.plan == base.PLAN_SPEND
    assert f.session_pct == ss.MAX_SHARE == 1000


@pytest.mark.parametrize("pct", [float("nan"), -1, -0.5, float("-inf"),
                                 float("inf"), True, "50", None])
def test_a_spend_share_that_is_not_one_is_not_drawn(pct):
    """NaN, a negative, an infinity or a non-number is no percentage at
    all, and an object without one is no evidence of an allowance. The
    first cut of K2 held infinity at 1000 and drew a full red dial for it
    (R4, round 2's review)."""
    assert frame_of(spend_payload(used_percentage=pct)).plan \
        == base.PLAN_WINDOWS


@pytest.mark.parametrize("resets", [HUGE, 1e300, -1e300, -1])
def test_a_reset_that_is_not_a_time_is_unknown(resets):
    """Bounded like a window's stamp. 10**400 reached the countdown, which
    raised out of every poll; 1e300 and -1e300 were read as a period that
    never ends and one that ended in prehistory."""
    f = frame_of(spend_payload(resets_at=resets))
    assert f.plan == base.PLAN_SPEND
    assert f.session_resets_at is None
    assert f.session_rolled_at is None
    assert f.session_pct == 62.8


def test_a_huge_figure_does_not_take_the_poll_down(tmp_path):
    import json
    from pc import ingest
    from pc.providers.claude_cli import ClaudeCliProvider
    p = tmp_path / "statusline.json"
    p.write_text(json.dumps(spend_payload(
        used_usd=HUGE, limit_usd=HUGE, resets_at=HUGE)), encoding="utf-8")
    import os
    os.utime(p, (NOW, NOW))
    bus = ingest.IngestionBus(providers=[ClaudeCliProvider(path=str(p))],
                              now=lambda: NOW)
    msg = bus.poll()
    assert msg is not None and msg["session_pct"] == 62.8
