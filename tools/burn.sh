#!/bin/sh
# Program one unit, end to end: build, flash, boot-verify, stamp, verify again.
#
#   tools/burn-claude.sh            tools/burn-codex.sh
#   tools/burn.sh --edition codex [--port /dev/cu.usbserial-XXXX]
#   tools/burn.sh --edition claude --logo acme.png      (a company unit)
#
# --logo makes a COMPANY unit: the picture (or clip, or a .bin already built
# by tools/encode_logo.py) is written to the logo partition in the same
# esptool call as the firmware, and the boot check insists the board saw it.
# Without --logo the logo partition is ERASED, so a re-burned board is an
# individual unit again -- the default is individual, and it has to stay the
# default on a board that was something else last week.
#
# This is the factory step. It is written to FAIL LOUDLY rather than half
# succeed, because the two things it does are the two that cannot be undone in
# the field: the edition latches on first write, and a unit boxed with the
# wrong clip or an unstamped record is a unit that has to come back.
#
# What it refuses to do:
#   - flash a FUSED chip (this writes plaintext; a fused ROM cannot read it and
#     the board goes dark until tools/flash_encrypted.sh restores it)
#   - flash without MCUboot (a plain `west flash` writes only the app and takes
#     the bootloader with it, which silently removes OTA from the unit)
#   - report success on a board that did not confirm the stamp
#   - flash a CONFIG_BLINK_FATAL_SELFTEST build, which faults on every boot,
#     unless --selftest says that is the point (the bench test in
#     firmware/Kconfig); the unit it makes is never one to box
#   - flash a CONFIG_BLINK_TOUCH_INJECT build, which lets the computer drive
#     the touch panel, unless --bench says that is the point (the bench
#     touch injector, tools/board_touch.py); never one to box either
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
# Sourced here rather than beside lib_efuse.sh below, because the Python this
# script picks (a few lines down) may come from the Zephyr venv.
. "$ROOT/tools/lib_zephyr.sh"
. "$ROOT/tools/lib_launchd.sh"
EDITION=""
PORT="${BLINK_PORT:-}"
SKIP_BUILD=0
SELFTEST=0
BENCH=0
LOGO=""
# Must match logo_partition in firmware/boards/esp32_devkitc_esp32_procpu.overlay.
LOGO_OFF=0x330000
LOGO_SIZE=0x80000

while [ $# -gt 0 ]; do
	case "$1" in
	--edition) EDITION="$2"; shift 2 ;;
	--port)    PORT="$2"; shift 2 ;;
	--no-build) SKIP_BUILD=1; shift ;;
	--logo)    LOGO="$2"; shift 2 ;;
	--selftest) SELFTEST=1; shift ;;
	--bench)   BENCH=1; shift ;;
	*) echo "usage: $0 --edition claude|codex [--port DEV] [--no-build] [--logo FILE] [--selftest] [--bench]" >&2
	   exit 2 ;;
	esac
done

case "$EDITION" in
claude|codex) ;;
*) echo "FATAL: --edition must be claude or codex" >&2; exit 2 ;;
esac
[ -z "$LOGO" ] || [ -f "$LOGO" ] || [ -d "$LOGO" ] || { echo "FATAL: --logo $LOGO: no such file" >&2; exit 2; }

