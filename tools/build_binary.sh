#!/bin/sh
# Build the single-file `blink` binary for THIS platform.
#
#   tools/build_binary.sh [outdir]
#
# PyInstaller cannot cross-compile, so each platform's binary is built on that
# platform -- see .github/workflows/release-binaries.yml, which does exactly
# this on a macOS and a Linux runner and attaches the results to the release.
#
# The customer needs none of this. It exists so they need none of it: no
# Python, no virtualenv, no pip, no PyPI at install time.
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
OUT="${1:-$ROOT/dist}"
BUILD="${TMPDIR:-/tmp}/blink-build"

command -v python3 >/dev/null 2>&1 || { echo "need python3 to build" >&2; exit 1; }

python3 -m venv "$BUILD" >/dev/null
# A Windows venv puts its executables in Scripts/, not bin/. This script runs
# under Git Bash there, so the path style is the only difference that matters.
VBIN="$BUILD/bin"
[ -d "$VBIN" ] || VBIN="$BUILD/Scripts"
"$VBIN/python" -m pip install --quiet --upgrade pip
# The daemon's own pinned dependencies get frozen INTO the binary, so the
# customer's install can no longer drift with whatever PyPI serves that day.
"$VBIN/python" -m pip install --quiet pyinstaller -r "$ROOT/pc/requirements.txt"

# Packaging and test machinery that PyInstaller collects on its own and this
# program never imports. It only ever showed up on Linux -- 323 modules the
# macOS build did not have, led by setuptools at 1.1 MB -- which is why the
# Linux download was twice the size of the others for the same program.
#
# espefuse is its own top-level package inside the esptool wheel, with its
# eFuse tables as data files; --collect-all esptool does not bring it, and
# without it the daemon cannot check a chip before flashing it (the first
# customer-path OTA test failed on exactly this, 2026-08-29). It imports
# bitstring, which picks its bitarray backend by importlib at runtime --
# invisible to PyInstaller's analysis, so both are collected whole too.
#
# Nothing under pc/ imports any of these, and neither does esptool, pyserial
# or ecdsa (checked against the pinned versions). asyncio and multiprocessing
# are also collected and also unused, but they are left in: together they are
# 0.4 MB, and unlike the list below they are plausible lazy imports for some
# future dependency, where a wrong exclusion surfaces as a crash on a
# customer's machine rather than at build time.
set -- \
	--exclude-module setuptools \
	--exclude-module pkg_resources \
	--exclude-module distutils \
	--exclude-module packaging \
	--exclude-module unittest \
	--exclude-module doctest \
	--exclude-module pydoc \
	--exclude-module tkinter

# The Linux libpython ships with its debug symbols: 23 MB unstripped, against
# 7 MB for the macOS framework, which Apple strips before shipping. Stripping
# it costs nothing a customer can observe -- there is no debugger on the other
# end of this download -- and is most of the remaining difference.
#
# Linux only. PyInstaller warns that --strip can produce unusable binaries on
# macOS, where it would also invalidate a code signature, and there is nothing
# for it to do on Windows.
if [ "$(uname -s)" = "Linux" ]; then
	set -- "$@" --strip
fi

cd "$ROOT"
"$VBIN/pyinstaller" \
	--onefile \
	--name blink \
	--distpath "$OUT" \
	--workpath "$BUILD/work" \
	--specpath "$BUILD" \
	--add-data "$ROOT/tools/blink-statusline.sh:." \
	--add-data "$ROOT/tools/blink-hook.sh:." \
	--collect-all esptool \
	--collect-all espefuse \
	--collect-all bitstring \
	--collect-all bitarray \
	--hidden-import claude_usage_bridge \
	--hidden-import ecdsa \
	--collect-all certifi \
	--hidden-import serial.tools.list_ports \
	"$@" \
	--noconfirm --clean \
	blink_main.py >"$BUILD/pyinstaller.log" 2>&1 || {
		tail -30 "$BUILD/pyinstaller.log" >&2
		echo "FATAL: build failed; full log at $BUILD/pyinstaller.log" >&2
		exit 1
	}

BUILT="$OUT/blink"
[ -f "$BUILT" ] || BUILT="$OUT/blink.exe"
echo "built $BUILT ($(du -h "$BUILT" | cut -f1))"
