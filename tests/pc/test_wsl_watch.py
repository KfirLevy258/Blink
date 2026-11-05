"""WslWatch: a WSL home read off the serial loop, only while it is in use.

The 2 s poll reads only the Windows drop folder; Claude Code's config, the
transcripts and the drift check are read from the distribution only while
the drop folder changed in the last ten minutes and the distribution is
running, on a worker thread (docs/plans/enterprise-spend.md rev 3, section
4, "Reading").

No real wsl.exe is started and no real UNC path is touched: `tmp_path/unc`
stands in for \\\\?\\UNC\\wsl.localhost\\<name>, and every os.stat,
os.lstat, os.scandir, os.listdir, os.open and builtin open() under it is
counted, so "no UNC access" is measured rather than assumed.
"""
import builtins
import dataclasses
import io
import json
import os
import threading
import time

import pytest

from pc import (burn_reader, claude_home, claude_plan, cli, ingest,
                install_statusline, wsl_watch)

GUID = "{5C3D2B1A-0000-4000-8000-00000000ABCD}"
SECRET = "do-not-keep-this-project-history"


def _home(tmp_path):
    """A WSL home whose UNC side is tmp_path/unc. config_dir_set, so the
    config is <config_dir>\\.claude.json -- on POSIX a file whose name
    holds a backslash, on Windows the real join; either way the path
    claude_plan.config_path gives, which is what the watch reads."""
    unc = tmp_path / "unc"
    unc.mkdir()
    h = claude_home.wsl(distro_guid=GUID, distro_name="Ubuntu",
                        linux_user="zoe", linux_home="/home/zoe",
                        config_dir=str(unc / "claude"), config_dir_set=True)
    h = dataclasses.replace(h, transcripts=str(unc / "projects"))
    os.makedirs(h.transcripts)
    # The config folder exists in a real distribution. On Windows the join
    # is a real one, unc\claude\.claude.json, and without the folder every
    # _write_config raised FileNotFoundError (thirteen tests on the Windows
    # VM, 2026-11-05). On POSIX it is unused, the config's name holding the
    # backslash.
    os.makedirs(h.config_dir)
    return h, str(unc)


def _write_config(home, seat=False, extra=None):
    doc = {"oauthAccount": {"accountUuid": "acct-1",
                            "organizationUuid": "org-1",
                            "organizationType": "claude_enterprise"
                            if seat else "claude_max",
                            "billingType": "usage_based" if seat
                            else "stripe_subscription"},
           "projects": {"/home/zoe/secret": {"history": [SECRET]}}}
    doc.update(extra or {})
    path = claude_plan.config_path(home)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(doc, f)
    return path


def _touch_drop(home, at):
    os.makedirs(home.drop_dir, exist_ok=True)
    p = os.path.join(home.drop_dir, "statusline.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{}")
    os.utime(p, (at, at))


class _Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


class _Running:
    def __init__(self, answer=True):
        self.answer = answer
        self.calls = []

    def __call__(self, name):
        self.calls.append(name)
        return self.answer


class _Drift:
    def __init__(self):
        self.ticks = 0

    def tick(self):
        self.ticks += 1
        return None


def _count_unc(monkeypatch, root):
    """Every filesystem call under `root`, by name: the os calls and
    builtin open()."""
    hits = []
    for name in ("stat", "lstat", "scandir", "listdir", "open"):
        real = getattr(os, name)

        def wrap(*a, _real=real, _name=name, **k):
            p = a[0] if a else k.get("path")
            if isinstance(p, (str, bytes, os.PathLike)) and \
                    os.fsdecode(p).startswith(root):
                hits.append(_name)
            return _real(*a, **k)
        monkeypatch.setattr(os, name, wrap)
    real_open = builtins.open

    def wrap_open(file, *a, **k):
        if isinstance(file, (str, bytes, os.PathLike)) and \
                os.fsdecode(file).startswith(root):
            hits.append("builtins.open")
        return real_open(file, *a, **k)
    # io.open is the same function under another name, and patching one
    # name does not change the other, so both are patched.
    monkeypatch.setattr(builtins, "open", wrap_open)
    monkeypatch.setattr(io, "open", wrap_open)
    return hits


