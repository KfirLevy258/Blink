"""The status-page watch: what a public incident turns into on the panel.

Every summary below is BUILT here, by summary(), in the Statuspage shape both
status.claude.com and status.openai.com serve (a `components` list of
{name, status}). None of it is a captured response, and no test reaches the
network: the opener is injected wherever a request is made, and anything
that slipped past that would be refused by the socket guard in
tests/conftest.py rather than quietly fetch.
"""
import io
import json
import os
import sys

import pytest

from pc import cli, protocol
from pc import status_page as sp
from pc import user_config


def summary(**statuses):
    """A Statuspage summary.json body, with filler components around ours.

    Keyword names are short aliases for the component names; the filler is
    there so a parser that keyed on position, or on the first component,
    would read the wrong one.
    """
    comps = [{"name": "Filler Before", "status": "major_outage"}]
    for k, v in statuses.items():
        comps.append({"name": _NAMES.get(k, k), "status": v})
    comps.append({"name": "Filler After", "status": "major_outage"})
    return json.dumps({"page": {"name": "x"},
                       "status": {"indicator": "none"},
                       "components": comps}).encode("utf-8")


_NAMES = {
    "code": "Claude Code",
    "web": "claude.ai",
    "api": "Claude API (api.anthropic.com)",
    "codex": "Codex in ChatGPT Desktop",
    "responses": "Responses",
    "login": "Login",
}

ALL_OK_CLAUDE = summary(code="operational", web="operational",
                        api="operational")
ALL_OK_CODEX = summary(codex="operational", responses="operational",
                       login="operational")


def _incident(level="major_outage", which="code"):
    statuses = {"code": "operational", "web": "operational",
                "api": "operational"}
    statuses[which] = level
    return summary(**statuses)


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Opener:
    """Stands in for urllib.request.urlopen and remembers every call."""

    def __init__(self, bodies=None, raises=None):
        self.bodies = dict(bodies or {})
        self.raises = raises
        self.calls = []

    def __call__(self, req, timeout=None, context=None):
        url = req.full_url
        self.calls.append((url, timeout))
        if self.raises is not None:
            raise self.raises
        return FakeResponse(self.bodies[url])

    def urls(self):
        return [u for u, _ in self.calls]


CLAUDE_URL = sp.PAGES["claude"].url
CODEX_URL = sp.PAGES["codex"].url


class Desk:
    """A watch, a wall clock, and a Bridge that keeps renewing its want().

    The real Bridge says want() every 2 s while the board answers, and a
    want lasts WANT_TTL_S; a test that stepped five minutes between ticks
    without renewing would be testing an unplugged board.
    """

    def __init__(self, opener, enabled=True, pages=("claude",)):
        self.t = 1_760_000_000.0           # wall time, like time.time()
        self.on = enabled
        self.opener = opener
        self.pages = set(pages)
        self.watch = sp.StatusWatch(enabled=lambda: self.on, opener=opener,
                                    now=lambda: self.t)

    def tick(self):
        self.watch.want(self.pages)
        return self.watch.tick()

    def poll(self, n=1):
        """n polls, POLL_S apart; the first one now."""
        for i in range(n):
            if i:
                self.t += sp.POLL_S
            self.tick()

    def later(self, seconds=sp.POLL_S):
        self.t += seconds
        return self.tick()

    def shown(self, p="claude"):
        return self.watch.shown(p)


# ------------------------------------------------------------------ mapping


@pytest.mark.parametrize("status,level", [
    ("operational", "ok"),
    ("under_maintenance", "maint"),
    ("degraded_performance", "degraded"),
    ("partial_outage", "partial"),
    ("major_outage", "major"),
])
def test_each_statuspage_status_maps_to_one_level(status, level):
    got = sp.parse_summary(_incident(status), "claude")
    assert got[0] == level
    assert got[1] == ("" if level == "ok" else "Claude Code")


def test_the_worst_listed_component_wins():
    body = summary(code="degraded_performance", web="major_outage",
                   api="partial_outage")
    assert sp.parse_summary(body, "claude") == ("major", "claude.ai")


def test_maintenance_ranks_below_any_real_fault():
    body = summary(code="under_maintenance", web="operational",
                   api="degraded_performance")
    assert sp.parse_summary(body, "claude") == ("degraded", "API")


def test_the_ranking_is_the_wires_own_order():
    assert [sp._rank(lv) for lv in protocol.SVC_LEVELS] == list(
        range(len(protocol.SVC_LEVELS)))
    assert set(sp._LEVEL_OF.values()) == set(protocol.SVC_LEVELS)


def test_a_tie_names_the_component_listed_first():
    body = summary(api="partial_outage", code="partial_outage",
                   web="operational")
    assert sp.parse_summary(body, "claude") == ("partial", "Claude Code")


