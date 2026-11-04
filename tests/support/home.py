"""The check a test helper makes before it writes where our code will look.

A helper that leaves a file for the code under test -- a pid file, a plist, a
Claude Desktop cache -- has to put it where that code reads, so it asks the
code: cli.pid_path(), claude_desktop.cache_path(). Those resolve ~ when
called, which is tmp_path under pytest (tests/conftest.py) and the real home
anywhere else. On 2026-10-27 a scratch script imported
tests.pc.test_hello_burst and called its helper, which found ~/.blink on its
own and wrote a test theme into the developer's real theme.json.

So a writing helper takes its home from the caller, and runs the path it
was handed through inside() first. Under pytest the two agree. In a script
that never moved HOME they do not, and the helper stops before it writes.
"""
import os


def inside(home, path):
    """`path`, once it is known to lie under `home`; AssertionError if not."""
    root = os.path.normcase(os.path.realpath(str(home)))
    where = os.path.normcase(os.path.realpath(str(path)))
    try:
        common = os.path.commonpath([root, where])
    except ValueError:          # Windows: two different drives
        common = None
    if common != root:
        raise AssertionError(
            f"test helper asked to write outside the caller's home "
            f"({home}): {path}. Is HOME still the real one?")
    return str(path)