def _watch(home, clock, running, drift=None, **kw):
    return wsl_watch.WslWatch(home, running=running, clock=clock,
                              drift=drift or _Drift(), **kw)


def test_no_unc_access_when_not_live(tmp_path, monkeypatch):
    home, unc = _home(tmp_path)
    _write_config(home)
    t0 = time.time()
    clock = _Clock(t0)

    # The drop folder is stale: the user is not using Claude Code.
    _touch_drop(home, t0 - wsl_watch.LIVE_S - 1)
    running = _Running(True)
    drift = _Drift()
    w = _watch(home, clock, running, drift)
    hits = _count_unc(monkeypatch, unc)
    for _ in range(5):
        w.tick()
        clock.t += 20
    assert hits == []
    assert drift.ticks == 0
    assert w.view() is None

    # Fresh drop folder, distribution stopped.
    _touch_drop(home, clock.t)
    running.answer = False
    for _ in range(5):
        w.tick()
        clock.t += 1
    assert hits == []
    assert drift.ticks == 0
    assert w.view() is None

    # The control: live, and the counter does see the reads.
    running.answer = True
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.tick()
    assert hits
    assert drift.ticks == 1
    assert w.view() is not None


def test_running_checked_at_most_every_15s(tmp_path):
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    running = _Running(True)
    w = _watch(home, clock, running)
    for dt in (0, 5, 10, 14.9):
        clock.t = t0 + dt
        _touch_drop(home, clock.t)
        w.tick()
    assert len(running.calls) == 1
    clock.t = t0 + 15
    _touch_drop(home, clock.t)
    w.tick()
    assert len(running.calls) == 2
    # A clock gone back is asked again, not trusted for "15 s".
    clock.t = t0 + 2
    _touch_drop(home, clock.t)
    w.tick()
    assert len(running.calls) == 3
    assert running.calls == ["Ubuntu"] * 3