def test_unlisted_components_are_ignored():
    # The filler around ours is all major_outage.
    assert sp.parse_summary(ALL_OK_CLAUDE, "claude") == ("ok", "")
    assert sp.parse_summary(ALL_OK_CODEX, "codex") == ("ok", "")


def test_the_codex_page_reads_its_own_components():
    body = summary(codex="operational", responses="degraded_performance",
                   login="operational")
    assert sp.parse_summary(body, "codex") == ("degraded", "API")
    body = summary(codex="partial_outage", responses="operational",
                   login="major_outage")
    assert sp.parse_summary(body, "codex") == ("major", "Login")


def test_a_component_listed_twice_counts_at_its_worst():
    # status.openai.com lists "Login" twice, once per product group.
    body = json.dumps({"components": [
        {"name": "Login", "status": "operational"},
        {"name": "Login", "status": "partial_outage"},
        {"name": "Codex in ChatGPT Desktop", "status": "operational"},
    ]}).encode()
    assert sp.parse_summary(body, "codex") == ("partial", "Login")


def test_claude_components_mean_nothing_on_the_codex_page():
    body = summary(code="major_outage", codex="operational")
    assert sp.parse_summary(body, "codex") == ("ok", "")


def test_every_short_name_fits_the_wire():
    for page in sp.PAGES.values():
        for _, short in page.components:
            assert 0 < len(short.encode()) <= protocol.SVC_WHAT_MAX_BYTES


# ---------------------------------------------------------------- malformed


@pytest.mark.parametrize("body", [
    b"",
    b"<html>Service Unavailable</html>",
    b"{\"components\": [",
    b"[]",
    b"{\"components\": {}}",
    b"{\"components\": \"nope\"}",
    b"{\"status\": {\"indicator\": \"major\"}}",
    b"\xff\xfe\x00garbage",
    # A status that is not a string at all: unhashable, so a dict lookup
    # on it raised TypeError instead of answering.
    json.dumps({"components": [{"name": "Claude Code",
                                "status": ["major_outage"]}]}).encode(),
    json.dumps({"components": [{"name": "Claude Code",
                                "status": {"a": 1}}]}).encode(),
    json.dumps({"components": [{"name": ["Claude Code"],
                                "status": "major_outage"}]}).encode(),
    # Deep enough to exhaust the parser's recursion: RecursionError, which
    # is not a ValueError. Named by hand: pytest puts the test id into the
    # PYTEST_CURRENT_TEST environment variable, and a 100,000-bracket id is
    # past Windows' 32,767-character limit, so on the Windows ARM64 VM
    # (2026-10-30) this case errored in setup and teardown instead of running.
    pytest.param(b"[" * 100_000, id="deep-nesting"),
    # None of ours at all: a page that renamed them says nothing about them.
    json.dumps({"components": [{"name": "Something Else",
                                "status": "major_outage"}]}).encode(),
    json.dumps({"components": [None, 3, "x", {"name": 7}]}).encode(),
])
def test_a_garbled_summary_is_unknown(body):
    assert sp.parse_summary(body, "claude") is None
    assert sp._read("claude", opener=Opener({CLAUDE_URL: body}))[0] is None


def test_a_garbled_reply_never_shows_an_incident():
    desk = Desk(Opener({CLAUDE_URL: b"{not json"}))
    desk.poll(4)
    assert desk.shown() == ("ok", "")


def test_a_body_that_breaks_one_page_does_not_starve_the_other():
    """The parse used to sit outside the read's try. A Claude body whose
    status was a list raised out of tick() after Codex's turn had been
    spent, so Codex was never fetched and its major outage never shown."""
    bad = json.dumps({"components": [{"name": "Claude Code",
                                      "status": ["x"]}]}).encode()
    op = Opener({CLAUDE_URL: bad, CODEX_URL: summary(codex="major_outage")})
    desk = Desk(op, pages=("claude", "codex"))
    desk.poll(2)
    assert op.urls().count(CODEX_URL) == 2
    assert desk.shown("codex") == ("major", "Codex")


# ------------------------------------------------------------------ debounce


def test_one_report_is_not_enough():
    desk = Desk(Opener({CLAUDE_URL: _incident()}))
    desk.poll(1)
    assert desk.shown() == ("ok", "")


def test_two_reports_in_a_row_show_it():
    desk = Desk(Opener({CLAUDE_URL: _incident()}))
    desk.poll(2)
    assert desk.shown() == ("major", "Claude Code")


def test_a_blip_between_two_clean_polls_stays_quiet():
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE})
    desk = Desk(op)
    desk.tick()
    op.bodies[CLAUDE_URL] = _incident()
    desk.later()
    op.bodies[CLAUDE_URL] = ALL_OK_CLAUDE
    desk.later()
    assert desk.shown() == ("ok", "")


