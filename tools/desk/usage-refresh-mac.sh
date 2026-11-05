#!/bin/sh
# The Mac half of the usage-refresh desk check (docs/plans/
# 2026-11-05-usage-refresh-plan.md, Task 9), unattended, with a report.
#
# Start it from Terminal (not from inside Claude Code: that session has to
# be quit before the watch starts, and it would take this script with it),
# in this worktree, like this:
#
#   mkdir -p ~/.blink/desk-check && nohup sh tools/desk/usage-refresh-mac.sh \
#       >~/.blink/desk-check/console.log 2>&1 </dev/null & disown; \
#       tail -f ~/.blink/desk-check/console.log
#
# nohup and disown are what let the Terminal window close and the screen
# lock without taking the check with it; tail is only for watching the flash
# and can be stopped (Ctrl-C) or its window closed at any time. When the
# console says "Now you can leave", quit Claude Code and Claude Desktop and
# go. The report lands in ~/.blink/desk-check/usage-refresh-<date>.md.
#
#   --dry-run            say what each phase would do; read-only looks only
#   --skip-flash         do not run tools/burn.sh
#   --skip-install       do not build and install the daemon from this tree
#   --logo FILE          the company logo burn.sh writes (default
#                        logos/<your-logo>.bin)
#   --port DEV           the board's port, for burn.sh (found by USB id else)
#   --phase-a-min N      minutes of "nobody home" (default 90)
#   --total-hours N      the whole watch ends this long after it starts (8)
#   --wait-claude-min N  how long to wait for Claude apps to be quit (20)
#
# Why it is built in phases. The refresh runs only while someone is at the
# computer: HIDIdleTime under 30 minutes (pc/usage_refresh.due, IDLE_LIMIT_S).
# With nobody at the keyboard that guard blocks every run, which is exactly
# phase A's claim. Whether the scheduler then runs at the right spacing can
# only be seen with idle kept low, so phase B pretends someone is here with
# `caffeinate -u` -- if, and only if, a probe first shows that caffeinate -u
# resets HIDIdleTime at all. Nobody has measured that on this macOS; the
# probe's answer goes in the report either way. If it does not, the
# scheduler path is not exercised unattended and the report says so, and
# one direct run_once (the same function RefreshWatch calls) stands in for
# the run itself.
#
# What it never does: print or keep a serial byte (burn.sh does its own
# flashing and boot check; this script reads only ~/.blink/board.json, which
# the daemon writes), read any field of ~/.claude.json but fetchedAtMs and
# whether the cache is this login's (tools/desk/usage_refresh_probe.py),
# print a usage figure, or send anything to the model: the only Claude Code
# it ever runs is `claude -p /usage`, through pc.usage_refresh.run_once,
# whose answer it never sees.
#
# Not `set -e`: this runs for eight hours, and one failed look (ioreg busy,
# a log rotated mid-read) must be a line in the report, not the end of the
# check. Every step that can fail is tested where it runs.
# The `[ x ] && echo a || at b` forms below choose between two echoes,
# neither of which can fail, so SC2015's caution does not apply.
# shellcheck disable=SC2015
set -u

ROOT=$(CDPATH='' cd -- "$(dirname -- "$0")/../.." && pwd)
PROBE="$ROOT/tools/desk/usage_refresh_probe.py"
PY="${BLINK_PYTHON:-$ROOT/.venv-test/bin/python}"
EXPECT_BRANCH="${BLINK_DESK_BRANCH:-feat/usage-refresh}"
BLINK_DIR="$HOME/.blink"
BLINK_BIN="$BLINK_DIR/bin/blink"
LOG="$BLINK_DIR/bridge.log"
AGENT="gui/$(id -u)/com.blink.bridge"
OUTDIR="$BLINK_DIR/desk-check"

LOGO="logos/logo.bin"
PORT=""
DRY=0
SKIP_FLASH=0
SKIP_INSTALL=0
PHASE_A_MIN=90
TOTAL_H=8
WAIT_CLAUDE_MIN=20

# The daemon's own numbers (pc/usage_refresh.py), kept in step by hand:
# IDLE_LIMIT_S 30 min, REFRESH_AFTER_S 25 min, TICK_S 60 s, FRESH_SOURCE_S
# 15 min. IDLE_JUDGE adds one tick: a run decided at 29:59 of idle is logged
# up to a minute later, when this script may already read 30:30.
IDLE_LIMIT=1800
IDLE_JUDGE=1860
# REFRESH_AFTER_S less a minute: the gap is between two looks a minute apart.
SPACING_SLACK=1440
FRESH_SOURCE=900

usage() {
	sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
}

while [ $# -gt 0 ]; do
	case "$1" in
	--dry-run)         DRY=1; shift ;;
	--skip-flash)      SKIP_FLASH=1; shift ;;
	--skip-install)    SKIP_INSTALL=1; shift ;;
	--logo)            LOGO="$2"; shift 2 ;;
	--port)            PORT="$2"; shift 2 ;;
	--phase-a-min)     PHASE_A_MIN="$2"; shift 2 ;;
	--total-hours)     TOTAL_H="$2"; shift 2 ;;
	--wait-claude-min) WAIT_CLAUDE_MIN="$2"; shift 2 ;;
	-h|--help)         usage; exit 0 ;;
	*) echo "Unknown option: $1 (see --help)" >&2; exit 2 ;;
	esac
