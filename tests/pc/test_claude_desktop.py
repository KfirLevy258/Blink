"""The desktop cache is an internal file of an app we do not control.

Every test here is about refusing to present a wrong number confidently, which
is the only failure mode of an ambient source that matters: a silent source is
visibly silent, a lying one is not.
"""
import json

from pc.providers import base
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
