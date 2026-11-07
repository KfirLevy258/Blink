"""One parse of Claude Code's config: plan, seat, login fingerprint and the
spend it saved at the last /usage (pc/claude_plan). Every document here is
built by this file, with made-up ids and figures."""
import dataclasses
import json
import os

import pytest

from pc import claude_account, claude_plan

FETCHED_MS = 1_793_500_000_000
NOW = 1_793_500_060.0
ACCT, ORG = "ACCT-RAW-1", "ORG-RAW-1"


def doc(seat="enterprise_usage_based", billing=None, used=1234,
        limit=25000, enabled=True, currency="usd", cache_acct=None,
        cache=True, org_type="claude_enterprise"):
    oauth = {"accountUuid": ACCT, "organizationUuid": ORG,
             "organizationType": org_type}
    if seat is not None:
        oauth["seatTier"] = seat
    if billing is not None:
        oauth["billingType"] = billing
    d = {"oauthAccount": oauth, "projects": {"/srv/demo": {}}}
    if cache:
        extra = {"is_enabled": enabled, "monthly_limit": limit,
                 "used_credits": used, "utilization": 4.9}
        if currency is not None:
            extra["currency"] = currency
        c = {"fetchedAtMs": FETCHED_MS,
             "utilization": {"extra_usage": extra}}
        if cache_acct is not None:
            c["accountUuid"] = cache_acct
        d["cachedUsageUtilization"] = c
    return d


def view(tmp_path, d):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    return claude_plan.parse(str(p))


def test_usage_based_seat_by_seat_tier(tmp_path):
    assert view(tmp_path, doc()).spend_seat is True


def test_usage_based_seat_by_billing_type(tmp_path):
    v = view(tmp_path, doc(seat=None, billing="usage_based"))
    assert v.spend_seat is True


def test_max_is_not_a_spend_seat(tmp_path):
    v = view(tmp_path, doc(seat="max", billing="subscription",
                           org_type="claude_max"))
    assert v.spend_seat is False
    assert v.org_kind == "consumer"


def test_anchor_reads_cents(tmp_path):
    a = view(tmp_path, doc()).anchor
    assert (a.used_cents, a.limit_cents, a.enabled) == (1234, 25000, True)
    assert a.fetched_at == FETCHED_MS / 1000.0


def test_absent_currency_is_usd(tmp_path):
    assert view(tmp_path, doc(currency=None)).anchor is not None


def test_reached_cap_keeps_the_anchor(tmp_path):
    a = view(tmp_path, doc(enabled=False, limit=25000, used=25000)).anchor
    assert a.enabled is False and a.used_cents == a.limit_cents == 25000


def test_null_limit_is_kept_as_none(tmp_path):
    a = view(tmp_path, doc(limit=None)).anchor
    assert a is not None and a.limit_cents is None


@pytest.mark.parametrize("kw", [
    {"used": -1}, {"used": float("inf")}, {"used": "1234"},
    {"limit": -5}, {"limit": "25000"}, {"limit": float("inf")},
    {"enabled": "yes"}, {"currency": "eur"}, {"used": True}])
def test_bad_values_are_no_anchor(tmp_path, kw):
    v = view(tmp_path, doc(**kw))
    assert v.anchor is None
    assert v.spend_seat is True         # the cache never decides the mode


def test_no_cache_is_no_anchor(tmp_path):
    assert view(tmp_path, doc(cache=False)).anchor is None


def test_cache_from_another_login_is_no_anchor(tmp_path):
    assert view(tmp_path, doc(cache_acct="ACCT-RAW-2")).anchor is None
    assert view(tmp_path, doc(cache_acct=ACCT)).anchor is not None


def test_view_keeps_no_raw_ids(tmp_path):
    v = view(tmp_path, doc(cache_acct=ACCT))
    assert ACCT not in repr(v) and ORG not in repr(v)
    assert v.account_fp == claude_account._fingerprint((ACCT, ORG))
    assert v.org_kind == "enterprise"
    assert "claude_enterprise" not in repr(v)


def test_months():
    assert claude_plan.month_start_utc(NOW) == 1_793_491_200.0   # 1 Nov
    assert claude_plan.next_month_start_utc(NOW) == 1_796_083_200.0
    dec = 1_796_100_000.0
    assert claude_plan.next_month_start_utc(dec) == 1_798_761_600.0


