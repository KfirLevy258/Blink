"""A logo image streamed to the board, over WiFi or the cable (spec S15.2).

WifiOtaStream's shape (pc/wifi_ota.py): not a blocking function, a small state
machine that Bridge.pump() drives once per pass, so pings and acks keep moving
between pumps. The ack that opens the window is `logo_ack off:0`; after that no
more than WINDOW bytes are unacknowledged, and `logo_end` goes out once an ack
reaches the size. Over WiFi the bytes ride `G` frames (512 per chunk, via
write_blob); over USB they are `logo_chunk` lines with base64 `d`, 256 bytes a
line so the line stays under the board's 512.

An image over LOGO_MAX is refused in the constructor, before anything is sent:
the board would refuse `logo_begin` anyway, and a refusal that costs nothing
beats one that costs a stream.

Logging: a percentage and fixed sentences. Never a byte, an offset or a
length (CLAUDE.md, S7.5).
"""
from __future__ import annotations

import sys
import time
import zlib

from pc import protocol
from pc.logo_encode import E_SIZE, LOGO_MAX

CHUNK_WIFI = 512
CHUNK_USB = 256
WINDOW = 32 * 1024
STALLED = "logo update stalled"
REFUSED = "board refused the logo"


class LogoStream:
    def __init__(self, blob: bytes, link_kind: str, now=time.monotonic,
                 stall_s: float = 30.0):
        if len(blob) > LOGO_MAX:
            raise ValueError(E_SIZE)
        if link_kind not in ("wifi", "usb"):
            raise ValueError("unknown link kind")
        self._blob, self._kind = bytes(blob), link_kind
        self._chunk = CHUNK_WIFI if link_kind == "wifi" else CHUNK_USB
        self._now, self._stall_s = now, stall_s
        self._sent = self._acked = 0
        self._begun = self._ready = self._end_sent = False
        self._result = None
        self._progress_at = now()
        self._pct = -1

    def ready(self) -> None:
        """logo_ack off:0 arrived: the board erased and opened the partition."""
        if not self._ready:
            self._ready = True
            self._progress_at = self._now()

    def ack(self, off, err=None) -> None:
        if self._result is not None:
            return
        if err:
            self._result = (False, REFUSED)
            return
        if not isinstance(off, int) or isinstance(off, bool):
            return
        if off == 0:
            if self._begun:
                self.ready()
        elif self._ready and self._acked < off <= self._sent:
            self._acked = off
            self._progress_at = self._now()
            pct = off * 100 // len(self._blob)
            if pct // 10 != self._pct // 10:
                print(f"[bridge] logo: {pct}%", file=sys.stderr)
            self._pct = pct

    def _send(self, link, msg) -> None:
        raw, _ = protocol.encode_checked(msg)
        link.write(raw)

    def pump(self, link) -> None:
        """logo_begin once, chunks up to WINDOW unacked, logo_end when the
        last byte is acked."""
        if self._result is not None or self._end_sent:
            return
        if self._now() - self._progress_at > self._stall_s:
            self._result = (False, STALLED)
            return
        n = len(self._blob)
        if not self._begun:
            # Marked before the write: a link that answers from inside write()
            # (a test's, or a loopback) acks before write() returns.
            self._begun = True
            self._send(link, protocol.logo_begin(
                n, zlib.crc32(self._blob) & 0xffffffff))
            return
        if self._ready and self._acked == n:
            self._send(link, protocol.logo_end())
            self._end_sent = True
            self._result = (True, "")
            return
        if not self._ready:
            return
        while self._sent < n and self._sent - self._acked < WINDOW:
            chunk = self._blob[self._sent:self._sent + self._chunk]
            at, self._sent = self._sent, self._sent + len(chunk)
            if self._kind == "wifi":
                link.write_blob(b"G", at, chunk)
            else:
                self._send(link, protocol.logo_chunk(at, chunk))

    def done(self):
        """None while running; (True, "") once logo_end is sent, or
        (False, reason)."""
        return self._result
