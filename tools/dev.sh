#!/usr/bin/env bash
# Dev loop for the CYD board.
#
#   tools/dev.sh flash   -- stop daemon, build, flash, restart daemon
#   tools/dev.sh up      -- start the daemon in the background
#   tools/dev.sh down    -- stop the daemon (frees the port)
#   tools/dev.sh log     -- tail the daemon log
#
# Only ONE process may own the serial port, so the daemon must be down while
# esptool talks to the board. `flash` sequences that for you.
set -euo pipefail

PORT="${PORT:-/dev/cu.usbserial-14420}"
BOARD="esp32_devkitc/esp32/procpu"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG="/tmp/claude-usage-bridge.log"
PIDFILE="/tmp/claude-usage-bridge.pid"

activate() {
	# shellcheck disable=SC1090
	source ~/zephyr-v4.4.0/.venv/bin/activate
}

down() {
	if [[ -f "$PIDFILE" ]] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
		kill "$(cat "$PIDFILE")" 2>/dev/null || true
		sleep 1
	fi
	pkill -f claude_usage_bridge.py 2>/dev/null || true
	rm -f "$PIDFILE"
	sleep 0.5
	if lsof "$PORT" >/dev/null 2>&1; then
		echo "warning: $PORT still held by:"
		lsof "$PORT"
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
	down
	activate
	# shellcheck disable=SC1090
	source ~/zephyr-v4.4.0/zephyr/zephyr-env.sh >/dev/null 2>&1
	cd "$ROOT/firmware"
	west build -b "$BOARD" . -- -DUSE_CCACHE=0 | tail -3
	# 921600 is beyond what this board's CH340 tolerates; it fails mid-write.
	west flash --esp-device "$PORT" --esp-baud-rate 115200 | tail -1
	up
	;;
up) down; up ;;
down) down; echo "daemon down, $PORT free" ;;
log) tail -f "$LOG" ;;
*) echo "usage: $0 {flash|up|down|log}"; exit 1 ;;
esac
