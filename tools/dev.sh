#!/usr/bin/env bash
# Dev loop for the CYD board.
#
#   tools/dev.sh flash   -- stop daemon, build, flash, restart daemon
#   tools/dev.sh up      -- start the daemon in the background
#   tools/dev.sh down    -- stop the daemon (frees the port)
#   tools/dev.sh log     -- tail the daemon log
#
# Only ONE process may own the serial port, so the daemon must be down while
# esptool talks to the board. `flash` sequences that for you: for this
# script's own daemon, and for the one `blink install` registered with
# launchd, which it steps aside and puts back the way tools/burn.sh does.
set -euo pipefail

PORT="${PORT:-$(ls /dev/cu.usbserial* 2>/dev/null | head -1 || true)}"
BOARD="esp32_devkitc/esp32/procpu"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="/tmp/claude-usage-bridge.log"
PIDFILE="/tmp/claude-usage-bridge.pid"

activate() {
	# shellcheck disable=SC1090
	source "$ROOT/tools/lib_zephyr.sh"
	blink_zephyr_activate || exit 1
}

down() {
	if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
		kill "$(cat "$PIDFILE")" 2>/dev/null || true
		sleep 1
	fi
	pkill -f claude_usage_bridge.py 2>/dev/null || true
	rm -f "$PIDFILE"
	# A signalled daemon can still be closing the port half a second later,
	# and `flash` then found its own daemon on the port and refused. Wait for
	# it to be gone, for up to 3 s.
	local i=0
	while [ $i -lt 12 ] && pgrep -f claude_usage_bridge.py >/dev/null 2>&1; do
		i=$((i + 1))
		sleep 0.25
	done
	if lsof "$PORT" >/dev/null 2>&1; then
		echo "warning: $PORT still held by:"
		lsof "$PORT"
	fi
}

# The login agent `blink install` registers, and the plist it writes: the
# same names pc/cli.py (LABEL, plist_path) and tools/burn.sh use.
AGENT="gui/$(id -u)/com.blink.bridge"
AGENT_PLIST="$HOME/Library/LaunchAgents/com.blink.bridge.plist"
# 1 only once THIS run has found the agent loaded and booted it out, so a dev
# flash never hands a machine an installed daemon it was not already running.
INSTALLED_WAS_UP=0
# 1 when the agent this run booted out was still exiting at the end of the
# stop grace: relaunch_agent then says how to finish rather than wait again.
INSTALLED_STUCK=0
# 1 when the agent refused its bootout and is still running: the restore
# then starts nothing, neither the agent (it never left) nor the dev daemon
# (it would share the port with it).
INSTALLED_LEFT_RUNNING=0
# shellcheck source=lib_launchd.sh
. "$ROOT/tools/lib_launchd.sh"