done
for n in "$PHASE_A_MIN" "$TOTAL_H" "$WAIT_CLAUDE_MIN"; do
	case "$n" in
	''|*[!0-9]*) echo "Minutes and hours are whole numbers: $n" >&2; exit 2 ;;
	esac
done

now() { date +%s; }
hms() { date '+%H:%M:%S'; }
day() { date '+%Y-%m-%d'; }
at() { date -r "$1" '+%H:%M:%S' 2>/dev/null || echo "-"; }
mins() { echo $(( ($1 + 30) / 60 )); }

STAMP=$(date '+%Y-%m-%d-%H%M')
WORK="$OUTDIR/usage-refresh-$STAMP"
REPORT="$OUTDIR/usage-refresh-$STAMP.md"
TIMELINE="$WORK/timeline.txt"
CHECKS="$WORK/checks.txt"
RUNS="$WORK/runs.txt"
FINDINGS="$WORK/findings.txt"

# Everything said goes to the console and, once WORK exists, the timeline.
# A dry run creates nothing, so it only prints.
say() {
	printf '%s  %s\n' "$(hms)" "$*"
	[ "$DRY" = 1 ] || [ ! -d "$WORK" ] || printf '%s  %s\n' "$(hms)" "$*" >>"$TIMELINE"
}
banner() {
	printf '\n==== %s\n' "$*"
	[ "$DRY" = 1 ] || [ ! -d "$WORK" ] || printf '\n%s  ==== %s\n' "$(hms)" "$*" >>"$TIMELINE"
}
# check RESULT NAME DETAIL -- one row of the report's table.
check() {
	say "$1: $2${3:+ -- $3}"
	[ "$DRY" = 1 ] || printf '%s|%s|%s\n' "$1" "$2" "${3:-}" >>"$CHECKS"
}
finding() {
	say "Finding: $*"
	[ "$DRY" = 1 ] || printf '%s\n' "$*" >>"$FINDINGS"
}
die() {
	printf '\nRefused: %s\n' "$*" >&2
	exit 1
}

probe() {
	PYTHONDONTWRITEBYTECODE=1 "$PY" -B "$PROBE" "$@"
}

