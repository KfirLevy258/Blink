"""Transport-agnostic bridge logic: react to board messages, poll usage, track
liveness. I/O (serial) is injected so this is unit-testable without hardware."""
import time

from pc import protocol

LIVENESS_WINDOW_S = 30.0


class Bridge:
    def __init__(self, write_msg, fetch_usage, now=time.monotonic, app_ver="0.2.0"):
        self._write = write_msg          # callable(dict)
        self._fetch = fetch_usage        # callable() -> usage message dict
        self._now = now
        self._app_ver = app_ver
        self._last_ping = None

    # --- inbound ---
    def on_message(self, msg: dict):
        t = msg.get("t")
        if t == "hello":
            self._write(protocol.welcome("claude-usage-bridge", self._app_ver))
            self.poll_once()             # push current data immediately
        elif t == "ping":
            self._last_ping = self._now()
            # Free: never fetch here. The usage endpoint is aggressively
            # rate-limited and this fires every 10 s.
            self._write(protocol.pong())
        # unknown types ignored

    def board_alive(self) -> bool:
        return (self._last_ping is not None
                and self._now() - self._last_ping <= LIVENESS_WINDOW_S)

    # --- outbound ---
    def poll_once(self):
        try:
            self._write(self._fetch())
        except Exception as e:
            self._write(protocol.status("error", str(e)[:80]))
