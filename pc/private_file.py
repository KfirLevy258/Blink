"""Owner-only files, kept by handle (spec 7.3, D48, D53).

The WiFi pairing key is the first secret the daemon keeps on disk, and on
Windows "create it, then tighten it" is not owner-only: another account under
the profile's inherited permissions can open the new object in the gap and
keep the handle, and a DACL change does not revoke a handle. So nothing here
is tightened after the fact. Each object is created WITH its descriptor
(POSIX: a 0600 mode under O_EXCL; Windows: an SDDL naming only the user's
SID), and every check is made on the descriptor or handle that will be read
or written, never on a name that may since resolve to something else.

The `private` directory is defence in depth: ~/.blink is not a trusted
parent on Windows, so the guarantee is on the file.

The Windows half talks to the OS only through `win`, a small surface (see
_WinApi) that tests replace with a recording fake. Its ctypes underneath is
proved only on a real Windows machine (the windows_real tests).

Every exception raised here carries one of a fixed set of sentences, and no
path, name or byte of the content: these end up in a log customers send us.
"""
from __future__ import annotations

import os
import re
import secrets
import stat

PRIVATE_DIR = "private"

_DIR_SDDL = "O:{sid}G:{sid}D:PAI(A;OICI;FA;;;{sid})"
_FILE_SDDL = "O:{sid}G:{sid}D:P(A;;FA;;;{sid})"
_DIR_ACE = "(A;OICI;FA;;;{sid})"
_FILE_ACE = "(A;;FA;;;{sid})"

FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_REPARSE_POINT = 0x400

# Indirection so the Windows branch can be driven from macOS with
# backslash paths (os.path is posixpath there).
_join = os.path.join


def _makedirs(path):
    os.makedirs(path, 0o700, exist_ok=True)


class NotPrivate(Exception):
    """The file or directory is not provably ours alone. The message is one
    of: "private directory not owner-only", "not owner-only", "not a regular
    file", "wrong owner", "acl check failed"."""


def _tmp_name(name):
    # Random, so a stale temp from a crash never blocks the next write and
    # nobody can pre-create the name we are about to use.
    return "%s.%s.tmp" % (name, secrets.token_hex(8))


def private_dir(home: str, win=None) -> str:
    """<home>/private, created owner-only if absent and validated either way."""
    if os.name == "nt":
        return _win_dir(home, win or _WinApi())
    os.close(_posix_dir(home))
    return _join(home, PRIVATE_DIR)


def write_private(home: str, name: str, data: bytes, win=None) -> str:
    """Write `data` to <home>/private/<name> atomically, owner-only from the
    first byte. Returns the final path."""
    if os.name == "nt":
        return _win_write(home, name, data, win or _WinApi())
    return _posix_write(home, name, data)


def open_private(home: str, name: str, win=None) -> bytes | None:
    """The content of <home>/private/<name>, or None if there is no such
    file. Anything not provably ours alone raises NotPrivate -- including
    access denied, which is not the same as absent."""
    if os.name == "nt":
        return _win_open(home, name, win or _WinApi())
    return _posix_open(home, name)


def remove_private(home: str, name: str) -> bool:
    """Delete <home>/private/<name> and the <name>.*.tmp a write interrupted
    mid-way left. True if <name> was there. Never through a `private` that
    is a symlink or a reparse point: the reviewer of 3.1 had a by-name
    remove() follow one out of ~/.blink and delete another directory's
    files. OSError from a removal propagates; the caller decides."""
    path = _join(home, PRIVATE_DIR)
    temp = re.compile(re.escape(name) + r"\.[^.]+\.tmp$")
    if os.name == "nt":
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            return False
        attrs = getattr(st, "st_file_attributes", 0)
        if (attrs & FILE_ATTRIBUTE_REPARSE_POINT
                or not attrs & FILE_ATTRIBUTE_DIRECTORY):
            raise NotPrivate("private directory not owner-only")
        names = os.listdir(path)
        unlink = lambda n: os.remove(_join(path, n))  # noqa: E731
        done = lambda: None                           # noqa: E731
    else:
        try:
            dfd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                          | getattr(os, "O_CLOEXEC", 0))
        except FileNotFoundError:
            return False
        except OSError:
            raise NotPrivate("private directory not owner-only") from None
        names = os.listdir(dfd)
        unlink = lambda n: os.unlink(n, dir_fd=dfd)   # noqa: E731
        done = lambda: os.close(dfd)                  # noqa: E731
    try:
        for n in names:
            if temp.match(n):
                unlink(n)
        if name not in names:
            return False
        unlink(name)
        return True
    finally:
        done()


