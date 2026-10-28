# shellcheck shell=sh
# Sourced by tools/release.sh, tools/burn.sh and tools/flash_encrypted.sh.
# POSIX sh: burn.sh is /bin/sh.
#
# Four builds that must not ship are refused here: the fatal selftest
# (blink_refuse_selftest, next), the touch injector (blink_refuse_inject),
# the touch trace (blink_refuse_trace) and the pilot-panel build
# (blink_refuse_pilot). The last two are release.sh's alone, since both are
# flashed to boards on purpose.
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

# blink_refuse_inject <.config> <image>
#
# The same two looks for CONFIG_BLINK_TOUCH_INJECT, the bench touch injector
# (firmware/src/touch_inject.h). Its image does not fault -- it boots, passes
# the boot check and looks exactly like a release -- and that is the danger:
# it takes {"t":"touch"} lines from the cable and plays them as a finger, so
# anything on the computer it is plugged into could operate the panel,
# Settings and the update row included. Shipped as an OTA, every desk that
# took it would be one; burned at the factory it would PASS and be boxed.
# The image carries touch_inject.c's boot line, and nothing else prints it.
#
# A function of its own rather than a second test inside
# blink_refuse_selftest: burn.sh lets each through with its own flag
# (--selftest, --bench), and an inject build must not need --selftest.
blink_refuse_inject() {
	if [ ! -f "$1" ]; then
		echo "FATAL: no $1 -- cannot tell whether this is a touch-inject build." >&2
		echo "       Refusing an image nothing has inspected." >&2
		return 1
	fi
	if [ ! -f "$2" ]; then
		echo "FATAL: no image at $2 to inspect." >&2
		return 1
	fi
	if grep -q '^CONFIG_BLINK_TOUCH_INJECT=y' "$1"; then
		echo "FATAL: CONFIG_BLINK_TOUCH_INJECT=y in $1." >&2
		echo "       This image lets the computer drive the touch panel; it" >&2
		echo "       is for the bench only. A -D value stays in the build" >&2
		echo "       directory's CMake cache: rebuild it pristine" >&2
		echo "       (west build -p always) without that option." >&2
		return 1
	fi
	if grep -aqF 'INJECT BUILD' "$2"; then
		echo "FATAL: $2 contains the touch inject boot line, though the" >&2
		echo "       .config says otherwise. It takes touches from the cable." >&2
		return 1
	fi
	return 0
}

# blink_refuse_trace <.config> <image>
#
# The same two looks for CONFIG_BLINK_TOUCH_TRACE (trace.conf), the touch
# trace diagnostic: every XPT2046 report goes into a RAM ring and out over
# the console as CSV each time a finger lifts. It boots like a release, so
# nothing after the build would notice it; shipped as an OTA, every desk
# that took it would print every touch onto the cable the daemon reads.
# The image carries touch_trace.c's "#TT-BEGIN" dump header, and nothing
# else prints it.
#
# Called by release.sh only. A trace image is meant to be flashed to a
# board, fused ones included -- flash_encrypted.sh's BLINK_BUILD_DIR exists
# for build-trace -- so the flashing scripts do not refuse it (review of
# develop/fork-picks, 2026-10-28, S6).
blink_refuse_trace() {
	if [ ! -f "$1" ]; then
		echo "FATAL: no $1 -- cannot tell whether this is a touch-trace build." >&2
		echo "       Refusing an image nothing has inspected." >&2
		return 1
	fi
	if [ ! -f "$2" ]; then
		echo "FATAL: no image at $2 to inspect." >&2
		return 1
	fi
	if grep -q '^CONFIG_BLINK_TOUCH_TRACE=y' "$1"; then
		echo "FATAL: CONFIG_BLINK_TOUCH_TRACE=y in $1." >&2
		echo "       This image prints every touch on the console; it is a" >&2
		echo "       diagnostic. A -D value or EXTRA_CONF_FILE=trace.conf stays" >&2
		echo "       in the build directory's CMake cache: rebuild it pristine" >&2
		echo "       (west build -p always) without it." >&2
		return 1
	fi
	if grep -aqF '#TT-BEGIN seq=' "$2"; then
		echo "FATAL: $2 contains the touch trace dump, though the" >&2
		echo "       .config says otherwise. It prints every touch." >&2
		return 1
	fi
	return 0
}

# blink_refuse_pilot <.config> <image>
#
# The same two looks for CONFIG_BLINK_PANEL_PILOT (pilot.conf), the build
# for the two pilot boards, whose display modules differ from production.
# It compiles out main.c's MADCTL correction, so on a production panel it
# renders mirrored, red and blue swapped, and dim (firmware/Kconfig, measured
# 2026-08-21). Shipped as an OTA, that is every desk.
#
# The image look runs the other way round from the others: a pilot build
# prints nothing of its own, but a production build carries the
# "panel: MADCTL=0x" line of the correction it compiles in (present in every
# build directory on this desk, 2026-10-28), and a pilot build has none of
# it. An image without that line is not a production image, whatever its
# .config says.
#
# Called by release.sh only: the pilot boards are flashed with this build on
# purpose (flash_encrypted.sh; dev.sh's fused-chip message says how).
blink_refuse_pilot() {
	if [ ! -f "$1" ]; then
		echo "FATAL: no $1 -- cannot tell whether this is a pilot-panel build." >&2
		echo "       Refusing an image nothing has inspected." >&2
		return 1
	fi
	if [ ! -f "$2" ]; then
		echo "FATAL: no image at $2 to inspect." >&2
		return 1
	fi
	if grep -q '^CONFIG_BLINK_PANEL_PILOT=y' "$1"; then
		echo "FATAL: CONFIG_BLINK_PANEL_PILOT=y in $1." >&2
		echo "       This image is for the two pilot boards; on a production" >&2
		echo "       panel it is mirrored, colour-swapped and dim. A -D value or" >&2
		echo "       EXTRA_CONF_FILE=pilot.conf stays in the build directory's" >&2
		echo "       CMake cache: rebuild it pristine (west build -p always)" >&2
		echo "       without it." >&2
		return 1
	fi
	if ! grep -aqF 'panel: MADCTL=0x' "$2"; then
		echo "FATAL: $2 lacks the production panel correction, though the" >&2
		echo "       .config says otherwise. It would render mirrored." >&2
		return 1
	fi
	return 0
}