say()  { printf '\n== %s\n' "$*"; }
die()  { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

# Two toolchains, found without an activation step to remember on the bench:
#
#  - the Python with pyserial, numpy and pillow (the boot check and the logo
#    encoder): BLINK_PYTHON, else a repo venv, else the Zephyr venv every
#    firmware build already uses;
#  - esptool.py: whatever is on PATH, else the espressif tools directory
#    lib_efuse.sh already uses (BLINK_ETOOLS or the Python 3.10 framework
#    bin), else the Python above's own bin. Never prepended over an
#    esptool.py that is already on PATH -- one chosen on purpose, or the
#    stubs in tests/ci/check_factory.sh, must win.
PY="${BLINK_PYTHON:-$ROOT/.venv/bin/python}"
# `|| true` inside the substitution, and it is load-bearing.
#
# blink_zephyr_ws returns non-zero when there is no workspace, which makes the
# ASSIGNMENT non-zero, which makes this whole `||` list non-zero -- and `set -e`
# then killed the script here, silently, with no message and status 1. On a
# machine with a Zephyr workspace it never fired; on one without, `burn.sh`
# exited before printing a single line. Two of the three fleet desks and every
# CI runner are machines without one, which is why the factory check failed
# there and passed on the author's desk for days.
#
# The next line already handles "no usable interpreter" properly, and says so.
[ -x "$PY" ] || PY="$(blink_zephyr_ws 2>/dev/null || true)/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3) || die "no python3"
for mod in serial numpy PIL; do
	"$PY" -c "import $mod" 2>/dev/null ||
		die "$PY lacks the '$mod' module. Run with BLINK_PYTHON=<python that has pyserial, numpy and pillow>"
done
ETOOLS="${BLINK_ETOOLS:-/Library/Frameworks/Python.framework/Versions/3.10/bin}"
if ! command -v esptool.py >/dev/null 2>&1; then
	for d in "$ETOOLS" "$(dirname -- "$PY")"; do
		if [ -x "$d/esptool.py" ]; then
			PATH="$d:$PATH"
			export PATH
			break
		fi
	done
fi
# The pip hint carries the pins, for the interpreter this script chose: a
# bare `pip install esptool` resolves bitstring 5.0.0, which espefuse cannot
# import (see pc/requirements.txt). Same line lib_zephyr.sh prints.
if ! command -v esptool.py >/dev/null 2>&1; then
	HINT=$(blink_zephyr_pip_hint "$PY") ||
		HINT="install esptool and bitstring at the versions pc/requirements.txt pins"
	die "esptool.py not found on PATH, in $ETOOLS, or next to $PY ($HINT, or set BLINK_ETOOLS)"
fi

BOARD="${BLINK_BOARD:-esp32_devkitc/esp32/procpu}"
KEY="${BLINK_SIGNING_KEY:-$HOME/.blink/ota_signing_key_p256.pem}"
# Overridable so tests/ci/check_factory.sh can point this at a fake build
# (and BLINK_ETOOLS, read by lib_efuse.sh, at fake tools) and run the whole
# script against a scripted board. Unset behaves exactly as before.
BUILD="${BLINK_BUILD_DIR:-$ROOT/firmware/build-sb}"

# ---------------------------------------------------------------- 0. the port
say "Finding the board"
# The installed daemon holds the port. Stop it for the duration and put it back
# on the way out, whatever happens -- including a Ctrl-C mid-flash.
AGENT="gui/$(id -u)/com.blink.bridge"
DAEMON_WAS_UP=0
restore_daemon() {
	[ "$DAEMON_WAS_UP" = 1 ] || return 0
	# Never over an instance that is still exiting: bootstrap fails against
	# a job launchd still holds, and a kickstart could only reach the old
	# instance. An interrupt during the wait below lands here with it still
	# leaving, so wait for it again rather than act on it.
	if ! blink_wait_agent_gone "$AGENT"; then
		echo "warning: the installed daemon is still stopping; once it has, start it with:" >&2
		echo "  launchctl bootstrap gui/$(id -u) $HOME/Library/LaunchAgents/com.blink.bridge.plist" >&2
		echo "  launchctl kickstart $AGENT" >&2
		return 0
	fi
	launchctl bootstrap "gui/$(id -u)" \
		"$HOME/Library/LaunchAgents/com.blink.bridge.plist" >/dev/null 2>&1 || true
	# BOOTSTRAP REGISTERS; IT DOES NOT START. RunAtLoad is honoured at login,
	# not on a bootstrap, so this left the desk with no daemon at all until
	# the next login -- the board silently unattended after a burn that
	# printed PASS. Measured five times out of five on the install path
	# (pc/cli.py _LaunchdBackend.install, 2026-09-04); this is the same
	# defect in the same shape, and it bit here on 2026-09-06.
	#
	# Plain kickstart, not -k: on the job just bootstrapped it starts it all
	# the same (pc/cli.py _LaunchdBackend.start), and if anything else loaded
	# it meanwhile, -k would kill whatever that instance is doing -- a
	# firmware write included -- where plain kickstart leaves it be.
	launchctl kickstart "$AGENT" >/dev/null 2>&1 || true
}
# EXIT restores; INT/TERM restore AND EXIT. A trap that only restored let a
# Ctrl-C mid-flash carry straight on to the stamp step, with the daemon just
# put back on the same port -- two processes on one wire, and a unit stamped
# over a torn image.
trap restore_daemon EXIT
trap 'restore_daemon; trap - EXIT; exit 130' INT TERM

if launchctl print "$AGENT" >/dev/null 2>&1; then
	DAEMON_WAS_UP=1
	launchctl bootout "$AGENT" >/dev/null 2>&1 || true
	# bootout RETURNS before the process is gone, and the process holds the
	# port until it is. Probing straight afterwards read as "cannot tell
	# whether this chip is fused -- is the port free?" and refused to flash
	# a board that was perfectly fine (2026-09-06, this desk). Wait for the
	# job to actually leave rather than racing it -- for as long as the
	# daemon's own stop grace, because a daemon inside an OTA write holds
	# its SIGTERM for up to 180 s (tools/lib_launchd.sh). This used to give
	# up after 5 s and carry on with the port still held.
	#
	# Under the traps, so a Ctrl-C during those minutes still puts the
	# daemon back once it has gone. A job still there at the deadline is
	# left alone, and so is the restore: waiting the same minutes again in
	# the EXIT trap would tell the reader nothing new.
	if ! blink_wait_agent_gone "$AGENT"; then
		DAEMON_WAS_UP=0
		die "the installed daemon is still stopping after $((BLINK_STOP_GRACE_S + BLINK_STOP_MARGIN_S)) s.
       Nothing was flashed. Check it with: launchctl print $AGENT"
	fi
fi

if [ -z "$PORT" ]; then
	PORT=$("$PY" - <<'EOF'
from serial.tools import list_ports
KNOWN = {(0x1A86, 0x7523), (0x1A86, 0x7522), (0x1A86, 0x5523),
         (0x10C4, 0xEA60), (0x0403, 0x6001)}
hits = [p.device for p in list_ports.comports()
        if (p.vid, p.pid) in KNOWN or p.vid == 0x303A]
print(hits[0] if len(hits) == 1 else ("" if not hits else "MANY:" + ",".join(hits)))
EOF
)
fi
case "$PORT" in
"")      die "no board found. Plug one in, or pass --port" ;;
MANY:*)  die "more than one candidate: ${PORT#MANY:}
       Burn one unit at a time, or name it with --port." ;;