# ---------------------------------------------------------------- POSIX


def _posix_dir(home):
    """An O_DIRECTORY descriptor of <home>/private, checked by fstat."""
    _makedirs(home)
    path = _join(home, PRIVATE_DIR)
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        pass                    # not success: validated below like any other
    try:
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                     | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        # A symlink (ELOOP / ENOTDIR) or a directory we cannot open.
        raise NotPrivate("private directory not owner-only") from None
    st = os.fstat(fd)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or st.st_mode & 0o077):
        os.close(fd)
        raise NotPrivate("private directory not owner-only")
    return fd


def _posix_check(fd):
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        raise NotPrivate("not a regular file")
    if st.st_uid != os.getuid():
        raise NotPrivate("wrong owner")
    if st.st_mode & 0o177:
        raise NotPrivate("not owner-only")


def _posix_write(home, name, data):
    dfd = _posix_dir(home)
    try:
        tmp = _tmp_name(name)
        # The mode is set at creation, under the umask, so the file is never
        # wider than 0600 -- not even for an instant.
        fd = os.open(tmp, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW
                     | getattr(os, "O_CLOEXEC", 0), 0o600, dir_fd=dfd)
        try:
            try:
                _posix_check(fd)
                view = memoryview(data)
                while view:
                    view = view[os.write(fd, view):]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, name, src_dir_fd=dfd, dst_dir_fd=dfd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dfd)
            except OSError:
                pass
            raise
        os.fsync(dfd)
    finally:
        os.close(dfd)
    return _join(home, PRIVATE_DIR, name)


def _posix_open(home, name):
    dfd = _posix_dir(home)
    try:
        try:
            # O_NONBLOCK: a FIFO planted under the name must not hang the
            # daemon; fstat below refuses it.
            fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
                         | getattr(os, "O_CLOEXEC", 0), dir_fd=dfd)
        except FileNotFoundError:
            return None
        except PermissionError:
            raise NotPrivate("acl check failed") from None
        except OSError:
            raise NotPrivate("not a regular file") from None   # ELOOP: a link
        try:
            _posix_check(fd)
            chunks = []
            while True:
                b = os.read(fd, 65536)
                if not b:
                    return b"".join(chunks)
                chunks.append(b)
        finally:
            os.close(fd)
    finally:
        os.close(dfd)


# -------------------------------------------------------------- Windows


_SDDL = re.compile(r"O:(?P<owner>.+?)(?:G:.+?)?D:(?P<flags>[A-Z]*)"
                   r"(?P<aces>(?:\([^()]*\))*)$")


def parse_sddl(sddl):
    """(owner, DACL flags, [ACE, ...]) from an SDDL string holding an owner
    and a DACL (an optional group is skipped), or None. SDDL is the same in
    every Windows language, unlike icacls output."""
    m = _SDDL.match(sddl)
    if not m:
        return None
    return (m.group("owner"), m.group("flags"),
            re.findall(r"\([^()]*\)", m.group("aces")))


def _owner_only(sddl, sid, ace):
    """Owner is `sid`; the DACL is protected and holds exactly `ace`. The
    AI flag is accepted beside P: it marks how the DACL was computed and
    grants nothing."""
    parts = parse_sddl(sddl)
    return (parts is not None and parts[0] == sid and "P" in parts[1]
            and parts[2] == [ace.format(sid=sid)])


