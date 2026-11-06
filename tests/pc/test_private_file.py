"""Owner-only files, kept by handle (spec 7.3, D48, D53).

The POSIX half runs for real on macOS and Linux. The Windows half is driven
on every platform through a fake `win` surface and a monkeypatched os.name,
so the order of calls -- create with the descriptor, validate the handle,
only then write -- is pinned here; the ctypes underneath it is proved only
by the three windows_real tests, on a Windows machine.
"""
import os
import stat
import sys

import pytest

from pc import private_file
from pc.private_file import NotPrivate

SID = "S-1-5-21-1111111111-2222222222-3333333333-1001"
OTHER = "S-1-5-21-1111111111-2222222222-3333333333-1002"
FILE_DACL = "D:P(A;;FA;;;%s)" % SID
DIR_DACL = "D:PAI(A;OICI;FA;;;%s)" % SID


def _home(tmp_path):
    return str(tmp_path / ".blink")


# ---------------------------------------------------------------- POSIX


posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX modes")


@posix_only
def test_posix_dir_is_0700_and_verified_each_call(tmp_path):
    home = _home(tmp_path)
    d = private_file.private_dir(home)
    st = os.stat(d)
    assert stat.S_IMODE(st.st_mode) == 0o700
    assert private_file.private_dir(home) == d      # EEXIST, re-validated
    os.chmod(d, 0o755)
    with pytest.raises(NotPrivate) as e:
        private_file.private_dir(home)
    assert str(e.value) == "private directory not owner-only"
    os.chmod(d, 0o700)
    os.rmdir(d)
    os.symlink(str(tmp_path), d)
    with pytest.raises(NotPrivate):
        private_file.private_dir(home)


@posix_only
def test_posix_mode_is_0600_from_creation(tmp_path, monkeypatch):
    """Created at 0600, never chmod'ed there: under umask 0 a create-then-
    chmod would have a window at 0666."""
    seen = []
    real_open = os.open

    def spy(path, flags, mode=0o777, *a, **kw):
        if str(path).endswith(".tmp"):
            seen.append((flags, mode))
        return real_open(path, flags, mode, *a, **kw)

    def no_chmod(*a, **kw):
        raise AssertionError("tightened after creation")

    monkeypatch.setattr(private_file.os, "open", spy)
    monkeypatch.setattr(private_file.os, "chmod", no_chmod)
    monkeypatch.setattr(private_file.os, "fchmod", no_chmod)
    old = os.umask(0)
    try:
        path = private_file.write_private(_home(tmp_path), "f.json", b"x")
    finally:
        os.umask(old)
    flags, mode = seen[0]
    for f in (os.O_CREAT, os.O_EXCL, os.O_WRONLY, os.O_NOFOLLOW):
        assert flags & f
    assert mode == 0o600
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600


@posix_only
def test_posix_refuses_symlink_wrong_owner_and_wide_mode(tmp_path,
                                                        monkeypatch):
    home = _home(tmp_path)
    path = private_file.write_private(home, "f.json", b"secret")
    assert private_file.open_private(home, "f.json") == b"secret"

    os.chmod(path, 0o644)
    with pytest.raises(NotPrivate) as e:
        private_file.open_private(home, "f.json")
    assert str(e.value) == "not owner-only"
    os.chmod(path, 0o600)

    elsewhere = tmp_path / "elsewhere"
    elsewhere.write_bytes(b"planted")
    os.symlink(str(elsewhere), os.path.join(os.path.dirname(path), "l.json"))
    with pytest.raises(NotPrivate) as e:
        private_file.open_private(home, "l.json")
    assert str(e.value) == "not a regular file"

    os.mkfifo(os.path.join(os.path.dirname(path), "p.json"))
    with pytest.raises(NotPrivate):     # and did not block on the FIFO
        private_file.open_private(home, "p.json")

    real_fstat = os.fstat

    def foreign(fd):
        st = real_fstat(fd)
        if stat.S_ISREG(st.st_mode):
            return os.stat_result((st.st_mode, st.st_ino, st.st_dev,
                                   st.st_nlink, st.st_uid + 1) + tuple(st)[5:])
        return st

    monkeypatch.setattr(private_file.os, "fstat", foreign)
    with pytest.raises(NotPrivate) as e:
        private_file.open_private(home, "f.json")
    assert str(e.value) == "wrong owner"


