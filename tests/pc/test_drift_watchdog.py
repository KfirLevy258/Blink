"""The watchdog restores a hook that was wiped, and never one that was removed.

That distinction is the whole feature. Anything can rewrite settings.json and
drop our command silently -- the symptom is a panel that stops updating while
the daemon reports success. But a user who ran `blink uninstall` has said
something, and a program that puts its hook back after being told to go away
is not self-healing, it is malware-shaped.
"""
import json
import os

import pytest

from pc import install_statusline as isl

SHIM = "/opt/blink/blink-statusline.sh"


@pytest.fixture
def settings(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"model": "opus"}))
    return str(p)


def _current(settings):
    return (json.loads(open(settings).read()).get("statusLine") or {}).get(
        "command", "")


def test_never_installed_means_hands_off(settings):
    """No marker, no business touching anyone's settings."""
    assert isl.drift_check(settings, SHIM) is None
    assert _current(settings) == ""


def test_a_wiped_hook_is_restored(settings):
    isl.install(settings, SHIM)
    data = json.loads(open(settings).read())
    del data["statusLine"]
    open(settings, "w").write(json.dumps(data))

    msg = isl.drift_check(settings, SHIM)
    assert "restored" in msg
    assert _current(settings) == isl.statusline_command(SHIM)


def test_an_intact_hook_is_left_alone(settings):
    isl.install(settings, SHIM)
    before = open(settings).read()
    assert isl.drift_check(settings, SHIM) is None
    assert open(settings).read() == before


def test_an_uninstalled_hook_is_never_put_back(settings):
    """uninstall() removes the marker, and that is what says 'intent'."""
    isl.install(settings, SHIM)
    isl.uninstall(settings, SHIM)
    assert isl.drift_check(settings, SHIM) is None
    assert _current(settings) == ""


def test_a_replacement_is_chained_not_clobbered(settings):
    """A user who set their own statusline after we installed keeps it."""
    isl.install(settings, SHIM)
    data = json.loads(open(settings).read())
    data["statusLine"] = {"type": "command", "command": "my-own-bar"}
    open(settings, "w").write(json.dumps(data))

    msg = isl.drift_check(settings, SHIM)
    assert "chained" in msg
    assert _current(settings) == isl.statusline_command(SHIM)
    chain = open(os.path.expanduser(isl.CHAIN_PATH)).read().strip()
    assert chain == "my-own-bar"


def test_a_moved_shim_is_repointed(settings):
    isl.install(settings, SHIM)
    moved = "/opt/blink-v2/blink-statusline.sh"
    msg = isl.drift_check(settings, moved)
    assert "old shim path" in msg
    assert _current(settings) == isl.statusline_command(moved)


def test_an_unparseable_settings_file_is_left_strictly_alone(settings):
    """Usually a file someone is halfway through editing."""
    isl.install(settings, SHIM)
    open(settings, "w").write("{ this is not json")
    assert isl.drift_check(settings, SHIM) is None
    assert open(settings).read() == "{ this is not json"


def test_the_escape_hatch_disables_it(settings, monkeypatch):
    isl.install(settings, SHIM)
    data = json.loads(open(settings).read())
    del data["statusLine"]
    open(settings, "w").write(json.dumps(data))

    monkeypatch.setenv(isl.WATCHDOG_DISABLE_ENV, "1")
    assert isl.drift_check(settings, SHIM) is None


# --- the interval and the give-up cap -------------------------------------


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_it_does_not_check_more_often_than_its_interval():
    clock, calls = _Clock(), []
    w = isl.DriftWatchdog("s", "p", interval_s=300.0, now=clock,
                          check=lambda *a: calls.append(1) or None)
    w.tick()
    w.tick()
    w.tick()
    assert len(calls) == 1
    clock.t = 301.0
    w.tick()
    assert len(calls) == 2


def test_it_stops_insisting_after_a_few_rounds():
    """Something on this machine that keeps removing the hook wins. A write
    fight with another program is worse than a hook that stays missing."""
    clock = _Clock()
    w = isl.DriftWatchdog("s", "p", interval_s=1.0, now=clock,
                          check=lambda *a: "restored it")
    seen = []
    for i in range(10):
        clock.t = i * 2.0
        m = w.tick()
        if m:
            seen.append(m)
    assert len(seen) == isl.MAX_REINSTATEMENTS
    assert "will stop putting it back" in seen[-1]


def test_a_quiet_machine_never_logs_anything():
    clock = _Clock()
    w = isl.DriftWatchdog("s", "p", interval_s=1.0, now=clock,
                          check=lambda *a: None)
    for i in range(10):
        clock.t = i * 2.0
        assert w.tick() is None


# --- the shim content check rides the same interval, not the same cap ------


def test_both_repairs_are_reported_on_one_tick(monkeypatch):
    monkeypatch.setattr(isl, "shim_content_check",
                        lambda shims: "blink-hook.sh was out of date")
    w = isl.DriftWatchdog("s", "p", interval_s=1.0, now=_Clock(),
                          check=lambda *a: "restored it",
                          shims=(("p", "blink-hook.sh"),))

    assert w.tick() == "restored it; blink-hook.sh was out of date"


def test_the_shim_check_waits_for_the_interval_like_everything_else(monkeypatch):
    """It reads every installed shim off disk; it must not run per loop turn."""
    clock, calls = _Clock(), []
    monkeypatch.setattr(isl, "shim_content_check",
                        lambda shims: calls.append(1) and None)
    w = isl.DriftWatchdog("s", "p", interval_s=300.0, now=clock,
                          check=lambda *a: None,
                          shims=(("p", "blink-hook.sh"),))
    w.tick()
    w.tick()
    w.tick()
    assert len(calls) == 1
    clock.t = 301.0
    w.tick()
    assert len(calls) == 2


def test_a_stale_shim_is_still_repaired_after_it_gave_up(monkeypatch):
    """The cap is about a write fight over settings.json, which a shim is not.

    Nothing else on the machine writes our shims, so giving up on the
    statusLine hook must not leave them stale forever.
    """
    monkeypatch.setattr(isl, "shim_content_check",
                        lambda shims: "blink-hook.sh was out of date")
    clock = _Clock()
    w = isl.DriftWatchdog("s", "p", interval_s=1.0, now=clock,
                          check=lambda *a: "restored it",
                          shims=(("p", "blink-hook.sh"),))
    for i in range(10):
        clock.t = i * 2.0
        m = w.tick()

    assert m == "blink-hook.sh was out of date"
    # And the shim rewrites never counted towards the cap on their way there.
    assert w._reinstatements == isl.MAX_REINSTATEMENTS
