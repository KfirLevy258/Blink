"""tools/release.sh refuses an interpreter it cannot use, before it is used.

Its Python steps -- the fleet gate, the manifest, the signature check -- run
under BLINK_PYTHON, else python3. A wrong choice used to surface late and
under the wrong name: as "the fleet gate refused" when the gate never ran,
or, with BLINK_SKIP_FLEET=1, only after the build and the draft release. The
gate needs 3.11 (tomllib); the BLINK_SKIP_FLEET=1 path has no gate and must
not be refused for want of one.

These never run a release. The script is COPIED into tmp_path with only a
version.h beside it. Past the probe, the gate path dies on a pc/ that is not
there, and the skip path dies at its next step, looking for the release tag
on a remote URL that names a directory which does not exist. So nothing past
the probe can do anything real, whatever it decides.
"""
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None,
    reason="needs bash, and a #!/bin/sh stand-in it can exec")


def _python39(tmp_path, name="python3.9"):
    """Answers the version question as 3.9, passes a floor of 3.9, and fails
    anything else -- a higher floor, or `import tomllib` -- the way
    /usr/bin/python3 on macOS does."""
    bin_dir = tmp_path / "py39"
    bin_dir.mkdir(exist_ok=True)
    fake = bin_dir / name
    fake.write_text('#!/bin/sh\ncase "$*" in\n'
                    '  *print*) echo 3.9; exit 0 ;;\n'
                    '  *"(3, 9)"*) exit 0 ;;\n'
                    'esac\nexit 1\n', encoding="utf-8")
    fake.chmod(0o755)
    return fake


def _release(tmp_path, python=None, skip_fleet=False, path_prefix=None,
             fleet=None):
    repo = tmp_path / "repo"
    (repo / "tools").mkdir(parents=True)
    (repo / "firmware" / "src").mkdir(parents=True)
    (repo / "firmware" / "src" / "version.h").write_text(
        '#define BLINK_FW_VERSION "9.9.9"\n', encoding="utf-8")
    shutil.copy(ROOT / "tools" / "release.sh", repo / "tools" / "release.sh")
    env = dict(os.environ)
    for leak in ("BLINK_SKIP_FLEET", "BLINK_PYTHON", "BASH_ENV", "ENV",
                 "OTA_REPO", "OTA_REPO_URL", "BLINK_FLEET"):
        env.pop(leak, None)
    env["HOME"] = str(tmp_path)
    env["OTA_REPO_URL"] = str(tmp_path / "no-such-remote")
    if python is not None:
        env["BLINK_PYTHON"] = str(python)
    if skip_fleet:
        env["BLINK_SKIP_FLEET"] = "1"
    if fleet is not None:
        env["BLINK_FLEET"] = fleet
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}:{env['PATH']}"
    return subprocess.run(["bash", str(repo / "tools" / "release.sh")],
                          env=env, cwd=str(tmp_path), capture_output=True,
                          text=True, timeout=30)


def test_an_old_python_is_refused_before_the_gate(tmp_path):
    fake = _python39(tmp_path)
    r = _release(tmp_path, fake)
    assert r.returncode == 1
    assert f"{fake} is Python 3.9." in r.stderr and "3.11" in r.stderr
    assert "the fleet gate refused" not in r.stderr


def test_a_python_that_does_not_exist_is_refused_before_the_gate(tmp_path):
    missing = tmp_path / "no-such-python"
    r = _release(tmp_path, missing)
    assert r.returncode == 1
    assert f"{missing} is not an interpreter that runs." in r.stderr
    assert "the fleet gate refused" not in r.stderr


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="needs this interpreter to be one release.sh takes")
def test_a_good_python_reaches_the_gate(tmp_path):
    """The gate then refuses, because the bare copy has no pc/ -- which is
    the proof the probe let it through."""
    r = _release(tmp_path, sys.executable)
    assert r.returncode == 1
    assert "needs Python" not in r.stderr
    assert "the fleet gate refused" in r.stderr


def test_skipping_the_gate_does_not_demand_its_python(tmp_path):
    """BLINK_SKIP_FLEET=1 with a 3.9 python3 on PATH and no BLINK_PYTHON:
    nothing on that path needs tomllib, so nothing may refuse over it."""
    fake = _python39(tmp_path, name="python3")
    r = _release(tmp_path, skip_fleet=True, path_prefix=fake.parent)
    assert "needs Python" not in r.stderr
    assert "is not on the remote" in r.stderr


def test_skipping_the_gate_still_refuses_a_named_python_that_does_not_run(
        tmp_path):
    missing = tmp_path / "no-such-python"
    r = _release(tmp_path, missing, skip_fleet=True)
    assert r.returncode == 1
    assert f"{missing} is not an interpreter that runs." in r.stderr
    assert "is not on the remote" not in r.stderr


def test_skipping_the_gate_takes_a_named_3_9(tmp_path):
    fake = _python39(tmp_path)
    r = _release(tmp_path, fake, skip_fleet=True)
    assert "needs Python" not in r.stderr
    assert "is not on the remote" in r.stderr


@pytest.mark.skipif(sys.version_info < (3, 11),
                    reason="needs this interpreter to be one release.sh takes")
def test_the_vm_fleet_is_asked_for_by_name_and_announced(tmp_path):
    r = _release(tmp_path, sys.executable, fleet="vm")
    assert "NOTE: BLINK_FLEET=vm" in r.stderr
    assert "not Windows 10 on x86" in r.stderr
    assert "the fleet gate refused" in r.stderr
    assert "--inventory tools/fleet/fleet-vm.toml" in r.stderr


def test_a_fleet_nobody_committed_is_refused_before_the_gate(tmp_path):
    """Only the two committed inventories can be named: a path would let
    any file with one desk in it vouch for a release."""
    r = _release(tmp_path, sys.executable, fleet="tools/one-desk.toml")
    assert r.returncode == 1
    assert "it is desks or vm" in r.stderr
    assert "the fleet gate refused" not in r.stderr
