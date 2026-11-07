"""flash_encrypted.sh --app-only writes slot 0 and nothing else.

Run against fake espsecure.py / esptool.py / python in a directory handed over
as BLINK_ETOOLS (PATH is not consulted), so no board and no key is touched.
Each fake appends its arguments to a log. Build tree and key are generated here.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "flash_encrypted.sh"

pytestmark = pytest.mark.skipif(
    os.name == "nt" or shutil.which("bash") is None, reason="POSIX bash script")


def _setup(tmp_path, config="# CONFIG_X is not set\n"):
    et = tmp_path / "etools"
    et.mkdir()
    log = tmp_path / "log"
    for name in ("espsecure.py", "esptool.py"):
        f = et / name
        f.write_text(f'#!/bin/sh\necho "{name} $*" >> "{log}"\n'
                     'while [ $# -gt 0 ]; do [ "$1" = -o ] && : > "$2"; shift; done\n')
        f.chmod(0o755)
    py = et / "fakepy"
    py.write_text('#!/bin/sh\nwhile [ $# -gt 0 ]; do [ "$1" = --out ] && : > "$2"; shift; done\n')
    py.chmod(0o755)
    b = tmp_path / "build"
    (b / "mcuboot" / "zephyr").mkdir(parents=True)
    (b / "firmware" / "zephyr").mkdir(parents=True)
    (b / "mcuboot" / "zephyr" / "zephyr.bin").write_bytes(b"m")
    (b / "firmware" / "zephyr" / "zephyr.signed.bin").write_bytes(b"a")
    (b / "firmware" / "zephyr" / ".config").write_text(config)
    key = tmp_path / "key.bin"
    key.write_bytes(b"k" * 32)
    env = dict(os.environ, BLINK_ETOOLS=str(et), BLINK_BUILD_DIR=str(b),
               BLINK_FLASH_KEY=str(key), BLINK_SKIP_EFUSE_CHECK="1",
               BLINK_PYTHON=str(py), HOME=str(tmp_path))
    return env, log


def _run(env, *args, port=True):
    args = (*args, "/dev/null") if port else args
    return subprocess.run(["bash", str(SCRIPT), *args], env=env,
                          capture_output=True, text=True, timeout=60)


def _log(log):
    return log.read_text().splitlines() if log.exists() else []


def test_app_only_writes_only_0x20000(tmp_path):
    env, log = _setup(tmp_path)
    r = _run(env, "--app-only")
    assert r.returncode == 0, r.stderr
    lines = _log(log)
    assert not any("erase_region" in l for l in lines)
    assert not any("0x1000" in l for l in lines)
    writes = [l for l in lines if "write_flash" in l]
    assert len(writes) == 1 and "0x20000" in writes[0]
    assert sum("encrypt_flash_data" in l for l in lines) == 1


def test_default_still_writes_both_and_erases_the_logo(tmp_path):
    env, log = _setup(tmp_path)
    r = _run(env)
    assert r.returncode == 0, r.stderr
    lines = _log(log)
    assert any("erase_region" in l for l in lines)
    w = [l for l in lines if "write_flash" in l][0]
    assert "0x1000" in w and "0x20000" in w


@pytest.mark.parametrize("cfg", ["CONFIG_BLINK_TEST_CONFIRM_FAILS=y\n",
                                 "CONFIG_BLINK_TEST_NEVER_HEALTHY=y\n",
                                 "CONFIG_BLINK_TEST_HANG_AFTER_BOOT=y\n",
                                 "CONFIG_BLINK_TOUCH_INJECT=y\n"])
@pytest.mark.parametrize("flag", [[], ["--app-only"]])
def test_knob_or_injector_refused_before_any_tool_runs(tmp_path, cfg, flag):
    env, log = _setup(tmp_path, cfg)
    r = _run(env, *flag)
    assert r.returncode != 0 and "FATAL" in r.stderr
    assert _log(log) == []


@pytest.mark.parametrize("args", [["/dev/null", "--app-only"],
                                  ["--app-only", "/dev/null", "extra"],
                                  ["--bogus", "/dev/null"]])
def test_an_option_after_the_port_or_unknown_is_refused_not_dropped(tmp_path, args):
    env, log = _setup(tmp_path)
    r = _run(env, *args, port=False)
    assert r.returncode != 0 and "FATAL" in r.stderr
    assert _log(log) == []


def test_app_only_still_refuses_a_plaintext_chip_before_any_write(tmp_path):
    env, log = _setup(tmp_path)
    env.pop("BLINK_SKIP_EFUSE_CHECK")
    e = Path(env["BLINK_ETOOLS"]) / "espefuse.py"
    e.write_text("#!/bin/sh\necho 'FLASH_CRYPT_CNT (BLOCK0) 0b000 R/W (0b000)'\n")
    e.chmod(0o755)
    r = _run(env, "--app-only")
    assert r.returncode != 0 and "NOT enabled" in r.stderr
    assert not any(w in l for l in _log(log)
                   for w in ("write_flash", "encrypt_flash_data", "erase_region"))
