# shellcheck shell=sh
# Sourced by tools/release.sh, tools/burn.sh and tools/flash_encrypted.sh.
# POSIX sh: burn.sh is /bin/sh.
#
# blink_refuse_selftest <.config> <image>
#
# Returns 1, with the reason on stderr, when the build is a
# CONFIG_BLINK_FATAL_SELFTEST image -- one that writes through a NULL pointer
# BLINK_FATAL_SELFTEST_AT_S seconds into every boot, on purpose, so the fatal
# handler can be watched on a bench (firmware/Kconfig). Shipped as an OTA it
# would put every desk that took it into a fault loop the fatal streak then
# halts; burned at the factory it would PASS, because the boot check is over
# long before the fault, and be boxed. The Kconfig help said "never ship
# this" and nothing enforced it (final review N3, 2026-10-27).
#
# The way it gets in is the one the help warns about: a -D value is kept in
# the build directory's CMake cache, so one selftest build in build-sb and
# every later release.sh build there carries it without saying so.
#
# Two looks, like release.sh's network-path check: the .config, which is what
# the build was asked for, and the image, which is what ships. The image
# carries the boot line fatal.c prints only in a selftest build. Either one
# refuses; a missing .config refuses too, because "could not look" must never
# read as "looked and it was fine".
blink_refuse_selftest() {
	if [ ! -f "$1" ]; then
		echo "FATAL: no $1 -- cannot tell whether this is a selftest build." >&2
		echo "       Refusing an image nothing has inspected." >&2
		return 1
	fi
	if [ ! -f "$2" ]; then
		echo "FATAL: no image at $2 to inspect." >&2
		return 1
	fi
	if grep -q '^CONFIG_BLINK_FATAL_SELFTEST=y' "$1"; then
		echo "FATAL: CONFIG_BLINK_FATAL_SELFTEST=y in $1." >&2
		echo "       This image faults on every boot, on purpose. A -D value" >&2
		echo "       stays in the build directory's CMake cache: rebuild it" >&2
		echo "       pristine (west build -p always) without that option." >&2
		return 1
	fi
	# -a: the image is binary, and grep would otherwise only say "Binary
	# file matches" -- which still exits 0, but -a keeps BSD and GNU alike.
	if grep -aqF 'SELFTEST BUILD' "$2"; then
		echo "FATAL: $2 contains the selftest boot line, though the" >&2
		echo "       .config says otherwise. It faults on every boot." >&2
		return 1
	fi
	return 0
}