def test_an_escalation_shows_the_milder_of_the_two_polls():
    """Two incidents in a row are confirmed as an incident; how bad it is
    takes the milder of the two, so one poll alone cannot raise it."""
    op = Opener({CLAUDE_URL: _incident("partial_outage", "api")})
    desk = Desk(op)
    desk.poll(2)
    op.bodies[CLAUDE_URL] = _incident("major_outage", "code")
    desk.later()
    # The milder level, with the component that poll named.
    assert desk.shown() == ("partial", "API")
    desk.later()
    assert desk.shown() == ("major", "Claude Code")


def test_a_worsening_outage_shows_from_the_second_poll():
    """degraded, partial, major, major used to show nothing until the
    fourth poll, because only an identical pair counted."""
    op = Opener({CLAUDE_URL: _incident("degraded_performance", "api")})
    desk = Desk(op)
    desk.tick()
    assert desk.shown() == ("ok", "")
    seen = []
    for level, which in (("partial_outage", "web"), ("major_outage", "code"),
                         ("major_outage", "code")):
        op.bodies[CLAUDE_URL] = _incident(level, which)
        desk.later()
        seen.append(desk.shown())
    assert seen == [("degraded", "API"), ("partial", "claude.ai"),
                    ("major", "Claude Code")]


def test_an_outage_that_flaps_between_levels_is_still_shown():
    """partial, major, partial, major ... used to stay "ok" for as long as
    it lasted."""
    op = Opener({CLAUDE_URL: b""})
    desk = Desk(op)
    for i in range(8):
        op.bodies[CLAUDE_URL] = _incident(
            "partial_outage" if i % 2 == 0 else "major_outage", "code")
        desk.later()
    assert desk.shown() == ("partial", "Claude Code")


def test_a_different_component_names_the_current_one():
    op = Opener({CLAUDE_URL: _incident("partial_outage", "api")})
    desk = Desk(op)
    desk.poll(2)
    op.bodies[CLAUDE_URL] = _incident("partial_outage", "web")
    desk.later()
    assert desk.shown() == ("partial", "claude.ai")


def test_an_unknown_status_on_a_watched_component_counts_as_degraded():
    """A status Statuspage adds tomorrow is not "operational", and skipping
    it would let an outage read as ok."""
    body = json.dumps({"components": [{"name": "Claude Code",
                                       "status": "on_fire"}]}).encode()
    assert sp.parse_summary(body, "claude") == ("degraded", "Claude Code")
    body = json.dumps({"components": [
        {"name": "Claude Code", "status": "on_fire"},
        {"name": "claude.ai", "status": "major_outage"}]}).encode()
    assert sp.parse_summary(body, "claude") == ("major", "claude.ai")


def test_nothing_is_printed_while_the_watch_holds_its_lock(monkeypatch):
    """A print can block -- a full pipe, the Journal's own lock -- and the
    serial loop's shown() would wait behind it."""
    desk = Desk(Opener({CLAUDE_URL: _incident()}))
    held = []

    class Probe(io.StringIO):
        def write(self, s):
            import threading
            got = []
            t = threading.Thread(target=lambda: got.append(
                desk.watch._lock.acquire(blocking=False)))
            t.start()
            t.join()
            if got[0]:
                desk.watch._lock.release()
            else:
                held.append(s)
            return super().write(s)

    monkeypatch.setattr(sys, "stderr", Probe())
    desk.poll(2)
    desk.t += sp.STALE_S + 10
    desk.tick()
    assert "major" in sys.stderr.getvalue()
    assert held == []


def test_a_step_down_applies_at_once():
    """A resolved major outage must not be held five more minutes because
    another component is still degraded."""
    op = Opener({CLAUDE_URL: _incident("major_outage", "code")})
    desk = Desk(op)
    desk.poll(2)
    op.bodies[CLAUDE_URL] = _incident("degraded_performance", "api")
    desk.later()
    assert desk.shown() == ("degraded", "API")


def test_a_clean_report_clears_at_once():
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.poll(2)
    op.bodies[CLAUDE_URL] = ALL_OK_CLAUDE
    desk.later()
    assert desk.shown() == ("ok", "")


def test_polls_are_spaced_by_the_interval():
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.tick()
    for _ in range(19):
        desk.later(10)
    # 190 s of ticks: the first fetch, and nothing again until 300 s.
    assert len(op.calls) == 1
    # Ticking fast must not count as the second confirmation either.
    assert desk.shown() == ("ok", "")


def test_the_fetch_has_a_timeout():
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE})
    Desk(op).tick()
    assert op.calls == [(CLAUDE_URL, sp.TIMEOUT_S)]
    assert 5 <= sp.TIMEOUT_S <= 15


# ------------------------------------------------------- failure, staleness


def test_a_failed_fetch_does_not_clear_a_known_incident():
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.poll(2)
    op.raises = OSError("network is unreachable")
    desk.later()
    assert desk.shown() == ("major", "Claude Code")


