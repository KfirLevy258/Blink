"""tools/console_tail.py: a console reader that cannot write to the board and
cannot print what the board did not let it.

No serial port: the fake below records every attribute the tool touches.
"""
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

from pc import logbook

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "console_tail.py"


def _load(name="console_tail_t"):
    spec = importlib.util.spec_from_file_location(name, TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


ct = _load()


class FakeSerial:
    """Hands over its chunks, then Ctrl-C. Any attribute outside `ALLOWED`
    is recorded: the tool may read and close, nothing more."""
    ALLOWED = {"read", "close", "touched", "chunks"}

    def __init__(self, chunks):
        object.__setattr__(self, "chunks", list(chunks))
        object.__setattr__(self, "touched", [])

    def __getattr__(self, name):
        self.touched.append(name)
        raise AttributeError(name)

    def __setattr__(self, name, value):
        self.touched.append(name)

    def read(self, n=1):
        if not self.chunks:
            raise KeyboardInterrupt
        return self.chunks.pop(0)

    def close(self):
        pass


def _run(chunks, argv=("/dev/fake",)):
    ser = FakeSerial(chunks)
    out = io.StringIO()
    rc = ct.main(list(argv), open_port=lambda port: ser, out=out)
    return rc, out.getvalue().splitlines(), ser


def test_import_is_inert(monkeypatch):
    class Boom(types.ModuleType):
        def __getattr__(self, name):
            if name.startswith("__"):
                raise AttributeError(name)
            raise AssertionError("serial touched on import: " + name)
    monkeypatch.setitem(sys.modules, "serial", Boom("serial"))
    _load("console_tail_inert")


def test_opens_plainly_and_sets_no_control_lines(monkeypatch):
    opened = []

    class Recording(FakeSerial):
        def __init__(self, *a, **kw):
            opened.append((a, kw))
            super().__init__([b"[usage] firmware boot OK\n"])

    fake = types.ModuleType("serial")
    fake.Serial = Recording
    monkeypatch.setitem(sys.modules, "serial", fake)
    out = io.StringIO()
    assert ct.main(["/dev/fake"], out=out) == 0
    (args, kwargs), = opened
    assert "dtr" not in kwargs and "rts" not in kwargs
    assert out.getvalue().strip() == "[usage] firmware boot OK"


def test_nothing_but_read_and_close_is_touched():
    rc, lines, ser = _run([b"[ota] image confirmed\n"])
    assert rc == 0 and ser.touched == []
    for name in ("dtr", "rts", "reset_input_buffer", "write", "setDTR",
                 "setRTS"):
        assert name not in ser.touched


def test_lines_go_through_tap_console_line():
    secret = '{"t":"wifi_state","st":"up","s":"6e6574"'
    rc, lines, _ = _run([
        (secret + "\n").encode(),
        b'{"t":"wifi_state","st":"up","s":"6e6574","ip":"10.0.0.2"}\r\n',
        # a fixed net_link.c line comes out as its constant
        b"[wifi] joined\n",
        b"garbage \x00\xff 6e6574\n",
        b"[usage] firm",   # cut in half, then completed by the next read
        b"ware boot OK\n",
    ])
    assert lines == [logbook.BOARD_LINE,
                     '{"t":"wifi_state","st":"up"}',
                     "[wifi] joined",
                     logbook.BOARD_LINE,
                     "[usage] firmware boot OK"]


def test_tap_mode_writes_console_records(tmp_path):
    tap = tmp_path / "tap.jsonl"
    _run([b"[usage] session 3% (1s)  weekly 4% (2s)\n",
          b"[usage] session 4% (1s)  weekly 4% (2s)  STALE\n"],
         argv=("/dev/fake", "--tap", str(tap)))
    recs = [json.loads(l) for l in tap.read_text().splitlines()]
    assert len(recs) == 2
    for r in recs:
        assert r["dir"] == "console" and isinstance(r["t"], float)
        assert isinstance(r["line"], str)
    sys.path.insert(0, str(ROOT))
    from tests.fleet import tap_asserts
    applied = tap_asserts._applied_lines(recs)
    assert len(applied) == 2
    assert [tap_asserts.STALE_MARKER in l for _, l in applied] == [False, True]


def test_no_raw_flag():
    with pytest.raises(SystemExit) as e:
        ct.main(["/dev/fake", "--raw"], open_port=lambda p: FakeSerial([]),
                out=io.StringIO())
    assert e.value.code == 2


def test_ctrl_c_inside_print_exits_cleanly():
    """Review of Task 0.1: only ser.read caught KeyboardInterrupt, so Ctrl-C
    landing in print (or the --tap write) came out as a traceback."""
    class Out:
        def write(self, s):
            raise KeyboardInterrupt

        def flush(self):
            pass

    ser = FakeSerial([b"hello\n"])
    assert ct.main(["/dev/x"], open_port=lambda p: ser, out=Out()) == 0
