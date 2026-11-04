"""The account-switch guard (task 15b): pc/claude_account and its seat in
pc/ingest.

Every profile here is made by save_login() below, with made-up ids.
Nothing is copied from a real ~/.claude.json, which holds a person's
project history beside the account.
"""
import json
import types
import os

import pytest

from pc import claude_account, claude_plan, ingest, normalizer
from pc.providers import base

NOW = 1_790_000_000.0


def save_login(path, org_type="claude_pro", account="u-0001",
               org="o-0001", more=None):
    """A ~/.claude.json-shaped file for one login. The keys beside
    oauthAccount are invented filler, there so the reader is shown it must
    pick one key out of a document with other things in it."""
    doc = {"oauthAccount": {"organizationType": org_type,
                            "accountUuid": account,
                            "organizationUuid": org},
           "editorMode": "vim", "lastSeenNotice": 4,
           "bookmarks": [{"dir": "/srv/demo", "pinned": True}]}
    doc.update(more or {})
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def bump(path, seconds=5):
    """Move the file's mtime on, the way a rewrite would."""
    st = os.stat(path)
    os.utime(path, (st.st_atime + seconds, st.st_mtime + seconds))


def spend(at, pct=62.8, usd=314.12, lim=500.0, src="cli"):
    return base.NormalizedUsageFrame(
        provider="claude", src=src, observed_at=at, session_pct=pct,
        plan=base.PLAN_SPEND, spend_usd=usd, spend_limit_usd=lim,
        spend_period="monthly")


def windows(at, s=41.0, w=12.0, src="desktop"):
    return base.NormalizedUsageFrame(
        provider="claude", src=src, observed_at=at, session_pct=s,
        weekly_pct=w)


def state_only(at):
    return base.NormalizedUsageFrame(
        provider="claude", src="hook", observed_at=at,
        state=base.STATE_RUNNING, n_run=1)


def view_of(path):
    """A view source over one file with no pacing: every poll parses, so a
    test sees each change at once. The cached, paced one is PlanReader's
    (tests/pc/test_ultra_plan.py, test_spend_seat.py)."""
    def source(now):
        return claude_plan.parse(str(path)) or claude_plan.EMPTY_VIEW
    return source


def watch_of(path):
    return claude_account.AccountWatch(view_of(path))


def test_profile_kinds(tmp_path):
    p = tmp_path / ".claude.json"
    for org_type, want in (("claude_enterprise", "enterprise"),
                           ("claude_team", "team"),
                           ("claude_max", "consumer"),
                           ("claude_pro", "consumer"),
                           ("something_new", "consumer"),
                           (None, "consumer"),
                           (17, "consumer")):
        save_login(p, org_type=org_type)
        assert claude_plan.parse(str(p)).org_kind == want, org_type


def test_no_profile_is_no_news(tmp_path):
    p = tmp_path / ".claude.json"
    for text in (json.dumps({"oauthAccount": {}})[:-3],
                 json.dumps({"editorMode": "emacs"}),      # API-key login
                 json.dumps({"oauthAccount": {
                     "organizationType": "claude_enterprise"}}),
                 "[1, 2]"):
        p.write_text(text, encoding="utf-8")
        assert (claude_plan.parse(str(p))
                or claude_plan.EMPTY_VIEW).account_fp == "", text
        w = watch_of(p)
        w.poll(NOW)
        assert w.kind == "" and w.since is None
    p.write_bytes(b"\xff\xfe{}")
    assert claude_plan.parse(str(p)) is None        # retry, not an answer
    w = watch_of(tmp_path / "missing.json")
    w.poll(NOW)
    assert w.kind == "" and w.touched_at is None


def test_an_oversized_profile_is_not_read(tmp_path, monkeypatch):
    p = save_login(tmp_path / ".claude.json",
                   org_type="claude_enterprise", more={"pad": "x" * 2000})
    monkeypatch.setattr(claude_plan, "MAX_BYTES", 1000)
    assert claude_plan.parse(str(p)).account_fp == ""
    monkeypatch.setattr(claude_plan, "MAX_BYTES", 10_000)
    assert claude_plan.parse(str(p)).org_kind == "enterprise"