# Claude Desktop and Claude Code processes, "pid what" per line, never their
# arguments: a `claude -p "..."` carries the person's prompt. The refresh's
# own `claude -p /usage` is left out (it is what is being watched), and so
# are Claude Code's long-lived bg-* helpers, which outlive a closed session
# and write no transcript; they are listed apart.
claude_procs() {
	ps -axo pid=,comm= 2>/dev/null | while read -r pid comm; do
		case "$comm" in
		*/Claude.app/Contents/MacOS/Claude)
			echo "$pid Claude Desktop" ;;
		"claude bg-"*)
			;;
		claude|"claude "*|*/claude|*/claude/versions/*)
			args=$(ps -o args= -p "$pid" 2>/dev/null || true)
			case "$args" in
			*" -p /usage "*|*" -p /usage") ;;
			*) echo "$pid Claude Code" ;;
			esac ;;
		esac
	done
}
claude_helpers() {
	ps -axo pid=,comm= 2>/dev/null | while read -r pid comm; do
		case "$comm" in "claude bg-"*) echo "$pid $comm" ;; esac
	done
}

# --------------------------------------------------------------- the report
REPORTED=0
CAF_PID=""
START_TS=$(now)
PHASE="preflight"
RESULT_NOTE=""

write_report() {
	[ "$DRY" = 1 ] && return 0
	[ "$REPORTED" = 1 ] && return 0
	[ -d "$WORK" ] || return 0
	REPORTED=1
	end=$(now)
	pass=$(grep -c '^PASS|' "$CHECKS" 2>/dev/null) || pass=0
	fail=$(grep -c '^FAIL|' "$CHECKS" 2>/dev/null) || fail=0
	skip=$(grep -c '^SKIPPED|' "$CHECKS" 2>/dev/null) || skip=0
	{
		echo "# Usage refresh desk check, $(day)"
		echo
		echo "Mac half of Task 9 (docs/plans/2026-11-05-usage-refresh-plan.md), run unattended by tools/desk/usage-refresh-mac.sh."
		echo
		echo "- Started $(at "$START_TS"), ended $(at "$end") ($(mins $((end - START_TS))) min)."
		echo "- Tree: $ROOT, branch $(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null), HEAD $(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)."
		echo "- Result: $pass passed, $fail failed, $skip skipped.${RESULT_NOTE:+ $RESULT_NOTE}"
		[ "$PHASE" = "done" ] || echo "- **The check stopped early, in $PHASE.** Everything below is what it saw until then."
		echo
		echo "## Checks"
		echo
		echo "| Result | Check | Detail |"
		echo "|---|---|---|"
		if [ -s "$CHECKS" ]; then
			while IFS='|' read -r r n d; do
				echo "| $r | $n | $d |"
			done <"$CHECKS"
		fi
		echo
		echo "## Findings"
		echo
		if [ -s "$FINDINGS" ]; then
			sed 's/^/- /' "$FINDINGS"
		else
			echo "- None recorded."
		fi
		echo
		echo "## Runs seen"
		echo
		echo "Each is a change of \`last_run_at\` in usage-refresh.json, with the daemon's own log line beside it. \"Idle before\" is HIDIdleTime at the look before the run, \"new transcripts\" the *.jsonl files under the transcripts folder newer than the phase's start."
		echo
		echo "| Phase | Started | Log says | Cache moved | Idle before (min) | New transcripts | Setting | Off reason | Gap (min) |"
		echo "|---|---|---|---|---|---|---|---|---|"
		if [ -s "$RUNS" ]; then
			while IFS='|' read -r p t v m i j o r g; do
				echo "| $p | $t | $v | $m | $i | $j | $o | $r | $g |"
			done <"$RUNS"
		else
			echo "| - | none | | | | | | | |"
		fi
		echo
		echo "## Not covered here"
		echo
		echo "- The board's weekly dial against Claude Code's own /usage (needs eyes on the board)."
		echo "- No run while a Claude Code terminal or a Desktop chat is in use (needs someone using them)."
		echo "- The board's \"Keep figures fresh?\" prompt (Step 4b), uninstall on a VM (Step 5), the Ubuntu and Windows suites (Step 1)."
		echo
		echo "## Leaving it as it is"
		echo
		echo "The install and the setting are left as they are. To turn the refresh off:"
		echo
		echo '    ~/.blink/bin/blink config usage-refresh off'
		echo
		echo "## Timeline"
		echo
		echo '```'
		cat "$TIMELINE" 2>/dev/null
		echo '```'
	} >"$REPORT"
	printf '\nReport: %s\n' "$REPORT"
	msg="Usage refresh check: $pass passed, $fail failed, $skip skipped"
	osascript -e "display notification \"$msg\" with title \"BLINK desk check\"" \
		>/dev/null 2>&1 || true
}

finish() {
	write_report
	[ -z "$CAF_PID" ] || kill "$CAF_PID" 2>/dev/null || true
}
trap finish EXIT
trap 'say "Interrupted"; exit 130' INT TERM
# The terminal closing must not end the check even if it was started
# without nohup.
trap '' HUP

# ------------------------------------------------------------- 0. preflight
banner "Usage refresh desk check"
[ "$DRY" = 1 ] && echo "Dry run: nothing is flashed, installed, written or run."
cat <<EOF

Before you leave: quit Claude Desktop and every Claude Code session on this
Mac, including the one that wrote this script. While either is open the
refresh is meant to stay quiet, and a session writes transcripts this check
counts as a failure. Keep the Mac on power with the lid open; it is kept
awake by caffeinate, and the screen may lock.

EOF

[ "$(uname -s)" = Darwin ] || die "this check is for macOS (ioreg's HIDIdleTime, launchd, caffeinate)."
command -v caffeinate >/dev/null 2>&1 || die "caffeinate not found."
git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1 || die "$ROOT is not a git checkout."
BRANCH=$(git -C "$ROOT" rev-parse --abbrev-ref HEAD 2>/dev/null)
HEAD=$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null)
[ "$BRANCH" = "$EXPECT_BRANCH" ] ||
	die "this tree is on $BRANCH, not $EXPECT_BRANCH. Run it from the usage-refresh worktree (or set BLINK_DESK_BRANCH)."
[ -x "$PY" ] || die "no Python at $PY. Make .venv-test (CLAUDE.md, Tests) or set BLINK_PYTHON."
"$PY" -B -c "import serial" 2>/dev/null || die "$PY has no pyserial (pip install -r pc/requirements.txt)."
say "Tree $ROOT, $BRANCH at $HEAD"

PORT_FOUND=$(probe port 2>/dev/null) || PORT_FOUND=""
[ -n "$PORT" ] || PORT="$PORT_FOUND"
case "$PORT" in
"")      die "no board found. Plug it in, or pass --port." ;;
MANY:*)  die "more than one board: ${PORT#MANY:}. Pass --port." ;;
esac
say "Board on $PORT"

HOME_LINES=$(probe home 2>/dev/null) || die "could not read the Claude home record."
HOME_KIND=$(printf '%s\n' "$HOME_LINES" | sed -n 1p)
TRANSCRIPTS=$(printf '%s\n' "$HOME_LINES" | sed -n 2p)
[ "$HOME_KIND" = native ] || die "the Claude home is $HOME_KIND; the refresh runs for a native home only."
say "Transcripts folder $TRANSCRIPTS"

CLAUDE_EXE=$(probe claude 2>/dev/null) ||
	die "Claude Code not found (PATH, ~/.local/bin, Homebrew, npm, nvm). Install it and log in first."
say "Claude Code at $CLAUDE_EXE"

PROCS=$(claude_procs)
if [ -n "$PROCS" ]; then
	printf '\n!!!! These are running and must be quit before the watch starts:\n%s\n!!!!\n\n' "$PROCS"
fi
HELPERS=$(claude_helpers)
[ -z "$HELPERS" ] || say "Claude Code helpers running (they write no transcript, left alone): $(printf '%s' "$HELPERS" | tr '\n' ';')"

if [ "$SKIP_FLASH" = 0 ]; then
	[ -f "$LOGO" ] || die "no logo at $LOGO (pass --logo, or --skip-flash)."
fi

TOTAL_S=$((TOTAL_H * 3600))
PHASE_A_S=$((PHASE_A_MIN * 60))

if [ "$DRY" = 1 ]; then
	set -f
	# shellcheck disable=SC2046
	set -- $(probe sample 2>/dev/null)
	set +f
	t=$(now)
	echo
	echo "Now: idle ${1:-?} s; /usage cache fetched $( [ "${2:--}" = - ] && echo never || echo "$(mins $((t - $2))) min ago"); setting ${6:-?} (1 on, 0 off, - unset); off reason ${7:--}; last run $( [ "${9:-0}" -gt 0 ] 2>/dev/null && echo "$(mins $((t - $9))) min ago" || echo never)."
	echo
	echo "What each phase would do:"
	echo " 1. Flash: $( [ "$SKIP_FLASH" = 1 ] && echo skipped || echo "sh tools/burn.sh --edition claude --logo $LOGO --port $PORT, then wait for the daemon to record the board's caps with \"ask\" in ~/.blink/board.json")."
	echo " 2. Install: $( [ "$SKIP_INSTALL" = 1 ] && echo "skipped" || echo "tools/build_binary.sh into $OUTDIR/usage-refresh-<stamp>/dist, then dist/blink/blink install </dev/null") ; then ~/.blink/bin/blink config usage-refresh on, and check blink status shows it on."
	echo " 3. Wait up to $WAIT_CLAUDE_MIN min for Claude Desktop and Claude Code to be quit, then phase A, nobody home, $PHASE_A_MIN min: no presence faked; expect no run once idle passes 30 min."
	echo " 4. Probe: does caffeinate -u -t 5 reset HIDIdleTime? Two tries, with idle over a minute."
	echo " 5. Phase B until $TOTAL_H h after the watch starts: if the probe said yes, caffeinate -u every 60 s and expect a run about every 25-30 min, each with the cache moved and no transcript; if no, one direct run_once and a nobody-home watch to the end."
	echo " 6. Report to $OUTDIR/usage-refresh-<stamp>.md and a notification."
	exit 0
fi

mkdir -p "$WORK" || die "could not make $WORK"
chmod 700 "$OUTDIR" "$WORK" 2>/dev/null || true
: >"$TIMELINE"; : >"$CHECKS"; : >"$RUNS"; : >"$FINDINGS"
say "Working files in $WORK"
say "Tree $ROOT, $BRANCH at $HEAD; board on $PORT; Claude Code at $CLAUDE_EXE; transcripts in $TRANSCRIPTS"
[ -z "$PROCS" ] || say "Claude running at the start: $(printf '%s' "$PROCS" | tr '\n' ';')"

# The check's own lifetime, and nothing longer: -w ends it with this shell.
# -i keeps the Mac from idle sleep (an asleep Mac answers no board and runs
# no daemon, and would read as "no run"), -s also on AC. Not -d or -u: the
# display may sleep and the screen lock, which is the "nobody home" phase A
# is about.
caffeinate -i -s -w $$ &
CAF_PID=$!
say "Kept awake by caffeinate (pid $CAF_PID)"

# ----------------------------------------------------------------- 1. flash
FW_WANT=$(sed -n 's/^#define BLINK_FW_VERSION "\(.*\)"/\1/p' "$ROOT/firmware/src/version.h")

# The daemon writes board.json from each hello, caps included (pc/protocol.
# board_caps, unfiltered), so a board.json newer than `since` that lists
# "ask" is the new firmware booted and heard. Waits up to $2 seconds.
wait_board_ask() {
	since=$1; limit=$2; t0=$(now)
	while [ $(( $(now) - t0 )) -lt "$limit" ]; do
		set -f
		# shellcheck disable=SC2046
		set -- $(probe board 2>/dev/null)
		set +f
		if [ "${3:--}" != - ] && [ "$3" -ge "$since" ] && [ "${2:-0}" = 1 ]; then
			BOARD_FW=$1
			return 0
		fi
		sleep 10
	done
	return 1
}

PHASE="flash"
if [ "$SKIP_FLASH" = 1 ]; then
	check SKIPPED "Flash with burn.sh" "--skip-flash"
else
	banner "1. Flash (tools/burn.sh, company logo kept)"
	say "This builds, checks FLASH_CRYPT_CNT, flashes, boot-verifies and puts the daemon back."
	t_flash=$(now)
	if sh "$ROOT/tools/burn.sh" --edition claude --logo "$LOGO" --port "$PORT" </dev/null; then
		check PASS "Flash with burn.sh" "burn.sh PASS (booted fw $FW_WANT, edition and logo survived a power cycle)"
	else
		check FAIL "Flash with burn.sh" "burn.sh exited non-zero; its output is in console.log"
		RESULT_NOTE="Stopped at the flash."
		die "burn.sh failed; nothing else was done."
	fi
	say "Waiting for the daemon to hear the board (up to 5 min)"
	if wait_board_ask "$t_flash" 300; then
		check PASS "Board booted and lists ask" "board.json fw $BOARD_FW, caps include ask"
	else
		check FAIL "Board booted and lists ask" "no board.json with ask within 5 min of the flash"
	fi
fi

# --------------------------------------------------------------- 2. install
PHASE="install"
banner "2. Install from this branch"
if [ "$SKIP_INSTALL" = 1 ]; then
	check SKIPPED "Install from the branch" "--skip-install"
else
	DIST="$WORK/dist"
	say "Building the program (tools/build_binary.sh); a few minutes"
	if PYTHONDONTWRITEBYTECODE=1 sh "$ROOT/tools/build_binary.sh" "$DIST" </dev/null; then
		t_inst=$(now)
		# stdin from /dev/null: the install question is asked only on a
		# terminal (cli._ask_yes), and the answer is given below with
		# `blink config`, which also records where claude is.
		if "$DIST/blink/blink" install </dev/null; then
			check PASS "Install from the branch" "dist/blink/blink install from $HEAD"
		else
			check FAIL "Install from the branch" "blink install exited non-zero; see console.log"
		fi
		if [ "$SKIP_FLASH" = 1 ]; then
			say "Waiting for the new daemon to hear the board (up to 5 min)"
			if wait_board_ask "$t_inst" 300; then
				check PASS "Board lists ask" "board.json fw $BOARD_FW, caps include ask"
			else
				check FAIL "Board lists ask" "no board.json with ask within 5 min of the install"
			fi
		fi
	else
		check FAIL "Install from the branch" "tools/build_binary.sh failed; see console.log"
	fi
fi
if [ "$SKIP_FLASH" = 1 ] && [ "$SKIP_INSTALL" = 1 ]; then
	# Neither step ran, so there is no moment to be newer than: the board
	# the daemon last heard, as it is now.
	if wait_board_ask 0 1; then
		check PASS "Board lists ask" "board.json fw $BOARD_FW, caps include ask (as last heard)"
	else
		check FAIL "Board lists ask" "board.json lists no ask: the board is not on this branch's firmware"
	fi
fi
[ -x "$BLINK_BIN" ] || die "no installed program at $BLINK_BIN."
if "$BLINK_BIN" config </dev/null 2>/dev/null | grep -q '^usage-refresh'; then
	check PASS "Installed program knows usage-refresh" "$("$BLINK_BIN" --version 2>/dev/null | head -1)"
else
	check FAIL "Installed program knows usage-refresh" "blink config lists no usage-refresh; the install is not this branch"
fi
if "$BLINK_BIN" config usage-refresh on </dev/null >/dev/null 2>&1; then
	say "blink config usage-refresh on"
else
	check FAIL "Turn the refresh on" "blink config usage-refresh on exited non-zero"
fi
ROW=$("$BLINK_BIN" status </dev/null 2>/dev/null | grep '^Usage refresh' | head -1)
case "$ROW" in
*" on"*) check PASS "Status shows the refresh on" "$(echo "$ROW" | sed 's/  */ /g')" ;;
*)       check FAIL "Status shows the refresh on" "row: ${ROW:-none}" ;;
esac
set -f
# shellcheck disable=SC2046
set -- $(probe sample 2>/dev/null)
set +f
if [ "${11:-0}" = 1 ]; then
	check PASS "Claude path recorded for the daemon" "usage-refresh.json has exe"