def test_idle_desk_asks_only_every_300s_and_never_reads(tmp_path,
                                                       monkeypatch):
    """A drop folder ten minutes stale -- or never written -- means no UNC
    read and no config or transcript read. wsl.exe is started only to ask
    `wsl -l --running`, which starts nothing, at most once every
    IDLE_REPAIR_EVERY_S, and the first not until one has passed."""
    home, unc = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    running = _Running(False)
    drift = _Drift()
    w = _watch(home, clock, running, drift)
    hits = _count_unc(monkeypatch, unc)
    for _ in range(int(wsl_watch.IDLE_REPAIR_EVERY_S // wsl_watch.TICK_S)):
        w.tick()                    # no drop folder at all
        clock.t += wsl_watch.TICK_S
    assert running.calls == []
    _touch_drop(home, clock.t - wsl_watch.LIVE_S - 1)
    for _ in range(20):
        w.tick()
        clock.t += 60
    # 1200 s of ticks: four asks, and a stopped distribution is never
    # entered -- the drift check is what would start it.
    assert running.calls == ["Ubuntu"] * 4
    assert drift.ticks == 0
    assert hits == []
    assert w.view() is None and not w.live()


def test_idle_running_distro_gets_the_drift_repair(tmp_path, monkeypatch):
    """U30: a shim or the out file deleted inside WSL while nobody used
    Claude Code there stops the drop folder ever getting fresh again, so a
    repair gated on "fresh" never came: a missing hook shim makes dash exit
    2 and Claude Code block every prompt, until a reinstall. Natively the
    watchdog restores it within 300 s; now so does this, when (and only
    when) the distribution is running anyway. Still no config read."""
    home, unc = _home(tmp_path)
    _write_config(home)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0 - wsl_watch.LIVE_S - 1)
    running = _Running(True)
    drift = _Drift()
    w = _watch(home, clock, running, drift)
    hits = _count_unc(monkeypatch, unc)
    w.tick()
    assert drift.ticks == 0 and running.calls == []
    clock.t += wsl_watch.IDLE_REPAIR_EVERY_S
    w.tick()
    assert running.calls == ["Ubuntu"] and drift.ticks == 1
    for _ in range(10):
        clock.t += wsl_watch.TICK_S
        w.tick()
    assert drift.ticks == 1 and len(running.calls) == 1
    assert hits == [] and w.view() is None and not w.live()
    # Stopped by the next ask: not entered.
    running.answer = False
    clock.t += wsl_watch.IDLE_REPAIR_EVERY_S
    w.tick()
    assert drift.ticks == 1 and len(running.calls) == 2


def test_live_reads_the_state_folder_too(tmp_path):
    """A hook firing writes state/, not statusline.json: that is use too."""
    home, _ = _home(tmp_path)
    _write_config(home)
    t0 = time.time()
    clock = _Clock(t0)
    os.makedirs(home.state_dir)
    st = os.path.join(home.state_dir, "s1.state")
    with open(st, "w") as f:
        f.write("x")
    os.utime(st, (t0, t0))
    os.utime(home.state_dir, (t0, t0))
    os.utime(home.drop_dir, (t0 - 3600, t0 - 3600))
    w = _watch(home, clock, _Running(True))
    w.tick()
    assert w.view() is not None


def test_stop_then_resume(tmp_path, monkeypatch):
    """`wsl --shutdown` with the daemon running, then WSL opened again:
    reads stop without a restart, and resume (wsl plan, Task 7 item 2)."""
    home, unc = _home(tmp_path)
    _write_config(home, seat=False)
    t0 = time.time()
    clock = _Clock(t0)
    running = _Running(True)
    w = _watch(home, clock, running)
    _touch_drop(home, clock.t)
    w.tick()
    first = w.view()
    assert first is not None and not first.spend_seat

    running.answer = False                      # wsl --shutdown
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    _write_config(home, seat=True)              # changed while away
    hits = _count_unc(monkeypatch, unc)
    for _ in range(4):
        w.tick()
        clock.t += 5
    assert hits == []
    assert w.view() == first                    # the last good view kept

    running.answer = True                       # opened again
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.tick()
    assert hits
    assert w.view().spend_seat


def test_readers_never_block(tmp_path):
    """A tick held for 5 s inside wsl.exe: the serial loop's readers
    answer at once, from memory."""
    home, _ = _home(tmp_path)
    _write_config(home)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    entered, release = threading.Event(), threading.Event()

    def slow_running(name):
        entered.set()
        release.wait(5)
        return True

    w = _watch(home, clock, slow_running)
    burn = w.burn_source()
    t = threading.Thread(target=w.tick, daemon=True)
    t.start()
    try:
        assert entered.wait(5)
        began = time.monotonic()
        assert w.view() is None
        assert burn.poll(clock.t) is None
        assert w.burn_snapshot() is None
        assert time.monotonic() - began < 0.5
    finally:
        release.set()
        t.join(10)
    assert w.view() is not None


def test_watch_keeps_no_document(tmp_path):
    home, _ = _home(tmp_path)
    _write_config(home, seat=True)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    w = _watch(home, clock, _Running(True))
    w.tick()
    assert isinstance(w.view(), claude_plan.ConfigView)

    seen = set()

    def walk(o, depth=0):
        if id(o) in seen or depth > 8:
            return
        seen.add(id(o))
        if isinstance(o, (str, bytes)):
            assert SECRET not in (o if isinstance(o, str)
                                  else o.decode("utf-8", "replace"))
            assert "oauthAccount" not in (o if isinstance(o, str) else "")
            return
        if isinstance(o, dict):
            assert "oauthAccount" not in o and "projects" not in o
            for k, v in o.items():
                walk(k, depth + 1)
                walk(v, depth + 1)
            return
        if isinstance(o, (list, tuple, set, frozenset)):
            for v in o:
                walk(v, depth + 1)
            return
        if dataclasses.is_dataclass(o) and not isinstance(o, type):
            for f in dataclasses.fields(o):
                walk(getattr(o, f.name), depth + 1)
            return
        d = getattr(o, "__dict__", None)
        if isinstance(d, dict) and not callable(o):
            for v in d.values():
                walk(v, depth + 1)

    walk(vars(w))


def test_burn_read_only_when_asked_and_live(tmp_path):
    """The transcripts are read only when the bus asks for a burn line (a
    page with no session window), and only while live. Pruning reads
    nothing and goes on regardless (README "Privacy")."""
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)

    class Reader:
        def __init__(self):
            self.polls = []

        def poll(self, now, budget=None):
            self.polls.append(now)
            return 7, [1] * 20

        def held(self, now):
            return (8, [0] * 20) if self.polls else None

    r = Reader()
    running = _Running(True)
    w = _watch(home, clock, running, burn_reader=r)
    src = w.burn_source()
    w.tick()
    assert r.polls == []                       # never asked: not read
    assert src.poll(clock.t) is None           # asked now; nothing yet
    w.tick()
    assert r.polls == []        # the running check is 0 s old: no UNC
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.tick()
    assert r.polls == [clock.t]
    assert src.poll(clock.t) == (7, [1] * 20)

    running.answer = False
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    src.poll(clock.t)
    w.tick()
    assert len(r.polls) == 1                   # not live: not read
    assert src.poll(clock.t) == (8, [0] * 20)  # from memory, slid on

    # Asked once, then not again for longer than WANT_TTL_S: not read.
    running.answer = True
    clock.t += wsl_watch.WANT_TTL_S + 1
    _touch_drop(home, clock.t)
    w.tick()
    assert len(r.polls) == 1