# `down` only knows this script's own daemon. On a desk that has run `blink
# install`, the port belongs to the launchd agent, and the eFuse probe below
# then said "cannot tell whether this chip is fused -- is the port free?"
# about a board that was fine. destaben ran into it in their fork, on a
# bench board, 2026-10-15.
#
# Whether the agent is in the way is launchd's answer, not the port's. The
# daemon closes the port whenever the board is unplugged or not answering,
# and reopens it on its next cycle -- so a loaded agent that happened to have
# it closed when this looked stayed live through the flash, free to reopen the
# port and pulse RTS/DTR in the middle of the write, and the restore then
# started a second daemon beside it (review, 2026-10-27). So, as burn.sh
# does: if launchd has the job, it is booted out, whatever the port shows.
#
# Only then is the port checked, and whatever still holds it is refused,
# never killed: it is somebody's terminal or another tool, and flashing under
# it tears the image, their session, or both.
step_installed_aside() {
	local listing agent_pid="" pids pid cmd i
	if listing="$(launchctl print "$AGENT" 2>/dev/null)"; then
		agent_pid="$(printf '%s\n' "$listing" |
			awk '$1 == "pid" && $2 == "=" { print $3; exit }')"
		echo "stopping the installed daemon${agent_pid:+ (pid $agent_pid)}"
		# Set BEFORE the bootout, so an interrupt landing between the two
		# still puts it back. bootout, not `launchctl stop`: the plist sets
		# KeepAlive, and a stopped job is back on the port within a second
		# (pc/cli.py, _LaunchdBackend.stop).
		INSTALLED_WAS_UP=1
		# A refused bootout leaves the agent running, unasked: refused at
		# once, rather than three minutes of waiting for a stop nobody
		# started, and nothing to put back -- and not the dev daemon either,
		# which would open the port beside it (INSTALLED_LEFT_RUNNING).
		if ! blink_bootout "$AGENT"; then
			INSTALLED_WAS_UP=0
			INSTALLED_LEFT_RUNNING=1
			echo "FATAL: could not stop the installed daemon (launchctl bootout exited $BLINK_BOOTOUT_RC)." >&2
			echo "       Nothing was flashed, and it is still running. Check it with: launchctl print $AGENT" >&2
			exit 1
		fi
		# First for launchd to let the job go, for as long as the daemon's
		# stop grace: a daemon inside an OTA write holds its SIGTERM for up
		# to 180 s (tools/lib_launchd.sh), and the 10 s below alone refused
		# with the write still running. A job still there at the deadline is
		# refused and left to launchd, and the restore does not wait for it
		# all over again (INSTALLED_STUCK).
		if ! blink_wait_agent_gone "$AGENT"; then
			INSTALLED_STUCK=1
			echo "FATAL: the installed daemon is still stopping after $((BLINK_STOP_GRACE_S + BLINK_STOP_MARGIN_S)) s." >&2
			echo "       Nothing was flashed. Check it with: launchctl print $AGENT" >&2
			exit 1
		fi
		# bootout RETURNS before the process is gone, and the process holds
		# the port until it is: burn.sh lost that race on 2026-09-06 and
		# refused a good board. Wait, up to 10 s, for the agent's own pid to
		# let go of the port -- not for the port to be free, so a stranger
		# on it is reported now rather than after ten idle seconds.
		if [ -n "$agent_pid" ]; then
			i=0
			while [ $i -lt 40 ]; do
				pids="$(lsof -t "$PORT" 2>/dev/null)" || true
				case " ${pids//$'\n'/ } " in
				*" $agent_pid "*) ;;
				*) break ;;
				esac
				i=$((i + 1))
				sleep 0.25
			done
		fi
	fi
	pids="$(lsof -t "$PORT" 2>/dev/null)" || true
	for pid in $pids; do
		cmd="$(ps -o command= -p "$pid" 2>/dev/null)" || true
		if [ -n "$agent_pid" ] && [ "$pid" = "$agent_pid" ]; then
			echo "FATAL: $PORT is still held 10 s after stopping the installed daemon (pid $pid)." >&2
			exit 1
		fi
		case "$cmd" in
		# The frozen daemon, but not one launchd knows: started by hand, or
		# left over from a job booted out earlier. Not ours to kill, and
		# not ours to bootstrap afterwards either.
		"$HOME/.blink/bin/blink run")
			echo "FATAL: a blink outside launchd holds $PORT (pid $pid); stop it first." >&2
			;;
		# This script's own daemon, which `down` signalled but which has not
		# finished exiting yet.
		*claude_usage_bridge.py*)
			echo "FATAL: the dev daemon (pid $pid) is still exiting and holds $PORT." >&2
			echo "       Give it a moment, or kill $pid, and run this again." >&2
			;;
		*)
			# Anything else: a terminal, a serial monitor, another flasher.
			echo "FATAL: pid $pid ($cmd) has $PORT open, and it is no Blink daemon." >&2
			echo "       Nothing was flashed. Close it, then rerun." >&2
			;;
		esac
		exit 1
	done
}

# Run "$@" until it succeeds, at most $1 times, $2 seconds apart. Returns 1
# if no attempt succeeded.
attempt() {
	local tries="$1" pause="$2" n=1
	shift 2
	until "$@"; do
		[ "$n" -lt "$tries" ] || return 1
		n=$((n + 1))
		sleep "$pause"
	done
}

agent_running() { launchctl print "$AGENT" 2>/dev/null | grep -q "state = running"; }
agent_register() { launchctl bootstrap "gui/$(id -u)" "$AGENT_PLIST" >/dev/null 2>&1; }