@posix_only
def test_write_is_atomic(tmp_path, monkeypatch):
    home = _home(tmp_path)
    path = private_file.write_private(home, "f.json", b"one")

    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(private_file.os, "replace", fail)
    with pytest.raises(OSError):
        private_file.write_private(home, "f.json", b"two")
    monkeypatch.undo()
    assert private_file.open_private(home, "f.json") == b"one"
    assert os.listdir(os.path.dirname(path)) == ["f.json"], "temp left"


def test_permission_denied_is_not_absent(tmp_path, monkeypatch):
    home = _home(tmp_path)
    if os.name == "posix":
        assert private_file.open_private(home, "f.json") is None
        real_open = os.open

        def denied(path, flags, *a, **kw):
            if path == "f.json":
                raise PermissionError(13, "denied")
            return real_open(path, flags, *a, **kw)

        monkeypatch.setattr(private_file.os, "open", denied)
        with pytest.raises(NotPrivate) as e:
            private_file.open_private(home, "f.json")
        assert str(e.value) == "acl check failed"
        monkeypatch.undo()

    win = FakeWin()
    _as_windows(monkeypatch)
    assert private_file.open_private(home, "f.json", win=win) is None
    win.open_error = PermissionError(13, "denied")
    with pytest.raises(NotPrivate) as e:
        private_file.open_private(home, "f.json", win=win)
    assert str(e.value) == "acl check failed"


# -------------------------------------------------------------- Windows


class FakeWin:
    """The `win` surface, recording every call. Handles are ints; each
    holds the facts validate_handle judges (through the real check)."""

    def __init__(self):
        self.log = []
        self.dirs = {}            # path -> (sddl, attrs)
        self.files = {}           # path -> bytes
        self.handles = {}         # h -> dict
        self.next = 100
        self.open_error = None
        self.rename_error = None
        self.facts = None         # override for the next validated handle

    def user_sid(self):
        return SID

    def create_dir_with_sddl(self, path, sddl):
        self.log.append(("create_dir_with_sddl", path, sddl))
        if path in self.dirs:
            return False
        owner = sddl.split("G:")[0]
        self.dirs[path] = (owner + "D:" + sddl.split("D:")[1], 0x10)
        return True

    def dir_facts(self, path):
        self.log.append(("dir_facts", path))
        return self.dirs[path]

    def _new_handle(self, path, sddl):
        self.next += 1
        owner = sddl.split("G:")[0]
        self.handles[self.next] = {
            "path": path, "sddl": owner + "D:" + sddl.split("D:")[1],
            "attrs": 0x80, "final": "\\\\?\\" + path, "data": b""}
        return self.next

    def create_new(self, path, sddl):
        self.log.append(("create_new", path))
        if path in self.files:
            raise FileExistsError(80, "exists")
        self.files[path] = b""
        return self._new_handle(path, sddl)

    def open_existing(self, path):
        self.log.append(("open_existing", path))
        if self.open_error:
            raise self.open_error
        if path not in self.files:
            raise FileNotFoundError(2, "absent")
        h = self._new_handle(path, "O:%sG:%sD:P(A;;FA;;;%s)" % (SID, SID, SID))
        self.handles[h]["data"] = self.files[path]
        return h

    def validate_handle(self, h, expect_dir):
        self.log.append(("validate_handle", h))
        f = self.handles[h]
        if self.facts:
            f.update(self.facts)
        private_file.check_facts(f["sddl"], f["attrs"], f["final"],
                                 expect_dir, SID)

    def write(self, h, data):
        self.log.append(("write", h))
        self.handles[h]["data"] += data
        self.files[self.handles[h]["path"]] = self.handles[h]["data"]

    def read(self, h):
        self.log.append(("read", h))
        return self.handles[h]["data"]

    def rename_by_handle(self, h, final):
        self.log.append(("rename_by_handle", h))
        if self.rename_error:
            raise self.rename_error
        src = self.handles[h]["path"]
        self.files[final] = self.files.pop(src)
        self.handles[h]["path"] = final

    def delete_by_handle(self, h):
        self.log.append(("delete_by_handle", h))
        self.files.pop(self.handles[h]["path"], None)

    def close(self, h):
        self.log.append(("close", h))

    def calls(self):
        return [c[0] for c in self.log]