def test_the_account_ids_are_never_held_only_a_digest_of_them(tmp_path):
    p = save_login(tmp_path / ".claude.json", account="RAW-ACCOUNT-ID",
                   org="RAW-ORG-ID")
    view = claude_plan.parse(str(p))
    w = watch_of(p)
    w.poll(NOW)
    held = repr(view) + repr(vars(w))
    assert "RAW-ACCOUNT-ID" not in held and "RAW-ORG-ID" not in held
    # Still a different digest for a different account, and the same one
    # for the same account read twice.
    other = save_login(tmp_path / "b.json", account="RAW-ACCOUNT-ID-2",
                       org="RAW-ORG-ID")
    assert claude_plan.parse(str(other)).account_fp != view.account_fp
    assert claude_plan.parse(str(p)) == view


def test_nothing_from_the_profile_is_ever_printed(tmp_path, capsys):
    p = save_login(tmp_path / ".claude.json",
                   org_type="claude_enterprise",
                   account="SECRET-ACCOUNT", org="SECRET-ORG")
    w = watch_of(p)
    w.poll(NOW)
    save_login(p, org_type="claude_max", account="OTHER-SECRET")
    bump(p)
    w.poll(NOW + 2)
    p.write_text('{"oauthAccount": SECRET-BROKEN', encoding="utf-8")
    bump(p, 10)
    w.poll(NOW + 4)
    claude_account.select([spend(NOW)], NOW + 4, w.kind, w.since,
                          w.touched_at)
    out = capsys.readouterr()
    for secret in ("SECRET", "claude_enterprise", "claude_max"):
        assert secret not in out.out and secret not in out.err


def test_a_switch_is_dated_when_seen_and_startup_is_not_a_switch(tmp_path):
    p = save_login(tmp_path / ".claude.json", org_type="claude_max")
    w = watch_of(p)
    w.poll(NOW)
    assert w.kind == "consumer"
    assert w.since is None          # the account found at startup
    save_login(p, org_type="claude_enterprise", account="acct-2",
               org="org-2")
    bump(p)
    w.poll(NOW + 2)
    assert w.kind == "enterprise"
    assert w.since == NOW + 2
    # A rewrite of the same account (project history) is no switch.
    save_login(p, org_type="claude_enterprise", account="acct-2",
               org="org-2", more={"lastSeenNotice": 5})
    bump(p, 10)
    w.poll(NOW + 4)
    assert w.since == NOW + 2


def _stamp(path, at):
    os.utime(path, (at, at))


def test_a_switch_is_dated_by_the_rewrite_that_carried_it(tmp_path):
    """/code-review max of 2026-11-04, finding 9: the switch was dated by
    the poll on which PlanReader's cached view first showed the new login,
    up to a minute after /login then, and select() dropped every reading
    the new login made in that minute as the old one's. /login writes the
    file, so the switch happened no later than the mtime that carries the
    new fingerprint, and no earlier than the mtime that still carried the
    old one."""
    p = save_login(tmp_path / ".claude.json", org_type="claude_max")
    _stamp(p, NOW - 500)
    reader = claude_plan.PlanReader(path=str(p))
    w = claude_account.AccountWatch(reader.look)
    w.poll(NOW - 100)
    save_login(p, org_type="claude_enterprise", account="acct-2",
               org="org-2")
    _stamp(p, NOW - 55)                       # /login
    # Seen at the next look since round 5's ruling A (every look stats
    # the file); a look a few seconds late still dates it by the stamp.
    w.poll(NOW - 50)
    assert w.kind == "enterprise"
    assert w.since == NOW - 55
    # The new login's first reading, made inside that minute, is kept.
    kept = claude_account.select([windows(NOW - 50, src="cli")], NOW,
                                 w.kind, w.since, w.touched_at)
    assert any(f.observed_at == NOW - 50 for f in kept)


def test_a_rewrite_stamp_that_cannot_be_the_switch_is_not_used(tmp_path):
    p = save_login(tmp_path / ".claude.json", org_type="claude_max")
    _stamp(p, NOW - 500)
    w = watch_of(p)
    w.poll(NOW - 100)
    # A stamp from the future (a clock that disagrees) dates it now.
    save_login(p, org_type="claude_enterprise", account="acct-2",
               org="org-2")
    _stamp(p, NOW + 3600)
    w.poll(NOW)
    assert w.since == NOW
    # One no newer than the stamp the old login was read under is not a
    # rewrite that carried the switch either.
    save_login(p, org_type="claude_max", account="u-0001", org="o-0001")
    _stamp(p, NOW - 900)
    w.poll(NOW + 10)
    assert w.since == NOW + 10


