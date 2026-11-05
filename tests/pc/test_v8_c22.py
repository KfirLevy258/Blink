"""Scratch checks for C22 (verifier v8). Run with -p tests.conftest."""
from tests.pc.test_spend_page import (Clock, NOW, WINDOWS, bus_on, config,
                                      max_config, render)


def _ages(bus, clock, offsets):
    out = []
    for off in offsets:
        clock.t = NOW + off
        msg = bus.poll()
        page = bus.claude_page()
        out.append((off, None if msg is None else msg.get("active_age_s"),
                    None if page is None else page.src,
                    None if page is None else page.active_at - NOW))
    return out


def test_spend_seat_future_render_no_new_render(tmp_path):
    clock = Clock(NOW)
    config(tmp_path, NOW - 600, fetched=NOW - 600)
    render(tmp_path, NOW + 5 * 3600, 1.00)      # dated by a fast clock
    bus = bus_on(clock)
    bus.poll()
    got = _ages(bus, clock, (60, 600, 3600, 4 * 3600, 5 * 3600 + 60,
                             5 * 3600 + 3600))
    print("\nspend seat, no rate_limits:", got)
    prov = [p for p in bus._providers if p.__class__.__name__ == "ClaudeCliProvider"][0]
    print("render_at - NOW:", prov._render_at - NOW)


def test_spend_seat_future_render_with_windows(tmp_path):
    clock = Clock(NOW)
    config(tmp_path, NOW - 600, fetched=NOW - 600)
    render(tmp_path, NOW + 5 * 3600, 1.00, rate_limits=WINDOWS)
    bus = bus_on(clock)
    bus.poll()
    got = _ages(bus, clock, (60, 600, 3600, 4 * 3600))
    print("\nspend seat, windows in render:", got)


def test_max_desk_future_render(tmp_path):
    clock = Clock(NOW)
    max_config(tmp_path, NOW - 600)
    render(tmp_path, NOW + 5 * 3600, 1.00, rate_limits=WINDOWS)
    bus = bus_on(clock)
    bus.poll()
    got = _ages(bus, clock, (60, 600, 3600, 4 * 3600))
    print("\nmax desk:", got)


def test_max_desk_render_now_baseline(tmp_path):
    clock = Clock(NOW)
    max_config(tmp_path, NOW - 600)
    render(tmp_path, NOW, 1.00, rate_limits=WINDOWS)
    bus = bus_on(clock)
    bus.poll()
    got = _ages(bus, clock, (60, 600, 3600, 4 * 3600))
    print("\nmax desk baseline (render at NOW):", got)


def test_spend_seat_render_now_baseline(tmp_path):
    clock = Clock(NOW)
    config(tmp_path, NOW - 600, fetched=NOW - 600)
    render(tmp_path, NOW, 1.00)
    bus = bus_on(clock)
    bus.poll()
    got = _ages(bus, clock, (60, 600, 3600, 4 * 3600))
    print("\nspend seat baseline (render at NOW):", got)