def _as_windows(monkeypatch):
    monkeypatch.setattr(private_file.os, "name", "nt")
    monkeypatch.setattr(private_file, "_join", lambda *p: "\\".join(p))
    monkeypatch.setattr(private_file, "_makedirs", lambda p: None)


@pytest.fixture
def win(monkeypatch):
    _as_windows(monkeypatch)
    return FakeWin()


HOME = "C:\\Users\\k\\.blink"
PDIR = HOME + "\\private"


def test_windows_dir_is_created_with_its_descriptor_not_tightened_after(win):
    assert private_file.private_dir(HOME, win=win) == PDIR
    assert win.log == [
        ("create_dir_with_sddl", PDIR,
         "O:%sG:%s%s" % (SID, SID, DIR_DACL)),
        ("dir_facts", PDIR)]


def test_windows_existing_dir_with_another_owner_is_refused(win):
    win.dirs[PDIR] = ("O:%s%s" % (OTHER, DIR_DACL), 0x10)
    with pytest.raises(NotPrivate) as e:
        private_file.private_dir(HOME, win=win)
    assert str(e.value) == "private directory not owner-only"
    with pytest.raises(NotPrivate):
        private_file.write_private(HOME, "f.json", b"k", win=win)
    assert "create_new" not in win.calls()
    win.dirs[PDIR] = ("O:%s%s" % (SID, DIR_DACL), 0x10 | 0x400)  # junction
    with pytest.raises(NotPrivate):
        private_file.private_dir(HOME, win=win)


def test_windows_dir_reopened_to_inheritance_is_refused(win):
    private_file.private_dir(HOME, win=win)
    for dacl in ("D:AI(A;OICI;FA;;;%s)" % SID,                 # not protected
                 DIR_DACL + "(A;OICI;FA;;;%s)" % OTHER,         # a second ACE
                 DIR_DACL + "(A;OICIID;FR;;;WD)"):              # inherited
        win.dirs[PDIR] = ("O:%s%s" % (SID, dacl), 0x10)
        with pytest.raises(NotPrivate):
            private_file.private_dir(HOME, win=win)


def test_windows_file_is_created_with_its_own_descriptor_and_validated_by_handle(
        win):
    private_file.write_private(HOME, "f.json", b"secret", win=win)
    calls = win.calls()
    first = calls.index("create_new")
    assert calls[first:first + 3] == ["create_new", "validate_handle", "write"]
    assert win.files == {PDIR + "\\f.json": b"secret"}


def test_windows_validation_failure_writes_nothing(win):
    for facts in ({"sddl": "O:%s%s" % (OTHER, FILE_DACL)},
                  {"attrs": 0x80 | 0x400},
                  {"final": "\\\\?\\C:\\Users\\evil\\f.json.tmp"}):
        win.facts = facts
        with pytest.raises(NotPrivate):
            private_file.write_private(HOME, "f.json", b"secret", win=win)
        assert "write" not in win.calls()
        assert win.calls()[-1] == "close"
    assert b"secret" not in b"".join(win.files.values())


def _file_calls(win):
    return win.log[win.calls().index("create_new"):]


def test_windows_rename_failure_deletes_the_temp_by_handle(win):
    win.rename_error = OSError(17, "not same device")
    with pytest.raises(NotPrivate):
        private_file.write_private(HOME, "f.json", b"secret", win=win)
    log = _file_calls(win)
    assert [c[0] for c in log] == ["create_new", "validate_handle", "write",
                                   "rename_by_handle", "delete_by_handle",
                                   "close"]
    assert len({c[1] for c in log[1:]}) == 1, "one handle, create to close"
    assert win.files == {}