# Put the login agent back after step_installed_aside booted it out.
#
# Three launchd facts shape this, each one already paid for elsewhere:
#   - bootout finishes after it returns. Bootstrapping while launchd still
#     holds the old job fails, which once left a reinstall with no service
#     (pc/cli.py, _LaunchdBackend.install, retries for the same reason). So
#     wait for `print` to stop knowing the job -- as long as the daemon's
#     stop grace, not the ~5 s this once allowed, because an instance still
#     inside an OTA write is still exiting (tools/lib_launchd.sh) -- then
#     try the bootstrap up to three times. An instance that is still there
#     is never acted on: kickstart is plain, not -k, which would kill it
#     mid-write; on a job just bootstrapped the plain one starts it all the
#     same (pc/cli.py, _LaunchdBackend.start).
#   - bootstrap only registers. Five installs out of five on 2026-09-04 came
#     up registered and idle, and burn.sh's restore had that gap until
#     2026-09-06; kickstart is what starts it.
#   - neither command returning means a process exists. Saying the daemon
#     is back over a job that is not running sends the reader away from a
#     desk on HOST LOST, so ask launchd, for up to 2 s while kickstart's
#     hand-off lands, and print the manual steps if it never shows.
relaunch_agent() {
	# restore() ignores INT and TERM, so a Ctrl-C during the stop wait in
	# step_installed_aside lands here, in a wait of up to three minutes that
	# no second Ctrl-C can end. Said, so the ignored keypress does not look
	# like a hang (review of fp/fix-tools, NICE 2).
	if [ "$INSTALLED_STUCK" != 1 ] && blink_agent_loaded "$AGENT"; then
		echo "putting the installed daemon back once it has stopped (up to $((BLINK_STOP_GRACE_S / 60)) minutes)" >&2
	fi
	if [ "$INSTALLED_STUCK" = 1 ] || ! blink_wait_agent_gone "$AGENT"; then
		{
			echo "warning: the installed daemon is still stopping; once it has, start it with:"
			echo "  launchctl bootstrap gui/$(id -u) $AGENT_PLIST"
			echo "  launchctl kickstart $AGENT"
		} >&2
		return 0
	fi
	attempt 3 0.5 agent_register || true
	launchctl kickstart "$AGENT" >/dev/null 2>&1 || true
	if attempt 8 0.25 agent_running; then
		echo "installed daemon running again"
		return 0
	fi
	{
		echo "warning: the installed daemon is still not running; to start it:"
		echo "  launchctl bootstrap gui/$(id -u) $AGENT_PLIST"
		echo "  launchctl kickstart $AGENT"
	} >&2
}

# Whichever daemon this run took the port from comes back. If that was the
# installed one, the dev daemon is NOT started too: `down` found none running,
# and starting one beside the agent puts two daemons on one port, talking over
# each other to one board.
#
# INT and TERM are IGNORED from here, not reset. A terminal's Ctrl-C goes to
# the whole process group, so with the default handler back a second Ctrl-C
# killed launchctl halfway through the restore and left the desk on HOST LOST
# (review, 2026-10-27). An ignored signal stays ignored across exec, so the
# launchctl, lsof and sleep this runs are covered too; a trap would not be.
restore() {
	trap - EXIT
	trap '' INT TERM
	if [ "$INSTALLED_WAS_UP" = 1 ]; then
		relaunch_agent
	elif [ "$INSTALLED_LEFT_RUNNING" = 1 ]; then
		:	# the agent never stopped; it still has the port
	else
		up
	fi
}

up() {
	activate
	cd "$ROOT"
	nohup python3 -u claude_usage_bridge.py --port "$PORT" >"$LOG" 2>&1 &
	echo $! >"$PIDFILE"
	sleep 2
	echo "daemon up (pid $(cat "$PIDFILE")), logging to $LOG"
	tail -n 5 "$LOG" || true
}

