"""The pluggable ingestion bus: every provider, polled and normalized.

This is the seam the daemon holds. claude_usage_bridge asks it for a usage
message and knows nothing about which providers exist, how many sources each
one has, or what any of their files look like -- which is the property that
lets a second provider be added without touching the daemon, the protocol or
the firmware.

A provider that misbehaves is contained here rather than being allowed to
reach the reconnect loop. The daemon's job is to keep a board fed; a parser
for an app we do not control is exactly the kind of code that should never be
able to stop it.
"""
import os
import sys
import time

from pc import normalizer, protocol
from pc.providers.claude_cli import ClaudeCliProvider
from pc.providers.claude_desktop import ClaudeDesktopProvider
from pc.providers.claude_state import ClaudeStateProvider


# Set to disable the localhost listener the browser extension reports to.
WEB_BRIDGE_DISABLE_ENV = "CLAUGE_NO_WEB_BRIDGE"


def default_providers():
    """Every provider shipped today, in preference order.

    Order is not the merge rule -- pc/normalizer merges by recency per field,
    not by position -- but it does decide which source is polled first and so
    which one a tie resolves toward.
    """
    return [ClaudeCliProvider(), ClaudeDesktopProvider(),
            ClaudeStateProvider()]


def start_web_bridge(providers, disable_env=WEB_BRIDGE_DISABLE_ENV):
    """Open the extension listener and add its provider. Returns it, or None.

    Never raises, and never blocks startup. The port can be held by a second
    daemon, by a previous instance that has not released it yet, or by
    something else entirely -- and none of those is a reason for the gauge to
    stop working. A failure here costs the browser source and nothing else.

    On by default. The listener is bound to loopback, answers one path and one
    method, caps the body before reading it and checks Origin against an
    allow-list (see pc/webbridge), and requiring a manual step to turn it on
    would undo the zero-configuration property that makes the extension worth
    shipping at all.
    """
    if os.environ.get(disable_env):
        return None
    try:
        from pc.webbridge import ClaudeWebProvider, WebBridge
        bridge = WebBridge()
        bridge.start()
    except Exception as e:
        # Exception, not OSError. A held port is the expected failure and is
        # an OSError, but this runs once at startup before the reconnect loop
        # exists -- so anything that escapes here kills the daemon outright
        # rather than costing one optional source. The house rule elsewhere in
        # this loop (see _self_update_tick) is the same: never take the gauge
        # down for a subsystem the gauge does not need.
        print(f"[ingest] browser bridge not started ({e}); the CLI and"
              " desktop sources are unaffected", file=sys.stderr)
        return None
    providers.append(ClaudeWebProvider(bridge.slot))
    return bridge


class IngestionBus:
    def __init__(self, providers=None, preferred_provider="claude",
                 now=time.time, web_bridge=False):
        self._providers = (default_providers() if providers is None
                           else list(providers))
        # Off unless asked for, so importing the bus in a test never opens a
        # socket. The daemon asks for it; nothing else does.
        self.web_bridge = (start_web_bridge(self._providers)
                           if web_bridge else None)
        self._preferred = preferred_provider
        self._now = now
        self._broken = set()

    def add_provider(self, provider):
        """Onboard a provider at runtime. Nothing else has to change."""
        self._providers.append(provider)

    def poll_frames(self):
        """Every frame every provider can produce right now.

        Each provider is polled inside its own try. A provider that raises is
        reported once and then skipped for the rest of the process: the
        alternative is a stack trace on the daemon's log every sixty seconds
        for as long as some other application's file stays malformed.
        """
        frames = []
        now = self._now()
        for p in self._providers:
            key = f"{p.__class__.__name__}"
            try:
                frames.extend(p.poll(now) or [])
            except Exception as e:
                if key not in self._broken:
                    print(f"[ingest] {key} failed and will be skipped: {e}",
                          file=sys.stderr)
                    self._broken.add(key)
        return frames

    def poll(self):
        """The usage message for the board, or None when nothing is known."""
        frame = normalizer.select(self.poll_frames(), preferred=self._preferred)
        if frame is None:
            return None
        return protocol.frame_to_usage(frame, self._now())


def make_fetch(providers=None, preferred_provider="claude",
               web_bridge=False):
    """Zero-arg callable for Bridge(fetch_usage=...).

    Same shape as the single-source make_fetch it replaces, so the daemon's
    wiring did not have to learn that there is now more than one source.
    """
    bus = IngestionBus(providers=providers,
                       preferred_provider=preferred_provider,
                       web_bridge=web_bridge)
    return bus.poll