def test_a_failed_fetch_never_shows_one_either():
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.tick()
    op.raises = TimeoutError()
    for _ in range(3):
        desk.later()
    assert desk.shown() == ("ok", "")


def test_the_last_good_answer_expires():
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.poll(2)
    op.raises = OSError("down")
    good_at = desk.t
    while desk.t - good_at <= sp.STALE_S - sp.POLL_S:
        desk.later()
    assert desk.shown() == ("major", "Claude Code")
    desk.t = good_at + sp.STALE_S + 1
    desk.tick()
    assert desk.shown() == ("ok", "")
    assert sp.STALE_S == 30 * 60


def test_an_expired_answer_does_not_count_toward_the_next_confirmation():
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.tick()                               # one report, then silence
    op.raises = OSError("down")
    desk.later(sp.STALE_S + 1)
    op.raises = None
    desk.later()                              # the first FRESH report
    assert desk.shown() == ("ok", "")


def test_a_page_that_comes_back_is_not_handed_an_old_incident():
    """shown()'s own staleness test. A page that left the board stops being
    ticked, so nothing expires it; when it returns, the Bridge asks before
    the next tick has run."""
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.poll(2)
    desk.watch.want(())
    desk.t += 3600
    desk.watch.want({"claude"})
    assert desk.watch.shown("claude") == ("ok", "")


def test_an_oversized_reply_is_refused():
    big = json.dumps({"components": [
        {"name": "Claude Code", "status": "major_outage"}],
        "pad": "x" * (sp.MAX_BYTES + 10)}).encode()
    desk = Desk(Opener({CLAUDE_URL: big}))
    desk.poll(2)
    assert desk.shown() == ("ok", "")
    assert sp.MAX_BYTES == 256 * 1024


def test_the_reader_stops_at_the_cap():
    """It must not read the whole body and THEN measure it."""
    asked = []

    class Endless(FakeResponse):
        def read(self, n=-1):
            asked.append(n)
            return b"x" * (n if n and n > 0 else 10 ** 7)

        read1 = read

    result, why = sp._read("claude", opener=lambda *a, **k: Endless(b""))
    assert result is None and "KB" in why
    assert asked and all(0 < n <= sp.MAX_BYTES + 1 for n in asked)


# --------------------------------------------------------------------- sleep


def test_an_incident_resolved_during_sleep_is_gone_on_waking():
    """Ages are wall time. Monotonic time stops while the machine sleeps,
    so a monotonic half hour could last a night and the morning's panel
    would show last night's outage."""
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.poll(2)
    op.bodies[CLAUDE_URL] = ALL_OK_CLAUDE
    desk.t += 8 * 3600                        # the lid was shut overnight
    assert desk.shown() == ("ok", "")         # before any fetch at all
    desk.tick()
    assert desk.shown() == ("ok", "")


def test_a_report_before_sleep_does_not_confirm_one_after():
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.tick()
    desk.t += 8 * 3600
    desk.tick()
    assert desk.shown() == ("ok", "")


def test_waking_asks_at_once():
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE})
    desk = Desk(op)
    desk.tick()
    desk.later(2 * sp.POLL_S)
    assert len(op.calls) == 2


def test_a_clock_that_went_back_does_not_stop_the_polling():
    """Back past the last request: that request counts as just made, so the
    next comes POLL_S on from here -- not never (arithmetic alone), and not
    at once (which would put two polls closer than POLL_S apart)."""
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE})
    desk = Desk(op)
    desk.tick()
    desk.t -= 3600
    desk.tick()
    assert len(op.calls) == 1
    desk.later(sp.POLL_S - 1)
    assert len(op.calls) == 1
    desk.later(1)
    assert len(op.calls) == 2


def test_the_milder_level_brings_its_own_component():
    """maint on the API, then a major outage on Claude Code: the panel says
    maint, so it must name the API -- not pair "maintenance" with the
    component that is actually down."""
    op = Opener({CLAUDE_URL: _incident("under_maintenance", "api")})
    desk = Desk(op)
    desk.poll(2)
    assert desk.shown() == ("maint", "API")
    op.bodies[CLAUDE_URL] = _incident("major_outage", "code")
    desk.later()
    assert desk.shown() == ("maint", "API")
    desk.later()
    assert desk.shown() == ("major", "Claude Code")


def test_the_daemon_default_clock_is_wall_time():
    import inspect
    import time
    default = inspect.signature(sp.StatusWatch).parameters["now"].default
    assert default is time.time


# ------------------------------------------------------------ the off switch


def test_off_means_no_network_call_at_all():
    op = Opener(raises=AssertionError("fetched while off"))
    desk = Desk(op, enabled=False, pages=("claude", "codex"))
    desk.poll(5)
    assert op.calls == []
    assert desk.shown() == ("ok", "")


