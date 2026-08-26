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
def bridge(tmp_path):
    """Port 0 AND a diag file under tmp_path.

    The port was already scoped; the diag file was not, and its default came
    from a module-level expanduser evaluated at import -- before conftest can
    redirect HOME. So every test through this fixture wrote a fixed test
    timestamp into the developer's real ~/.clauge/webbridge.json, and
    `clauge status` then reported the extension as last seen a day ago on a
    machine where it had reported seconds earlier.
    """
    b = webbridge.WebBridge(port=0, now=lambda: NOW,
                            diag_path=str(tmp_path / "webbridge.json"))
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


# --- diagnostics: the answer to "is the extension working?" ---------------
#
# The one thing nobody could verify without a browser is whether claude.ai
# emits rate-limit headers at all. These pin the mechanism that lets the USER
# answer it in one command instead of a DevTools session.


@pytest.fixture
def diag_bridge(tmp_path):
    b = webbridge.WebBridge(port=0, now=lambda: NOW,
                            diag_path=str(tmp_path / "webbridge.json"))
    b.start()
    yield b
    b.stop()


def test_a_diagnostic_is_accepted(diag_bridge):
    assert post(diag_bridge, {"responses": 42, "matched": 0},
                path="/diag") == 204


def test_a_diagnostic_never_puts_a_number_on_the_panel(diag_bridge):
    """It exists to report the ABSENCE of numbers. If it could reach the slot
    it could show one, which would be exactly backwards."""
    post(diag_bridge, {"responses": 42, "matched": 0, "session_pct": 99},
         path="/diag")
    assert diag_bridge.slot.get() is None


def test_the_crumb_records_what_the_extension_saw(diag_bridge, tmp_path):
    post(diag_bridge, {"responses": 42, "matched": 3}, path="/diag")
    d = webbridge.read_diag(str(tmp_path / "webbridge.json"))
    assert d["responses"] == 42
    assert d["matched"] == 3


def test_usage_reports_are_counted_separately_from_matches(diag_bridge,
                                                           tmp_path):
    """matched > 0 with usage_reports == 0 is a real and distinct situation:
    headers exist but do not yield a usable percentage."""
    post(diag_bridge, {"session_pct": 10})
    d = webbridge.read_diag(str(tmp_path / "webbridge.json"))
    assert d["usage_reports"] == 1
    assert d["matched"] == 0


def test_a_missing_crumb_reads_as_none(tmp_path):
    assert webbridge.read_diag(str(tmp_path / "nope.json")) is None


def test_a_corrupt_crumb_reads_as_none(tmp_path):
    p = tmp_path / "webbridge.json"
    p.write_text("{not json")
    assert webbridge.read_diag(str(p)) is None


def test_a_diagnostic_from_a_foreign_origin_is_refused(diag_bridge):
    assert post(diag_bridge, {"responses": 1}, path="/diag",
                origin="https://evil.example") == 403


def test_the_crumb_is_not_rewritten_on_every_post(tmp_path):
    """A busy tab must not turn this into a write per response."""
    writes = []
    d = webbridge._Diag(path=str(tmp_path / "w.json"), now=lambda: NOW)
    d._write = lambda snap: writes.append(snap)
    for _ in range(20):
        d.record(responses=1, matched=0)
    assert len(writes) == 1, "throttling did not hold"


def test_the_diag_path_follows_a_redirected_home(tmp_path, monkeypatch):
    """The regression that made this worth changing.

    A constant built with expanduser at import time is fixed before any test
    can move HOME, so the sandbox conftest.py installs does not apply to it.
    Resolving on use is what makes the sandbox actually cover this file.
    """
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert webbridge.diag_file() == str(tmp_path / ".clauge/webbridge.json")