else
	check FAIL "Claude path recorded for the daemon" "no exe in usage-refresh.json"
fi
if launchctl print "$AGENT" 2>/dev/null | grep -q 'state = running'; then
	check PASS "Daemon running under launchd" "$AGENT"
else
	check FAIL "Daemon running under launchd" "launchctl print $AGENT does not say running"
fi
EXE_RECORDED="$CLAUDE_EXE"

printf '\n========================================================\n'
printf ' Now you can leave. Quit Claude Code (this session too)\n'
printf ' and Claude Desktop first; the watch waits up to %s min\n' "$WAIT_CLAUDE_MIN"
printf ' for them to be gone. The report will be in\n %s\n' "$REPORT"
printf '========================================================\n\n'

PHASE="waiting for Claude apps to be quit"
t0=$(now)
while :; do
	PROCS=$(claude_procs)
	[ -z "$PROCS" ] && break
	if [ $(( $(now) - t0 )) -ge $((WAIT_CLAUDE_MIN * 60)) ]; then
		break
	fi
	sleep 30
done
if [ -z "$PROCS" ]; then
	check PASS "Claude Desktop and Claude Code quit" "none running at the start of the watch"
else
	check FAIL "Claude Desktop and Claude Code quit" "still running after $WAIT_CLAUDE_MIN min: $(printf '%s' "$PROCS" | tr '\n' ';'); runs may be held off and transcripts may be theirs"