def check_facts(sddl, attrs, final, expect_dir, sid):
    """Judge what a handle reported: its owner+DACL SDDL, its attributes and
    its final path, against `sid` spelled as SDDL output spells it
    (_WinApi.sddl_sid). Raises NotPrivate; returns None when the file is
    ours."""
    if attrs & (FILE_ATTRIBUTE_REPARSE_POINT | FILE_ATTRIBUTE_DIRECTORY):
        raise NotPrivate("not a regular file")
    parts = parse_sddl(sddl)
    if parts is None:
        raise NotPrivate("acl check failed")
    if parts[0] != sid:
        raise NotPrivate("wrong owner")
    if not _owner_only(sddl, sid, _FILE_ACE):
        raise NotPrivate("not owner-only")
    # The name the handle really has must be directly inside the directory we
    # validated. Compared unresolved on purpose: resolving expect_dir by name
    # would follow whatever a swap put there. shortcut: a home reached through
    # a junction fails closed here; resolve the directory by handle if a
    # customer ever has one.
    prefix = "\\\\?\\" + expect_dir.rstrip("\\") + "\\"
    if not final.lower().startswith(prefix.lower()) or \
            "\\" in final[len(prefix):]:
        raise NotPrivate("not owner-only")


def _win_dir(home, win):
    _makedirs(home)
    path = _join(home, PRIVATE_DIR)
    # False means it already existed: validated below, never adopted.
    win.create_dir_with_sddl(path, _DIR_SDDL.format(sid=win.user_sid()))
    try:
        sddl, attrs = win.dir_facts(path)
    except OSError:
        raise NotPrivate("private directory not owner-only") from None
    if (attrs & FILE_ATTRIBUTE_REPARSE_POINT
            or not attrs & FILE_ATTRIBUTE_DIRECTORY
            or not _owner_only(sddl, win.sddl_sid(), _DIR_ACE)):
        raise NotPrivate("private directory not owner-only")
    return path


def _win_write(home, name, data, win):
    d = _win_dir(home, win)
    sid = win.user_sid()
    # One handle from creation to close: CREATE_NEW with our own protected
    # descriptor, so the object is never readable by anyone else and never
    # one somebody else made.
    h = win.create_new(_join(d, _tmp_name(name)), _FILE_SDDL.format(sid=sid))
    try:
        try:
            win.validate_handle(h, d)       # before a single byte
        except BaseException:
            _quietly(win.delete_by_handle, h)
            raise
        try:
            win.write(h, data)
        except BaseException:
            # A WriteFile or flush that failed part-way leaves some of the
            # secret in the temp: gone by this handle, before the close.
            _quietly(win.delete_by_handle, h)
            raise
        try:
            win.rename_by_handle(h, _join(d, name))
        except OSError:
            # The temp's name no longer leads where we put it (a swapped
            # directory). Delete by this handle, not by a name.
            _quietly(win.delete_by_handle, h)
            raise NotPrivate("private directory not owner-only") from None
        except BaseException:
            _quietly(win.delete_by_handle, h)
            raise
    finally:
        win.close(h)
    return _join(d, name)


def _quietly(fn, *a):
    try:
        fn(*a)
    except OSError:
        pass


def _win_open(home, name, win):
    d = _win_dir(home, win)
    try:
        h = win.open_existing(_join(d, name))
    except FileNotFoundError:
        return None
    except PermissionError:
        raise NotPrivate("acl check failed") from None
    try:
        win.validate_handle(h, d)
        return win.read(h)
    finally:
        win.close(h)


