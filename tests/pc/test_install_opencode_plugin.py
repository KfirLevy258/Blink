"""Putting Blink's plugin into OpenCode's plugin directory, and never anyone else's.

Every function under test takes OpenCode's config directory as a parameter, so
nothing here depends on XDG_CONFIG_HOME or on a real ~/.config/opencode: each
test hands it a directory under tmp_path. tests/conftest.py redirects HOME as
well, which covers anything that would resolve ~ regardless.
"""
import os
import sys

import pytest

from pc import install_opencode_plugin as iop

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SOURCE = os.path.join(ROOT, "tools", iop.SOURCE_NAME)


def _source_text():
    with open(SOURCE, encoding="utf-8") as f:
        return f.read()


def _read(path):
    with open(path, "rb") as f:
        return f.read()


@pytest.fixture
def cfg(tmp_path):
    return str(tmp_path / "xdg" / "opencode")


def test_the_shipped_plugin_starts_with_the_marker():
    # The installer's whole notion of ownership. A reworded first line would
    # make every installed copy look like somebody else's file.
    assert _source_text().startswith(iop.MARKER)
    assert _source_text().split("\n", 1)[0].startswith(iop.MARKER + " ")


def test_plugin_path_is_singular_plugin_dir(cfg):
    assert iop.plugin_path(cfg) == os.path.join(cfg, "plugin", "blink.js")


def test_install_creates_directories_and_writes_the_bundle(cfg):
    msg = iop.install(cfg, _source_text())
    assert "installed" in msg
    assert _read(iop.plugin_path(cfg)) == _source_text().encode("utf-8")
    assert iop.is_current(cfg, _source_text())


def test_install_accepts_a_path_to_the_source(cfg):
    iop.install(cfg, SOURCE)
    assert iop.is_current(cfg, _source_text())


def test_install_is_idempotent(cfg):
    iop.install(cfg, _source_text())
    before = os.stat(iop.plugin_path(cfg))
    msg = iop.install(cfg, _source_text())
    assert msg == "OpenCode plugin already installed."
    after = os.stat(iop.plugin_path(cfg))
    # Not rewritten at all, not merely rewritten with the same bytes.
    assert (before.st_ino, before.st_mtime_ns) == (after.st_ino, after.st_mtime_ns)


def test_install_updates_a_stale_copy_of_ours(cfg):
    path = iop.plugin_path(cfg)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write(iop.MARKER + " an older release\nexport const X = 1\n")
    assert not iop.is_current(cfg, _source_text())
    assert "updated" in iop.install(cfg, _source_text())
    assert iop.is_current(cfg, _source_text())


def test_install_writes_lf_only(cfg):
    iop.install(cfg, _source_text())
    assert b"\r" not in _read(iop.plugin_path(cfg))


def test_install_leaves_no_temp_behind(cfg):
    iop.install(cfg, _source_text())
    assert os.listdir(os.path.dirname(iop.plugin_path(cfg))) == ["blink.js"]


@pytest.mark.parametrize("foreign", [
    b"export const Mine = async () => ({})\n",
    b"",
    b"\n// blink-opencode-plugin: on line two is not line one\n",
    b"\xff\xfe not even utf-8",
])
def test_install_refuses_someone_elses_blink_js(cfg, foreign):
    path = iop.plugin_path(cfg)
    os.makedirs(os.path.dirname(path))
    with open(path, "wb") as f:
        f.write(foreign)
    msg = iop.install(cfg, _source_text())
    assert "not Blink's" in msg and "left it alone" in msg
    assert _read(path) == foreign


def test_install_refuses_a_directory_named_blink_js(cfg):
    os.makedirs(iop.plugin_path(cfg))
    msg = iop.install(cfg, _source_text())
    assert "directory" in msg
    assert os.path.isdir(iop.plugin_path(cfg))


def test_install_refuses_a_source_without_the_marker(cfg, tmp_path):
    other = tmp_path / "other.js"
    other.write_text("export const X = 1\n", encoding="utf-8")
    msg = iop.install(cfg, str(other))
    assert "marker" in msg
    assert not os.path.exists(iop.plugin_path(cfg))


@pytest.mark.parametrize("source", [None, "", "/no/such/file.js", 42])
def test_install_reports_an_unreadable_source(cfg, source):
    msg = iop.install(cfg, source)
    assert msg.startswith("OpenCode plugin not installed")
    assert not os.path.exists(iop.plugin_path(cfg))