fi

# ------------------------------------------------------ the watch, shared
# One look a minute, as often as the daemon's own tick. Each look:
# HIDIdleTime, the cache's fetch time as the daemon reads it, the state file,
# the new "usage refresh ..." lines of bridge.log (fixed event words, no
# figure: RefreshWatch's docstring), and Claude processes.
LOG_OFF=0
[ -f "$LOG" ] && LOG_OFF=$(wc -c <"$LOG" | tr -d ' ')
LAST_RUN=""
PREV_IDLE=0
PREV_RUN_AT=0
SL_PREV=""

# New lines of bridge.log since the last look that name a refresh event.
# A rotation (logbook.rotate_if_full, at 4 MB) shows as a file smaller than
# the offset: the rest of bridge.log.1 is read first, then the new file.
new_log_lines() {
	[ -f "$LOG" ] || return 0
	size=$(wc -c <"$LOG" | tr -d ' ')
	if [ "$size" -lt "$LOG_OFF" ]; then
		[ -f "$LOG.1" ] && tail -c +"$((LOG_OFF + 1))" "$LOG.1" 2>/dev/null | grep 'usage refresh'
		LOG_OFF=0
	fi
	tail -c +"$((LOG_OFF + 1))" "$LOG" 2>/dev/null | head -c "$((size - LOG_OFF))" | grep 'usage refresh'
	LOG_OFF=$size
}

