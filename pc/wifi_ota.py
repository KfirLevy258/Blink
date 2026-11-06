"""A firmware image streamed to a board that has no cable (spec S9).

Not a blocking function: the acks that open the window arrive through the
same read loop that would be stuck inside it, so this is a small state
machine that Bridge.pump() drives once per pass (D43). Pings, pongs and acks
keep moving between pumps.

Logging: a percentage and fixed sentences. Never a byte, an offset or a
length (CLAUDE.md, S7.5).
"""
from __future__ import annotations

import sys
import time

from pc import protocol

CHUNK = 512
WINDOW = 32 * 1024          # unacknowledged bytes in flight; the board acks every 16 KiB
ACK_EVERY = 16 * 1024
# Fixed, board-facing: ota_error carries it to a 47-character panel line.
UNCONFIRMED = "board has not confirmed its last update"
# No ack inside stall_s: a board that fell silent, or an ack the board lost
# to a full TX ring (it has no retry). Either way the stream is over.
STALLED = "Board stopped acknowledging the update"
REJECTED = "Board rejected the update image"
REFUSED = "Board refused the update"


class WifiOtaStream:
    def __init__(self, blob: bytes, version: str, now=time.monotonic,
                 stall_s: float = 30.0):
        self._blob, self.version = blob, version
        self._now, self._stall_s = now, stall_s
        self._sent = self._acked = 0
        self._ready = self._end_due = self._end_sent = False
        self._result = None
        self.went_quiet = False         # silent after ota_end: the board rebooted
        self.refused = False            # ota_begin refused: the board holds no offer
        self._progress_at = now()       # a board that never says ready stalls too
        self._pct = -1

    def ready(self) -> None:
        """ota_ack off:0 arrived: the board's flash context and hash exist."""
        if not self._ready:
            self._ready = True
            self._progress_at = self._now()

    def ack(self, off, err=None) -> None:
        if self._result is not None:
            return
        if err:
            # Only "unconfirmed" is defined (D38); any err is a refusal, and
            # its text is the board's, so the reason said is ours.
            self.refused = err == "refused"
            self._result = (False, UNCONFIRMED if err == "unconfirmed"
                            else REJECTED if err == "failed" else REFUSED)
            return
        if not isinstance(off, int) or isinstance(off, bool):
            return
        if off == 0:
            self.ready()
        elif self._ready and self._acked < off <= self._sent:
            self._acked = off
            self._progress_at = self._now()
            pct = off * 100 // len(self._blob)
            if pct // 10 != self._pct // 10:
                print(f"[bridge] ota: {pct}%", file=sys.stderr)
            self._pct = pct
            self._end_due = off == len(self._blob)

    def pump(self, link, write=None) -> None:
        """Send up to WINDOW unacked bytes; ota_end once the last is acked.
        `write(msg)` carries ota_end like every other line (tapped, counted);
        without it the line goes straight to the link."""
        if self._result is not None:
            return
        if self._now() - self._progress_at > self._stall_s:
            if self._end_sent:
                # The real board answers a good ota_end with a screen, a 1 s
                # sleep and a cold reboot, and never closes the socket. Silence
                # after ota_end is the reboot, not a failure.
                self.went_quiet = True
            else:
                self._result = (False, STALLED)
            return
        if self._end_sent:
            return              # sealing: the board's verdict is an err ack or a reboot
        if self._end_due:
            end = protocol.ota_end(len(self._blob))
            if write is not None:
                write(end)
            else:
                link.write(protocol.encode_checked(end)[0])
            self._end_sent = True
            self._progress_at = self._now()
            return
        if not self._ready:
            return
        n = len(self._blob)
        while self._sent < n and self._sent - self._acked < WINDOW:
            chunk = self._blob[self._sent:self._sent + CHUNK]
            link.write_blob(b"O", self._sent, chunk)
            self._sent += len(chunk)

    @property
    def end_sent(self) -> bool:
        return self._end_sent

    def done(self):
        """None while running, and through sealing: the board sends no
        success reply, it reboots and the session drops. (False, reason)
        once it refused, failed to verify, or went quiet."""
        return self._result