def test_turning_it_off_mid_round_stops_the_next_request():
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE, CODEX_URL: ALL_OK_CODEX})
    desk = Desk(op, pages=("claude", "codex"))
    real = op.__call__

    def switch_off_after_first(req, timeout=None, context=None):
        r = real(req, timeout, context)
        desk.on = False
        return r

    desk.watch._opener = switch_off_after_first
    desk.tick()
    assert op.urls() == [CLAUDE_URL]


def test_off_by_default(tmp_path):
    assert sp.enabled(str(tmp_path / ".blink")) is False


def test_the_setting_turns_it_on_and_off(tmp_path):
    home = str(tmp_path / ".blink")
    user_config.put(home, sp.SETTING, True)
    assert sp.enabled(home) is True
    user_config.put(home, sp.SETTING, False)
    assert sp.enabled(home) is False


def test_only_a_literal_true_is_on(tmp_path):
    home = str(tmp_path / ".blink")
    for value in ("on", 1, "true", None, []):
        user_config.put(home, sp.SETTING, value)
        assert sp.enabled(home) is False


def test_a_garbled_config_file_is_unknown_not_off(tmp_path):
    home = tmp_path / ".blink"
    home.mkdir()
    (home / "config.json").write_text("{ status_page: yes")
    assert sp.enabled(str(home)) is None


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0,
                    reason="chmod 000 does not stop this reader")
def test_one_unreadable_tick_keeps_the_incident_and_asks_nothing(tmp_path):
    """A read that failed once used to mean "off": the confirmed incident
    vanished from the panel and the spacing was forgotten, so the very next
    tick fetched again."""
    home = str(tmp_path / ".blink")
    user_config.put(home, sp.SETTING, True)
    op = Opener({CLAUDE_URL: _incident()})
    desk = Desk(op)
    desk.watch._enabled = lambda: sp.enabled(home)
    desk.poll(2)
    assert desk.shown() == ("major", "Claude Code")
    cfg = user_config.path(home)
    os.chmod(cfg, 0)
    try:
        desk.later(10)
        assert desk.shown() == ("major", "Claude Code")
    finally:
        os.chmod(cfg, 0o600)
    desk.later(10)
    assert len(op.calls) == 2
    assert desk.shown() == ("major", "Claude Code")


def test_turning_it_off_clears_what_was_shown():
    desk = Desk(Opener({CLAUDE_URL: _incident()}))
    desk.poll(2)
    assert desk.shown()[0] == "major"
    desk.on = False
    desk.tick()
    assert desk.shown() == ("ok", "")
    # And back on, it starts over: no carried-forward confirmation.
    desk.on = True
    desk.later(1)
    assert desk.shown() == ("ok", "")


# ------------------------------------------------------- per-provider pages


def test_codex_is_only_fetched_when_its_page_is_shown():
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE, CODEX_URL: ALL_OK_CODEX})
    desk = Desk(op)
    desk.tick()
    assert op.urls() == [CLAUDE_URL]
    desk.pages = {"claude", "codex"}
    desk.tick()
    assert op.urls() == [CLAUDE_URL, CODEX_URL]


def test_nothing_is_fetched_for_a_provider_with_no_page():
    op = Opener(raises=AssertionError("no page for opencode"))
    desk = Desk(op, pages=("opencode",))
    desk.tick()
    assert op.calls == []
    assert desk.shown("opencode") == ("ok", "")


def test_an_incident_on_one_page_stays_on_that_page():
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE,
                 CODEX_URL: summary(codex="major_outage")})
    desk = Desk(op, pages=("claude", "codex"))
    desk.poll(2)
    assert desk.shown("codex") == ("major", "Codex")
    assert desk.shown("claude") == ("ok", "")


def test_one_page_failing_does_not_touch_the_other():
    class Split(Opener):
        def __call__(self, req, timeout=None, context=None):
            if req.full_url == CODEX_URL:
                self.calls.append((req.full_url, timeout))
                raise OSError("openai unreachable")
            return super().__call__(req, timeout, context)

    desk = Desk(Split({CLAUDE_URL: _incident()}), pages=("claude", "codex"))
    desk.poll(2)
    assert desk.shown("claude") == ("major", "Claude Code")
    assert desk.shown("codex") == ("ok", "")


# --------------------------------------------------------- no board, no ask


def test_a_want_that_is_not_renewed_runs_out():
    """Unplugged, reflashing, waiting for a port, or a board gone quiet: on
    every one of those paths the Bridge stops renewing, and that alone has
    to stop the requests."""
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE})
    desk = Desk(op)
    desk.tick()
    desk.t += sp.WANT_TTL_S + 1
    desk.watch.tick()                         # no want() renewed
    desk.t += sp.POLL_S
    desk.watch.tick()
    assert len(op.calls) == 1
    assert sp.WANT_TTL_S == 2 * 60


def test_a_renewed_want_keeps_polling():
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE})
    desk = Desk(op)
    desk.poll(3)
    assert len(op.calls) == 3