def test_an_unreadable_moment_changes_nothing(tmp_path):
    p = save_login(tmp_path / ".claude.json",
                   org_type="claude_enterprise")
    w = watch_of(p)
    w.poll(NOW)
    p.write_text('{"oauthAccount": {"accountU', encoding="utf-8")
    bump(p)
    w.poll(NOW + 2)
    assert w.kind == "enterprise" and w.since is None
    os.remove(p)
    w.poll(NOW + 4)
    assert w.kind == "enterprise" and w.since is None


def test_the_profile_is_read_once_per_change(tmp_path, monkeypatch):
    p = save_login(tmp_path / ".claude.json")
    reads = []
    real = claude_plan.parse
    monkeypatch.setattr(claude_plan, "parse",
                        lambda path, st=None: reads.append(path)
                        or real(path, st))
    reader = claude_plan.PlanReader(path=str(p))
    w = claude_account.AccountWatch(reader.look)
    for i in range(30):
        w.poll(NOW + 2 * i)
    assert len(reads) == 1
    bump(p)
    for i in range(30):
        w.poll(NOW + 100 + 2 * i)
    assert len(reads) == 2


def test_nothing_from_before_a_switch_reaches_the_page():
    """Review focus 5: /login from Max to Enterprise mid-window. The Max
    figures are still in every file; none of them may be drawn."""
    switch = NOW - 30
    frames = [windows(NOW - 120, src="cli"), windows(NOW - 60),
              spend(NOW - 10)]
    out = claude_account.select(frames, NOW, "enterprise", switch)
    assert [f.plan for f in out] == [base.PLAN_SPEND]
    m = normalizer.merge(out)
    assert m.plan == base.PLAN_SPEND and m.session_pct == 62.8
    assert m.weekly_pct == base.UNKNOWN

    # And with nothing from the new account yet: the old figures are gone
    # and the page says the new one reports nothing, rather than showing
    # Max's 41% under an Enterprise login.
    out = claude_account.select(frames[:2], NOW, "enterprise", switch)
    assert [f.plan for f in out] == [base.PLAN_UNREPORTED]


def test_an_ended_window_from_before_a_switch_is_dropped_too():
    old = base.NormalizedUsageFrame(
        provider="claude", src="cli", observed_at=NOW - 100,
        session_rolled_at=NOW - 200)
    out = claude_account.select([old], NOW, "consumer", NOW - 50)
    assert out == []


def test_the_session_state_is_not_an_accounts_business():
    hook = state_only(NOW - 100)
    out = claude_account.select([hook, spend(NOW)], NOW, "enterprise",
                                NOW - 50)
    assert hook in out


def test_a_fresher_spend_reading_drops_every_window_reading():
    frames = [windows(NOW - 60), windows(NOW - 30, src="cli"),
              spend(NOW - 5)]
    out = claude_account.select(frames, NOW)
    assert all(f.plan == base.PLAN_SPEND for f in out)


def test_a_fresher_window_reading_drops_the_spend_reading():
    """Back on Max after the corporate login, with a daemon that never saw
    the switch (it was not running): the freshest reading still decides."""
    frames = [spend(NOW - 60), windows(NOW - 5, src="cli")]
    out = claude_account.select(frames, NOW)
    assert [f.plan for f in out] == [base.PLAN_WINDOWS]


def test_an_enterprise_login_with_nothing_to_read_says_so():
    out = claude_account.select([state_only(NOW - 3)], NOW, "enterprise",
                                touched_at=NOW - 40)
    unrep = [f for f in out if f.plan == base.PLAN_UNREPORTED]
    assert len(unrep) == 1
    f = unrep[0]
    assert f.account == "enterprise"
    assert f.observed_at == NOW          # true as of this poll
    assert f.active_at == NOW - 40       # but not proof anyone is here
    assert not f.has_usage()
    m = normalizer.merge(out)
    assert m.plan == base.PLAN_UNREPORTED and m.account == "enterprise"
    assert m.state == base.STATE_RUNNING     # the state still travels

    team = claude_account.select([], NOW, "team")
    assert team[0].account == "team"


def test_a_profile_mtime_in_the_future_does_not_wake_the_board():
    out = claude_account.select([], NOW, "enterprise", touched_at=NOW + 99)
    assert out[0].active_at == NOW


def test_a_team_account_that_reports_windows_shows_windows():
    out = claude_account.select([windows(NOW - 5)], NOW, "team")
    assert [f.plan for f in out] == [base.PLAN_WINDOWS]


