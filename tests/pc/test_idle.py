"""idle.seconds_idle: the runner is always injected, never the real ioreg."""
import subprocess
from types import SimpleNamespace

from pc import idle

OUT = '    |   "HIDIdleTime" = 1500000000\n'


def _runner(stdout="", code=0, exc=None):
    calls = []

    def run(argv, **kw):
        calls.append((argv, kw))
        if exc:
            raise exc
        return SimpleNamespace(returncode=code, stdout=stdout)
    run.calls = calls
    return run


def test_macos_parses_nanoseconds():
    r = _runner(OUT)
    assert idle.seconds_idle(r, "darwin") == 1.5
    assert r.calls[0][1]["timeout"] == 5


def test_macos_garbage_none():
    assert idle.seconds_idle(_runner("nothing here"), "darwin") is None


def test_macos_nonzero_none():
    assert idle.seconds_idle(_runner(OUT, code=1), "darwin") is None


def test_macos_runner_errors_none():
    for exc in (OSError("x"), subprocess.TimeoutExpired("ioreg", 5)):
        assert idle.seconds_idle(_runner(exc=exc), "darwin") is None


def test_linux_none_without_runner():
    r = _runner(OUT)
    assert idle.seconds_idle(r, "linux") is None
    assert r.calls == []


def test_windows_wraparound(monkeypatch):
    monkeypatch.setattr(idle, "_win_ticks", lambda: (5, 2**32 - 5))
    assert idle.seconds_idle(_runner(), "win32") == 0.01


def test_windows_unavailable_none(monkeypatch):
    monkeypatch.setattr(idle, "_win_ticks", lambda: None)
    assert idle.seconds_idle(_runner(), "win32") is None