# ----------------------------------------------------------- poll override


@pytest.mark.parametrize("raw", ["inf", "nan", "0", "-5", "1e400", "soon"])
def test_a_bad_poll_override_keeps_the_default(raw, tmp_path, monkeypatch,
                                               capsys):
    monkeypatch.setenv(sp.DIR_ENV, str(tmp_path))
    monkeypatch.setenv(sp.POLL_ENV, raw)
    assert sp._poll_interval() == sp.POLL_S
    assert "not a usable number" in capsys.readouterr().err


def test_a_good_poll_override_applies_only_with_the_test_directory(
        tmp_path, monkeypatch):
    monkeypatch.setenv(sp.POLL_ENV, "5")
    assert sp._poll_interval() == sp.POLL_S
    monkeypatch.setenv(sp.DIR_ENV, str(tmp_path))
    assert sp._poll_interval() == 5.0
    monkeypatch.setenv(sp.POLL_ENV, "0.01")
    assert sp._poll_interval() == 1.0


# ------------------------------------------------------------------ logging


def test_the_body_never_reaches_the_log(capsys):
    marker = "MARKER-DO-NOT-LEAK"
    bodies = [
        json.dumps({"components": [{"name": "Claude Code",
                                    "status": "major_outage"}],
                    "x": marker}).encode(),
        ("{" + marker).encode(),
        json.dumps({"components": [{"name": "Claude Code",
                                    "status": marker}]}).encode(),
        json.dumps({"components": [{"name": "Claude Code",
                                    "status": [marker]}]}).encode(),
    ]
    for body in bodies:
        Desk(Opener({CLAUDE_URL: body})).poll(2)
    out = capsys.readouterr()
    assert marker not in out.out and marker not in out.err


def test_a_state_change_is_one_short_line(capsys):
    Desk(Opener({CLAUDE_URL: _incident()})).poll(2)
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["[status] claude major (Claude Code)"]


def test_a_failing_page_is_logged_once_per_run(capsys):
    op = Opener(raises=OSError("down"))
    Desk(op).poll(5)
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["[status] claude page unreadable (OSError)"]


def test_a_check_that_keeps_failing_is_logged_once(monkeypatch, capsys):
    """The background loop's catch-all. Idle ticks between polls used to
    reset it, so the same line came back at every poll."""
    w = sp.StatusWatch(enabled=lambda: True, opener=Opener())
    calls = {"n": 0}

    def tick():
        calls["n"] += 1
        if calls["n"] % 3 == 0:
            raise RuntimeError("boom")
        if calls["n"] > 30:
            raise SystemExit             # end the loop
        return 0

    monkeypatch.setattr(w, "tick", tick)
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit):
        w._run()
    err = capsys.readouterr().err.strip().splitlines()
    assert err == ["[status] check failed: RuntimeError"]


# ---------------------------------------------------------------------- CLI


def _home():
    return cli.blink_home()


def test_blink_config_turns_it_on_and_off(capsys):
    assert cli.main(["config", "status-page", "on"]) == 0
    assert sp.enabled(_home()) is True
    assert "status-page: on" in capsys.readouterr().out
    assert cli.main(["config", "status-page", "off"]) == 0
    assert sp.enabled(_home()) is False


def test_the_on_notice_says_what_it_does_and_what_it_needs(capsys):
    cli.main(["config", "status-page", "on"])
    out = capsys.readouterr().out
    assert f"every {sp.POLL_S // 60} minutes" in out
    # The last firmware without it, which cannot go stale the way a guess
    # at the release that brings it can (theme contract, Addendum 3).
    assert "1.3.4 and earlier does not show it" in out
    assert "No account, token or usage is sent." in out
    cli.main(["config", "status-page", "off"])
    assert "makes no status-page requests" in capsys.readouterr().out


def test_blink_config_alone_lists_the_settings(capsys):
    assert cli.main(["config"]) == 0
    out = capsys.readouterr().out
    assert "status-page" in out and "off" in out


def test_blink_config_without_a_value_says_the_current_one(capsys):
    user_config.put(_home(), sp.SETTING, True)
    assert cli.main(["config", "status-page"]) == 0
    assert "status-page: on" in capsys.readouterr().out
    assert sp.enabled(_home()) is True


def test_blink_config_will_not_write_over_a_broken_file(capsys):
    os.makedirs(_home(), exist_ok=True)
    p = user_config.path(_home())
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"status_page": false,}')        # a stray comma
    assert cli.main(["config", "status-page", "on"]) == 1
    assert open(p, encoding="utf-8").read() == '{"status_page": false,}'
    assert "config.json" in capsys.readouterr().out


# ------------------------------------------------------------- config file


def test_the_config_file_keeps_other_keys(tmp_path):
    home = str(tmp_path / ".blink")
    user_config.put(home, "some_future_key", 3)
    user_config.put(home, sp.SETTING, True)
    assert user_config.load(home) == {"some_future_key": 3, sp.SETTING: True}


