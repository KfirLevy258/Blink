#!/bin/sh
# Build the single-file `clauge` binary for THIS platform.
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
BUILD="${TMPDIR:-/tmp}/clauge-build"

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

cd "$ROOT"
"$VBIN/pyinstaller" \
	--onefile \
	--name clauge \
	--distpath "$OUT" \
	--workpath "$BUILD/work" \
	--specpath "$BUILD" \
	--add-data "$ROOT/tools/clauge-statusline.sh:." \
	--collect-all esptool \
	--hidden-import claude_usage_bridge \
	--hidden-import ecdsa \
	--hidden-import serial.tools.list_ports \
	--noconfirm --clean \
	clauge_main.py >"$BUILD/pyinstaller.log" 2>&1 || {
		tail -30 "$BUILD/pyinstaller.log" >&2
		echo "FATAL: build failed; full log at $BUILD/pyinstaller.log" >&2
		exit 1
	}

BUILT="$OUT/clauge"
[ -f "$BUILT" ] || BUILT="$OUT/clauge.exe"
echo "built $BUILT ($(du -h "$BUILT" | cut -f1))"
