"""Scratch checks for C23 (verifier v8): U11 (same person, other org, the
old org's /usage kept in the config) plus a clock step back."""
import pytest
from tests.pc.test_spend_page import (Clock, NOW, ORG_B, bus_on, config,
                                      render)


def _show(bus, label):
    p = bus.plan_msg()
    page = bus.claude_page()
    print(label, {k: p.get(k) for k in ("plan", "usd", "lim", "hint")},
          "exact=", getattr(page, "spend_exact", None))
    return p, page


@pytest.mark.parametrize("step", [600, 3600])
def test_u11_then_clock_back(tmp_path, step):
    clock = Clock(NOW)             # the clock reads `step` seconds fast
    config(tmp_path, NOW - 50, used=30000, limit=50000, fetched=NOW - 60)
    bus = bus_on(clock)
    bus.poll()
    _show(bus, "\n[%d] org A:" % step)
    t = NOW + 100
    # /login to org B, same accountUuid; the old org's /usage kept (U11).
    config(tmp_path, t, used=30000, limit=50000, fetched=NOW - 60, org=ORG_B)
    clock.t = t + 1
    bus.poll()
    _show(bus, "[%d] org B before step:" % step)
    # NTP puts the clock right: back by `step`.
    clock.t = t + 1 - step
    for k in range(3):
        clock.t += 2
        bus.poll()
    _show(bus, "[%d] org B just after step:" % step)
    # Real time runs on; nobody runs /usage in org B.
    for at in (NOW - 960, NOW - 900, NOW - 60, NOW + 400, NOW + 1200):
        if at <= clock.t:
            continue
        clock.t = at
        bus.poll()
        _show(bus, "[%d] org B at NOW%+d:" % (step, at - NOW))