def test_install_never_raises_when_the_plugin_dir_is_a_file(cfg):
    os.makedirs(cfg)
    with open(os.path.join(cfg, "plugin"), "w") as f:
        f.write("not a directory")
    msg = iop.install(cfg, _source_text())
    assert "could not be written" in msg


@pytest.mark.skipif(sys.platform == "win32" or (hasattr(os, "geteuid")
                                                and os.geteuid() == 0),
                    reason="POSIX permissions, and root ignores them")
def test_install_never_raises_on_an_unwritable_config_dir(cfg):
    os.makedirs(cfg)
    os.chmod(cfg, 0o500)
    try:
        msg = iop.install(cfg, _source_text())
        assert "could not be written" in msg
        assert iop.uninstall(cfg) == "No OpenCode plugin to remove."
    finally:
        os.chmod(cfg, 0o700)


@pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege")
def test_install_writes_through_a_symlink(cfg, tmp_path):
    real = tmp_path / "dotfiles" / "blink.js"
    real.parent.mkdir()
    real.write_text(iop.MARKER + " old\n", encoding="utf-8")
    os.makedirs(os.path.join(cfg, "plugin"))
    os.symlink(str(real), iop.plugin_path(cfg))
    iop.install(cfg, _source_text())
    assert os.path.islink(iop.plugin_path(cfg))
    assert real.read_text(encoding="utf-8") == _source_text()


def test_is_current_false_when_absent_or_different(cfg):
    assert not iop.is_current(cfg, _source_text())
    iop.install(cfg, _source_text())
    assert not iop.is_current(cfg, _source_text() + "\n")
    assert not iop.is_current(cfg, None)


def test_uninstall_removes_ours(cfg):
    iop.install(cfg, _source_text())
    msg = iop.uninstall(cfg)
    assert "removed" in msg
    assert not os.path.exists(iop.plugin_path(cfg))
    # The directory is OpenCode's, and stays.
    assert os.path.isdir(os.path.dirname(iop.plugin_path(cfg)))


def test_uninstall_also_removes_ours_from_plugins_plural(cfg):
    plural = os.path.join(cfg, "plugins", "blink.js")
    os.makedirs(os.path.dirname(plural))
    with open(plural, "w", encoding="utf-8") as f:
        f.write(_source_text())
    iop.uninstall(cfg)
    assert not os.path.exists(plural)


def test_uninstall_leaves_someone_elses_file(cfg):
    path = iop.plugin_path(cfg)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write("export const Mine = 1\n")
    msg = iop.uninstall(cfg)
    assert "not Blink's" in msg
    assert os.path.exists(path)


def test_uninstall_with_nothing_there(cfg):
    assert iop.uninstall(cfg) == "No OpenCode plugin to remove."
    assert not os.path.exists(cfg)


def test_uninstall_never_raises_on_a_directory_named_blink_js(cfg):
    os.makedirs(iop.plugin_path(cfg))
    assert "not Blink's" in iop.uninstall(cfg)
    assert os.path.isdir(iop.plugin_path(cfg))


def test_refresh_only_touches_an_existing_copy_of_ours(cfg, tmp_path):
    # Nothing there: an update is not the command that installs.
    assert iop.refresh(cfg, _source_text()) is None
    assert not os.path.exists(iop.plugin_path(cfg))
    # Somebody else's: never. newline="\n" because the comparison below is
    # of bytes: a text-mode write on Windows turned this \n into \r\n, and
    # the test failed there (CI, 2026-10-19) on its own fixture, not on
    # anything refresh() did.
    path = iop.plugin_path(cfg)
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("export const Mine = 1\n")
    assert iop.refresh(cfg, _source_text()) is None
    assert _read(path) == b"export const Mine = 1\n"
    # Ours and stale: rewritten. Ours and current: left.
    with open(path, "w", encoding="utf-8") as f:
        f.write(iop.MARKER + " old\n")
    assert "updated" in iop.refresh(cfg, _source_text())
    assert iop.refresh(cfg, _source_text()) is None


def test_announce_names_the_file_and_what_is_recorded(cfg):
    text = iop.announce(cfg)
    assert iop.plugin_path(cfg) in text
    assert "state-opencode" in text
    assert "Nothing of your prompts" in text
    assert "blink uninstall" in text


def test_windows_style_config_dir(tmp_path):
    # A config dir with backslashes and a space survives the round trip on
    # whatever platform runs this; on Windows it is a real path shape.
    cfg = str(tmp_path / "Users" / "a b" / ".config" / "opencode")
    iop.install(cfg, _source_text())
    assert iop.is_current(cfg, _source_text())
    assert "removed" in iop.uninstall(cfg)
