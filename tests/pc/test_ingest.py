"""The bus: many providers polled, one message out, nothing allowed to escape."""
import json

from pc import ingest
from pc.providers import base
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider

NOW = 1_787_700_000.0


class Boom(base.ProviderParser):
    def get_provider_id(self):
        return "boom"

    def poll(self, now_epoch):
        raise RuntimeError("upstream changed shape")


class Fixed(base.ProviderParser):
    def __init__(self, frame):
        self._frame = frame

    def get_provider_id(self):
        return self._frame.provider

    def poll(self, now_epoch):
        return [self._frame]


def frame(provider="claude", src="cli", at=NOW, session=50.0, weekly=20.0):
    return base.NormalizedUsageFrame(
        provider=provider, src=src, observed_at=at,
        session_pct=session, weekly_pct=weekly)


def test_a_provider_that_raises_does_not_take_the_bus_down(capsys):
    bus = ingest.IngestionBus(providers=[Boom(), Fixed(frame())],
                              now=lambda: NOW)
    msg = bus.poll()
    assert msg["session_pct"] == 50.0
    assert "will be skipped" in capsys.readouterr().err


def test_a_broken_provider_is_reported_once_not_every_poll(capsys):
    bus = ingest.IngestionBus(providers=[Boom(), Fixed(frame())],
                              now=lambda: NOW)
    bus.poll()
    capsys.readouterr()
    bus.poll()
    bus.poll()
    assert capsys.readouterr().err == ""


def test_no_sources_at_all_returns_none():
    bus = ingest.IngestionBus(providers=[], now=lambda: NOW)
    assert bus.poll() is None


def test_the_message_names_the_winning_source():
    bus = ingest.IngestionBus(
        providers=[Fixed(frame(src="cli", at=NOW - 900, session=10.0)),
                   Fixed(frame(src="desktop", at=NOW, session=77.0))],
        now=lambda: NOW)
    msg = bus.poll()
    assert msg["session_pct"] == 77.0
    assert msg["src"] == "desktop"
    assert msg["provider"] == "claude"


def test_a_provider_can_be_added_at_runtime():
    bus = ingest.IngestionBus(providers=[], now=lambda: NOW)
    assert bus.poll() is None
    bus.add_provider(Fixed(frame(provider="codex")))
    assert bus.poll()["provider"] == "codex"


def test_the_two_real_providers_compose_end_to_end(tmp_path):
    """CLI supplies resets, context and model; desktop supplies a fresher
    session percentage. The merged message carries all four."""
    statusline = tmp_path / "statusline.json"
    statusline.write_text(json.dumps({
        "model": {"display_name": "Opus 5"},
        "context_window": {"used_percentage": 61},
        "rate_limits": {
            "five_hour": {"used_percentage": 10, "resets_at": NOW + 900},
            "seven_day": {"used_percentage": 20, "resets_at": NOW + 90_000},
        },
    }))
    import os
    os.utime(statusline, (NOW - 900, NOW - 900))

    cache = tmp_path / "plan-usage-history.json"
    cache.write_text(json.dumps({"version": 2, "samples": [
        {"t": int(NOW * 1000), "org": "o", "u": {"fh": 77, "sd": 44}}]}))

    bus = ingest.IngestionBus(
        providers=[ClaudeCliProvider(path=str(statusline)),
                   ClaudeDesktopProvider(path=str(cache))],
        now=lambda: NOW)
    msg = bus.poll()

    assert msg["session_pct"] == 77.0          # desktop, fresher
    assert msg["weekly_pct"] == 44.0           # desktop, fresher
    assert msg["session_resets_in_s"] == 900   # CLI, the only source with it
    assert msg["ctx_pct"] == 61.0              # CLI only
    assert msg["model"] == "Opus 5"            # CLI only
    assert msg["src"] == "desktop"


def test_make_fetch_is_a_zero_arg_callable():
    fetch = ingest.make_fetch(providers=[Fixed(frame())])
    assert fetch()["session_pct"] == 50.0
