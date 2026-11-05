"""Scratch checks for C24 (verifier v8): the login watch's `since` moved to
now by a clock step back, and an old-login reading dated in the fast
period: the saved /usage windows frame (claude_cli._usage_frame), which
no source-side guard covers, under U11 (same person, other org, cache
kept)."""
import datetime
import json
import os

import pytest
from pc import claude_plan
from tests.pc.test_spend_page import (ACCT, ORG, ORG_B, Clock, NOW, WINDOWS,
                                      bus_on, render)
from tests.support.home import inside


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.timezone.utc).isoformat()


def write_cfg(home, at, *, org, spend, fetched):
    oauth = {"accountUuid": ACCT, "organizationUuid": org}
    if spend:
        oauth.update(organizationType="claude_enterprise",
                     seatTier="enterprise_usage_based")
    else:
        oauth.update(organizationType="claude_max", seatTier="max",
                     billingType="subscription")
    doc = {"oauthAccount": oauth, "projects": {"/srv/demo": {}},
           "cachedUsageUtilization": {
               "fetchedAtMs": int(fetched * 1000), "accountUuid": ACCT,
               "utilization": {
                   "five_hour": {"utilization": 77.0,
                                 "resets_at": iso(NOW + 3 * 3600)},
                   "seven_day": {"utilization": 66.0,
                                 "resets_at": iso(NOW + 3 * 86400)},
                   "extra_usage": {"is_enabled": True, "monthly_limit": 25000,
                                   "used_credits": 1234, "utilization": 4.9,
                                   "currency": "usd"}}}}
    path = inside(home, claude_plan.config_path())
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    os.utime(path, (at, at))


def _show(bus, msg, label):
    page = bus.claude_page()
    print(label, None if msg is None else
          {k: msg.get(k) for k in ("session_pct", "weekly_pct", "src")},
          "page.src=", None if page is None else page.src,
          "plan=", bus.plan_msg().get("plan") if bus.plan_msg() else None)


@pytest.mark.parametrize("step", [3600])
def test_old_org_cache_windows_after_step(tmp_path, step):
    clock = Clock(NOW)                       # `step` seconds fast
    write_cfg(tmp_path, NOW - 50, org=ORG, spend=True, fetched=NOW - 60)
    render(tmp_path, NOW - 40, 1.00)
    bus = bus_on(clock)
    _show(bus, bus.poll(), "\nA spend:")
    t = NOW + 100
    write_cfg(tmp_path, t, org=ORG_B, spend=False, fetched=NOW - 60)
    msg = None
    clock.t = t + 1
    for _ in range(4):
        clock.t += 2
        msg = bus.poll()
    _show(bus, msg, "B max, before step:")
    clock.t = clock.t - step
    for _ in range(3):
        clock.t += 2
        msg = bus.poll()
    _show(bus, msg, "B max, just after step:")
    # B renders its own status line on the corrected clock.
    render(tmp_path, clock.t + 1, 0.10, rate_limits=WINDOWS)
    for _ in range(3):
        clock.t += 2
        msg = bus.poll()
    _show(bus, msg, "B max, own render (41/12) after step:")
    clock.t += 1800
    render(tmp_path, clock.t - 1, 0.20, rate_limits=WINDOWS)
    msg = bus.poll()
    _show(bus, msg, "B max, 30 min later, render again:")
