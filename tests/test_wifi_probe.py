"""tools/wifi_probe.py against the loopback fake board, never the network."""
import importlib.util
import io
import os
import re
from pathlib import Path

import pytest

from tests.support.fake_wifi_board import FakeWifiBoard

TOOL = Path(__file__).resolve().parents[1] / "tools" / "wifi_probe.py"
KEY = bytes(range(32))


def _load():
    spec = importlib.util.spec_from_file_location("wifi_probe_t", TOOL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def run(tmp_path):
    made = []
    kf = tmp_path / "key"
    kf.write_text(KEY.hex())

    def go(*flags, key_file=None, **board_kw):
        b = FakeWifiBoard(KEY, **board_kw).start()
        made.append(b)
        out = io.StringIO()
        rc = _load().main(["--key-file", str(key_file or kf), "--addr", "127.0.0.1",
                           "--port", str(b.address[1]), *flags], out=out)
        return rc, out.getvalue(), b

    yield go
    for b in made:
        b.stop()


def test_probe_talks_to_the_fake_board(run):
    rc, text, b = run("--lengths", ping_interval_s=0.2)
    assert rc == 0
    assert text.splitlines() == ["ok: session open", "ok: state received",
                                 "ok: 500 lengths"]
    assert not re.search(r"[0-9a-f]{5,}", text)
    assert b.pongs >= 500


def test_probe_fails_when_the_board_closes_on_a_length(run):
    rc, text, _ = run("--lengths", ping_interval_s=0.2, close_on_length=200)
    assert rc == 1
    # The probe names the last length it sent before it saw the close, and
    # says the true one is that or up to 2 lower: it pumps 25 ms per frame.
    # Under Docker's emulation on Python 3.9 (2026-11-07) the close landed
    # one frame late and it said 201.
    assert text.splitlines()[-1] in {"fail: length %d" % n for n in (200, 201, 202)}


def test_probe_reports_the_impostor_as_a_fixed_failure(run):
    rc, text, _ = run("--lengths", close_after_m1=True)
    assert rc == 1 and text.strip() == "handshake refused"


def test_a_wrong_key_file_is_refused(run, tmp_path):
    bad = tmp_path / "bad"
    bad.write_text(bytes(32).hex())
    rc, text, _ = run("--verify", key_file=bad)
    assert rc == 1 and text.strip() == "handshake refused"


def test_hold_reports_theme_edit(run):
    rc, text, _ = run("--hold", "--seconds", "0.7", ping_interval_s=0.2,
                      extra_lines=[{"t": "theme_edit"}])
    lines = text.splitlines()
    assert rc == 0
    assert "ok: theme_edit received" in lines
    assert re.fullmatch(r"ok: \d+ board frames", lines[-1])


def test_verify_prints_ok_verified(run):
    rc, text, _ = run("--verify", verify_window=True)
    assert rc == 0 and text.strip() == "ok: verified"


def test_new_key_file_is_owner_only_and_never_overwrites(tmp_path):
    p = tmp_path / "new"
    out = io.StringIO()
    assert _load().main(["--new-key-file", str(p)], out=out) == 0
    assert len(bytes.fromhex(p.read_text())) == 32
    # The mode is POSIX's: NTFS reports 0o666 for any writable file whatever
    # its ACL. The daemon's own pairing file goes through pc/private_file,
    # whose ACLs are proved by the windows_real tests in test_private_file.py.
    if os.name == "posix":
        assert (p.stat().st_mode & 0o777) == 0o600
    assert _load().main(["--new-key-file", str(p)], out=io.StringIO()) == 2


def test_a_board_that_says_no_hello_still_gets_a_session(run):
    # The real board greets at boot only; the fake used to hello every session.
    rc, text, _ = run("--hold", "--seconds", "0.3")
    assert rc == 0 and text.splitlines()[:2] == ["ok: session open", "ok: state received"]


def test_ctrl_c_in_hold_prints_the_frame_count():
    mod, out = _load(), io.StringIO()

    class C:
        frames = 7
        lines = []

        def pump(self, _):
            raise KeyboardInterrupt

    assert mod._hold(C(), out, None) == 0
    assert out.getvalue().strip() == "ok: 7 board frames"
