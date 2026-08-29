#!/usr/bin/env python3
"""Package a one-directory build as the archive the release feed serves.

    tools/package_binary.py <platform-key> [build-dir] [out-dir]

`build-dir` is what tools/build_binary.sh produced (default dist/blink); the
result is <out-dir>/blink-<key>.tar.gz, or .zip for a Windows key, with one
top-level directory `blink/` inside -- the shape pc/update.unpack() expects and
the shape `tar xz` hands a person. Python rather than tar/zip commands so the
same bytes come out of a runner, a developer's machine and the CI harness that
fakes a release; tarfile keeps the executable bit, and zipfile is told to.
"""
import os
import stat
import sys
import tarfile
import zipfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from pc.update import archive_name  # noqa: E402


def package(key, build_dir, out_dir):
    name = archive_name(key)
    out = os.path.join(out_dir, name)
    # Symlinks travel as symlinks -- a macOS bundle is full of them
    # (Python.framework/Versions/Current -> 3.11, Python -> Versions/Current/
    # Python), and the first packager, which only listed regular files, left
    # every one of them dangling after extraction (2026-08-29). A symlinked
    # directory is added as the link and not walked into.
    files = []
    for dp, dn, fn in os.walk(build_dir):
        dn.sort()
        for d in list(dn):
            path = os.path.join(dp, d)
            if os.path.islink(path):
                files.append(path)
                dn.remove(d)
        for f in sorted(fn):
            files.append(os.path.join(dp, f))
    if name.endswith(".zip"):
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for path in files:
                if os.path.islink(path):
                    raise SystemExit(f"a zip cannot carry the symlink {path}")
                arc = "blink/" + os.path.relpath(path, build_dir).replace(os.sep, "/")
                info = zipfile.ZipInfo.from_file(path, arc)
                info.compress_type = zipfile.ZIP_DEFLATED
                mode = os.stat(path).st_mode
                info.external_attr = (stat.S_IMODE(mode) | stat.S_IFREG) << 16
                with open(path, "rb") as fh:
                    z.writestr(info, fh.read())
    else:
        with tarfile.open(out, "w:gz") as t:
            for path in files:
                arc = "blink/" + os.path.relpath(path, build_dir).replace(os.sep, "/")
                t.add(path, arcname=arc, recursive=False)
    return out


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    key = sys.argv[1]
    build = sys.argv[2] if len(sys.argv) > 2 else "dist/blink"
    outd = sys.argv[3] if len(sys.argv) > 3 else os.path.dirname(os.path.abspath(build))
    print(package(key, build, outd))