def test_the_real_burn_reader_reads_the_wsl_transcripts(tmp_path):
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    proj = os.path.join(home.transcripts, "p")
    os.makedirs(proj)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t0))
    line = {"type": "assistant", "timestamp": stamp, "requestId": "r1",
            "message": {"id": "m1", "model": "claude-x",
                        "usage": {"output_tokens": 42}}}
    with open(os.path.join(proj, "s.jsonl"), "w") as f:
        f.write(json.dumps(line) + "\n")
    w = _watch(home, clock, _Running(True))
    src = w.burn_source()
    src.poll(clock.t)
    w.tick()
    got = src.poll(clock.t)
    assert got is not None
    assert sum(got[1]) == 42


def test_default_drift_goes_through_wsl_run_and_skips_the_shim_check(
        tmp_path, monkeypatch):
    """The WSL drift check runs in the watch's tick, through wsl_run, and
    never the shim-content check, which would write shims through UNC
    paths (Task 4's note)."""
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    install_statusline._write_marker(
        install_statusline.wsl_statusline_command(home), home)
    calls = []

    def run(argv, stdin=None):
        calls.append(list(argv))
        return 1, b"", b""

    def no_shim_check(*a, **k):
        raise AssertionError("shim_content_check ran for a WSL home")
    monkeypatch.setattr(install_statusline, "shim_content_check",
                        no_shim_check)
    w = wsl_watch.WslWatch(home, running=_Running(True), clock=clock,
                           wsl_run=run)
    w.tick()
    assert calls                                # read through wsl_run


def test_bus_reads_the_watch_not_the_unc_path(tmp_path, monkeypatch):
    home, unc = _home(tmp_path)
    _write_config(home, seat=True)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    w = _watch(home, clock, _Running(True))
    monkeypatch.setattr(claude_home, "current", lambda: home)
    bus = ingest.IngestionBus(plan_source=w.view,
                              burn={"claude": w.burn_source()})
    hits = _count_unc(monkeypatch, unc)
    assert bus._plan_reader.look(t0) == claude_plan.EMPTY_VIEW
    assert hits == []
    w.tick()
    hits.clear()
    assert bus._plan_reader.look(t0 + 60).spend_seat
    bus.prune_burn()
    assert hits == []