def test_a_symlinked_config_stays_a_symlink(tmp_path):
    home = tmp_path / ".blink"
    home.mkdir()
    real = tmp_path / "dotfiles-config.json"
    real.write_text("{}")
    try:
        os.symlink(real, home / "config.json")
    except (OSError, NotImplementedError):
        pytest.skip("no symlinks here")
    user_config.put(str(home), sp.SETTING, True)
    assert os.path.islink(home / "config.json")
    assert json.loads(real.read_text()) == {sp.SETTING: True}


def test_a_config_saved_with_a_bom_reads(tmp_path):
    # Windows PowerShell 5's Set-Content -Encoding utf8 writes one.
    home = tmp_path / ".blink"
    home.mkdir()
    (home / "config.json").write_bytes(b'\xef\xbb\xbf{"status_page": true}')
    assert sp.enabled(str(home)) is True


def test_a_failed_write_leaves_no_temp_file(tmp_path, monkeypatch):
    home = tmp_path / ".blink"
    user_config.put(str(home), sp.SETTING, False)

    def boom(*a, **k):
        raise OSError("disk full")

    from pc import update
    monkeypatch.setattr(update, "_replace", boom)
    with pytest.raises(OSError):
        user_config.put(str(home), sp.SETTING, True)
    assert sorted(os.listdir(home)) == ["config.json"]
    assert sp.enabled(str(home)) is False


def test_uninstall_removes_the_setting(tmp_path, monkeypatch):
    """The opt-in is a choice about the network; it must not outlive the
    app and be found already switched on by the next install."""
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude").mkdir()
    user_config.put(_home(), sp.SETTING, True)
    stray = user_config.path(_home()) + ".abc123.blink-tmp"
    open(stray, "w").close()
    cli.main(["uninstall"])
    assert not os.path.exists(user_config.path(_home()))
    assert not os.path.exists(stray)


# ------------------------------------------------------------ test fixture


def test_the_suite_refuses_a_real_request():
    """The claim at the top of this file, pinned: with no opener injected,
    _read reaches urllib, and the conftest guard stops it there -- past the
    reader's own catch-all, which would otherwise call it "unreadable"."""
    with pytest.raises(BaseException) as e:
        sp._read("claude")
    assert "tried to reach" in str(e.value)


def test_a_test_directory_stands_in_for_the_network(tmp_path, monkeypatch):
    (tmp_path / "claude.json").write_bytes(_incident())
    monkeypatch.setenv(sp.DIR_ENV, str(tmp_path))
    got = sp._read("claude", opener=Opener(raises=AssertionError("network")))
    assert got == (("major", "Claude Code"), None)


# ------------------------------------------------------------- re-review 2


def test_the_suite_has_no_proxy_to_go_around_the_guard():
    """The socket guard allows loopback, and a proxy on 127.0.0.1 -- set by
    HTTPS_PROXY or by macOS's system settings -- would carry a request
    through it to the internet."""
    import urllib.request
    assert not [k for k in os.environ if k.lower().endswith("_proxy")]
    assert urllib.request.getproxies() == {}


def test_a_trickling_server_cannot_hold_the_thread():
    """The 10 s timeout is per read. A server sending a byte every few
    seconds never trips it, so the whole read has its own deadline."""
    t = [0.0]

    class Trickle(FakeResponse):
        def read(self, n=-1):
            t[0] += 2.0
            return b"x"

        read1 = read

    result, why = sp._read("claude", opener=lambda *a, **k: Trickle(b""),
                           clock=lambda: t[0])
    assert result is None and "slow" in why
    assert t[0] <= sp.READ_DEADLINE_S + 2.0
    assert sp.READ_DEADLINE_S == 15


def test_a_body_in_small_chunks_still_reads():
    body = _incident()

    class Chunky(FakeResponse):
        def read(self, n=-1):
            return super().read(min(n, 7) if n and n > 0 else 7)

        read1 = read

    result, _ = sp._read("claude", opener=lambda *a, **k: Chunky(body))
    assert result == ("major", "Claude Code")


def test_an_unreadable_config_mid_round_keeps_the_rest_due():
    """Cut short by a config file that could not be read, the pages not yet
    asked must not have their turn spent: they go on the next good tick,
    not five minutes later."""
    op = Opener({CLAUDE_URL: ALL_OK_CLAUDE, CODEX_URL: ALL_OK_CODEX})
    desk = Desk(op, pages=("claude", "codex"))
    answers = iter([True, True, None])
    desk.watch._enabled = lambda: next(answers, True)
    desk.tick()
    assert op.urls() == [CLAUDE_URL]
    desk.later(10)
    assert op.urls() == [CLAUDE_URL, CODEX_URL]