esac
echo "   port: $PORT"

# ------------------------------------------------------- 1. fused or not
say "Checking the chip"
. "$ROOT/tools/lib_efuse.sh"
efuse_probe "$PORT"
case "$EFUSE_STATE" in
plaintext) echo "   FLASH_CRYPT_CNT = 0b$EFUSE_BITS -- plaintext, correct for this script" ;;
encrypted) die "this chip is FUSED (0b$EFUSE_BITS${EFUSE_MAC:+, MAC $EFUSE_MAC}).
       This script writes plaintext, which a fused ROM cannot read.
       Use tools/flash_encrypted.sh instead." ;;
*)         die "cannot tell whether this chip is fused -- $EFUSE_REASON
       Refusing to flash blind." ;;
esac
[ -n "${EFUSE_MAC:-}" ] && echo "   MAC: $EFUSE_MAC"

# ------------------------------------------------------------- 2. build
if [ "$SKIP_BUILD" = 0 ]; then
	say "Building (signed, with MCUboot)"
	[ -f "$KEY" ] || die "signing key not found at $KEY.
       Every unit must be signed with the SAME key or its OTA updates will be
       rejected in the field. See tools/backup_keys.sh."
	# Both say why on stderr first; this line only stops the script. It used
	# to read "cannot build without a Zephyr workspace", which was wrong
	# whenever the workspace had been found and only a tool in it was missing.
	blink_zephyr_activate || die "Zephyr environment not ready (see above)."
	blink_zephyr_build_ready || die "Zephyr environment not ready (see above)."
	# Kept, not thrown away: "run the west build by hand to see why" sent the
	# operator to repeat a multi-minute build to read an error this one had
	# already printed. Outside the build tree, which a pristine rebuild wipes.
	BUILD_LOG=$(mktemp "${TMPDIR:-/tmp}/blink-burn-build.XXXXXX") ||
		die "could not create a temp file for the build log"
	if ! ( cd "$ROOT/firmware" && west build --sysbuild -d build-sb -b "$BOARD" . -- \
		-DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
		-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$KEY\"" ) >"$BUILD_LOG" 2>&1; then
		tail -30 "$BUILD_LOG" >&2
		die "build failed (last 30 lines above; full log at $BUILD_LOG)"
	fi
	rm -f "$BUILD_LOG"
	echo "   built"