def test_failed_read_keeps_the_last_view(tmp_path, monkeypatch):
    p = tmp_path / ".claude.json"
    p.write_text("{}", encoding="utf-8")
    good = dataclasses.replace(claude_plan.EMPTY_VIEW, spend_seat=True)
    results = [good, None]
    monkeypatch.setattr(claude_plan, "parse", lambda path, st=None:
                        results.pop(0))
    r = claude_plan.PlanReader(path=str(p))
    first = r.look(NOW)
    os.utime(p, (NOW, NOW + 7))
    assert r.look(NOW + 60) == first == good


def test_missing_file_is_the_empty_view_and_other_errors_keep_the_last(
        tmp_path, monkeypatch):
    p = tmp_path / ".claude.json"
    r = claude_plan.PlanReader(path=str(p))
    assert r.look(NOW) is claude_plan.EMPTY_VIEW
    good = view(tmp_path, doc())
    r = claude_plan.PlanReader(path=str(p))
    assert r.look(NOW) == good
    real = os.stat

    def boom(path, *a, **k):
        if str(path) == str(p):
            raise PermissionError
        return real(path, *a, **k)
    monkeypatch.setattr(os, "stat", boom)
    assert r.look(NOW + 120) == good
    monkeypatch.undo()
    os.remove(p)
    assert r.look(NOW + 240) is claude_plan.EMPTY_VIEW


def test_look_parses_once(tmp_path, monkeypatch):
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    real = claude_plan.parse
    n = []
    monkeypatch.setattr(claude_plan, "parse", lambda path, st=None:
                        n.append(1) or real(path, st))
    r = claude_plan.PlanReader(path=str(p))
    a = r.look(NOW)
    b = r.look(NOW + 120)
    assert a == b and len(n) == 1


def test_a_source_replaces_the_file(tmp_path):
    seat = dataclasses.replace(claude_plan.EMPTY_VIEW, spend_seat=True)
    answers = [seat, None]
    r = claude_plan.PlanReader(path=str(tmp_path / "nope"),
                               source=lambda: answers.pop(0))
    assert r.look(NOW) == seat
    assert r.look(NOW + 60) == seat      # None: last good


def test_no_answer_yet_is_not_held_for_a_minute(tmp_path):
    """/code-review max of 2026-11-04, U18: a WSL home's source answers
    None until its watch's first tick, and the first look stamped its time
    all the same, so EMPTY_VIEW -- "no seat" -- was held for a minute
    after every daemon start. A spend desk then showed Claude Desktop's
    windows figures, the saved Ultra verdict's mark and the token burn
    view for most of a minute. The source is asked on every look (since
    round 5's ruling A, after a first answer too), and a None keeps the
    last answer."""
    seat = dataclasses.replace(claude_plan.EMPTY_VIEW, spend_seat=True)
    answers = [None, None, seat]
    asked = []

    def source():
        asked.append(1)
        return answers.pop(0) if answers else None

    r = claude_plan.PlanReader(path=str(tmp_path / "nope"), source=source)
    assert r.look(NOW) == claude_plan.EMPTY_VIEW
    assert r.look(NOW + 2) == claude_plan.EMPTY_VIEW
    assert r.look(NOW + 4) == seat
    assert r.look(NOW + 6) == seat and len(asked) == 4     # None: kept


