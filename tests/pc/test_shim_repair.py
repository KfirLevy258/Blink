"""The installed shims repair themselves when they fall behind the daemon.

`blink update` swaps the program directory and restarts the service. It has
never rewritten ~/.blink/blink-hook.sh, and nothing else did either, so a
customer upgrading the documented way ran a new daemon that reads `name` out
of the state files against an old shim that never writes one. The board was
never named, `blink status` said "hooks installed (10/10 events)" because the
path existed and still ran, and nothing anywhere said why.
"""
import os

from pc import cli


def _write(path, text):
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def test_a_shim_matching_the_bundle_is_current(tmp_path):
    p = str(tmp_path / "blink-hook.sh")
    _write(p, cli._shim_source("blink-hook.sh"))

    assert cli.shim_is_current(p, "blink-hook.sh") is True


def test_an_older_shim_is_not_current(tmp_path):
    """The real defect: same path, same name, older contents."""
    p = str(tmp_path / "blink-hook.sh")
    _write(p, '#!/bin/sh\n# a shim from before this feature existed\n')

    assert cli.shim_is_current(p, "blink-hook.sh") is False


def test_a_missing_shim_is_not_current(tmp_path):
    p = str(tmp_path / "does-not-exist.sh")

    assert cli.shim_is_current(p, "blink-hook.sh") is False


def test_an_unreadable_shim_is_not_current(tmp_path):
    """Answer the question rather than raising it.

    This runs inside the daemon's poll loop. "I cannot read it" and "it is
    stale" lead to the same action -- rewrite it -- and a raise here would
    take the daemon down over a file permission.
    """
    p = str(tmp_path / "blink-hook.sh")
    _write(p, cli._shim_source("blink-hook.sh"))
    os.chmod(p, 0o000)
    try:
        assert cli.shim_is_current(p, "blink-hook.sh") is False
    finally:
        os.chmod(p, 0o644)


def test_line_endings_alone_make_a_shim_stale(tmp_path):
    """CRLF is not a cosmetic difference on this file.

    cli._write_shim writes newline="\\n" because Windows wrote the shim with
    CRLF and Git Bash then failed on `case ... in\\r` at every status line
    render and every tool call. A shim that differs ONLY by line ending is
    the exact shape of that bug, so it must read as stale and be rewritten.
    """
    p = str(tmp_path / "blink-hook.sh")
    with open(p, "w", encoding="utf-8", newline="\r\n") as f:
        f.write(cli._shim_source("blink-hook.sh"))

    assert cli.shim_is_current(p, "blink-hook.sh") is False