new_jsonl() {
	find "$TRANSCRIPTS" -name '*.jsonl' -newer "$1" 2>/dev/null | wc -l | tr -d ' '
}

# watch PHASE_NAME END PULSE MARK -- looks until END; PULSE 1 sends
# caffeinate -u first. Sets W_RUNS, W_BAD_IDLE (runs with idle past the
# limit), W_MAX_IDLE, W_MIN_GAP, W_FAILS, W_OFF, W_JSONL, W_PROCS, W_SL.
watch() {
	name=$1; end=$2; pulse=$3; mark=$4
	W_RUNS=0; W_BAD_IDLE=0; W_MAX_IDLE=0; W_MIN_GAP=""; W_FAILS=0; W_OFF=0
	W_PROCS=0; W_SL=0; W_NOTMOVED=0; last_note=0
	while [ "$(now)" -lt "$end" ]; do
		if [ "$pulse" = 1 ]; then
			# In the background: it holds its assertion for 5 s, and
			# the look below should not wait it out.
			caffeinate -u -t 5 >/dev/null 2>&1 &
			sleep 1
		fi
		LINES=$(new_log_lines)
		set -f
		# shellcheck disable=SC2046
		set -- $(probe sample 2>/dev/null)
		set +f
		t=$(now)
		idle=${1:--}; cache=${2:--}; sl=${5:--}; on=${6:--}; off=${7:--}
		fails=${8:-0}; lr=${9:-0}
		if [ -n "$LINES" ]; then
			printf '%s\n' "$LINES" | while IFS= read -r l; do say "log: $l"; done
			case "$LINES" in *"failed"*) W_FAILS=$((W_FAILS + 1)) ;; esac
			case "$LINES" in *"refresh off"*) W_OFF=$((W_OFF + 1)) ;; esac
		fi
		[ "$idle" != - ] && [ "$idle" -gt "$W_MAX_IDLE" ] && W_MAX_IDLE=$idle
		if [ "$sl" != - ] && [ -n "$SL_PREV" ] && [ "$sl" != "$SL_PREV" ]; then
			W_SL=$((W_SL + 1))
			say "The status line file was written: a Claude Code session is in use somewhere"
		fi
		SL_PREV=$sl
		p=$(claude_procs)
		if [ -n "$p" ]; then
			W_PROCS=$((W_PROCS + 1))
			[ $((W_PROCS % 30)) = 1 ] && say "Claude running: $(printf '%s' "$p" | tr '\n' ';')"
		fi
		if [ -n "$LAST_RUN" ] && [ "$lr" != "$LAST_RUN" ] && [ "$lr" -gt 0 ] 2>/dev/null; then
			W_RUNS=$((W_RUNS + 1))
			verdict=$(printf '%s\n' "$LINES" | grep -o 'usage refresh [a-z]*\( ([^)]*)\)\{0,1\}' | tail -1)
			[ -n "$verdict" ] || verdict="(no log line)"
			moved=no
			if [ "$cache" != - ] && [ "$cache" -ge $((lr - 5)) ]; then moved=yes; fi
			[ "$moved" = yes ] || W_NOTMOVED=$((W_NOTMOVED + 1))
			if [ "$PREV_IDLE" != - ] && [ "$PREV_IDLE" -gt "$IDLE_JUDGE" ]; then
				W_BAD_IDLE=$((W_BAD_IDLE + 1))
			fi
			gap="-"
			if [ "$PREV_RUN_AT" -gt 0 ]; then
				gap=$(( (lr - PREV_RUN_AT) / 60 ))
				if [ -z "$W_MIN_GAP" ] || [ $((lr - PREV_RUN_AT)) -lt "$W_MIN_GAP" ]; then
					W_MIN_GAP=$((lr - PREV_RUN_AT))
				fi
			fi
			PREV_RUN_AT=$lr
			j=$(new_jsonl "$mark")
			printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "$name" "$(at "$lr")" "$verdict" \
				"$moved" "$( [ "$PREV_IDLE" = - ] && echo - || mins "$PREV_IDLE")" "$j" \
				"$( [ "$on" = 1 ] && echo on || echo "off ($on)")" "$off" "$gap" >>"$RUNS"
			say "Run at $(at "$lr"): $verdict, cache moved $moved, idle before $PREV_IDLE s, new transcripts $j, fails $fails"
		fi
		LAST_RUN=$lr
		PREV_IDLE=$idle
		if [ $((t - last_note)) -ge 600 ]; then
			last_note=$t
			say "$name: idle ${idle} s, cache $( [ "$cache" = - ] && echo none || echo "$(mins $((t - cache))) min old"), runs $W_RUNS, setting $on, off reason $off"
		fi
		sleep 59
	done
	W_JSONL=$(new_jsonl "$mark")
}

# The look before the first watch, so a run is only a change after it.
set -f
# shellcheck disable=SC2046
set -- $(probe sample 2>/dev/null)
set +f
LAST_RUN=${9:-0}
PREV_IDLE=${1:-0}
SL_PREV=${5:-}
[ "${9:-0}" -gt 0 ] 2>/dev/null && PREV_RUN_AT=$9
WATCH_START=$(now)
WATCH_END=$((WATCH_START + TOTAL_S))
if [ "${5:--}" != - ] && [ $((WATCH_START - $5)) -lt "$FRESH_SOURCE" ]; then
	say "The status line was written $(mins $((WATCH_START - $5))) min ago: no run before it is 15 min old (FRESH_SOURCE_S)"