def test_a_file_not_read_yet_is_tried_again_at_the_next_look(tmp_path,
                                                              monkeypatch):
    """Likewise a native config locked at the first look (Windows), or
    caught mid-write: no answer yet, so no minute of "no seat"."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    real = claude_plan.parse
    fails = [1]
    monkeypatch.setattr(claude_plan, "parse", lambda path, st=None:
                        None if fails and fails.pop() else real(path, st))
    r = claude_plan.PlanReader(path=str(p))
    assert r.look(NOW).spend_seat is False
    assert r.look(NOW + 2).spend_seat is False        # not every poll
    assert r.look(NOW + claude_plan.FIRST_ANSWER_RETRY_S).spend_seat is True
    assert claude_plan.FIRST_ANSWER_RETRY_S < 60


def test_ultra_and_read_still_answer(tmp_path):
    d = doc()
    d["oauthAccount"].update(organizationRateLimitTier="default_claude_max_20x",
                             planDisplayName="Ultra")
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert claude_plan.read(str(p)) is True
    assert claude_plan.PlanReader(path=str(p)).ultra(NOW) is True


def test_account_watch_reads_no_file(monkeypatch):
    v = dataclasses.replace(claude_plan.EMPTY_VIEW, account_fp="f" * 64,
                            org_kind="enterprise", mtime=NOW - 10)

    def no_open(*a, **k):
        raise AssertionError("opened a file")
    monkeypatch.setattr("builtins.open", no_open)
    monkeypatch.setattr(os, "stat", no_open)
    w = claude_account.AccountWatch(lambda now: v)
    w.poll(NOW)
    assert w.kind == "enterprise" and w.touched_at == NOW - 10
    assert w.since is None


def test_half_written_config_keeps_the_last_view(tmp_path):
    p = tmp_path / ".claude.json"
    full = json.dumps(doc())
    p.write_text(full, encoding="utf-8")
    r = claude_plan.PlanReader(path=str(p))
    good = r.look(NOW)
    assert good.spend_seat is True and good.anchor is not None
    # Claude Code mid-rewrite: the text stops part-way, or is not UTF-8.
    for i, raw in enumerate((full[:len(full) // 2].encode(), b"\xff\xfe{")):
        p.write_bytes(raw)
        os.utime(p, (NOW + 10 + i, NOW + 10 + i))
        assert claude_plan.parse(str(p)) is None
        assert r.look(NOW + 100 * (i + 1)) == good
    # Valid JSON of the wrong shape is an answer, kept: not a seat.
    p.write_text("[1, 2]", encoding="utf-8")
    os.utime(p, (NOW + 50, NOW + 50))
    assert r.look(NOW + 300).spend_seat is False


# -- parse() never raises (final review of 2026-11-04, U1's siblings) --------

HUGE = 10 ** 400        # json reads it; math.isfinite raises OverflowError


def _with_cache(**over):
    d = doc()
    extra = d["cachedUsageUtilization"]["utilization"]["extra_usage"]
    for k, v in over.items():
        if k == "fetchedAtMs":
            d["cachedUsageUtilization"][k] = v
        else:
            extra[k] = v
    return d


@pytest.mark.parametrize("over", [
    {"used_credits": HUGE}, {"monthly_limit": HUGE}, {"fetchedAtMs": HUGE},
    # Finite but absurd: past ten million dollars in cents, or a fetch
    # time past 2100. Refused, not carried to the page and the wire.
    {"used_credits": 10 ** 300}, {"monthly_limit": 1e300},
    {"used_credits": 10 ** 9 + 1}, {"fetchedAtMs": 1e300},
])
def test_an_absurd_figure_is_no_anchor_and_does_not_raise(tmp_path, over):
    v = view(tmp_path, _with_cache(**over))
    assert v is not None
    assert v.anchor is None
    assert v.spend_seat is True


def test_an_id_that_will_not_encode_does_not_raise(tmp_path):
    """json reads a lone surrogate ("\\ud800") into a str that .encode
    ("utf-8") refuses with UnicodeEncodeError, and _fingerprint encodes
    the ids: parse() raised, though it says it never does, and with it
    PlanReader.look, a WSL watch tick and the Ultra check."""
    p = tmp_path / ".claude.json"
    d = doc()
    d["oauthAccount"]["accountUuid"] = "\ud800-x"
    p.write_text(json.dumps(d), encoding="utf-8")
    v = claude_plan.parse(str(p))
    assert v is not None and v.spend_seat is True
    assert len(v.account_fp) == 64
    d["oauthAccount"]["accountUuid"] = "\udc00-x"
    p.write_text(json.dumps(d), encoding="utf-8")
    assert claude_plan.parse(str(p)).account_fp != v.account_fp


def test_a_cache_id_nested_deep_does_not_raise(tmp_path):
    p = tmp_path / ".claude.json"
    d = doc()
    nest = []
    for _ in range(900):
        nest = [nest]
    d["cachedUsageUtilization"]["accountUuid"] = nest
    p.write_text(json.dumps(d), encoding="utf-8")
    v = claude_plan.parse(str(p))
    assert v is not None and v.anchor is None


def test_a_changed_config_is_read_at_the_next_look(tmp_path, monkeypatch):
    """Round 5 of the 2026-11-04 review, ruling A: the config was looked at
    once a minute while the status line is read every two seconds, so a
    /login was seen up to a minute late -- the new login's renders counted
    on the old one, and its first render taken for the old login's. The
    file is stat'ed at every look and parsed when its signature changes;
    an unchanged file is still parsed once."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    real = claude_plan.parse
    n = []
    monkeypatch.setattr(claude_plan, "parse", lambda path, st=None:
                        n.append(1) or real(path, st))
    r = claude_plan.PlanReader(path=str(p))
    first = r.look(NOW)
    for k in range(1, 10):
        assert r.look(NOW + 2 * k) == first
    assert len(n) == 1
    d = doc()
    d["oauthAccount"]["accountUuid"] = "ACCT-OTHER-0c0c0c"
    p.write_text(json.dumps(d), encoding="utf-8")
    os.utime(p, (NOW + 21, NOW + 21))
    assert r.look(NOW + 22).account_fp != first.account_fp
    assert len(n) == 2