else
	echo "   (--no-build: using whatever is in $BUILD)"
fi

MCUBOOT="$BUILD/mcuboot/zephyr/zephyr.bin"
SIGNED="$BUILD/firmware/zephyr/zephyr.signed.bin"
[ -f "$MCUBOOT" ] || die "no bootloader at $MCUBOOT"
[ -f "$SIGNED" ]  || die "no signed app at $SIGNED"

# A selftest image faults BLINK_FATAL_SELFTEST_AT_S seconds into every boot,
# and the boot check below is over in less than that, so without this a burn
# of one says PASS and the unit goes in a box. It gets into build-sb the way
# the Kconfig help warns: one -D on the command line, kept in the CMake cache.
# --selftest is the bench test that help describes, asked for by name. It is
# a permission, not a label: the check runs either way, and only an image it
# refuses is called a selftest unit.
#
# A build with no .config cannot be inspected at all, and is refused here
# whatever the flags: under --selftest or --bench it used to fall into the
# permitted branch and be flashed as the bench image it was never shown to
# be (review of the injector, NICE-5). The image itself was checked above.
. "$ROOT/tools/lib_selftest.sh"
[ -f "$BUILD/firmware/zephyr/.config" ] ||
	die "no $BUILD/firmware/zephyr/.config -- cannot tell what this build is"
SELFTEST_IMAGE=0
if [ "$SELFTEST" = 0 ]; then
	blink_refuse_selftest "$BUILD/firmware/zephyr/.config" "$SIGNED" ||
		die "this is a selftest build (above). Pass --selftest if that is the point."
elif blink_refuse_selftest "$BUILD/firmware/zephyr/.config" "$SIGNED" 2>/dev/null; then
	echo "   --selftest given, but this is not a selftest build: a normal unit"
else
	SELFTEST_IMAGE=1
	echo "   --selftest: flashing a SELFTEST build that faults on every boot"
fi

# The touch injector the same way, with its own flag. Its image does not
# fault: it boots, passes every check below and looks like a release, which
# is why it must never reach a box -- it takes touches from the cable, so the
# computer it is plugged into can operate the panel. --bench, like
# --selftest, is a permission and not a label.
INJECT_IMAGE=0
if [ "$BENCH" = 0 ]; then
	blink_refuse_inject "$BUILD/firmware/zephyr/.config" "$SIGNED" ||
		die "this is a touch-inject build (above). Pass --bench if that is the point."
elif blink_refuse_inject "$BUILD/firmware/zephyr/.config" "$SIGNED" 2>/dev/null; then
	echo "   --bench given, but this is not a touch-inject build: a normal unit"
