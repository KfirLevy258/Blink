"""Nothing from a conversation store may reach a log, a message, or a fixture.

README.md:90 tells customers to email support the tail of ~/.blink/bridge.log
and promises that "nothing in either is a secret". Every source this project
reads has to keep that promise true, and one of them now reads a store made
largely of chat text.
"""
import os

from pc import desktop_idb
from pc import desktop_local_storage
from tests.support import leveldb_fixture as fx
from tests.support import v8_fixture as vfx

MARKER = "MARKER-DO-NOT-LEAK"
LS_KEY = (b"_https://claude.ai\x00\x01"
          b"claudeai.ochre_heron_tide.3d2fe603-510b-4256-bd4e-2f2b1b689bef")


def test_reading_the_conversation_store_leaks_nothing(tmp_path, capsys):
    chat = {"messages": [{"text": MARKER}], "created_at": MARKER}
    (tmp_path / "000103.log").write_bytes(fx.build_log(
        [[("put", b"\x00cowork:cse_x", vfx.dumps(chat))]]))
    try:
        desktop_idb.seven_day_reset(str(tmp_path))
    except Exception as exc:            # pragma: no cover - must not happen
        assert MARKER not in str(exc)
        raise
    captured = capsys.readouterr()
    assert MARKER not in captured.out
    assert MARKER not in captured.err


def test_a_corrupt_conversation_store_leaks_nothing(tmp_path, capsys):
    """The failure path is where buffer bytes escape, not the happy one."""
    (tmp_path / "000103.log").write_bytes(
        b"\x00cowork" + MARKER.encode() + b"\xff" * 200)
    desktop_idb.seven_day_reset(str(tmp_path))
    captured = capsys.readouterr()
    assert MARKER not in captured.out
    assert MARKER not in captured.err


def test_reading_local_storage_leaks_nothing(tmp_path, capsys):
    (tmp_path / "000001.log").write_bytes(fx.build_log(
        [[("put", LS_KEY, b"\x01" + MARKER.encode("utf-8"))]]))
    desktop_local_storage.newest_record(str(tmp_path))
    captured = capsys.readouterr()
    assert MARKER not in captured.out
    assert MARKER not in captured.err


def test_no_fixture_was_captured_from_a_real_machine():
    """A standing check, not a one-off. A captured fixture would put somebody's
    chat text into a public repository, which is the outcome the whole rule
    exists to prevent."""
    root = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "fixtures")
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as fh:
            body = fh.read()
        assert b"ochre_heron_tide" not in body, name
        assert b"unifiedWindows" not in body, name
