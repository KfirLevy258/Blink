#!/bin/sh
# Program one unit, end to end: build, flash, boot-verify, stamp, verify again.
#
#   tools/burn-claude.sh            tools/burn-codex.sh
#   tools/burn.sh --edition codex [--port /dev/cu.usbserial-XXXX]
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
set -eu

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/.." && pwd)
EDITION=""
PORT="${BLINK_PORT:-}"
SKIP_BUILD=0

while [ $# -gt 0 ]; do
	case "$1" in
	--edition) EDITION="$2"; shift 2 ;;
	--port)    PORT="$2"; shift 2 ;;
	--no-build) SKIP_BUILD=1; shift ;;
	*) echo "usage: $0 --edition claude|codex [--port DEV] [--no-build]" >&2
	   exit 2 ;;
	esac
done

case "$EDITION" in
claude|codex) ;;
*) echo "FATAL: --edition must be claude or codex" >&2; exit 2 ;;
esac

say()  { printf '\n== %s\n' "$*"; }
die()  { printf '\nFAILED: %s\n' "$*" >&2; exit 1; }

PY="${BLINK_PYTHON:-$ROOT/.venv/bin/python}"
[ -x "$PY" ] || PY=$(command -v python3) || die "no python3"

BOARD="${BLINK_BOARD:-esp32_devkitc/esp32/procpu}"
KEY="${BLINK_SIGNING_KEY:-$HOME/.blink/ota_signing_key_p256.pem}"
BUILD="$ROOT/firmware/build-sb"

# ---------------------------------------------------------------- 0. the port
say "Finding the board"
# The installed daemon holds the port. Stop it for the duration and put it back
# on the way out, whatever happens -- including a Ctrl-C mid-flash.
AGENT="gui/$(id -u)/com.blink.bridge"
DAEMON_WAS_UP=0
if launchctl print "$AGENT" >/dev/null 2>&1; then
	DAEMON_WAS_UP=1
	launchctl bootout "$AGENT" >/dev/null 2>&1 || true
fi
restore_daemon() {
	[ "$DAEMON_WAS_UP" = 1 ] || return 0
	launchctl bootstrap "gui/$(id -u)" \
		"$HOME/Library/LaunchAgents/com.blink.bridge.plist" >/dev/null 2>&1 || true
}
# EXIT restores; INT/TERM restore AND EXIT. A trap that only restored let a
# Ctrl-C mid-flash carry straight on to the stamp step, with the daemon just
# put back on the same port -- two processes on one wire, and a unit stamped
# over a torn image.
trap restore_daemon EXIT
trap 'restore_daemon; trap - EXIT; exit 130' INT TERM

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
	# shellcheck disable=SC1090
	. "$HOME/zephyr-v4.4.0/.venv/bin/activate" 2>/dev/null || true
	ZEPHYR_BASE="${ZEPHYR_BASE:-$HOME/zephyr-v4.4.0/zephyr}"
	export ZEPHYR_BASE
	( cd "$ROOT/firmware" && west build --sysbuild -d build-sb -b "$BOARD" . -- \
		-DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \
		-DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$KEY\"" ) >/dev/null 2>&1 \
		|| die "build failed. Run the west build by hand to see why."
	echo "   built"
else
	echo "   (--no-build: using whatever is in $BUILD)"
fi

MCUBOOT="$BUILD/mcuboot/zephyr/zephyr.bin"
APP="$BUILD/firmware/zephyr/zephyr.signed.bin"
[ -f "$MCUBOOT" ] || die "no bootloader at $MCUBOOT"
[ -f "$APP" ]     || die "no signed app at $APP"
# The version this tree builds, so the boot check below can insist the unit
# is running THIS image and not whatever was on it before. With --no-build
# against a stale build directory the two can differ, and a stamp on the
# wrong firmware is still a stamp.
FW_VERSION=$(sed -n 's/^#define BLINK_FW_VERSION "\(.*\)"$/\1/p' \
	"$ROOT/firmware/src/version.h")
[ -n "$FW_VERSION" ] || die "cannot read BLINK_FW_VERSION from firmware/src/version.h"

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
if ! esptool.py --port "$PORT" --baud 115200 write_flash \
	0x1000 "$MCUBOOT" 0x20000 "$APP" >"$FLASH_LOG" 2>&1; then
	tail -5 "$FLASH_LOG" >&2
	die "flash failed (esptool exited non-zero; full log at $FLASH_LOG)"
fi
grep -E "Wrote|Hash of data" "$FLASH_LOG" || true
[ "$(grep -c "Hash of data verified" "$FLASH_LOG")" -eq 2 ] ||
	die "expected both images to verify; see $FLASH_LOG"

# ------------------------------------------------- 4. boot, stamp, confirm
say "Boot-verifying and stamping as $EDITION"
"$PY" - "$PORT" "$EDITION" "$FW_VERSION" <<'EOF' || die "the board did not confirm the stamp"
import json, sys, time
import serial

port, edition, want_fw = sys.argv[1], sys.argv[2], sys.argv[3]
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

s.write((json.dumps({"t": "edition", "v": 2, "edition": edition}) + "\n").encode())
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
"$PY" - "$PORT" "$EDITION" <<'EOF' || die "the edition did not survive a reboot"
import sys, time
import serial
port, edition = sys.argv[1], sys.argv[2]
s = serial.Serial(port, 115200, timeout=0.2)
s.dtr = False; s.rts = True; time.sleep(0.15); s.rts = False
end, out = time.time() + 15, ""
while time.time() < end and "[boot] edition" not in out:
    out += s.read(4096).decode("utf-8", "replace")
s.close()
line = next((l for l in out.splitlines() if "[boot] edition" in l), "")
if not line:
    print("   the board did not report an edition on boot", file=sys.stderr)
    sys.exit(1)
print("   " + line.strip())
if edition not in line:
    print("   WRONG EDITION after reboot -- expected %s" % edition,
          file=sys.stderr)
    sys.exit(1)
EOF

printf '\n================================\n'
printf 'PASS -- this unit is a %s board\n' "$EDITION"
printf '================================\n'
printf 'Put it in a %s enclosure.\n' "$EDITION"