else
	INJECT_IMAGE=1
	echo "   --bench: flashing a TOUCH INJECT build the computer can drive"
fi

# ------------------------------------------- 2a. a CONFIRMED image to flash
# zephyr.signed.bin is signed but not confirmed: its trailer says "boot this
# once". Right for OTA -- it lands in slot 1 and main.c confirms it once the
# image proves itself. Wrong here: a board on the bench has no daemon and no
# network, never becomes healthy, and reverts at 90 s.
#
# On a blank unit that is invisible, because there is nothing to revert TO.
# On a re-burn it silently undoes the flash minutes after this script printed
# PASS, and the board looks like it is crash-looping. Measured 2026-09-06:
# the burn said "booted: fw 1.3.0" and the board served 1.2.5 shortly after.
say "Signing a confirmed image for direct flashing"
APP="$BUILD/firmware/zephyr/zephyr.confirmed.bin"
rm -f "$APP"
"$PY" "$ROOT/tools/sign_confirmed.py" --build-dir "$BUILD" --out "$APP" \
	|| die "could not sign a confirmed image. Flashing the unconfirmed one
       would produce a unit that reverts 90 s after this script says PASS."
[ -f "$APP" ] || die "sign_confirmed.py reported success but wrote no $APP"
# The version this tree builds, so the boot check below can insist the unit
# is running THIS image and not whatever was on it before. With --no-build
# against a stale build directory the two can differ, and a stamp on the
# wrong firmware is still a stamp.
FW_VERSION=$(sed -n 's/^#define BLINK_FW_VERSION "\(.*\)"$/\1/p' \
	"$ROOT/firmware/src/version.h")
[ -n "$FW_VERSION" ] || die "cannot read BLINK_FW_VERSION from firmware/src/version.h"

# ------------------------------------------------------------- 2b. logo
# Built BEFORE anything is written to the board: a bad picture must fail the
# burn while the unit is still whatever it was, not after the app is on it.
LOGO_BIN=""
if [ -n "$LOGO" ]; then
	say "Encoding the company logo"
	case "$LOGO" in
	*.bin) LOGO_BIN="$LOGO" ;;
	*)
		LOGO_BIN="$BUILD/logo.bin"
		mkdir -p "$BUILD"
		if [ -d "$LOGO" ]; then
			src="--frames"
		else
			case "$LOGO" in
			*.mp4|*.mov|*.webm|*.mkv|*.gif) src="--video" ;;
			*) src="--image" ;;
			esac
		fi
		"$PY" "$ROOT/tools/encode_logo.py" "$src" "$LOGO" --out "$LOGO_BIN" \
			|| die "could not encode $LOGO (see tools/encode_logo.py --help)"
		;;
	esac
	"$PY" "$ROOT/tools/encode_logo.py" --info "$LOGO_BIN" \
		|| die "$LOGO_BIN is not a logo image this firmware can read"
	[ "$(wc -c <"$LOGO_BIN")" -le $((LOGO_SIZE)) ] \
		|| die "$LOGO_BIN is larger than the logo partition ($LOGO_SIZE bytes)"
fi

# ------------------------------------------------------------- 3. flash
say "Flashing"
# BOTH images, explicitly. `west flash` writes only the app at 0x20000 and
# leaves whatever is at 0x1000 -- which on a fresh board is the factory
# bootloader and on a re-burned one may be nothing. Either way the unit loses
# its OTA chain, silently, and only finds out months later in someone's house.
#
# esptool's own exit status decides, not grep's. Piping into grep made the
# pipeline's status grep's (POSIX sh has no pipefail), so esptool dying after
# the first image's "Wrote" line still counted as success -- and the script
# went on to stamp a unit with a bootloader and a torn app. Now the output
# goes to a file, the status is esptool's, and BOTH images must have been
# hash-verified.
FLASH_LOG="$BUILD/flash.log"
if [ -n "$LOGO_BIN" ]; then
	# The logo rides in the same call, so a unit is never left with new
	# firmware and last week's logo (or none) because a second call failed.
	WANT_VERIFIED=3
	if ! esptool.py --port "$PORT" --baud 115200 write_flash \
		0x1000 "$MCUBOOT" 0x20000 "$APP" "$LOGO_OFF" "$LOGO_BIN" \
		>"$FLASH_LOG" 2>&1; then
		tail -5 "$FLASH_LOG" >&2
		die "flash failed (esptool exited non-zero; full log at $FLASH_LOG)"
	fi