def test_windows_handle_lifetime_on_success(win):
    private_file.write_private(HOME, "f.json", b"secret", win=win)
    log = _file_calls(win)
    assert [c[0] for c in log] == ["create_new", "validate_handle", "write",
                                   "rename_by_handle", "close"]
    assert len({c[1] for c in log[1:]}) == 1


def test_windows_load_validates_the_opened_handle_not_the_name(win):
    private_file.write_private(HOME, "f.json", b"secret", win=win)
    del win.log[:]
    assert private_file.open_private(HOME, "f.json", win=win) == b"secret"
    log = _file_calls_open(win)
    assert [c[0] for c in log] == ["open_existing", "validate_handle", "read",
                                   "close"]
    assert len({c[1] for c in log[1:]}) == 1
    assert ("dir_facts", PDIR + "\\f.json") not in win.log


def _file_calls_open(win):
    return win.log[win.calls().index("open_existing"):]


def test_windows_retained_handle_cannot_see_the_secret(win, monkeypatch):
    """Another account that made the temp name first, and kept a handle to
    it, never gets our bytes: CREATE_NEW refuses the name, and nothing is
    written into an object we did not create."""
    planted = PDIR + "\\f.json.planted.tmp"
    win.files[planted] = b""
    monkeypatch.setattr(private_file, "_tmp_name",
                        lambda name: name + ".planted.tmp")
    with pytest.raises(OSError):
        private_file.write_private(HOME, "f.json", b"secret", win=win)
    assert win.files == {planted: b""}
    assert "write" not in win.calls()


def test_windows_load_rejects_foreign_sid_and_reparse_and_owner(win):
    private_file.write_private(HOME, "f.json", b"secret", win=win)
    cases = [
        ({"sddl": "O:%s%s(A;;FR;;;%s)" % (SID, FILE_DACL, OTHER)},
         "not owner-only"),
        ({"sddl": "O:%sD:(A;;FA;;;%s)" % (SID, SID)}, "not owner-only"),
        ({"attrs": 0x80 | 0x400}, "not a regular file"),
        ({"attrs": 0x10}, "not a regular file"),
        ({"sddl": "O:%s%s" % (OTHER, FILE_DACL)}, "wrong owner"),
        ({"final": "\\\\?\\C:\\Users\\evil\\f.json"}, "not owner-only"),
        ({"final": "\\\\?\\" + PDIR + "\\sub\\f.json"}, "not owner-only"),
    ]
    for facts, message in cases:
        win.facts = facts
        with pytest.raises(NotPrivate) as e:
            private_file.open_private(HOME, "f.json", win=win)
        assert str(e.value) == message, facts
        assert win.calls()[-1] == "close"
    win.facts = {"final": "\\\\?\\" + PDIR.upper() + "\\F.JSON"}
    assert private_file.open_private(HOME, "f.json", win=win) == b"secret"


def test_sddl_parse_is_locale_free():
    """SDDL is the same string in every Windows language; icacls output is
    not. The owner alias case is the one a lazy regex gets wrong."""
    assert private_file.parse_sddl("O:%sD:P(A;;FA;;;%s)" % (SID, SID)) == (
        SID, "P", ["(A;;FA;;;%s)" % SID])
    assert private_file.parse_sddl(
        "O:DAG:DUD:PAI(A;OICI;FA;;;%s)(A;;FR;;;WD)" % SID) == (
        "DA", "PAI", ["(A;OICI;FA;;;%s)" % SID, "(A;;FR;;;WD)"])
    assert private_file.parse_sddl("O:BAD:AI") == ("BA", "AI", [])
    assert private_file.parse_sddl("garbage") is None


# ---------------------------------------------- on a real Windows machine


windows_real = [pytest.mark.windows_real,
                pytest.mark.skipif(sys.platform != "win32",
                                   reason="real Windows ACLs")]


def _apply(marks):
    def deco(f):
        for m in marks:
            f = m(f)
        return f
    return deco


