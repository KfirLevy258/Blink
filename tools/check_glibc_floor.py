#!/usr/bin/env python3
"""Fail if a Linux build needs a newer glibc than the oldest we promise.

    tools/check_glibc_floor.py [build-dir]

`build-dir` is what tools/build_binary.sh produced (default dist/blink).
Every ELF file in it is read for the `GLIBC_x.y` version names it asks the
loader for, and the build is refused if any is newer than NEWEST_ALLOWED.

1.5.0 shipped a Linux binary that could not start its daemon on Ubuntu 24.04
(glibc 2.39): termios.cpython-311-x86_64-linux-gnu.so, which pyserial loads,
asked for GLIBC_2.42 (issue #11, 2026-11-19). release-binaries.yml built on
`ubuntu-latest`, that label moved to 26.04 between the 1.4.1 and 1.5.0
builds, and setup-python handed the newer runner a Python built against its
glibc. Nothing failed anywhere we could see: the binary ran on the runner
that built it, `blink --version` and `blink status` never load termios, and
every other file in the bundle still tops out at GLIBC_2.38. The workflow
now names its runner, and this is what notices if that stops being enough.

The names are found by scanning the bytes, not by parsing .gnu.version_r: a
required version is stored as plain text in the file's string table, so the
scan sees every one a parser would, runs on the Mac and the Windows machine
this project is written on, and needs neither objdump nor a dependency. What
it can get wrong is a file that merely mentions a newer version in its data,
and that fails the build loudly, which is the safe direction.
"""
import os
import re
import sys

# Ubuntu 24.04's glibc, and the runner release-binaries.yml builds on. Raising
# it drops every customer on an older distribution, the next update included:
# the daemon replaces itself, so they find out when the board goes quiet.
NEWEST_ALLOWED = (2, 39)

_VERSION = re.compile(rb"GLIBC_(\d+)\.(\d+)(?:\.(\d+))?(?![\d.])")


def needs(path):
    """The newest glibc version `path` names, as a tuple, or None when it is
    not an ELF file or names none."""
    with open(path, "rb") as f:
        if f.read(4) != b"\x7fELF":
            return None
        data = f.read()
    found = [tuple(int(g) for g in m.groups() if g is not None)
             for m in _VERSION.finditer(data)]
    return max(found) if found else None


def too_new(build_dir, newest=NEWEST_ALLOWED):
    """[(relative path, version)] for every file needing more than `newest`,
    sorted, and the number of ELF files that named a version at all."""
    over, seen = [], 0
    for root, _dirs, files in os.walk(build_dir):
        for name in files:
            path = os.path.join(root, name)
            if os.path.islink(path):
                continue
            ver = needs(path)
            if ver is None:
                continue
            seen += 1
            if ver > newest:
                over.append((os.path.relpath(path, build_dir), ver))
    return sorted(over), seen


def _dotted(ver):
    return ".".join(str(n) for n in ver)


def main(argv):
    build_dir = argv[1] if len(argv) > 1 else os.path.join("dist", "blink")
    over, seen = too_new(build_dir)
    # A directory with no ELF in it is a wrong path or a build for another
    # platform. Passing it would make this check a no-op that reads as green.
    if not seen:
        print(f"FATAL: no ELF file under {build_dir} names a glibc version;"
              " is this a Linux build?", file=sys.stderr)
        return 1
    if over:
        print(f"FATAL: this build needs a glibc newer than"
              f" {_dotted(NEWEST_ALLOWED)} and will not start on the"
              " distributions we support:", file=sys.stderr)
        for rel, ver in over:
            print(f"  GLIBC_{_dotted(ver)}  {rel}", file=sys.stderr)
        print("  Build on the runner release-binaries.yml names, not a newer"
              " one. See the top of tools/check_glibc_floor.py.",
              file=sys.stderr)
        return 1
    print(f"glibc: {seen} files, none needs more than"
          f" {_dotted(NEWEST_ALLOWED)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
