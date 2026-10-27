"""The daemon's answer to a hello must fit the board's receive ring.

proto.c's ISR copies the UART FIFO into a ring of RX_RING_SIZE bytes, and the
main loop empties it between LVGL frames. Whatever arrives while that loop is
busy -- painting the first full gauge frame, say, which is the very moment
the reply to the second boot hello lands -- waits in the ring, and what does
not fit is dropped by the ISR and counted, nothing more. A dropped byte is
usually the middle of a line, so the line is lost and the next one with it.

The reply to a hello is the largest thing the daemon ever sends without
pausing for an answer: welcome, a waiting ota_error, time, svc for both
pages, the usage line, the session name and an update offer, back to back.
The daemon review put it at about 1095 bytes with an offer. Measured here at
the widest the Bridge builds, with the usage line counted at its 512-byte cap
and a 200-character failure report waiting, it is 1266 -- against a ring of
1024, so the bytes lost were the tail: the offer, which is the message the
Settings row turns into "Install 1.x.y". That is why the ring was enlarged to
2048; this test is what notices if the burst ever outgrows it again.

Built through the Bridge, not by adding up hand-written message sizes: the
sequence and its contents are the Bridge's to decide, and a test that guessed
them would go on passing after the Bridge learned to send an eighth message.
"""
import os
import pathlib
import re

from pc import protocol
from pc import theme
from pc.bridge import Bridge
from pc.providers import base

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _ring_size():
    src = (ROOT / "firmware" / "src" / "proto.c").read_text()
    m = re.search(r"^#define RX_RING_SIZE\s+(\d+)", src, re.M)
    assert m, "proto.c no longer defines RX_RING_SIZE as a number"
    return int(m.group(1))


def _widest_usage():
    # The same frame tests/pc/test_protocol.py builds for the 512-byte line
    # guard: both providers, every field at its widest.
    now = 1_787_700_000.0

    def widest(provider):
        return base.NormalizedUsageFrame(
            provider=provider, src="cli-state",
            observed_at=now - 2147483647, active_at=now - 2147483647,
            session_pct=102.33333333333333,
            session_resets_at=now + 31536000.1234567,
            weekly_pct=102.66666666666667,
            weekly_resets_at=now + 31536000.1234567,
            state="running", stale=False, session_burn_pph=999.93333,
            n_run=99, n_wait=99, n_stuck=99, n_idle=99, n_agents=99,
            label="a-project-with-a-long-name")

    return protocol.frame_to_usage(widest("claude"), now,
                                   secondary=widest("codex"))


class _WidestWatch:
    """Both pages in an incident, at the widest level and component name."""

    def want(self, providers):
        pass

    def shown(self, provider):
        return ("degraded", "x" * (protocol.SVC_WHAT_MAX_BYTES + 8))


def _hello_burst():
    lines = []

    def write(msg):
        raw, why = protocol.encode_checked(msg)
        assert why is None, why
        lines.append(raw)

    usage = _widest_usage()

    # Themes owed in both files, at their widest: a custom save that differs
    # from the board and a preview waiting. None of it may join the burst
    # (CONTRACT-theme, Addendum 2): it goes out on the next fast tick. ~ is
    # tmp_path here (tests/conftest.py), resolved now, not at import.
    home = os.path.join(os.path.expanduser("~"), ".blink")
    widest_theme = {"id": theme.CUSTOM, "c": ["ffffff", "000000"]
                    + ["123456"] * 5, "w": 22, "ends": "round", "dot": 1,
                    "by": "level"}
    theme.save(home, widest_theme)
    theme.write_preview(home, widest_theme)

    def fetch():
        return dict(usage)
    # The project name rides its own message; the widest one the daemon
    # allows, with a session count at the bus's clamp.
    fetch.session_pair = lambda: ("p" * (protocol.SESSION_LABEL_MAX_BYTES + 8),
                                  9999)

    b = Bridge(write_msg=write, fetch_usage=fetch,
               now=lambda: 0.0, wall=lambda: (4102444799, -840),
               fetch_manifest=lambda: {"version": "10.10.10", "size": 9999999,
                                       "sha256": "ab" * 32},
               fetch_signed_manifest=lambda: None,
               status_watch=_WidestWatch(),
               theme_sync=theme.ThemeSync(home),
               report_failure="x" * 200)
    b._app_available = lambda m: ("10.10.10", {"size": 1, "sha256": "cd" * 32})
    b.on_message({"t": "hello", "v": protocol.VERSION, "fw": "1.4.0"})
    return lines


def test_the_burst_is_what_this_test_thinks_it_is():
    kinds = [protocol.json.loads(raw)["t"] for raw in _hello_burst()]
    assert kinds.count("svc") == 2, kinds
    for t in ("welcome", "ota_error", "time", "usage", "session",
              "ota_avail"):
        assert t in kinds, kinds


def test_no_theme_line_joins_the_burst():
    kinds = [protocol.json.loads(raw)["t"] for raw in _hello_burst()]
    assert "theme" not in kinds, kinds


def test_the_widest_hello_reply_fits_the_boards_receive_ring():
    """With a quarter of the ring left over: the ring also holds whatever
    the board has not yet read when the burst starts (a pong, the tail of
    a heartbeat), and the daemon is free to grow by a field or two before
    this is worth revisiting."""
    # The usage line is counted at the line cap rather than as built: its
    # width moves with every field added to it, and the cap is the one
    # number about it that encode_checked guarantees.
    total = sum(protocol.MAX_LINE_BYTES
                if protocol.json.loads(raw)["t"] == "usage" else len(raw)
                for raw in _hello_burst())
    ring = _ring_size()
    assert total <= ring * 3 // 4, (total, ring)