@_apply(windows_real)
def test_real_owner_and_sddl_read_back_through_a_fresh_handle(tmp_path):
    home = _home(tmp_path)
    private_file.write_private(home, "f.json", b"secret")
    api = private_file._WinApi()
    h = api.open_existing(os.path.join(home, "private", "f.json"))
    try:
        api.validate_handle(h, os.path.join(home, "private"))
        assert api.read(h) == b"secret"
    finally:
        api.close(h)
    sddl, attrs = api.dir_facts(os.path.join(home, "private"))
    owner, flags, aces = private_file.parse_sddl(sddl)
    assert owner == api.user_sid() and "P" in flags and len(aces) == 1


@_apply(windows_real)
def test_real_runas_precreated_directory_is_refused(tmp_path):
    """A `private` directory pre-created by another account. Needs that
    account: BLINK_TEST_RUNAS_DIR names a directory it made (see
    docs/windows-check.md); without it the test cannot be honest."""
    pre = os.environ.get("BLINK_TEST_RUNAS_DIR")
    if not pre:
        pytest.skip("no directory pre-created by another account")
    with pytest.raises(NotPrivate):
        private_file.private_dir(os.path.dirname(pre))


@_apply(windows_real)
def test_real_swapped_directory_between_calls_fails_closed(tmp_path):
    home = _home(tmp_path)
    private_file.write_private(home, "f.json", b"secret")
    d = os.path.join(home, "private")
    os.rename(d, d + ".old")
    os.mkdir(d)                     # inherits the profile's permissions
    with pytest.raises(NotPrivate):
        private_file.write_private(home, "f.json", b"secret")
    with pytest.raises(NotPrivate):
        private_file.open_private(home, "f.json")


def test_windows_write_failure_deletes_the_temp_by_handle(win):
    """WriteFile or FlushFileBuffers failing part-way leaves bytes of the
    secret in the temp; it goes by the same handle, before the close."""
    def broken(h, data):
        win.log.append(("write", h))
        win.files[win.handles[h]["path"]] = data[:3]
        raise OSError(112, "disk full")

    win.write = broken
    with pytest.raises(OSError):
        private_file.write_private(HOME, "f.json", b"secret", win=win)
    log = _file_calls(win)
    assert [c[0] for c in log] == ["create_new", "validate_handle", "write",
                                   "delete_by_handle", "close"]
    assert len({c[1] for c in log[1:]}) == 1
    assert win.files == {}


@posix_only
def test_remove_refuses_a_symlinked_private_dir(tmp_path):
    home = _home(tmp_path)
    os.makedirs(home)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "f.json").write_bytes(b"theirs")
    (outside / "f.json.abcd.tmp").write_bytes(b"theirs")
    os.symlink(str(outside), os.path.join(home, "private"))
    with pytest.raises(NotPrivate):
        private_file.remove_private(home, "f.json")
    assert sorted(os.listdir(outside)) == ["f.json", "f.json.abcd.tmp"]


def test_remove_refuses_a_reparse_point_private_dir(tmp_path, monkeypatch):
    """Windows: a junction is not a symlink to os.path.islink, so the
    attribute is what counts."""
    home = _home(tmp_path)
    os.makedirs(os.path.join(home, "private"))
    real = os.lstat

    def junction(p, *a, **kw):
        st = real(p, *a, **kw)
        if str(p).endswith("private"):
            class S:
                st_mode = st.st_mode
                st_file_attributes = 0x10 | 0x400
            return S()
        return st

    monkeypatch.setattr(private_file.os, "name", "nt")
    monkeypatch.setattr(private_file.os, "lstat", junction)
    with pytest.raises(NotPrivate):
        private_file.remove_private(home, "f.json")


def test_remove_takes_the_file_and_its_temps_only(tmp_path):
    home = _home(tmp_path)
    assert private_file.remove_private(home, "f.json") is False
    d = private_file.private_dir(home)
    for n in ("f.json", "f.json.1a.tmp", "g.json", "f.jsonx"):
        open(os.path.join(d, n), "wb").close()
    assert private_file.remove_private(home, "f.json") is True
    assert sorted(os.listdir(d)) == ["f.jsonx", "g.json"]
