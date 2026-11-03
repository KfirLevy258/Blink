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
    assert r.look(NOW + claude_plan.RECHECK_S) == first == good


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
    b = r.look(NOW + 2 * claude_plan.RECHECK_S)
    assert a == b and len(n) == 1


def test_a_source_replaces_the_file(tmp_path):
    seat = dataclasses.replace(claude_plan.EMPTY_VIEW, spend_seat=True)
    answers = [seat, None]
    r = claude_plan.PlanReader(path=str(tmp_path / "nope"),
                               source=lambda: answers.pop(0))
    assert r.look(NOW) == seat
    assert r.look(NOW + claude_plan.RECHECK_S) == seat      # None: last good


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
