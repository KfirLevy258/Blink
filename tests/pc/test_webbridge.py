"""The extension receiver. First listening socket this daemon has ever had,
so most of these are about what it REFUSES."""
import json
import urllib.error
import urllib.request

import pytest

from pc import webbridge
from pc.providers import base

NOW = 1_787_700_000.0
EXT_ORIGIN = "chrome-extension://abcdefghijklmnop"


@pytest.fixture
def bridge():
    b = webbridge.WebBridge(port=0, now=lambda: NOW)
    b.start()
    yield b
    b.stop()


def post(bridge, body, origin=EXT_ORIGIN, path="/usage", method="POST",
         content_type="application/json"):
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    req = urllib.request.Request(
        f"http://127.0.0.1:{bridge.port}{path}", data=raw, method=method)
    req.add_header("Content-Type", content_type)
    if origin is not None:
        req.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(req, timeout=5) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code


# --- what it accepts ------------------------------------------------------


def test_a_report_from_the_extension_is_accepted(bridge):
    assert post(bridge, {"session_pct": 25, "weekly_pct": 42}) == 204
    f = bridge.slot.get()
    assert (f.session_pct, f.weekly_pct) == (25.0, 42.0)
    assert f.provider == "claude" and f.src == "web"


def test_claude_ai_itself_is_an_accepted_origin(bridge):
    assert post(bridge, {"session_pct": 5}, origin="https://claude.ai") == 204


def test_reset_times_come_through_when_present(bridge):
    post(bridge, {"session_pct": 5, "session_resets_at": NOW + 900})
    assert bridge.slot.get().session_resets_at == NOW + 900


def test_only_the_newest_report_is_kept(bridge):
    post(bridge, {"session_pct": 5})
    post(bridge, {"session_pct": 9})
    assert bridge.slot.get().session_pct == 9.0


# --- what it refuses ------------------------------------------------------


def test_a_request_with_no_origin_is_refused(bridge):
    """curl, a script, another program. Only extensions are expected here,
    and browsers always send an Origin."""
    assert post(bridge, {"session_pct": 5}, origin=None) == 403


def test_a_random_web_page_cannot_post(bridge):
    assert post(bridge, {"session_pct": 5},
                origin="https://evil.example") == 403


def test_another_path_is_a_404(bridge):
    assert post(bridge, {"session_pct": 5}, path="/admin") == 404


def test_an_oversized_body_is_refused_before_it_is_read(bridge):
    big = json.dumps({"session_pct": 5, "pad": "x" * webbridge.MAX_BODY})
    assert post(bridge, big.encode()) == 413


def test_malformed_json_is_refused(bridge):
    assert post(bridge, b"{not json") == 400


def test_a_report_with_no_usable_number_is_refused(bridge):
    assert post(bridge, {"hello": "world"}) == 422


def test_a_percentage_out_of_range_is_not_accepted(bridge):
    assert post(bridge, {"session_pct": 4000}) == 422


def test_nothing_is_stored_by_a_refused_request(bridge):
    post(bridge, {"session_pct": 5}, origin="https://evil.example")
    assert bridge.slot.get() is None


def test_it_binds_loopback_only(bridge):
    """A bind to 0.0.0.0 would put a usage feed on the office network."""
    assert bridge._server.server_address[0] == "127.0.0.1"


# --- the parser, without a socket -----------------------------------------


def test_page_content_is_never_read():
    """Not filtered out -- never looked at. A report carrying a conversation
    contributes nothing but its numbers."""
    f = webbridge.parse_report(
        {"session_pct": 10, "conversation": "secret", "title": "also secret"},
        NOW)
    assert f.session_pct == 10.0
    assert not hasattr(f, "conversation")


def test_a_nonsense_reset_time_is_dropped_not_rendered():
    f = webbridge.parse_report({"session_pct": 10, "session_resets_at": 1}, NOW)
    assert f.session_resets_at is None


def test_one_window_alone_is_enough():
    assert webbridge.parse_report({"weekly_pct": 3}, NOW).weekly_pct == 3.0
    assert webbridge.parse_report({"session_pct": 3}, NOW).session_pct == 3.0


def test_origin_allow_list():
    assert webbridge.origin_allowed("chrome-extension://x")
    assert webbridge.origin_allowed("moz-extension://x")
    assert webbridge.origin_allowed("https://claude.ai")
    assert not webbridge.origin_allowed("https://claude.ai.evil.com")
    assert not webbridge.origin_allowed("")


# --- the provider ---------------------------------------------------------


def test_the_provider_reports_what_the_slot_holds(bridge):
    p = webbridge.ClaudeWebProvider(bridge.slot)
    assert p.poll(NOW) == []
    post(bridge, {"session_pct": 77})
    assert p.poll(NOW)[0].session_pct == 77.0


def test_the_web_source_merges_like_any_other():
    """A browser reading is just another source; the same field-by-field
    recency rule applies."""
    from pc import normalizer
    web = webbridge.parse_report({"weekly_pct": 88}, NOW)
    cli = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW - 3600,
        session_pct=10.0, weekly_pct=20.0, weekly_resets_at=NOW + 900)
    m = normalizer.merge([cli, web])
    assert m.weekly_pct == 88.0            # web is fresher
    assert m.session_pct == 10.0           # only the CLI has it
    assert m.weekly_resets_at == NOW + 900  # only the CLI has it
