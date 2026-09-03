"""The installed shims repair themselves when they fall behind the daemon.

`blink update` swaps the program directory and restarts the service. It has
never rewritten ~/.blink/blink-hook.sh, and nothing else did either, so a
customer upgrading the documented way ran a new daemon that reads `name` out
of the state files against an old shim that never writes one. The board was
never named, `blink status` said "hooks installed (10/10 events)" because the
path existed and still ran, and nothing anywhere said why.
"""
import ast
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


from pc import install_statusline


def test_a_stale_shim_is_rewritten(tmp_path, monkeypatch):
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")

    what = install_statusline.shim_content_check([(str(p), "blink-hook.sh")])

    assert what is not None
    assert "blink-hook.sh" in what
    assert p.read_text(encoding="utf-8") == cli._shim_source("blink-hook.sh")


def test_a_current_shim_is_left_alone(tmp_path, monkeypatch):
    """Silence is the normal case -- this runs every 300 seconds forever."""
    p = tmp_path / "blink-hook.sh"
    _write(str(p), cli._shim_source("blink-hook.sh"))
    before = p.stat().st_mtime_ns
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")

    assert install_statusline.shim_content_check([(str(p), "blink-hook.sh")]) is None
    assert p.stat().st_mtime_ns == before


def test_no_marker_means_hands_off(tmp_path, monkeypatch):
    """An uninstalled machine is not a broken one.

    Same rule drift_check states: a missing marker means the user uninstalled,
    and that is never overridden. Without this, `blink uninstall` would be
    undone by the next tick of a daemon that had not exited yet.
    """
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "")

    assert install_statusline.shim_content_check([(str(p), "blink-hook.sh")]) is None
    assert p.read_text(encoding="utf-8") == "#!/bin/sh\n# old\n"


def test_the_disable_switch_is_honoured(tmp_path, monkeypatch):
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")
    monkeypatch.setenv(install_statusline.WATCHDOG_DISABLE_ENV, "1")

    assert install_statusline.shim_content_check([(str(p), "blink-hook.sh")]) is None


def test_an_unwritable_shim_reports_and_does_not_raise(tmp_path, monkeypatch):
    """The daemon survives a repair it cannot perform.

    _write_shim() opens the existing path for "w" and truncates it in place
    -- it never unlinks or renames -- so a read-only *directory* (os.chmod(d,
    0o500)) does not stop the write: POSIX only consults directory
    permissions for adding/removing/renaming entries, not for rewriting the
    bytes of a file that already exists and is itself writable. Verified on
    this machine (uid 502, not root) before trusting the assertion: the
    write succeeds regardless of the directory's mode. The directory chmod
    is kept below because a real deployment could plausibly have either
    fail, but the file's own mode is what actually has to be revoked to
    reproduce "the daemon cannot write this shim".
    """
    d = tmp_path / "ro"
    d.mkdir()
    p = d / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    os.chmod(p, 0o400)
    os.chmod(d, 0o500)
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")
    try:
        what = install_statusline.shim_content_check([(str(p), "blink-hook.sh")])
        assert what is not None
        assert "could not" in what
    finally:
        os.chmod(d, 0o700)
        os.chmod(p, 0o600)


def test_a_second_stale_shim_is_also_repaired(tmp_path, monkeypatch):
    """Both shims, one pass. The statusline shim has the same failure mode."""
    a = tmp_path / "blink-hook.sh"
    b = tmp_path / "blink-statusline.sh"
    _write(str(a), "#!/bin/sh\n# old\n")
    _write(str(b), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(install_statusline, "_read_marker", lambda: "installed")

    what = install_statusline.shim_content_check(
        [(str(a), "blink-hook.sh"), (str(b), "blink-statusline.sh")])

    assert what is not None
    assert a.read_text(encoding="utf-8") == cli._shim_source("blink-hook.sh")
    assert b.read_text(encoding="utf-8") == cli._shim_source("blink-statusline.sh")


# --- the wiring, and what `blink status` says before it heals -------------


def test_the_daemon_hands_the_watchdog_both_shims():
    """A guard against the exact regression this plan exists to fix.

    The feature was dead because one call site was missing, and every test
    passed anyway. Read the daemon's source and assert the wiring, because
    the alternative is a test that constructs its own watchdog and proves
    only that the constructor works -- which is what let the original gap
    through.
    """
    src = open("claude_usage_bridge.py", encoding="utf-8").read()
    tree = ast.parse(src)

    calls = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and isinstance(n.func, ast.Attribute)
             and n.func.attr == "DriftWatchdog"]
    assert len(calls) == 1, "expected exactly one watchdog"

    kwargs = {k.arg for k in calls[0].keywords}
    assert "shims" in kwargs, (
        "the daemon builds a DriftWatchdog without shims=, so the hook shim "
        "is never checked and the naming feature is dead on every install "
        "that arrived by `blink update`")


def test_status_reports_a_stale_shim(tmp_path, monkeypatch):
    """`blink status` said "hooks installed (10/10 events)" throughout.

    It compares the command strings in settings.json against the shim path.
    The path existed and still ran; only its contents were stale, so the one
    place a user would look reported health.
    """
    p = tmp_path / "blink-hook.sh"
    _write(str(p), "#!/bin/sh\n# old\n")
    monkeypatch.setattr(cli, "hook_shim_path", lambda: str(p))

    assert cli.hook_shim_status_note() == "the activity hook shim is out of date"


def test_status_is_quiet_when_the_shim_is_current(tmp_path, monkeypatch):
    p = tmp_path / "blink-hook.sh"
    _write(str(p), cli._shim_source("blink-hook.sh"))
    monkeypatch.setattr(cli, "hook_shim_path", lambda: str(p))

    assert cli.hook_shim_status_note() is None
