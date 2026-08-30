"""The desktop cache is an internal file of an app we do not control.

Every test here is about refusing to present a wrong number confidently, which
is the only failure mode of an ambient source that matters: a silent source is
visibly silent, a lying one is not.
"""
import json

from pc.providers import base
from pc.providers import claude_desktop
from pc.providers.claude_desktop import ClaudeDesktopProvider

NOW = 1_787_700_000.0


def _doc(samples, version=2):
    return json.dumps({"version": version, "samples": samples})


def _sample(t_ms, fh=10, sd=20):
    return {"t": t_ms, "org": "org-1", "u": {"fh": fh, "sd": sd}}


def test_parses_the_observed_v2_shape():
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(_doc([_sample(int(NOW * 1000), 2, 100)]), NOW)
    assert f.provider == "claude"
    assert f.src == "desktop"
    assert f.session_pct == 2.0
    assert f.weekly_pct == 100.0


def test_the_timestamp_is_milliseconds_not_seconds():
    """Read as seconds, a 2026 sample lands in the year 56649 -- every
    freshness check then passes trivially and any reading in history presents
    as current. This is the single most dangerous field in the file."""
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(_doc([_sample(int(NOW * 1000))]), NOW)
    assert abs(f.observed_at - NOW) < 1.0
    assert f.stale is False


def test_an_old_sample_is_stale_because_the_units_were_right():
    p = ClaudeDesktopProvider()
    old = int((NOW - 40_000) * 1000)
    f = p.parse_cache_file(_doc([_sample(old)]), NOW)
    assert f.stale is True


def test_this_source_never_claims_a_reset_time():
    """The cache has no reset timestamps. None, not a guess -- and this is
    the reason the normalizer merges field by field."""
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(_doc([_sample(int(NOW * 1000))]), NOW)
    assert f.session_resets_at is None
    assert f.weekly_resets_at is None


def test_the_newest_sample_wins_even_when_the_list_is_not_sorted():
    """samples[-1] is correct in the file observed today, but that is not a
    guarantee from an app we do not control."""
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(_doc([
        _sample(int(NOW * 1000), 90, 90),
        _sample(int((NOW - 10_000) * 1000), 5, 5),   # older, but last
    ]), NOW)
    assert f.session_pct == 90.0


def test_a_percentage_out_of_range_is_refused():
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(_doc([_sample(int(NOW * 1000), 340, 20)]), NOW)
    assert f.session_pct == base.UNKNOWN
    assert f.weekly_pct == 20.0      # the sound field is unaffected


def test_a_sample_with_neither_percentage_is_skipped():
    """A fresh empty sample must not outrank a good older one purely by
    being newer -- that is how a live source blanks a working panel."""
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(_doc([
        _sample(int((NOW - 100) * 1000), 42, 43),
        {"t": int(NOW * 1000), "org": "o", "u": {}},
    ]), NOW)
    assert f.session_pct == 42.0


def test_an_unknown_version_falls_back_to_shape():
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(_doc([_sample(int(NOW * 1000), 7, 8)], version=99),
                           NOW)
    assert f.session_pct == 7.0


def test_a_bare_list_of_samples_still_parses():
    p = ClaudeDesktopProvider()
    f = p.parse_cache_file(json.dumps([_sample(int(NOW * 1000), 7, 8)]), NOW)
    assert f.session_pct == 7.0


def test_malformed_json_is_silence_not_an_exception():
    p = ClaudeDesktopProvider()
    assert p.parse_cache_file("{not json", NOW) is None


def test_an_empty_sample_list_is_silence():
    p = ClaudeDesktopProvider()
    assert p.parse_cache_file(_doc([]), NOW) is None


def test_a_completely_foreign_document_is_silence():
    p = ClaudeDesktopProvider()
    assert p.parse_cache_file(json.dumps({"something": "else"}), NOW) is None


def test_a_missing_file_polls_empty_and_says_nothing(tmp_path):
    p = ClaudeDesktopProvider(path=str(tmp_path / "nope.json"))
    assert p.poll(NOW) == []


def test_poll_reads_a_real_file(tmp_path):
    f = tmp_path / "plan-usage-history.json"
    f.write_text(_doc([_sample(int(NOW * 1000), 11, 22)]))
    frames = ClaudeDesktopProvider(path=str(f)).poll(NOW)
    assert len(frames) == 1
    assert frames[0].session_pct == 11.0
    assert frames[0].weekly_pct == 22.0


def test_an_extra_unknown_key_in_u_is_harmless():
    """The real file carries an undocumented `xu` on some samples."""
    p = ClaudeDesktopProvider()
    s = {"t": int(NOW * 1000), "org": "o", "u": {"fh": 3, "sd": 4, "xu": 9}}
    f = p.parse_cache_file(_doc([s]), NOW)
    assert (f.session_pct, f.weekly_pct) == (3.0, 4.0)


# --- burn rate -------------------------------------------------------------
#
# The rate exists for one configuration: Claude Desktop with no Claude Code,
# where there are percentages and no reset time of any kind. Every test below
# is about a refusal, because refusing is the common answer and the one that
# has to be right -- a rate that reports confidently on data it did not
# observe is the failure this whole feature was designed around.

def _samples(*pairs):
    """[(seconds_before_now, session_pct), ...] -> the file's sample shape."""
    return [{"t": int((NOW - back) * 1000), "org": "o",
             "u": {"fh": pct, "sd": 1}}
            for back, pct in pairs]