def test_native_home_builds_no_watch(tmp_path, monkeypatch):
    import claude_usage_bridge as cub
    built = []

    class Recorder:
        def __init__(self, home, **kw):
            built.append(home)
            self.started = False

        def start(self):
            self.started = True

        def view(self):
            return None

        def burn_source(self):
            return object()

    monkeypatch.setattr(wsl_watch, "WslWatch", Recorder)
    monkeypatch.delenv("BLINK_SCENARIO", raising=False)
    native = claude_home.native()
    monkeypatch.setattr(claude_home, "current", lambda: native)
    bus = cub.build_bus()
    assert built == []
    assert isinstance(bus._burn["claude"], burn_reader.BurnReader)
    assert bus._plan_reader._source is None
    assert isinstance(cli.drift_watchdog(native),
                      install_statusline.DriftWatchdog)

    # The control: a WSL home builds one, starts it, and the bus reads it.
    home, _ = _home(tmp_path)
    monkeypatch.setattr(claude_home, "current", lambda: home)
    bus = cub.build_bus()
    assert built == [home]
    assert bus._plan_reader._source is not None
    assert not isinstance(bus._burn["claude"], burn_reader.BurnReader)
    assert isinstance(bus._burn["codex"], burn_reader.CodexBurnReader)
    # And no DriftWatchdog for it on the main thread.
    dog = cli.drift_watchdog(home)
    assert not isinstance(dog, install_statusline.DriftWatchdog)
    assert dog.tick() is None


def test_a_failing_tick_is_said_once_by_kind(tmp_path, capsys):
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)

    def boom(name):
        raise RuntimeError(home.config_dir)

    w = _watch(home, clock, boom)
    for _ in range(3):
        w._tick_once()
        clock.t += wsl_watch.RUNNING_EVERY_S
    err = capsys.readouterr().err
    assert err.count("RuntimeError") == 1
    assert home.config_dir not in err and "unc" not in err


def test_a_new_view_survives_a_tick_that_raises_after_it(tmp_path):
    """_look() takes the file's signature as read; if the burn poll (or the
    drift check) then raised and the view was not yet published, the new
    view was lost until the file next changed (review of Task 5)."""
    home, _ = _home(tmp_path)
    _write_config(home, seat=False)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)

    class Boom:
        def poll(self, now, budget=None):
            raise OSError("transcripts")

        def held(self, now):
            return None

    w = _watch(home, clock, _Running(True), burn_reader=Boom())
    w.burn_source().poll(clock.t)               # wanted: poll will raise
    with pytest.raises(OSError):
        w.tick()
    assert w.view() is not None and not w.view().spend_seat

    _write_config(home, seat=True)
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.burn_source().poll(clock.t)
    with pytest.raises(OSError):
        w.tick()
    assert w.view().spend_seat


# --- the final branch review --------------------------------------------

def test_a_future_drop_file_is_not_fresh(tmp_path, monkeypatch):
    """Finding 6: a payload stamped in the future -- a clock set wrong, a
    copy from elsewhere -- does not keep the distribution being read; a
    few seconds of skew between Linux's clock and Windows' still counts."""
    home, unc = _home(tmp_path)
    _write_config(home)
    clock = _Clock(time.time())
    running = _Running(True)
    w = _watch(home, clock, running)
    _touch_drop(home, clock.t + 3600)
    w.tick()
    assert running.calls == [] and not w.live()
    _touch_drop(home, clock.t + wsl_watch.FUTURE_SKEW_S)
    w.tick()
    assert running.calls == ["Ubuntu"] and w.live()