def test_a_source_is_asked_at_every_look(tmp_path):
    """The same for a WSL home's source, which answers from memory."""
    seat = dataclasses.replace(claude_plan.EMPTY_VIEW, spend_seat=True)
    other = dataclasses.replace(seat, account_fp="other")
    answers = [seat, other]
    r = claude_plan.PlanReader(path=str(tmp_path / "nope"),
                               source=lambda: answers.pop(0))
    assert r.look(NOW) == seat
    assert r.look(NOW + 2) == other


def test_a_changed_file_that_fails_is_retried_within_seconds(tmp_path,
                                                             monkeypatch):
    """Round 6 of the 2026-11-04 review, item 3: after a first answer, a
    file that changed and then failed to read (a write caught half done)
    was tried again only a minute later, so a /login caught mid-write was
    seen a minute late. It is tried again FIRST_ANSWER_RETRY_S later."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    r = claude_plan.PlanReader(path=str(p))
    first = r.look(NOW)
    d = doc()
    d["oauthAccount"]["accountUuid"] = "ACCT-OTHER-0c0c0c"
    p.write_text(json.dumps(d), encoding="utf-8")
    os.utime(p, (NOW + 9, NOW + 9))
    real = claude_plan.parse
    fails = [1]
    monkeypatch.setattr(claude_plan, "parse", lambda path, st=None:
                        None if fails and fails.pop() else real(path, st))
    assert r.look(NOW + 10) == first                  # failed: kept
    assert r.look(NOW + 10 + claude_plan.FIRST_ANSWER_RETRY_S) \
        .account_fp != first.account_fp


def test_a_changing_file_is_parsed_at_most_once_in_five_seconds(tmp_path,
                                                                 monkeypatch):
    """Item 4: the file is stat'ed at every look, but a file Claude Code
    rewrites every poll was parsed every poll -- the whole of it, up to
    MAX_BYTES, on the serial thread. A change is parsed at most once a
    FIRST_ANSWER_RETRY_S; one that comes sooner waits for it."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    real = claude_plan.parse
    n = []
    monkeypatch.setattr(claude_plan, "parse", lambda path, st=None:
                        n.append(1) or real(path, st))
    r = claude_plan.PlanReader(path=str(p))
    r.look(NOW)
    for k in range(1, 6):                             # a rewrite a poll
        os.utime(p, (NOW + 2 * k, NOW + 2 * k))
        r.look(NOW + 2 * k)
    # Parsed at NOW and NOW + 6; the changes at +2, +4, +8 and +10 wait.
    assert claude_plan.FIRST_ANSWER_RETRY_S == 5
    assert len(n) == 2
    r.look(NOW + 11)                                  # the one owed
    assert len(n) == 3


def test_one_look_a_poll_whoever_asks(tmp_path, monkeypatch):
    """Item 4: the provider, the login watch, the bus's spend check and
    ultra() each looked, a stat each; looks at one poll's time share one."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    real = os.stat
    stats = []

    def counting(path, *a, **k):
        if str(path) == str(p):
            stats.append(1)
        return real(path, *a, **k)
    r = claude_plan.PlanReader(path=str(p))
    monkeypatch.setattr(os, "stat", counting)
    for _ in range(4):
        r.look(NOW)
    r.ultra(NOW)
    assert len(stats) == 1
    r.look(NOW + 2)
    assert len(stats) == 2


def test_a_config_deleted_and_written_again_is_read_at_once(tmp_path,
                                                            monkeypatch):
    """Final review of round 7, F3: a rewrite by delete and create answered
    "no file" between the two, and the new file then waited out the five
    seconds since the last parse: polls of "no seat", the spend page gone.
    After a "no file" answer the next file is parsed at once."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    r = claude_plan.ConfigFile()
    first = r.look(str(p), NOW)
    assert first is not None and first.spend_seat
    os.remove(p)
    assert r.look(str(p), NOW + 1) == claude_plan.EMPTY_VIEW
    p.write_text(json.dumps(doc()), encoding="utf-8")
    got = r.look(str(p), NOW + 2)
    assert got is not None and got.spend_seat