def test_a_deeply_nested_config_is_unreadable_not_a_crash(tmp_path):
    home = tmp_path / ".blink"
    home.mkdir()
    (home / "config.json").write_text("[" * 100_000)
    assert sp.enabled(str(home)) is None


def test_blink_config_names_what_is_wrong_with_the_file(capsys):
    os.makedirs(_home(), exist_ok=True)
    p = user_config.path(_home())
    with open(p, "w", encoding="utf-8") as f:
        f.write("[]")
    assert cli.main(["config", "status-page", "on"]) == 1
    assert "valid JSON but not an object" in capsys.readouterr().out
    with open(p, "w", encoding="utf-8") as f:
        f.write('{"a": 1,}')
    assert cli.main(["config", "status-page", "on"]) == 1
    assert "not valid JSON" in capsys.readouterr().out


def test_blink_config_lists_an_unreadable_file_legibly(capsys):
    os.makedirs(_home(), exist_ok=True)
    with open(user_config.path(_home()), "w", encoding="utf-8") as f:
        f.write("{")
    assert cli.main(["config"]) == 0
    line = capsys.readouterr().out.splitlines()[0]
    assert "unreadable " in line and "unreadableshow" not in line


def test_install_says_what_it_sends_with_the_setting_on(capsys, tmp_path,
                                                        monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    (tmp_path / ".claude").mkdir(exist_ok=True)
    cli.main(["install"])
    off = capsys.readouterr().out
    assert "sends nothing anywhere" not in off
    assert "status-page" in off
    user_config.put(_home(), sp.SETTING, True)
    cli.main(["install"])
    on = capsys.readouterr().out
    assert "status.claude.com" in on
    assert "sends nothing anywhere" not in on


def test_a_real_server_trickling_bytes_is_cut_off_at_the_deadline(
        monkeypatch):
    """Against a real http.server on loopback, sending one byte a second.

    A fake response could not show this: HTTPResponse.read(n) blocks across
    trickled bytes until it has n, so the deadline between reads was never
    reached, and the per-read socket timeout never fired either. read1 and
    a socket timeout set to the time left are what make it hold.
    """
    import http.server
    import threading
    import time as _time

    stop = threading.Event()

    class Slow(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            try:
                while not stop.is_set():
                    self.wfile.write(b" ")
                    self.wfile.flush()
                    stop.wait(1.0)
            except OSError:
                pass

        def log_message(self, *a):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Slow)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        url = f"http://127.0.0.1:{server.server_address[1]}/summary.json"
        monkeypatch.setitem(sp.PAGES, "claude",
                            sp.Page(url, sp.PAGES["claude"].components))
        monkeypatch.setattr(sp, "READ_DEADLINE_S", 3)
        start = _time.monotonic()
        result, why = sp._read("claude")
        took = _time.monotonic() - start
    finally:
        stop.set()
        server.shutdown()
        server.server_close()
    assert result is None
    assert took <= 3 + 1, took


# --------------------------------------------- the wall clock steps back
#
# Review of 1.4.0 (tests and docs), N3/S11. _fresh() has a lower bound,
# -CLOCK_SLACK_S, so an answer stamped "in the future" (the clock was reset
# behind it) is not held up as fresh. Removing the bound passed the suite:
# nothing tested a stamp ahead of now.

def test_fresh_has_a_lower_bound_as_well_as_an_upper():
    now = 1_760_000_000.0
    assert sp._fresh(now - 10, now, sp.STALE_S) is True
    assert sp._fresh(now - sp.STALE_S, now, sp.STALE_S) is True
    assert sp._fresh(now - sp.STALE_S - 1, now, sp.STALE_S) is False
    # An NTP slew: a stamp a little ahead of now is the same moment.
    assert sp._fresh(now + sp.CLOCK_SLACK_S, now, sp.STALE_S) is True
    # A clock that was reset: a stamp further ahead than the slack is not.
    assert sp._fresh(now + sp.CLOCK_SLACK_S + 1, now, sp.STALE_S) is False
    assert sp._fresh(None, now, sp.STALE_S) is False


def test_an_answer_stamped_after_a_clock_reset_does_not_hold_an_incident():
    """The incident was confirmed, then the clock was put back an hour and
    the page stopped answering. Its age measured across the step is
    negative; counted as fresh, the incident would stay on the desk until
    the clock caught up with it."""
    op = Opener({CLAUDE_URL: _incident("major_outage")})
    desk = Desk(op)
    desk.poll(2)
    assert desk.shown() == ("major", "Claude Code")
    op.raises = OSError("network down")
    desk.t -= 3600
    assert desk.shown() == sp.OK


def test_a_small_step_back_keeps_the_incident():
    op = Opener({CLAUDE_URL: _incident("major_outage")})
    desk = Desk(op)
    desk.poll(2)
    op.raises = OSError("network down")
    desk.t -= sp.CLOCK_SLACK_S - 1
    assert desk.shown() == ("major", "Claude Code")