else
	# No logo means NO logo: erase the partition rather than leave whatever
	# a previous burn put there. Erased first, so a failure here stops the
	# burn before the firmware is touched.
	WANT_VERIFIED=2
	if ! esptool.py --port "$PORT" --baud 115200 erase_region \
		"$LOGO_OFF" "$LOGO_SIZE" >"$FLASH_LOG" 2>&1; then
		tail -5 "$FLASH_LOG" >&2
		die "could not erase the logo partition (log at $FLASH_LOG)"
	fi
	echo "   logo partition erased (individual unit)"
	if ! esptool.py --port "$PORT" --baud 115200 write_flash \
		0x1000 "$MCUBOOT" 0x20000 "$APP" >>"$FLASH_LOG" 2>&1; then
		tail -5 "$FLASH_LOG" >&2
		die "flash failed (esptool exited non-zero; full log at $FLASH_LOG)"
	fi
fi
grep -E "Wrote|Hash of data" "$FLASH_LOG" || true
[ "$(grep -c "Hash of data verified" "$FLASH_LOG")" -eq "$WANT_VERIFIED" ] ||
	die "expected $WANT_VERIFIED images to verify; see $FLASH_LOG"

# ------------------------------------------------- 4. boot, stamp, confirm
say "Boot-verifying and stamping as $EDITION"
# PYTHONDONTWRITEBYTECODE: it imports pc/ from the tree (CLAUDE.md).
PYTHONDONTWRITEBYTECODE=1 "$PY" - "$PORT" "$EDITION" "$FW_VERSION" "$ROOT" <<'EOF' || die "the board did not confirm the stamp"
import json, sys, time
import serial

port, edition, want_fw = sys.argv[1], sys.argv[2], sys.argv[3]
# pc/ is stdlib only, so the daemon's own encoder imports into any
# interpreter that has pyserial. Its edition message, through the same
# encode_checked every production writer uses (CLAUDE.md), rather than a
# second hand-built copy of the line here.
sys.path.insert(0, sys.argv[4])
from pc import protocol
s = serial.Serial(port, 115200, timeout=0.2)
s.dtr = False; s.rts = True; time.sleep(0.15); s.rts = False

seen = ""
end = time.time() + 20
hello = None
while time.time() < end and hello is None:
    seen += s.read(4096).decode("utf-8", "replace")
    for line in seen.splitlines():
        if '"t":"hello"' in line or '"t": "hello"' in line:
            try:
                hello = json.loads(line)
            except ValueError:
                pass
if hello is None:
    print("   no hello in 20 s -- the board did not boot", file=sys.stderr)
    sys.exit(1)
if "MCUboot" not in seen and "chainload" not in seen:
    print("   no MCUboot banner -- the bootloader is missing, so this unit"
          " could never take an OTA", file=sys.stderr)
    sys.exit(1)
print("   booted: fw %s, board_id %s" % (hello.get("fw"), hello.get("board_id")))
if hello.get("fw") != want_fw:
    print("   the board runs %s but this tree builds %s -- the flash did not"
          " take, or the build directory is stale" % (hello.get("fw"), want_fw),
          file=sys.stderr)
    sys.exit(1)

raw, why = protocol.encode_checked(protocol.edition(edition))
if raw is None:
    print("   edition message not sent: %s" % why, file=sys.stderr)
    sys.exit(1)
s.write(raw)
s.flush()