def test_a_steady_climb_is_reported():
    # 10% over 30 minutes is 20%/hour.
    s = _samples((1800, 10), (1200, 13.3), (600, 16.7), (0, 20))
    got = claude_desktop.session_burn_pph(s, NOW)
    assert round(got, 1) == 20.0


def test_flat_usage_reports_nothing():
    """Not 0.0 -- None. Zero would render as a rate of zero, which is a
    claim; the absence of movement is not one worth putting on a panel."""
    s = _samples((1800, 40), (1200, 40), (600, 40), (0, 40))
    assert claude_desktop.session_burn_pph(s, NOW) is None


def test_a_gap_refuses():
    """The app was closed for twenty minutes. A slope drawn across that
    averages over time nobody observed -- the same mistake as deriving a
    reset time, which this feature exists instead of."""
    s = _samples((1800, 10), (1500, 12), (300, 40), (0, 42))
    assert claude_desktop.session_burn_pph(s, NOW) is None


def test_the_idle_cadence_is_reported():
    """Claude Desktop's OTHER schedule.

    It fetches every 300 s while the machine is in use and every 900 s
    otherwise -- present at the desk, reading rather than typing, which is
    the ordinary way to sit in front of this panel. The old 600 s gap limit
    sat below that interval and refused every one of these readings, so the
    single line under a Desktop-only gauge showed "--" for as long as its
    owner was not typing. Nothing here is a hole: three real samples, evenly
    spaced, 10% of the window in half an hour.
    """
    s = _samples((1800, 10), (900, 15), (0, 20))
    assert round(claude_desktop.session_burn_pph(s, NOW), 1) == 20.0


def test_a_newest_sample_one_idle_interval_old_is_accepted():
    """The same cadence seen from the other end.

    On a 900 s schedule the freshest sample there IS can be 900 s old, so a
    freshness bound below that refused the series for being exactly as
    current as the source can ever be. Still bounded -- see the test above
    this one's neighbour, where a genuinely stale newest sample refuses.
    """
    s = _samples((2700, 10), (1800, 15), (900, 20))
    assert round(claude_desktop.session_burn_pph(s, NOW), 1) == 20.0


def test_a_reset_inside_the_window_refuses():
    """The percentage fell because the window rolled, not because usage
    went backwards -- it cannot. A slope spanning that is meaningless."""
    s = _samples((1800, 90), (1200, 95), (600, 2), (0, 6))
    assert claude_desktop.session_burn_pph(s, NOW) is None


def test_a_stale_newest_sample_refuses():
    """Perfectly computable, and it describes a session that ended half an
    hour ago. Freshness is about the answer, not about the arithmetic."""
    s = _samples((3600, 10), (3300, 20), (3000, 30))
    assert claude_desktop.session_burn_pph(s, NOW) is None


def test_too_few_samples_refuses():
    s = _samples((900, 10), (0, 30))
    assert claude_desktop.session_burn_pph(s, NOW) is None


def test_too_short_a_span_refuses():
    """Three samples five minutes apart: one 5-minute step would swing the
    answer by the whole range."""
    s = _samples((300, 10), (150, 11), (0, 12))
    assert claude_desktop.session_burn_pph(s, NOW) is None


def test_junk_never_raises():
    for bad in (None, "samples", 42, [], [None, {}, {"u": 3}]):
        assert claude_desktop.session_burn_pph(bad, NOW) is None


def test_the_frame_carries_it():
    doc = {"version": 2, "samples": _samples(
        (1800, 10), (1200, 13.3), (600, 16.7), (0, 20))}
    p = claude_desktop.ClaudeDesktopProvider()
    f = p.parse_cache_file(json.dumps(doc), NOW)
    assert f is not None
    assert round(f.session_burn_pph, 1) == 20.0
    # And the thing that has not changed: this source still has no reset
    # times, which is the whole reason the rate is here.
    assert None is (f.session_resets_at)


def test_a_version_field_that_is_not_a_scalar_is_not_an_exception():
    """dict.get on a list raised TypeError -- outside every try here, so the
    bus marked the whole source broken over one odd field."""
    p = ClaudeDesktopProvider()
    for v in ([2], {"v": 2}, None, True):
        f = p.parse_cache_file(_doc([_sample(int(NOW * 1000), 7, 8)],
                                    version=v), NOW)
        assert f is not None and f.session_pct == 7.0, v


# --- a real file --------------------------------------------------------------


import os as _os

DESKTOP_FIXTURE = _os.path.join(_os.path.dirname(_os.path.dirname(__file__)),
                                "fixtures", "claude_desktop_plan_usage_history.json")


def test_the_real_cache_file_parses():
    """The newest 40 samples of a real plan-usage-history.json (2026-08-28),
    org id redacted, values verbatim. The parser above is otherwise pinned to
    hand-written documents."""
    now = 1787854171.881 + 60                 # a minute after the last sample
    frames = ClaudeDesktopProvider(path=DESKTOP_FIXTURE).poll(now)
    assert len(frames) == 1
    f = frames[0]
    assert (f.provider, f.src) == ("claude", "desktop")
    assert f.session_pct == 24.0
    assert f.weekly_pct == 25.0
    assert f.session_resets_at is None and f.weekly_resets_at is None
    assert f.stale is False
    assert abs(f.observed_at - 1787854171.881) < 0.01


def test_the_real_cache_file_has_no_reset_time_anywhere():
    """The claim the product's support matrix rests on, checked against the
    file rather than asserted: no key in any sample names a reset."""
    doc = json.load(open(DESKTOP_FIXTURE))
    keys = set()
    for s in doc["samples"]:
        keys |= set(s) | set(s["u"])
    assert keys == {"t", "org", "u", "fh", "sd", "xu"}