def test_drift_runs_before_the_burn_poll(tmp_path):
    """Finding 8: the drift check starts wsl.exe -d, which starts a
    stopped distribution; it runs while the running answer is fresh, not
    after a burn poll that may take a long while."""
    home, _ = _home(tmp_path)
    clock = _Clock(time.time())
    _touch_drop(home, clock.t)
    order = []

    class Drift:
        def tick(self):
            order.append("drift")

    class Reader:
        def poll(self, now, budget=None):
            order.append("poll")
            return None

        def held(self, now):
            return None
    w = _watch(home, clock, _Running(True), drift=Drift(),
               burn_reader=Reader())
    w.burn_source().poll(clock.t)
    w.tick()
    assert order == ["drift", "poll"]


def _unc_home(name="Ubuntu"):
    from pc import wsl
    return claude_home.wsl(distro_guid=GUID, distro_name=name,
                           linux_user="zoe", linux_home="/home/zoe",
                           config_dir=wsl.unc(name, "/home/zoe/.claude"),
                           config_dir_set=False)


def test_a_renamed_distro_is_read_under_its_new_name(monkeypatch):
    """Finding 7: found by its GUID under another name, the watch reads
    the config, transcripts and drift check under the name it has now.
    Nothing is read until resolve_name(), which start()'s thread asks."""
    from pc import wsl
    asked = []

    def distros():
        asked.append(1)
        return [wsl.Distro(GUID.lower(), "Work", 2, True)]
    built = []
    real = install_statusline.wsl_drift_watchdog
    monkeypatch.setattr(install_statusline, "wsl_drift_watchdog",
                        lambda h, run=None: built.append(h) or real(h, run))
    w = wsl_watch.WslWatch(_unc_home(), running=_Running(True),
                           distros=distros)
    assert asked == []
    w.resolve_name()
    assert asked == [1]
    assert w._home.distro_name == "Work"
    assert w._home.config_dir == wsl.unc("Work", "/home/zoe/.claude")
    assert w._home.drop_dir == _unc_home().drop_dir
    assert "\\Work\\" in w._reader.root()
    assert built[-1].distro_name == "Work"
    assert "\\Work\\" in claude_plan.config_path(w._home)


def test_resolve_name_leaves_the_same_name_and_a_missing_distro():
    from pc import wsl
    home = _unc_home()
    for found in ([wsl.Distro(GUID, "Ubuntu", 2, True)], [], None):
        w = wsl_watch.WslWatch(home, running=_Running(True),
                               distros=lambda found=found: found,
                               drift=_Drift())
        reader = w._reader
        w.resolve_name()
        assert w._home is home and w._reader is reader


def test_a_native_bus_stays_native_when_the_record_turns_wsl(tmp_path,
                                                             monkeypatch):
    """Finding 5: the bus is built once per process. A native one whose
    readers asked claude_home.current() on every poll moved onto a WSL
    record the moment `install --wsl` saved it -- before the restart, or
    for good when the restart was refused -- and the serial loop then
    stat'ed UNC paths. Its readers ask claude_home.native() instead."""
    import claude_usage_bridge as cub
    from pc import wsl
    monkeypatch.delenv("BLINK_SCENARIO", raising=False)
    bus = cub.build_bus()
    native = claude_home.native()
    claude_home.save(claude_home.wsl(
        GUID, "Ubuntu", "zoe", "/home/zoe",
        wsl.unc("Ubuntu", "/home/zoe/.claude"), False))
    assert claude_home.current().kind == claude_home.WSL
    from pc.providers.claude_cli import ClaudeCliProvider
    from pc.providers.claude_state import ClaudeStateProvider
    paths = [bus._plan_reader.path(), bus._burn["claude"].root()]
    for p in bus._providers:
        if isinstance(p, ClaudeCliProvider):
            paths.append(p.path())
        if isinstance(p, ClaudeStateProvider):
            paths.append(p._dir)
    assert len(paths) == 4
    assert not any("wsl" in p.lower() for p in paths), paths
    assert paths[1] == native.transcripts
    assert paths[2] == os.path.join(native.drop_dir, "statusline.json")
    assert paths[3] == native.state_dir


