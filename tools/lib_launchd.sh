# Waiting for the installed daemon to stop, the way it actually stops.
# Sourced, not run, by tools/burn.sh and tools/dev.sh.
#
# The daemon defers a SIGTERM that lands inside a firmware write or an app
# swap until the work returns (claude_usage_bridge.no_exit_while), because
# esptool killed half way through slot 0 leaves a board that will not boot
# until it is reflashed by hand. The plist `blink install` writes gives that
# deferral ExitTimeOut = 180 s (STOP_GRACE_S in pc/cli.py, derived there from
# two 75 s app writes and a probe). burn.sh and dev.sh used to wait 5 and 10 s
# after their bootout, and their restore then ran bootstrap against a job
# launchd still held, which fails, and `kickstart -k` against the instance
# that was still exiting -- and -k kills it, mid-write, which is exactly what
# the grace exists to prevent (full review of develop/fork-picks, 2026-10-28,
# S3).
#
# So this waits as long as launchd will: the grace, plus a margin for
# launchd's own SIGKILL to land, because past ExitTimeOut launchd kills the
# job itself and the wait is over either way. After SLOW_STOP_NOTE_S it says
# what it is waiting for, as `blink update` does (pc/cli.py
# _noting_a_slow_stop): an idle daemon exits within a second of SIGTERM, and
# three silent minutes read as a hung script.
#
# The two numbers are pc/cli.py's, and tests/test_lib_launchd.py fails if
# they drift apart. BLINK_STOP_GRACE_S overrides the grace for that test,
# which would otherwise sit through three minutes of a job that never leaves.
BLINK_STOP_GRACE_S="${BLINK_STOP_GRACE_S:-180}"
BLINK_SLOW_STOP_NOTE_S=4
BLINK_STOP_MARGIN_S=10

# blink_agent_loaded AGENT: launchd still knows the job. While a booted-out
# job is inside its ExitTimeOut, `launchctl print` still answers for it, so
# this is "still exiting" as well as "running".
blink_agent_loaded() {
	launchctl print "$1" >/dev/null 2>&1
}

# blink_bootout AGENT: ask launchd to stop AGENT. Returns 0 when the stop
# is under way or there was nothing to stop, 1 when the job is plainly still
# running, with the code in BLINK_BOOTOUT_RC either way. It used to be
# `|| true`, and a refused bootout ("5: Input/output error") then looked
# exactly like a slow stop: the daemon kept running, unasked, while the
# script polled for three minutes and called it "still stopping" (review of
# fp/fix-tools, S-2).
#
# 0 is a stop and ESRCH (3, "Boot-out failed: 3: No such process") is "not
# loaded" -- an answer, not a failure (pc/cli.py _ESRCH,
# _LaunchdBackend.stop). Any other code is not decided from the code alone,
# because what launchctl returns for a stop it has started but not finished
# differs between macOS releases: on 27.0.1 a second bootout inside the
# grace gave 0 and `print` read "state = SIGTERMed" (measured 2026-10-28),
# and older releases are reported to answer "36: Operation now in progress"
# -- exactly the mid-OTA case. Treating that as "still running" would print
# no way back and leave the desk on HOST LOST once the daemon exited. So
# launchd is asked: gone, or any state but running, is a stop under way;
# only "state = running" is a refusal.
blink_bootout() {
	BLINK_BOOTOUT_RC=0
	launchctl bootout "$1" >/dev/null 2>&1 || BLINK_BOOTOUT_RC=$?
	case "$BLINK_BOOTOUT_RC" in
	0 | 3) return 0 ;;
	esac
	_bl_listing=$(launchctl print "$1" 2>/dev/null) || return 0
	if printf '%s\n' "$_bl_listing" | grep -q '^[[:space:]]*state = running$'; then
		return 1
	fi
	return 0
}

# blink_restart_hint AGENT PLIST: the two commands that put the daemon back
# by hand, on stderr, for a run that leaves it stopped.
blink_restart_hint() {
	echo "       Once it has stopped, start it again with:" >&2
	echo "         launchctl bootstrap gui/$(id -u) $2" >&2
	echo "         launchctl kickstart $1" >&2
}

# blink_wait_agent_gone AGENT: wait, in quarter seconds, for launchd to
# forget AGENT, up to the stop grace plus its margin. Says once, on stderr,
# what it is waiting for if that takes longer than an ordinary stop. Returns
# 0 when the job is gone, 1 when it is still there at the deadline.
#
# Counted in polls, not read off a clock: `date +%s` would do, but a count is
# what a test can drive with a stand-in `sleep` that returns at once.
blink_wait_agent_gone() {
	_bl_agent="$1"
	_bl_i=0
	_bl_note=$((BLINK_SLOW_STOP_NOTE_S * 4))
	_bl_max=$(((BLINK_STOP_GRACE_S + BLINK_STOP_MARGIN_S) * 4))
	while blink_agent_loaded "$_bl_agent"; do
		if [ "$_bl_i" -ge "$_bl_max" ]; then
			return 1
		fi
		if [ "$_bl_i" -eq "$_bl_note" ]; then
			echo "   waiting for the installed daemon to finish a firmware write or update before it stops (up to $((BLINK_STOP_GRACE_S / 60)) minutes)..." >&2
		fi
		_bl_i=$((_bl_i + 1))
		sleep 0.25
	done
	return 0
}