fi

# ------------------------------------------------------- 3. phase A
PHASE="phase A"
banner "3. Phase A: nobody home, $PHASE_A_MIN min"
say "No presence faked. A run is allowed while idle is under 30 min; none after."
MARK_A="$WORK/mark-a"; : >"$MARK_A"
watch A $((WATCH_START + PHASE_A_S)) 0 "$MARK_A"
A_RUNS=$W_RUNS; A_MAX=$W_MAX_IDLE
if [ "$A_MAX" -le "$IDLE_JUDGE" ]; then
	check SKIPPED "Phase A: no run while nobody is home" "idle never passed 31 min (max $(mins "$A_MAX") min): someone was at the Mac"
elif [ "$W_BAD_IDLE" = 0 ]; then
	check PASS "Phase A: no run while nobody is home" "$A_RUNS run(s) in all, none with idle past 31 min; max idle $(mins "$A_MAX") min"
else
	check FAIL "Phase A: no run while nobody is home" "$W_BAD_IDLE run(s) with idle past 31 min"
fi
if [ "$W_JSONL" = 0 ]; then
	check PASS "Phase A: no new transcript" "no *.jsonl newer than the phase start"
else
	check FAIL "Phase A: no new transcript" "$W_JSONL *.jsonl changed (a run's, or an open Claude session's: processes seen in $W_PROCS looks)"
fi
[ "$W_SL" = 0 ] || finding "Phase A: the status line file was written $W_SL time(s); a Claude Code session was in use."

# --------------------------------------------- 4. does caffeinate -u reset idle
PHASE="caffeinate probe"
banner "4. Does caffeinate -u reset HIDIdleTime?"
idle_now() {
	set -f
	# shellcheck disable=SC2046
	set -- $(probe sample 2>/dev/null)
	set +f
	echo "${1:--}"
}
RESETS=""
t0=$(now)
b=$(idle_now)
while { [ "$b" = - ] || [ "$b" -lt 120 ]; } && [ $(( $(now) - t0 )) -lt 900 ]; do
	sleep 30
	b=$(idle_now)
done
if [ "$b" = - ] || [ "$b" -lt 120 ]; then
	check SKIPPED "caffeinate -u resets HIDIdleTime" "idle stayed under 2 min for 15 min (someone is here), so it could not be told"
	RESETS=unknown
else
	ok=0; tries=""
	for try in 1 2; do
		b=$(idle_now)
		caffeinate -u -t 5 >/dev/null 2>&1
		sleep 1
		a=$(idle_now)
		tries="$tries try $try: ${b} s before, ${a} s after;"
		if [ "$b" != - ] && [ "$a" != - ] && [ "$b" -gt 60 ] && [ "$a" -lt 15 ]; then
			ok=$((ok + 1))
		fi
		[ "$try" = 1 ] && sleep 90
	done
	if [ "$ok" = 2 ]; then
		RESETS=yes
		check PASS "caffeinate -u resets HIDIdleTime" "$tries"
		finding "caffeinate -u -t 5 resets HIDIdleTime on this macOS ($(sw_vers -productVersion 2>/dev/null)), with the screen as it was after $PHASE_A_MIN min away.$tries"
	else
		RESETS=no
		check SKIPPED "caffeinate -u resets HIDIdleTime" "it did not ($tries); phase B falls back to one direct run"
		finding "caffeinate -u -t 5 does NOT reset HIDIdleTime on this macOS ($(sw_vers -productVersion 2>/dev/null)).$tries So with nobody here the idle guard holds every run, as designed."
	fi
fi

# --------------------------------------------------------------- 5. phase B
MARK_B="$WORK/mark-b"; : >"$MARK_B"
B_START=$(now)
if [ "$RESETS" = yes ]; then
	PHASE="phase B"
	banner "5. Phase B: someone is here (caffeinate -u every minute) until $(at "$WATCH_END")"
	watch B "$WATCH_END" 1 "$MARK_B"
	B_MIN=$(( ($(now) - B_START) / 60 ))
	want=$(( B_MIN / 30 - 1 ))
	[ "$want" -ge 1 ] || want=1
	if [ "$W_RUNS" -ge "$want" ]; then
		check PASS "Phase B: runs while someone is here" "$W_RUNS run(s) in $B_MIN min (wanted at least $want)"
	else
		check FAIL "Phase B: runs while someone is here" "$W_RUNS run(s) in $B_MIN min (wanted at least $want; status line written $W_SL time(s), max idle $(mins "$W_MAX_IDLE") min)"
	fi
	if [ "$W_RUNS" -gt 0 ] && [ "$W_NOTMOVED" = 0 ]; then
		check PASS "Phase B: each run moved fetchedAtMs" "all $W_RUNS"
	elif [ "$W_RUNS" -gt 0 ]; then
		check FAIL "Phase B: each run moved fetchedAtMs" "$W_NOTMOVED of $W_RUNS did not"
	else
		check SKIPPED "Phase B: each run moved fetchedAtMs" "no run"
	fi
	if [ -z "$W_MIN_GAP" ]; then
		check SKIPPED "Phase B: runs at least 25 min apart" "fewer than two runs"
	elif [ "$W_MIN_GAP" -ge "$SPACING_SLACK" ]; then
		check PASS "Phase B: runs at least 25 min apart" "closest two $(mins "$W_MIN_GAP") min apart"
	else
		check FAIL "Phase B: runs at least 25 min apart" "two runs $(mins "$W_MIN_GAP") min apart"
	fi
	if [ "$W_FAILS" = 0 ] && [ "$W_OFF" = 0 ]; then
		check PASS "Phase B: no failed run, no shutdown" "no failed or off line in bridge.log"
	else
		check FAIL "Phase B: no failed run, no shutdown" "$W_FAILS look(s) with a failed line, $W_OFF with an off line"
	fi
	if [ "$W_JSONL" = 0 ]; then
		check PASS "Phase B: no new transcript" "no *.jsonl newer than the phase start"
	else
		check FAIL "Phase B: no new transcript" "$W_JSONL *.jsonl changed (processes seen in $W_PROCS looks)"
	fi
	[ "$W_MAX_IDLE" -lt "$IDLE_LIMIT" ] ||
		finding "Phase B: idle still reached $(mins "$W_MAX_IDLE") min despite the pulses."
	[ "$W_SL" = 0 ] || finding "Phase B: the status line file was written $W_SL time(s); runs were held off for 15 min after each."