def test_a_wsl_bus_keeps_its_own_drop_folder(tmp_path, monkeypatch):
    """The same for a WSL-built bus: `install --wsl Debian` over Ubuntu,
    with the restart refused, does not move its payload reads onto
    Debian's drop folder while its watch still reads Ubuntu."""
    import claude_usage_bridge as cub
    from pc import wsl
    from pc.providers.claude_cli import ClaudeCliProvider
    monkeypatch.delenv("BLINK_SCENARIO", raising=False)
    monkeypatch.setattr(wsl_watch.WslWatch, "start", lambda self: None)
    first = claude_home.wsl(GUID, "Ubuntu", "zoe", "/home/zoe",
                            wsl.unc("Ubuntu", "/home/zoe/.claude"), False)
    claude_home.save(first)
    bus = cub.build_bus()
    claude_home.save(claude_home.wsl(
        "{11111111-2222-4333-8444-555555555555}", "Debian", "zoe",
        "/home/zoe", wsl.unc("Debian", "/home/zoe/.claude"), False))
    cli_p, = [p for p in bus._providers if isinstance(p, ClaudeCliProvider)]
    assert cli_p.path() == os.path.join(first.drop_dir, "statusline.json")


# --- review of feat/enterprise-spend, W4 ---------------------------------

@pytest.mark.parametrize("winerror", [53, 67])
def test_an_unreachable_share_is_not_a_missing_config(tmp_path, monkeypatch,
                                                      winerror):
    """CPython maps ERROR_BAD_NETPATH (53) and ERROR_BAD_NET_NAME (67) to
    ENOENT, so a \\\\wsl.localhost share that drops for a moment raised
    FileNotFoundError like a config that is not there, and the watch
    published EMPTY_VIEW: no seat, the spend page gone until the file
    next changed. Not knowing keeps the last view."""
    home, _ = _home(tmp_path)
    _write_config(home, seat=True)
    clock = _Clock(time.time())
    _touch_drop(home, clock.t)
    w = _watch(home, clock, _Running(True))
    w.tick()
    assert w.view().spend_seat
    _write_config(home, seat=True, extra={"x": 1})     # a new signature
    real = os.stat
    path = claude_plan.config_path(home)

    def stat(p, *a, **k):
        if os.fspath(p) == path:
            e = FileNotFoundError(2, "The network path was not found")
            e.winerror = winerror
            raise e
        return real(p, *a, **k)
    monkeypatch.setattr(os, "stat", stat)
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.tick()
    assert w.view().spend_seat
    monkeypatch.setattr(os, "stat", real)
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.tick()
    assert w.view().spend_seat


def test_a_config_really_gone_is_still_no_seat(tmp_path):
    home, _ = _home(tmp_path)
    path = _write_config(home, seat=True)
    clock = _Clock(time.time())
    _touch_drop(home, clock.t)
    w = _watch(home, clock, _Running(True))
    w.tick()
    os.remove(path)
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.tick()
    assert w.view() is claude_plan.EMPTY_VIEW


def test_the_watch_reads_the_config_as_plan_reader_does(tmp_path,
                                                        monkeypatch):
    """Round 6 of the 2026-11-04 review, item 7: WslWatch._look was a copy
    of PlanReader.look's stat, compare and parse, and the two had drifted
    (a failed read retried at every tick here, a minute later there). Both
    go through one claude_plan.ConfigFile now: a failed read is retried
    FIRST_ANSWER_RETRY_S later, a change parsed at most that often."""
    home, _unc = _home(tmp_path)
    path = _write_config(home)
    t0 = time.time()
    clock = _Clock(t0)
    w = _watch(home, clock, _Running(True))
    assert isinstance(w._config, claude_plan.ConfigFile)
    real = claude_plan.parse
    calls = []

    def flaky(p, st=None):
        calls.append(1)
        return None if len(calls) == 1 else real(p, st)
    monkeypatch.setattr(claude_plan, "parse", flaky)
    assert w._look() is None                          # failed: nothing new
    clock.t = t0 + 2
    assert w._look() is None and len(calls) == 1      # not yet
    clock.t = t0 + claude_plan.FIRST_ANSWER_RETRY_S
    assert w._look() is not None and len(calls) == 2
    clock.t += 1
    assert w._look() is None and len(calls) == 2      # unchanged
    _write_config(home, seat=True)
    os.utime(path, (t0 + 100, t0 + 100))
    assert w._look() is None and len(calls) == 2      # parsed 1 s ago
    clock.t += claude_plan.FIRST_ANSWER_RETRY_S
    assert w._look().spend_seat and len(calls) == 3


