"""tools/lib_efuse.sh: efuse_probe tells a broken tool from a busy port.

On a freshly set-up bench Mac (2026-10-12) bitstring 5.0.0 had dropped
BitStream, so espefuse.py died on import. efuse_probe only ever ran
`espefuse.py --port ... summary`, so that read as "is the port free?", and
burn.sh refused to flash over a port nothing was holding. When summary fails,
the probe now asks whether the tool starts at all (`--help`, which opens no
port) before it blames the port. A summary that succeeds costs nothing extra.

These run the library in a real sh against a directory of stand-in tools in
tmp_path. No serial port is opened: the port handed in is a path that does
not exist, and the stand-ins record every call they get.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "tools" / "lib_efuse.sh"
WORKING_STUB = ROOT / "tests" / "ci" / "fakes" / "bin" / "espefuse.py"

# The stand-ins are #!/bin/sh scripts made executable with chmod, which a
# Windows runner cannot exec, so there is no Windows coverage of this path:
# tests/ci/check_factory.sh, the other thing that runs lib_efuse.sh, runs
# only on ubuntu-latest (.github/workflows/installer.yml).
pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("sh") is None,
    reason="needs a POSIX sh that can exec #!/bin/sh stand-ins")

# Anything espefuse prints may be serial bytes, so none of it may reach the
# reason. The broken stand-in prints this and the reason must not carry it.
MARKER = "ImportError-from-the-stand-in-0xC0FFEE"


def _probe(etools, log, port=None):
    if port is None:
        port = log.parent / "no-such-port"
    env = {"HOME": str(log.parent), "PATH": os.environ["PATH"],
           "FAKE_TOOL_LOG": str(log)}
    r = subprocess.run(
        ["sh", "-c",
         f'. "{LIB}"; efuse_probe "{port}" "{etools}"; '
         'printf "%s|%s\\n" "$EFUSE_STATE" "$EFUSE_REASON"'],
        env=env, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    state, _, reason = r.stdout.strip().partition("|")
    return state, reason, r


def _stub(etools, summary_rc, help_rc):
    """An espefuse.py that logs its arguments, prints MARKER on both streams,
    and exits with one status for summary and another for --help."""
    etools.mkdir()
    stub = etools / "espefuse.py"
    stub.write_text(
        "#!/bin/sh\n"
        'echo "espefuse $*" >>"$FAKE_TOOL_LOG"\n'
        f'echo "{MARKER}"\n'
        f'echo "{MARKER}" >&2\n'
        f'case "$*" in --help) exit {help_rc} ;; esac\n'
        f"exit {summary_rc}\n", encoding="utf-8")
    stub.chmod(0o755)


def _calls(log):
    return log.read_text(encoding="utf-8").splitlines()


def test_a_tool_that_does_not_start_is_not_blamed_on_the_port(tmp_path):
    etools = tmp_path / "etools"
    _stub(etools, summary_rc=1, help_rc=1)
    log = tmp_path / "tools.log"
    log.write_text("", encoding="utf-8")

    state, reason, r = _probe(etools, log)

    assert state == "unknown"
    assert "does not start" in reason
    assert "never reached the port" in reason
    assert f"{etools}/espefuse.py --help" in reason
    assert "port free" not in reason
    # Not echoed anywhere: not in the reason, not on the caller's terminal.
    assert MARKER not in reason
    assert MARKER not in r.stdout + r.stderr
    calls = _calls(log)
    assert calls[0].endswith("summary") and calls[1:] == ["espefuse --help"]


def test_a_port_that_exists_is_the_suspect_when_the_tool_starts(tmp_path):
    """summary fails, --help runs, and the port is there: something else may
    really be holding it."""
    etools = tmp_path / "etools"
    _stub(etools, summary_rc=2, help_rc=0)
    log = tmp_path / "tools.log"
    log.write_text("", encoding="utf-8")
    port = tmp_path / "cu.usbserial-test"   # a plain file stands in for it
    port.write_text("", encoding="utf-8")

    state, reason, r = _probe(etools, log, port=port)

    assert state == "unknown"
    assert f"could not read {port} -- is the port free?" in reason
    assert MARKER not in reason
    assert MARKER not in r.stdout + r.stderr


def test_a_port_that_does_not_exist_is_named_as_missing(tmp_path):
    """Not "a logger or the usage daemon may hold it": nothing can hold a
    port that is not there, and the likely cause is an unplugged board."""
    etools = tmp_path / "etools"
    _stub(etools, summary_rc=2, help_rc=0)
    log = tmp_path / "tools.log"
    log.write_text("", encoding="utf-8")

    state, reason, r = _probe(etools, log)

    assert state == "unknown"
    assert reason == (f"no such port: {tmp_path}/no-such-port "
                      "(is the board plugged in?)")
    assert MARKER not in r.stdout + r.stderr


def test_an_empty_port_is_named_as_missing(tmp_path):
    etools = tmp_path / "etools"
    _stub(etools, summary_rc=2, help_rc=0)
    log = tmp_path / "tools.log"
    log.write_text("", encoding="utf-8")

    state, reason, _ = _probe(etools, log, port="")

    assert state == "unknown"
    assert reason == "no port given (is the board plugged in?)"


def test_a_windows_com_name_is_not_called_missing(tmp_path):
    """COM3 is never a path, so it never "exists": the existence test is
    only for /dev-style names, and a COM port keeps the old reason."""
    etools = tmp_path / "etools"
    _stub(etools, summary_rc=2, help_rc=0)
    log = tmp_path / "tools.log"
    log.write_text("", encoding="utf-8")

    state, reason, _ = _probe(etools, log, port="COM3")

    assert state == "unknown"
    assert reason.startswith("espefuse.py could not read COM3 -- is the port free?")


def test_a_working_tool_still_reads_the_chip(tmp_path):
    etools = tmp_path / "etools"
    etools.mkdir()
    shutil.copy(WORKING_STUB, etools / "espefuse.py")
    (etools / "espefuse.py").chmod(0o755)
    log = tmp_path / "tools.log"
    log.write_text("", encoding="utf-8")

    state, reason, _ = _probe(etools, log)

    assert (state, reason) == ("plaintext", "")
    # One call: a working tool is not asked to start first.
    calls = _calls(log)
    assert len(calls) == 1 and calls[0].endswith("summary")