def test_a_consumer_login_with_nothing_is_left_alone():
    assert claude_account.select([], NOW, "consumer") == []
    assert claude_account.select([], NOW, "") == []


def test_codex_is_never_filtered():
    codex = base.NormalizedUsageFrame(provider="codex", src="cli",
                                      observed_at=NOW - 500, session_pct=9)
    out = claude_account.select([codex, spend(NOW)], NOW, "enterprise",
                                NOW - 50)
    assert codex in out


def test_a_merged_spend_page_keeps_its_dollars_together():
    """The plan and dollars come from the primary reading as one statement:
    an older reading's dollars never pair with a newer reading's share."""
    older = spend(NOW - 400, pct=50.0, usd=250.0, lim=500.0)
    newer = spend(NOW - 5, pct=63.0, usd=None, lim=None)
    m = normalizer.merge([older, newer])
    assert m.session_pct == 63.0
    assert m.plan == base.PLAN_SPEND
    assert m.spend_usd is None and m.spend_limit_usd is None
    assert m.spend_period == "monthly"


def test_an_unreported_page_survives_the_merge():
    f = base.NormalizedUsageFrame(provider="claude", src="account",
                                  observed_at=NOW,
                                  plan=base.PLAN_UNREPORTED,
                                  account="enterprise")
    primary, secondary = normalizer.select_pair([f])
    assert primary is not None and primary.plan == base.PLAN_UNREPORTED
    assert secondary is None


def test_a_windows_merge_is_still_a_windows_frame():
    m = normalizer.merge([windows(NOW - 60), windows(NOW - 5, src="cli")])
    assert m.plan == base.PLAN_WINDOWS
    assert m.spend_usd is None and m.account == ""


def _source(held):
    """A claude provider that reports whatever is in `held` at each poll;
    a test changes what it reports by refilling that list."""
    return types.SimpleNamespace(get_provider_id=lambda: "claude",
                                 poll=lambda now: list(held))


class _Watch:
    """An AccountWatch stand-in that counts how often it is asked."""

    def __init__(self, kind="", since=None):
        self.kind, self.since, self.touched_at = kind, since, None
        self.polls = 0

    def poll(self, now):
        self.polls += 1


def test_a_bus_that_never_saw_a_spend_cap_never_asks_about_the_account():
    """The release gate: a windows-only desk runs exactly what it ran."""
    watch = _Watch("enterprise", since=NOW - 1)
    src_frames = [windows(NOW - 600)]
    src = _source(src_frames)
    bus = ingest.IngestionBus(providers=[src], now=lambda: NOW,
                              account=watch)
    for _ in range(5):
        msg = bus.poll()
    assert watch.polls == 0
    # The pre-"switch" reading is still drawn: nothing was filtered.
    assert msg["session_pct"] == 41.0


def test_the_default_bus_does_not_open_the_profile_on_a_windows_desk(
        tmp_path, monkeypatch):
    opened = []
    monkeypatch.setattr(claude_plan, "parse",
                        lambda path, st=None: opened.append(path))
    bus = ingest.IngestionBus()
    bus.poll()
    bus.poll()
    assert opened == []


def test_one_spend_reading_arms_the_guard_for_good():
    watch = _Watch("consumer")
    src_frames = [spend(NOW - 5)]
    src = _source(src_frames)
    bus = ingest.IngestionBus(providers=[src], now=lambda: NOW,
                              account=watch)
    bus.poll()
    assert watch.polls == 1
    src_frames[:] = [windows(NOW - 1)]
    bus.poll()
    assert watch.polls == 2


def test_an_account_that_cannot_be_read_costs_the_poll_nothing():
    class Broken(_Watch):
        def poll(self, now):
            raise PermissionError("nope")

    bus = ingest.IngestionBus(providers=[_source([spend(NOW - 5)])],
                              now=lambda: NOW, account=Broken())
    msg = bus.poll()
    assert msg is not None and msg["session_pct"] == 62.8


@pytest.mark.parametrize("since", [None, NOW - 100])
def test_the_bus_applies_the_switch(since):
    watch = _Watch("enterprise", since=since)
    src_frames = [windows(NOW - 200, s=88.0), spend(NOW - 5)]
    src = _source(src_frames)
    bus = ingest.IngestionBus(providers=[src], now=lambda: NOW,
                              account=watch)
    msg = bus.poll()
    assert msg["session_pct"] == 62.8
    assert msg["weekly_pct"] == -1.0