class _WinApi:
    """The real `win` surface: ctypes over kernel32 and advapi32. Built only
    on Windows, and only when called -- importing this module does nothing.

    Every failure is an OSError from ctypes.WinError, which Python maps by
    winerror: ERROR_FILE_NOT_FOUND / ERROR_PATH_NOT_FOUND to
    FileNotFoundError, ERROR_ACCESS_DENIED to PermissionError.
    """

    GENERIC_READ = 0x80000000
    GENERIC_WRITE = 0x40000000
    DELETE = 0x00010000
    FILE_READ_ATTRIBUTES = 0x80     # FileAttributeTagInfo; not in GENERIC_WRITE
    FILE_SHARE_READ = 0x1
    FILE_SHARE_DELETE = 0x4
    CREATE_NEW = 1
    OPEN_EXISTING = 3
    FILE_ATTRIBUTE_NORMAL = 0x80
    FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
    ERROR_ALREADY_EXISTS = 183
    SE_FILE_OBJECT = 1
    OWNER_SECURITY_INFORMATION = 0x1
    DACL_SECURITY_INFORMATION = 0x4
    SDDL_REVISION_1 = 1
    TOKEN_QUERY = 0x8
    TokenUser = 1
    FileRenameInfo = 3
    FileDispositionInfo = 4
    FileAttributeTagInfo = 9

    def __init__(self):
        import ctypes
        from ctypes import wintypes as w
        self.ct, self.w = ctypes, w
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        a = ctypes.WinDLL("advapi32", use_last_error=True)
        H, D, B, P = w.HANDLE, w.DWORD, w.BOOL, ctypes.c_void_p
        pP = ctypes.POINTER(P)

        class SA(ctypes.Structure):
            _fields_ = [("nLength", D), ("lpSecurityDescriptor", P),
                        ("bInheritHandle", B)]
        self.SA = SA

        def fn(dll, name, res, *args):
            f = getattr(dll, name)
            f.restype, f.argtypes = res, list(args)
            return f

        self.CreateFileW = fn(k, "CreateFileW", H, w.LPCWSTR, D, D, P, D, D, H)
        self.CreateDirectoryW = fn(k, "CreateDirectoryW", B, w.LPCWSTR, P)
        self.CloseHandle = fn(k, "CloseHandle", B, H)
        self.WriteFile = fn(k, "WriteFile", B, H, P, D, ctypes.POINTER(D), P)
        self.ReadFile = fn(k, "ReadFile", B, H, P, D, ctypes.POINTER(D), P)
        self.FlushFileBuffers = fn(k, "FlushFileBuffers", B, H)
        self.GetFileAttributesW = fn(k, "GetFileAttributesW", D, w.LPCWSTR)
        self.GetFileInformationByHandleEx = fn(
            k, "GetFileInformationByHandleEx", B, H, ctypes.c_int, P, D)
        self.SetFileInformationByHandle = fn(
            k, "SetFileInformationByHandle", B, H, ctypes.c_int, P, D)
        self.GetFinalPathNameByHandleW = fn(
            k, "GetFinalPathNameByHandleW", D, H, w.LPWSTR, D, D)
        self.GetCurrentProcess = fn(k, "GetCurrentProcess", H)
        self.LocalFree = fn(k, "LocalFree", P, P)
        self.OpenProcessToken = fn(a, "OpenProcessToken", B, H, D,
                                   ctypes.POINTER(H))
        self.GetTokenInformation = fn(a, "GetTokenInformation", B, H,
                                      ctypes.c_int, P, D, ctypes.POINTER(D))
        self.ConvertSidToStringSidW = fn(a, "ConvertSidToStringSidW", B, P,
                                         ctypes.POINTER(w.LPWSTR))
        self.CSSDtoSD = fn(
            a, "ConvertStringSecurityDescriptorToSecurityDescriptorW", B,
            w.LPCWSTR, D, pP, ctypes.POINTER(D))
        self.SDtoCSSD = fn(
            a, "ConvertSecurityDescriptorToStringSecurityDescriptorW", B,
            P, D, D, ctypes.POINTER(w.LPWSTR), ctypes.POINTER(D))
        self.GetSecurityInfo = fn(a, "GetSecurityInfo", D, H, ctypes.c_int, D,
                                  pP, pP, pP, pP, pP)
        self.GetNamedSecurityInfoW = fn(a, "GetNamedSecurityInfoW", D,
                                        w.LPCWSTR, ctypes.c_int, D,
                                        pP, pP, pP, pP, pP)
        self._sid = None
        self._sddl_sid = None

    def _err(self, code=None):
        return self.ct.WinError(self.ct.get_last_error() if code is None
                                else code)

    def user_sid(self):
        """From the process token, never from a user name."""
        if self._sid:
            return self._sid
        ct, w = self.ct, self.w
        tok = w.HANDLE()
        if not self.OpenProcessToken(self.GetCurrentProcess(),
                                     self.TOKEN_QUERY, ct.byref(tok)):
            raise self._err()
        try:
            need = w.DWORD()
            self.GetTokenInformation(tok, self.TokenUser, None, 0,
                                     ct.byref(need))
            buf = ct.create_string_buffer(need.value)
            if not self.GetTokenInformation(tok, self.TokenUser, buf,
                                            need, ct.byref(need)):
                raise self._err()
            psid = ct.c_void_p.from_buffer(buf).value   # TOKEN_USER.User.Sid
            s = w.LPWSTR()
            if not self.ConvertSidToStringSidW(psid, ct.byref(s)):
                raise self._err()
            try:
                self._sid = s.value
            finally:
                self.LocalFree(s)
        finally:
            self.CloseHandle(tok)
        return self._sid

    def sddl_sid(self):
        """user_sid() as a descriptor read back spells it. Not always the
        same string: SDDL output writes a few well-known accounts as an
        alias, and the built-in Administrator (RID 500) comes back as "LA".
        That account is what an Azure VM's admin user is, so on the GitHub
        Windows runner (runneradmin, PR #9, 2026-11-07) a directory we had
        just created owner-only read back as O:LA...(A;OICI;FA;;;LA), and
        comparing it with the numeric SID refused our own directory: every
        pairing and settings write failed. The OS spells the SID here, by
        round-tripping the very descriptor we create with, so the checks
        compare like with like and still refuse any other account."""
        if self._sddl_sid is None:
            psd = self.ct.c_void_p()
            if not self.CSSDtoSD(_FILE_SDDL.format(sid=self.user_sid()),
                                 self.SDDL_REVISION_1, self.ct.byref(psd),
                                 None):
                raise self._err()
            parts = parse_sddl(self._sddl_of(psd))
            if parts is None:
                raise NotPrivate("acl check failed")
            self._sddl_sid = parts[0]
        return self._sddl_sid

    def _with_sa(self, sddl, call):
        ct = self.ct
        psd = ct.c_void_p()
        if not self.CSSDtoSD(sddl, self.SDDL_REVISION_1, ct.byref(psd), None):
            raise self._err()
        try:
            sa = self.SA(ct.sizeof(self.SA), psd, False)
            return call(ct.byref(sa))
        finally:
            self.LocalFree(psd)

    def create_dir_with_sddl(self, path, sddl):
        def call(sa):
            if self.CreateDirectoryW(path, sa):
                return True
            code = self.ct.get_last_error()
            if code == self.ERROR_ALREADY_EXISTS:
                return False
            raise self._err(code)
        return self._with_sa(sddl, call)

    def _sddl_of(self, psd):
        ct, w = self.ct, self.w
        s = w.LPWSTR()
        try:
            if not self.SDtoCSSD(psd, self.SDDL_REVISION_1,
                                 self.OWNER_SECURITY_INFORMATION
                                 | self.DACL_SECURITY_INFORMATION,
                                 ct.byref(s), None):
                raise self._err()
            return s.value
        finally:
            if s:
                self.LocalFree(s)
            self.LocalFree(psd)

    def _security(self, getter, target):
        ct = self.ct
        owner, dacl, psd = ct.c_void_p(), ct.c_void_p(), ct.c_void_p()
        code = getter(target, self.SE_FILE_OBJECT,
                      self.OWNER_SECURITY_INFORMATION
                      | self.DACL_SECURITY_INFORMATION,
                      ct.byref(owner), None, ct.byref(dacl), None,
                      ct.byref(psd))
        if code:
            raise self._err(code)
        return self._sddl_of(psd)

    def dir_facts(self, path):
        attrs = self.GetFileAttributesW(path)
        if attrs == 0xFFFFFFFF:
            raise self._err()
        return self._security(self.GetNamedSecurityInfoW, path), attrs

    def _handle(self, h):
        if h is None or h == self.ct.c_void_p(-1).value:
            raise self._err()
        return h

    def create_new(self, path, sddl):
        return self._with_sa(sddl, lambda sa: self._handle(self.CreateFileW(
            path, self.GENERIC_WRITE | self.DELETE | self.FILE_READ_ATTRIBUTES,
            self.FILE_SHARE_DELETE, sa, self.CREATE_NEW,
            self.FILE_ATTRIBUTE_NORMAL, None)))

    def open_existing(self, path):
        return self._handle(self.CreateFileW(
            path, self.GENERIC_READ, self.FILE_SHARE_READ, None,
            self.OPEN_EXISTING, self.FILE_FLAG_OPEN_REPARSE_POINT, None))

    def validate_handle(self, h, expect_dir):
        ct, w = self.ct, self.w
        try:
            sddl = self._security(self.GetSecurityInfo, h)

            class TAG(ct.Structure):
                _fields_ = [("FileAttributes", w.DWORD),
                            ("ReparseTag", w.DWORD)]
            tag = TAG()
            if not self.GetFileInformationByHandleEx(
                    h, self.FileAttributeTagInfo, ct.byref(tag),
                    ct.sizeof(tag)):
                raise self._err()
            buf = ct.create_unicode_buffer(1024)
            n = self.GetFinalPathNameByHandleW(h, buf, len(buf), 0)
            if not n or n >= len(buf):
                raise self._err()
        except OSError:
            raise NotPrivate("acl check failed") from None
        check_facts(sddl, tag.FileAttributes, buf.value, expect_dir,
                    self.sddl_sid())

    def write(self, h, data):
        # WriteFile on the handle itself rather than msvcrt.open_osfhandle:
        # the CRT descriptor would own the handle, and closing it would end
        # the handle's lifetime before the rename.
        ct, w = self.ct, self.w
        view = memoryview(data)
        while view:
            chunk = (ct.c_char * len(view)).from_buffer_copy(view)
            done = w.DWORD()
            if not self.WriteFile(h, chunk, len(view), ct.byref(done), None):
                raise self._err()
            view = view[done.value:]
        if not self.FlushFileBuffers(h):
            raise self._err()

    def read(self, h):
        ct, w = self.ct, self.w
        out = []
        buf = ct.create_string_buffer(65536)
        while True:
            got = w.DWORD()
            if not self.ReadFile(h, buf, len(buf), ct.byref(got), None):
                raise self._err()
            if not got.value:
                return b"".join(out)
            out.append(buf.raw[:got.value])

    def rename_by_handle(self, h, final):
        ct, w = self.ct, self.w

        class RENAME(ct.Structure):     # FILE_RENAME_INFO
            _fields_ = [("ReplaceIfExists", w.DWORD),  # BOOLEAN in a union
                        ("RootDirectory", w.HANDLE),
                        ("FileNameLength", w.DWORD),
                        ("FileName", w.WCHAR * 1)]
        name = final.encode("utf-16-le")
        size = ct.sizeof(RENAME) + len(name)
        buf = ct.create_string_buffer(size)
        info = RENAME.from_buffer(buf)
        info.ReplaceIfExists = 1
        info.FileNameLength = len(name)
        ct.memmove(ct.addressof(buf) + RENAME.FileName.offset, name, len(name))
        if not self.SetFileInformationByHandle(h, self.FileRenameInfo, buf,
                                               size):
            raise self._err()

    def delete_by_handle(self, h):
        flag = self.ct.c_ubyte(1)                   # FILE_DISPOSITION_INFO
        if not self.SetFileInformationByHandle(h, self.FileDispositionInfo,
                                               self.ct.byref(flag), 1):
            raise self._err()

    def close(self, h):
        self.CloseHandle(h)