# ------------------------------- review of the WSL work, 2026-11-05: E-4

class _CountingReader:
    """A reader whose walk is counted, and whose held() answers from the
    last walk, as BurnReader.held does: news since then is not in it."""

    def __init__(self):
        self.polls = []
        self.total = 100

    def poll(self, now, budget=None):
        self.polls.append(now)
        self.walked = self.total
        return 1, [self.walked] + [0] * 19

    def held(self, now):
        return (1, [self.walked] + [0] * 19) if self.polls else None


def test_a_page_asked_again_after_a_gap_gets_nothing_until_a_walk(tmp_path):
    """The page is marked again after more than WANT_TTL_S unasked while
    Claude Code kept working in the distribution: every tick meanwhile ran
    held() only, and the first asks were handed the pre-gap buckets as
    current (100 tokens where a native walk said 25,100). Now a snapshot
    is current only for BURN_STALE_S after a real walk."""
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    r = _CountingReader()
    w = _watch(home, clock, _Running(True), burn_reader=r)
    src = w.burn_source()
    src.poll(clock.t)
    w.tick()
    assert src.poll(clock.t) == (1, [100] + [0] * 19)
    # Unasked for longer than WANT_TTL_S, the desk busy all the while:
    # the walks stop once the last ask is WANT_TTL_S old.
    end = clock.t + wsl_watch.WANT_TTL_S + wsl_watch.BURN_STALE_S + 30
    while clock.t < end:
        clock.t += wsl_watch.TICK_S
        _touch_drop(home, clock.t)
        r.total += 5000
        w.tick()
    walks = len(r.polls)
    assert r.polls[-1] < clock.t - wsl_watch.BURN_STALE_S
    assert src.poll(clock.t) is None            # asked: nothing stale
    clock.t += wsl_watch.RUNNING_EVERY_S
    _touch_drop(home, clock.t)
    w.tick()
    assert len(r.polls) == walks + 1
    assert src.poll(clock.t) == (1, [r.total] + [0] * 19)


def test_a_blocked_watch_thread_does_not_look_current(tmp_path):
    """The watch thread held in wsl.exe or a hung 9P call: every ask got the
    old tick's q and buckets, and two hours on the bars had not slid."""
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    r = _CountingReader()
    w = _watch(home, clock, _Running(True), burn_reader=r)
    src = w.burn_source()
    src.poll(clock.t)
    w.tick()
    assert src.poll(clock.t) is not None
    assert src.poll(clock.t + wsl_watch.BURN_STALE_S) is not None
    assert src.poll(clock.t + wsl_watch.BURN_STALE_S + 1) is None
    assert src.poll(clock.t + 7200) is None


def test_a_quiet_desk_keeps_the_last_walk_current(tmp_path):
    """Nothing written since the last walk: the drop folder, which every
    render and every prompt touches, is older than it, so the transcripts
    have nothing the walk missed and the held buckets, slid to now, are
    the truth."""
    home, _ = _home(tmp_path)
    t0 = time.time()
    clock = _Clock(t0)
    _touch_drop(home, t0)
    r = _CountingReader()
    w = _watch(home, clock, _Running(True), burn_reader=r)
    src = w.burn_source()
    src.poll(clock.t)
    w.tick()
    clock.t += wsl_watch.LIVE_S + 1             # walked away
    w.tick()
    assert src.poll(clock.t) == (1, [100] + [0] * 19)