case "${1:-flash}" in
flash)
	# Quietly: its "still held by" warning would name the installed daemon
	# just before step_installed_aside stops it. Whatever is on the port is
	# reported there instead, by pid and command, with what is done about it.
	down >/dev/null
	# From here on the daemon comes back however this ends: the eFuse
	# refusals below, a build check that fails, a failed west build or flash
	# under `set -e`, an interrupt, or success. It used to be restarted only
	# by the last line, so every early exit left the desk on HOST LOST. (Not
	# after a failed activate: `up` needs the same venv, so it fails the same
	# way.) The same shape as burn.sh's restore_daemon trap, and the only
	# place `up` runs in this path, so a good flash starts it once.
	#
	# Every handler disarms the others before restore() runs, so the restore
	# runs once: a second Ctrl-C landing in `up`'s own `sleep 2` used to run
	# `up` again from the INT trap, two nohup'd daemons on one port (review,
	# 2026-10-25). restore() drops EXIT and ignores INT and TERM itself, as
	# its first two lines; the signal handlers do the same before they print,
	# so even that echo cannot be interrupted into a second restore. It then
	# brings back whichever daemon this run stopped: the installed one if
	# step_installed_aside booted it out, this script's own otherwise.
	trap restore EXIT
	trap 'trap - EXIT; trap "" INT TERM; echo "flash interrupted; re-run dev.sh flash" >&2; restore; exit 130' INT
	trap 'trap - EXIT; trap "" INT TERM; echo "flash interrupted; re-run dev.sh flash" >&2; restore; exit 143' TERM
	# Under the traps, so its refusals and an interrupt in its wait restore
	# too. Before the eFuse probe, which needs the port.
	step_installed_aside
	# dev.sh writes PLAINTEXT. A fused chip's ROM cannot read that, and the
	# board goes dark until tools/flash_encrypted.sh restores it -- the exact
	# mirror of the mistake that script refuses. Ask the chip, before the build.
	if [ "${BLINK_SKIP_EFUSE_CHECK:-0}" != "1" ]; then
		# shellcheck source=lib_efuse.sh
		. "$ROOT/tools/lib_efuse.sh"
		efuse_probe "$PORT"
		case "$EFUSE_STATE" in
		plaintext)
			;;	# what this script is for
		encrypted)
			{
				echo "FATAL: this chip is FUSED (FLASH_CRYPT_CNT = 0b$EFUSE_BITS${EFUSE_MAC:+, MAC $EFUSE_MAC})."
				echo "       dev.sh writes plaintext, which a fused ROM cannot read -- the board"
				echo "       would go dark until re-flashed. Use the encrypted path instead:"
				echo "         cd $ROOT/firmware && west build --sysbuild -d build-sb -b $BOARD . \\"
				echo "           -- -DSB_CONFIG_BOOTLOADER_MCUBOOT=y -DUSE_CCACHE=0 \\"
				# Single-quoted so the key path reaches the user's shell unexpanded,
				# and the nested quotes survive: sysbuild needs the value quoted, and
				# a build that omits this silently signs with MCUboot's bundled dev
				# key -- the board boots, but every later OTA is rejected and reverts.
				echo '             -DSB_CONFIG_BOOT_SIGNATURE_KEY_FILE="\"$HOME/.blink/ota_signing_key_p256.pem\"" \'
				echo "           && $ROOT/tools/flash_encrypted.sh $PORT"
				echo "       If this is one of the two PILOT boards, add to that build:"
				echo "           -DEXTRA_CONF_FILE=pilot.conf"
				echo "       (their display module differs; a stock build renders them"
				echo "        mirrored, red/blue swapped and dim. Judge per board -- being"
				echo "        fused is NOT the same thing as being a pilot.)"
				echo "       BLINK_SKIP_EFUSE_CHECK=1 overrides."
			} >&2
			exit 1
			;;
		*)
			echo "FATAL: cannot tell whether this chip is fused -- $EFUSE_REASON" >&2
			echo "       Refusing to flash blind. BLINK_SKIP_EFUSE_CHECK=1 overrides." >&2
			exit 1
			;;
		esac
	fi
	activate   # sources zephyr-env.sh too
	# Here and not in activate(), which `up` shares: the daemon must come back
	# whether or not this machine can build.
	blink_zephyr_build_ready || exit 1
	cd "$ROOT/firmware"
	west build -b "$BOARD" . -- -DUSE_CCACHE=0 | tail -3
	# A developer's own board, so a bench image is not refused here -- but
	# it is named, because a -D left in build/'s CMake cache makes every
	# later dev.sh flash one without a word: a selftest faults on every
	# boot, a touch-inject image takes touches from the cable
	# (tools/lib_selftest.sh; burn.sh is where the refusal lives).
	# A warning and never a stop, so a checkout without the lib flashes as
	# before rather than failing on a missing file.
	if [ -f "$ROOT/tools/lib_selftest.sh" ]; then
		# shellcheck source=lib_selftest.sh
		. "$ROOT/tools/lib_selftest.sh"
		blink_refuse_selftest build/zephyr/.config build/zephyr/zephyr.bin \
			2>/dev/null || echo "WARNING: flashing a SELFTEST build (or one nothing could inspect)" >&2
		blink_refuse_inject build/zephyr/.config build/zephyr/zephyr.bin \
			2>/dev/null || echo "WARNING: flashing a TOUCH INJECT build (or one nothing could inspect)" >&2
	fi
	# 921600 is beyond what this board's CH340 tolerates; it fails mid-write.
	west flash --esp-device "$PORT" --esp-baud-rate 115200 | tail -1
	;;	# the EXIT trap starts the daemon
up) down; up ;;
down) down; echo "daemon down, $PORT free" ;;
log) tail -f "$LOG" ;;
*) echo "usage: $0 {flash|up|down|log}"; exit 1 ;;
esac