else
	PHASE="direct run"
	banner "5. One direct run_once (the scheduler path is not exercised unattended)"
	RESULT_NOTE="The scheduler path was NOT exercised unattended: caffeinate -u could not stand in for a person ($RESETS)."
	set -f
	# shellcheck disable=SC2046
	set -- $(probe sample 2>/dev/null)
	set +f
	before=${2:--}
	t=$(now)
	if [ "$before" != - ] && [ $((t - before)) -lt 360 ]; then
		# Claude Code reuses a recent answer for 1-5 minutes (spec, measured
		# 2026-11-04), and a reused answer writes no cache.
		say "The cache is $((t - before)) s old; waiting out Claude Code's reuse window"
		sleep $((360 - (t - before)))
	fi
	MARK_D="$WORK/mark-d"; : >"$MARK_D"
	OUT=$(probe run-once "$EXE_RECORDED" 2>/dev/null) || OUT="failed - -"
	set -f
	# shellcheck disable=SC2086
	set -- $OUT
	set +f
	v=${1:-failed}; started=${2:--}; after=${3:--}
	say "run_once: verdict $v, started $( [ "$started" = - ] && echo - || at "$started"), cache now $( [ "$after" = - ] && echo none || at "$after")"
	case "$v" in
	ok)   check PASS "Direct run: zero cost" "usage_refresh.judge said ok (num_turns 0, total_cost_usd 0, no modelUsage)" ;;
	cost) check FAIL "Direct run: zero cost" "judge said cost: /usage used the model" ;;
	*)    check FAIL "Direct run: zero cost" "verdict $v (not a zero-cost success)" ;;
	esac
	if [ "$after" != - ] && [ "$started" != - ] && [ "$after" -ge $((started - 5)) ]; then
		check PASS "Direct run: fetchedAtMs moved" "to $(at "$after")"
	else
		check FAIL "Direct run: fetchedAtMs moved" "cache at $after, run started $started"
	fi
	j=$(new_jsonl "$MARK_D")
	if [ "$j" = 0 ]; then
		check PASS "Direct run: no transcript" "no *.jsonl newer than the run"
	else
		check FAIL "Direct run: no transcript" "$j *.jsonl changed"
	fi
	printf '%s|%s|%s|%s|%s|%s|%s|%s|%s\n' "direct" "$( [ "$started" = - ] && echo - || at "$started")" \
		"run_once $v" "$( [ "$after" != - ] && [ "$started" != - ] && [ "$after" -ge $((started - 5)) ] && echo yes || echo no)" \
		"-" "$j" "-" "-" "-" >>"$RUNS"
	# The direct run moved last_run_at not at all (run_once writes no state),
	# so the watch below still sees only the daemon's own runs.
	PHASE="phase A, continued"
	banner "Nobody home, continued, until $(at "$WATCH_END")"
	watch A2 "$WATCH_END" 0 "$MARK_B"
	if [ "$W_MAX_IDLE" -le "$IDLE_JUDGE" ]; then
		check SKIPPED "Nobody home, continued: no run" "idle never passed 31 min"
	elif [ "$W_BAD_IDLE" = 0 ]; then
		check PASS "Nobody home, continued: no run" "$W_RUNS run(s), none with idle past 31 min"
	else
		check FAIL "Nobody home, continued: no run" "$W_BAD_IDLE run(s) with idle past 31 min"
	fi
fi

# ------------------------------------------------------------ the end state
set -f
# shellcheck disable=SC2046
set -- $(probe sample 2>/dev/null)
set +f
if [ "${6:-}" = 1 ]; then
	check PASS "Setting still on at the end" "config.json usage-refresh true"
else
	check FAIL "Setting still on at the end" "usage-refresh is ${6:-unreadable}"
fi
if [ "${7:--}" = - ]; then
	check PASS "No cost or failing shutdown" "usage-refresh.json off_reason empty, fails ${8:-?}"
else
	check FAIL "No cost or failing shutdown" "off_reason ${7}"
fi
ROW=$("$BLINK_BIN" status </dev/null 2>/dev/null | grep '^Usage refresh' | head -1)
say "Status at the end: $(echo "${ROW:-none}" | sed 's/  */ /g')"
say "Left installed and on. To turn it off: ~/.blink/bin/blink config usage-refresh off"
PHASE="done"
