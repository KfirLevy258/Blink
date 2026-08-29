"""A scripted board on a fake serial port, for tests/ci/check_factory.sh.

The factory scripts drive the board through pyserial: pulse RTS to reset it,
read its boot log, send the edition message, read the answer. This module is
what they import instead of pyserial when the test puts it first on
PYTHONPATH. The "board" replays FAKE_BOARD_TRANSCRIPT (a file of the lines a
real one prints from reset to the usage screen) every time it is reset, and
answers an edition message with FAKE_BOARD_EDITION_REPLY. Every write and
every reset is appended to FAKE_TOOL_LOG, so a test can assert not just the
verdict but the conversation that produced it.
"""
import os
import time


def _log(line):
    with open(os.environ["FAKE_TOOL_LOG"], "a") as f:
        f.write(line + "\n")


class Serial:
    def __init__(self, port=None, baudrate=9600, timeout=None, **_kw):
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._rts = False
        self._dtr = False
        self._buf = b""
        self.is_open = port is not None
        if self.is_open:
            self._boot("open")

    def _boot(self, why):
        _log(f"board boot ({why})")
        with open(os.environ["FAKE_BOARD_TRANSCRIPT"], "rb") as f:
            self._buf = f.read()

    def open(self):
        self.is_open = True
        self._boot("open")

    @property
    def rts(self):
        return self._rts

    @rts.setter
    def rts(self, value):
        # EN is held low while RTS is asserted; releasing it boots the board.
        if self._rts and not value:
            self._boot("rts pulse")
        self._rts = bool(value)

    @property
    def dtr(self):
        return self._dtr

    @dtr.setter
    def dtr(self, value):
        self._dtr = bool(value)

    def read(self, n=1):
        if not self._buf:
            time.sleep(self.timeout or 0.01)
            return b""
        # Small chunks, like a real UART: the scripts must reassemble lines.
        n = min(n, 64)
        chunk, self._buf = self._buf[:n], self._buf[n:]
        return chunk

    def write(self, data):
        text = data.decode("utf-8", "replace").strip()
        _log(f"host -> {text}")
        if '"t": "edition"' in text or '"t":"edition"' in text:
            reply = os.environ.get("FAKE_BOARD_EDITION_REPLY", "")
            self._buf += (reply + "\n").encode()
        return len(data)

    def flush(self):
        pass

    def reset_input_buffer(self):
        self._buf = b""

    def close(self):
        self.is_open = False


class SerialException(Exception):
    pass