# Wait for a COMPLETE line, not just the marker.
#
# Stopping at the first sight of "[cfg] edition" caught the message mid-print:
# the text read back as "edition already stamped as" with the rest still in
# flight, so the "refusing" test missed and this script reported PASS on a
# board it should have rejected. Found by running it against a stamped unit,
# which is the only way that would ever have shown up.
def read_line(sock, marker, timeout):
    end, buf = time.time() + timeout, ""
    while time.time() < end:
        buf += sock.read(4096).decode("utf-8", "replace")
        # The marker AND the newline that ends its line: only then is the
        # text complete enough to test.
        if marker in buf and "\n" in buf.split(marker, 1)[1]:
            return next(l for l in buf.splitlines() if marker in l)
    return ""

line = read_line(s, "[cfg] edition", 6.0)
s.close()
if not line:
    print("   the board never answered the edition message. Firmware older"
          " than this feature ignores it.", file=sys.stderr)
    sys.exit(1)
print("   " + line.split("[cfg] ", 1)[-1].strip())
if "refusing" in line:
    # Already stamped. Whether that is a failure depends on WHAT it says.
    if ("stamped as %s" % edition) in line:
        # A re-burn of the same edition. The image is new, the record is
        # already right, and there is nothing to fix -- say so plainly rather
        # than failing a unit that is correct.
        print("   (already this edition -- re-burn, nothing to change)")
    else:
        print("   WRONG EDITION, and it cannot be changed. This unit is"
              " already stamped as\n   something else; clearing it means"
              " erasing the config partition over USB.", file=sys.stderr)
        sys.exit(1)
EOF

# --------------------------------------------- 5. it has to survive a reboot
say "Confirming the stamp survives a power cycle"
"$PY" - "$PORT" "$EDITION" "$LOGO_BIN" <<'EOF' || die "the edition or the logo did not survive a reboot"
import sys, time
import serial
port, edition, logo = sys.argv[1], sys.argv[2], sys.argv[3]
s = serial.Serial(port, 115200, timeout=0.2)
s.dtr = False; s.rts = True; time.sleep(0.15); s.rts = False
# The logo line is printed AFTER the boot clip has played (it is the next
# thing the splash does), so this waits through the clip, not just to hello.
end, out = time.time() + 30, ""
while time.time() < end and "[boot] logo:" not in out:
    out += s.read(4096).decode("utf-8", "replace")
s.close()
def find(marker):
    return next((l for l in out.splitlines() if marker in l), "")
line = find("[boot] edition")
if not line:
    print("   the board did not report an edition on boot", file=sys.stderr)
    sys.exit(1)
print("   " + line.strip())
if edition not in line:
    print("   WRONG EDITION after reboot -- expected %s" % edition,
          file=sys.stderr)
    sys.exit(1)
line = find("[boot] logo:")
if not line:
    print("   the board did not report a logo state on boot (firmware older"
          " than the logo partition?)", file=sys.stderr)
    sys.exit(1)
print("   " + line.strip())
if logo and "company" not in line:
    print("   the logo was flashed but the board does not see it", file=sys.stderr)
    sys.exit(1)
if not logo and "none" not in line:
    print("   no logo was flashed but the board still shows one", file=sys.stderr)
    sys.exit(1)
EOF

printf '\n================================\n'
if [ -n "$LOGO_BIN" ]; then
	printf 'PASS -- this unit is a %s board with a company logo\n' "$EDITION"
else
	printf 'PASS -- this unit is a %s board (individual)\n' "$EDITION"
fi
printf '================================\n'
if [ "$SELFTEST_IMAGE" = 1 ]; then
	printf 'SELFTEST build: it faults on every boot -- never box this unit.\n'
elif [ "$INJECT_IMAGE" = 1 ]; then
	printf 'TOUCH INJECT build: the computer can drive its panel -- never box this unit.\n'
else
	printf 'Put it in a %s enclosure.\n' "$EDITION"
fi
