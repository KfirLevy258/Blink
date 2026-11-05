"""Scratch check for C21 (verifier v8), end to end through the bus."""
from tests.pc.test_spend_page import Clock, NOW, bus_on, config, render


def test_rises_from_the_fast_period_counted_over_a_later_usage(tmp_path):
    D = 1800                                   # the clock runs 30 min fast
    # A /usage long before, on the fast clock: $10.00 used.
    clock = Clock(NOW - 3000 + D)
    config(tmp_path, NOW - 3000 + D, used=1000, fetched=NOW - 3000 + D)
    bus = bus_on(clock)
    bus.poll()
    # Real NOW-1200 and NOW-600, dated by the fast clock.
    for real, cost in ((NOW - 1200, 1.00), (NOW - 600, 1.30)):
        render(tmp_path, real + D, cost)
        clock.t = real + D + 1
        bus.poll()
    print("\nfast period:", bus.plan_msg())
    # NTP puts the clock right at real NOW.
    clock.t = NOW + 2
    bus.poll()
    # /usage at real NOW+300 holds every cent so far: the 30 cents too,
    # spent at real NOW-600. 1000 + 30 = 1030 cents (the first sighting's
    # $1.00 was never counted, so take the /usage as 10.30 used).
    config(tmp_path, NOW + 300, used=1030, fetched=NOW + 300)
    for at in (NOW + 310, NOW + 400, NOW + 900, NOW + 3600, NOW + 6 * 3600):
        clock.t = at
        bus.poll()
        p = bus.plan_msg()
        print("at NOW%+d:" % (at - NOW), p.get("usd"), "exact=",
              bus.claude_page().spend_exact)