def test_a_time_before_the_last_parse_is_not_due(tmp_path, monkeypatch):
    """F4: the spacing read a time before the last parse as due, so a clock
    gone back parsed at once and every look after it could. Such a look
    is not due; the parse comes FIRST_ANSWER_RETRY_S later on the clock
    as it now reads, not when the clock catches up with the old one."""
    p = tmp_path / ".claude.json"
    p.write_text(json.dumps(doc()), encoding="utf-8")
    real = claude_plan.parse
    n = []
    monkeypatch.setattr(claude_plan, "parse", lambda path, st=None:
                        n.append(1) or real(path, st))
    r = claude_plan.ConfigFile()
    r.look(str(p), NOW)
    d = doc()
    d["oauthAccount"]["accountUuid"] = "ACCT-OTHER-0c0c0c"
    p.write_text(json.dumps(d), encoding="utf-8")
    os.utime(p, (NOW + 9, NOW + 9))
    assert r.look(str(p), NOW - 3600) is None          # the clock went back
    assert r.look(str(p), NOW - 3598) is None
    assert len(n) == 1
    got = r.look(str(p), NOW - 3600 + claude_plan.FIRST_ANSWER_RETRY_S)
    assert got is not None and len(n) == 2


# --- the 5-hour and weekly figures in the same cache -------------------

def _wdoc(five=None, week=None, uuid="u1", cache_uuid="u1",
          fetched_ms=1_793_828_166_919):
    util = {"five_hour": five, "seven_day": week}
    return {"oauthAccount": {"accountUuid": uuid},
            "cachedUsageUtilization": {"accountUuid": cache_uuid,
                                       "fetchedAtMs": fetched_ms,
                                       "utilization": util}}


def test_cache_windows_reads_both_windows():
    w = claude_plan._cache_windows(_wdoc(
        five={"utilization": 42, "resets_at": "2026-11-05T10:00:00+00:00"},
        week={"utilization": 15,
              "resets_at": "2026-11-11T05:59:59.801815+00:00"}))
    assert (w.five_pct, w.week_pct) == (42.0, 15.0)
    assert w.five_resets_at == 1793872800.0
    assert w.week_resets_at == pytest.approx(1794376799.8018, abs=1e-3)
    assert w.fetched_at == pytest.approx(1793828166.919)


def test_cache_windows_null_five_hour_keeps_week():
    w = claude_plan._cache_windows(_wdoc(
        five=None, week={"utilization": 15,
                         "resets_at": "2026-11-11T05:59:59+00:00"}))
    assert w.five_pct is None and w.five_resets_at is None
    assert w.week_pct == 15.0


def test_cache_windows_other_login_is_ignored():
    assert claude_plan._cache_windows(_wdoc(
        week={"utilization": 15, "resets_at": "2026-11-11T05:59:59+00:00"},
        cache_uuid="someone-else")) is None


@pytest.mark.parametrize("bad", [
    {"utilization": "x"}, {"utilization": -1}, {"utilization": 1e309},
    {"utilization": 5, "resets_at": "not a date"}, [], "15"])
def test_cache_windows_bad_values_never_raise(bad):
    w = claude_plan._cache_windows(_wdoc(week=bad))
    assert w is None or w.week_pct is None or 0 <= w.week_pct <= 1000


def test_cache_windows_bad_reset_costs_only_the_reset():
    w = claude_plan._cache_windows(_wdoc(
        week={"utilization": 5, "resets_at": "not a date"}))
    assert w.week_pct == 5.0 and w.week_resets_at is None


def test_cache_windows_none_without_any_window():
    assert claude_plan._cache_windows(_wdoc()) is None


def test_view_carries_windows_and_empty_view_has_none(tmp_path):
    d = doc(seat="max", cache=False)
    d["cachedUsageUtilization"] = _wdoc(
        week={"utilization": 15, "resets_at": "2026-11-11T05:59:59+00:00"}
    )["cachedUsageUtilization"]
    d["cachedUsageUtilization"]["accountUuid"] = ACCT
    assert view(tmp_path, d).windows.week_pct == 15.0
    assert claude_plan.EMPTY_VIEW.windows is None
