#!/bin/sh
# Compile and run every standalone firmware host test.
#
#   tests/ci/check_host_tests.sh
#
# These test the firmware's pure logic on a laptop, with no board and no
# Zephyr -- native_sim is Linux-only, so this is how the C in this project
# gets exercised on a Mac. Eight of them existed and NOTHING ran them
# automatically: each carried a `cc ...` line in a comment that a human was
# expected to copy and paste. A test nobody runs is documentation.
#
# The table below is explicit rather than parsed out of those comments: a
# runner that greps for its own build commands fails open the moment a comment
# is reworded, which is the failure mode of every clever version of this.
set -eu

# shellcheck source=tests/ci/lib.sh
. "$(dirname -- "$0")/lib.sh"

WORK="${TMPDIR:-/tmp}/blink-host-tests"
rm -rf "$WORK"
mkdir -p "$WORK"

CC="${CC:-cc}"
SRC="$ROOT/firmware/src"
failures=0

# name | extra sources (space separated, relative to firmware/src) | extra cflags
run_one() {
	name=$1
	extra=$2
	cflags=$3
	src="$ROOT/tests/$name/host_test.c"

	if [ ! -f "$src" ]; then
		echo "MISSING: tests/$name/host_test.c" >&2
		failures=$((failures + 1))
		return
	fi

	objs=""
	for e in $extra; do
		# An absolute path is a source outside firmware/src (mbedTLS's).
		# shortcut: $extra and $cflags are word-split, so a path with a
		# space in it (an mbedTLS checkout under "My Projects") breaks the
		# build line; quote through arrays if a machine ever needs one.
		case $e in
		/*) objs="$objs $e" ;;
		*) objs="$objs $SRC/$e" ;;
		esac
	done

	# -Wall -Werror: these compile in seconds and a warning in firmware
	# logic is worth stopping for. The Zephyr build already runs with
	# -Wall, so anything caught here would have been caught there anyway --
	# but here it is caught before a nine-minute build.
	# shellcheck disable=SC2086
	# $cflags AFTER the sources. It carries -lm, and GNU ld on Ubuntu runs
	# with --as-needed: a library named before the objects that need it is
	# dropped, and usage_contrast failed to link with an undefined pow().
	# Apple's linker does not care about the order, which is why it passed
	# on the machine it was written on.
	if ! $CC -Wall -Werror -I "$SRC" "$src" $objs $cflags \
		-o "$WORK/$name" 2>"$WORK/$name.cc"; then
		printf '  %-14s BUILD FAILED\n' "$name"
		sed 's/^/      /' "$WORK/$name.cc"
		failures=$((failures + 1))
		return
	fi

	if "$WORK/$name" >"$WORK/$name.out" 2>&1; then
		n=$(grep -c '^PASS' "$WORK/$name.out" || true)
		# A suite that ran nothing is not a suite that is fine, and
		# until now this runner could not tell the two apart. Two of
		# them printed only on failure, so they reported "ok (0
		# checks)" and their whole main() could be deleted without
		# changing a byte of this output -- which is how "the sleep
		# logic is covered" stayed true on paper while nothing
		# enforced it. Exiting zero is the easiest thing in the world
		# for broken code to do; asserting something is not. Every
		# suite here has checks, so zero means they stopped running.
		if [ "$n" -eq 0 ]; then
			printf '  %-14s NO CHECKS RAN\n' "$name"
			failures=$((failures + 1))
			return
		fi
		printf '  %-14s ok (%s checks)\n' "$name" "$n"
	else
		printf '  %-14s FAILED\n' "$name"
		grep '^FAIL' "$WORK/$name.out" | sed 's/^/      /' || true
		failures=$((failures + 1))
	fi
}

echo "== firmware host tests"
run_one backlight    ""                ""
run_one bootanim     "bootanim_dec.c"  ""
# cfg_store.c unchanged against a RAM flash; the stubs stand in for Zephyr.
# CFG_STUB_CAPTURE keeps its log lines for the boot-line check.
run_one cfg_store    "cfg_store.c"     "-I$ROOT/tests/cfg_store/stub -DCFG_STUB_CAPTURE"
# "Edit on computer" held while its builder is open: every way out of it.
run_one edit_hold    "edit_hold.c theme.c msg_parse.c" "-lm"
run_one fatal_policy "fatal_policy.c"  ""
run_one fmt          "fmt.c"           ""
# Which link protocol lines go to: USB wins, backoff, the verify window.
run_one link         "link.c"          ""
run_one logo         "logo_parse.c bootanim_dec.c" ""
run_one msg_parse    "msg_parse.c"     ""
# The WiFi scan table: one entry a name, strongest first, at most 20.
run_one net_link     ""                ""
run_one ota_parse    "ota_parse.c"     ""
run_one sleep_gate   "sleep_gate.c usage_freshness.c" ""
run_one theme        "theme.c msg_parse.c" "-lm"
# ui_theme.c and cfg_store.c unchanged, on cfg_store's RAM flash; the test
# file stands in for proto, the screens and LVGL's timer.
run_one ui_theme     "ui_theme.c cfg_store.c theme.c msg_parse.c" \
	"-I$ROOT/tests/ui_theme/stub -I$ROOT/tests/cfg_store/stub -lm"
run_one ui_slide_geom ""               ""
run_one ui_swipe_geom ""               ""
# The bench touch injector (CONFIG_BLINK_TOUCH_INJECT): its transform against
# the LVGL glue, and its strokes against ui_swipe's own rules.
run_one touch_inject ""                ""
# Its widget listing's wire format: the worst page fits, a label needs no
# escaping, the aim grid finds a strip beside a popup.
run_one touch_widgets ""               ""
run_one upd_prompt  "upd_prompt.c"    ""
run_one upd_row     "upd_row.c"       ""
run_one upd_tap     "upd_tap.c"       ""
run_one whatsnew    "whatsnew.c ota_parse.c" ""
run_one usage_contrast "theme.c msg_parse.c" "-lm"
run_one usage_freshness "usage_freshness.c" ""
run_one usage_layout ""                ""
run_one usage_state  "usage_state.c"   ""
run_one svc_state    "svc_state.c fmt.c" ""

# The handshake and the frame, on the toy cipher in tests/support (an
# absolute path is a source outside firmware/src, as run_one allows).
run_one wl_hs    "wl_hs.c $ROOT/tests/support/wl_crypto_fake.c"    ""
run_one wl_frame "wl_frame.c $ROOT/tests/support/wl_crypto_fake.c" ""

# The board's cipher (wl_crypto_mbedtls.c, unchanged) against the PC's
# (pc/wifi_crypto.py, through tests/wl_crypto_real/vectors.h) at every
# plaintext length, on mbedTLS's own sources: the library files the wrapper
# reaches, built with tests/wl_crypto_real/mbedtls_config.h. The sources are
# the module Zephyr 4.3.1 pins (west.yml:318, 3.6.6): BLINK_MBEDTLS_SRC when
# set (CI fetches the pinned tarball, .github/workflows/installer.yml), else
# the workspace's modules/crypto/mbedtls, else its mbedtls-3.6 copy. A
# machine with none skips this one suite and every other runs as before;
# BLINK_REQUIRE_MBEDTLS=1 makes that a failure, so CI cannot skip it quietly.
mbedtls_src() {
	if [ -n "${BLINK_MBEDTLS_SRC:-}" ]; then
		printf '%s\n' "$BLINK_MBEDTLS_SRC"
		return 0
	fi
	# shellcheck source=tools/lib_zephyr.sh
	. "$ROOT/tools/lib_zephyr.sh"
	ws=$(blink_zephyr_ws) || return 1
	for d in "$ws/modules/crypto/mbedtls" "$ws/modules/crypto/mbedtls-3.6"; do
		if [ -f "$d/library/chachapoly.c" ]; then
			printf '%s\n' "$d"
			return 0
		fi
	done
	return 1
}
if MBEDTLS=$(mbedtls_src); then
	mbed_objs="wl_crypto_mbedtls.c"
	for f in chachapoly.c chacha20.c poly1305.c hkdf.c md.c sha256.c \
		platform_util.c constant_time.c; do
		mbed_objs="$mbed_objs $MBEDTLS/library/$f"
	done
	run_one wl_crypto_real "$mbed_objs" \
		"-I$MBEDTLS/include -I$ROOT -I$ROOT/tests/wl_crypto_real/stub -DMBEDTLS_CONFIG_FILE=\"tests/wl_crypto_real/mbedtls_config.h\""
	# The handshake and frame code on that same real cipher, against the
	# PC's session bytes (tools/gen_wire_vectors.py).
	run_one wl_wire_real "wl_hs.c wl_frame.c $mbed_objs" \
		"-I$MBEDTLS/include -I$ROOT -I$ROOT/tests/wl_crypto_real/stub -DMBEDTLS_CONFIG_FILE=\"tests/wl_crypto_real/mbedtls_config.h\""
elif [ "${BLINK_REQUIRE_MBEDTLS:-}" = 1 ]; then
	printf '  %-14s NO MBEDTLS SOURCE (BLINK_REQUIRE_MBEDTLS=1)\n' \
		"wl_crypto_real, wl_wire_real"
	failures=$((failures + 1))
else
	echo "  SKIP wl_crypto_real, wl_wire_real (no mbedTLS source; set BLINK_MBEDTLS_SRC)"
fi

if [ "$failures" -ne 0 ]; then
	printf '\n%d host test(s) failed\n' "$failures" >&2
	exit 1
fi
echo "PASS [host tests]"
