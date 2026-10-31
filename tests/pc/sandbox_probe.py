"""Run by tests/test_sandbox.py in a child pytest started inside tests/pc.

Named so the main run never collects it: it is handed to pytest by path.
It passes only if tests/conftest.py's sandbox applied, i.e. HOME is not the
account's real home. It reads the environment and writes nothing.
"""
import os
import pwd


def test_the_sandbox_moved_home():
    real = pwd.getpwuid(os.getuid()).pw_dir
    assert os.environ["HOME"] != real, "HOME is the real home: no sandbox"
    assert os.environ.get("BLINK_SKIP_SERVICE") == "1"
