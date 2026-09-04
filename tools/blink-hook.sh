#!/bin/sh
# Blink execution-state hook.
#
# Claude Code runs this on lifecycle events and pipes the event JSON to stdin.
# We record which event fired, for which session, and when.
#
# WHAT IS CAPTURED, exactly: an event name, a session id, an agent id, the
# PROJECT DIRECTORY NAME, a clock reading, and this hook's PARENT PROCESS ID.
# Nothing else is read from the payload -- not the prompt, not the tool
# arguments, not the transcript path, not the assistant's message, and not the
# path above the project directory.
#
# The pid is the only item on that list that does not come from the payload at
# all: it is a number about the machine, not about the work, and it stays on
# disk -- the daemon reads it to ask the kernel whether the session's process
# is still there, and it is never put on the wire or shown on the panel.
#
# The project name is the second widening of this file, and a larger one than
# the first. The ids are opaque identifiers Claude Code generates; a directory
# name is content, chosen by the user, and it is rendered on a display other
# people can see. It is captured because a status with no subject cannot say
# WHICH of three open sessions is the one waiting on you. The final segment
# only: the pattern below matches the path above it and discards it, so what
# is written is "LiveClaudeUi" and never "/Users/kfir/Projects/LiveClaudeUi".
#
# This is a real widening from the first version, which captured an event name
# and a timestamp and nothing else. That made the metadata-only promise
# structural -- there was nothing there to leak. It is now a policy instead,
# which is weaker, and the reason for accepting that is that a single global
# slot silently misreports the moment a second session exists: two terminals
# overwrite each other and the panel confidently shows the wrong one.
#
# This remains a policy rather than a structural guarantee, as the first
# widening already made it. Nothing here enforces the list above except the
# code below it.
#
# The event name arrives as $1, from the settings entry we wrote, rather than
# being parsed out of the payload. POSIX sh has no JSON parser and this runs on
# every tool call, so what parsing there is stays to one sed per invocation.
event=${1:-unknown}

# WHICH TOOL'S SESSIONS these are, from $2, also written by the installer.
#
# Codex grew a hooks interface whose event names are deliberately the same
# words Claude Code uses -- PreToolUse, PostToolUse, PermissionRequest, Stop,
# SessionEnd, SubagentStart/Stop -- and whose command hooks are handed the
# same session_id and cwd on stdin. Everything below therefore already works
# for it unchanged, and the only thing that has to differ is WHERE the slots
# are written: a Codex session counted out of ~/.blink/state would be reported
# to the board as a Claude one, on a Claude pip, against a Claude account.
#
# One shim rather than a second copy of this file, because what is valuable
# here is not the case statement below -- it is the two sanitisers, each of
# which is the fix for a bug that reached a real machine. A second copy is a
# second place for the next such fix to be forgotten, and the cost of avoiding
# it is one parameter expansion on a path that already forks sed twice.
#
# A `case` over a fixed list rather than using $2 as a path fragment: it
# arrives from a configuration file a person can hand-edit, and an
# unrecognised value has to fall back rather than name a directory.
case ${2:-} in
codex) sub=state-codex ;;
*) sub=state ;;
esac

input=$(cat)

# Extract the session id, and SANITISE IT IN THE PATTERN rather than after.
#
# This value ends up in a filename, so it is the one piece of attacker-shaped
# input on this path. The character class is the sanitiser: a value containing
# a slash, a quote, a space or a NUL simply fails to match and falls through to
# "unknown" -- there is no separate validation step that can be forgotten or
# reordered. The {0,63} bound stops a pathological payload producing a filename
# the filesystem rejects.
#
# The FIRST character must be alphanumeric. The class used to admit any of
# `._-` anywhere, which let the literal names `.` and `..` through -- and
# `$DIR/..` is ~/.blink itself, where the SubagentStart/Stop branches below
# would then truncate or delete whichever file the agent id named. Reproduced
# against the signing key before this was tightened.
#
# And the FIRST occurrence, not the last. The pattern's leading `.*` was
# greedy, so on PreToolUse/PostToolUse it matched the last "session_id" on the
# line -- which is inside tool_input whenever a tool's own arguments carry one
# (MCP tools commonly do). A tool argument then became a filename here and the
# real session's slot stopped updating. Claude Code puts the top-level id
# first, so: break the line before every occurrence and take the second line,
# which begins with the first one.
_ident() {
	sed 's/"'"$1"'"/\
&/g' |
		sed -n '2{s/^"'"$1"'"[[:space:]]*:[[:space:]]*"\([0-9A-Za-z][0-9A-Za-z._-]\{0,63\}\)".*/\1/p;}'
}
sid=$(printf '%s' "$input" | _ident session_id)
[ -n "$sid" ] || sid=unknown

# The project's DIRECTORY NAME, and only that: the path above it is matched
# and thrown away inside the pattern, so the full path is never held in a
# variable, never written, and never sent.
#
# Sanitised in the pattern for the same reason _ident is -- there is no
# separate validation step that can be forgotten or reordered. This value goes
# into a JSON string rather than a filename, so the class must also exclude the
# two characters that could end it early: `"` and `\`. A name that does not
# match produces nothing and the key is omitted, which already means unknown
# on the other side.
#
# `[^"]*[/\]` is greedy up to the LAST separator before the closing quote, so
# what the class captures is the final segment. Both separators, because on
# Windows the payload carries an escaped backslash and there is no `/` at all.
#
# The first character must be alphanumeric, for the reason spelled out above
# _ident: it is what keeps the bare names `.` and `..` from becoming a label.
#
# The `1{...q}` line is the TOP-LEVEL guard, and it is not decoration. Taking
# the first "cwd" is only the right key while a top-level one exists; with none
# in the payload the first occurrence is whatever nested one comes first, so
# `{"tool_input":{"cwd":"/tmp/Fake"}}` promoted a tool's own argument to the
# name on the display. That is worse than the session_id bug it rhymes with:
# _ident falls back to the harmless literal "unknown", while this fell back to
# attacker-chosen content that other people can see.
#
# So: line 1 is everything before the first "cwd", and if a second `{` opened
# in it we are already inside a nested object and refuse. This COUNTS braces
# rather than tracking depth, which can only ever over-count -- a `{` inside a
# string value costs us the name on that payload. It can never under-count,
# because a nested "cwd" is a key in an object and an object cannot be entered
# without a literal `{`. Conservative in the safe direction, which is the only
# direction a sanitiser is allowed to be wrong in.
#
# 24 bytes, matching the daemon's cap: a first character plus 23.
_projname() {
	sed 's/"cwd"/\
&/g' |
		sed -n -e '1{/[{].*[{]/q;}' \
			-e '2{s|^"cwd"[[:space:]]*:[[:space:]]*"[^"]*[/\]\([0-9A-Za-z][0-9A-Za-z._-]\{0,23\}\)".*|\1|p;}'
}

# Private to the user. These files name the sessions someone has open, and
# the default umask would leave them readable by every account on the machine.
umask 077
DIR="$HOME/.blink/$sub"
[ -d "$DIR" ] || mkdir -p "$DIR" 2>/dev/null

case $event in
SessionEnd)
	# The session is over: take its whole directory with it, agents and all.
	# rm -rf on a path built from the sanitised id above; a traversal cannot
	# reach here because the pattern would not have matched.
	rm -rf "${DIR:?}/$sid" 2>/dev/null
	rm -f "$DIR/$sid.state" "$DIR/$sid.waiting" 2>/dev/null
	;;
SubagentStart|SubagentStop)
	# One file per agent, named by the id Claude Code assigns. This is why
	# the count is exact and lock-free: every agent owns its own file, so
	# two agents starting at once cannot race on a shared counter, and a
	# stop removes precisely the agent that stopped rather than
	# decrementing something and hoping.
	aid=$(printf '%s' "$input" | _ident agent_id)
	[ -n "$aid" ] || aid=unknown
	[ -d "$DIR/$sid" ] || mkdir -p "$DIR/$sid" 2>/dev/null
	if [ "$event" = "SubagentStart" ]; then
		# `true`, not `:`. A redirection error on `:` -- a POSIX SPECIAL
		# built-in -- makes the shell EXIT, skipping the `exit 0` at the
		# bottom of this file and handing the tool a FAILING hook, for the
		# sake of a subagent count. Measured: status 1 under sh, 2 under
		# dash. `true` is a regular built-in and merely returns non-zero.
		#
		# And 2>/dev/null comes BEFORE the > redirect on purpose: the
		# message about a failed redirection is printed by the SHELL, not
		# by the command, so stderr has to be pointed away already by the
		# time the failing redirect is attempted.
		true 2>/dev/null > "$DIR/$sid/$aid"
	else
		rm -f "$DIR/$sid/$aid" 2>/dev/null
	fi
	;;
*)
	# WAITING LIVES IN ITS OWN FILE, not in the slot below.
	#
	# The slot is written with mv -f, so the last writer wins -- and on Codex
	# the write order is not the event order. The first interactive session
	# anyone captured fired PreToolUse and PermissionRequest in the SAME
	# SECOND, twice (docs/research/codex-hook-contract.md). Those are two
	# copies of this script running at once, and the comment further down
	# already warns that two of them can be writing one session's slot. If
	# PreToolUse renames last, the slot says the session is working while it
	# is in fact blocked on a person -- and it says so for exactly as long as
	# the person takes to answer, which is the one interval this whole
	# feature exists to display. The next event corrects it, but the next
	# event IS the answer, so the correction arrives once it is worthless.
	#
	# A marker file cannot lose that race because it shares no file with
	# anything: creating it and removing it are each atomic on their own
	# path, and neither is a read-modify-write. Nothing here has to know
	# which of two same-second processes went first.
	#
	# PreToolUse is deliberately NOT in the removal list. It is the one event
	# known to arrive in the same second as PermissionRequest, so clearing
	# the marker on it would put the race straight back. It stays registered
	# regardless -- not for the state, which UserPromptSubmit has already
	# set, but for the timestamp: it is what keeps a long tool call's slot
	# fresh, and without it a slow turn ages out of the census entirely.
	#
	# The removers are the events that mean a human has answered, or that the
	# turn has moved on past the question: PostToolUse (approved), Interrupt
	# (refused, and terminal for its turn), Stop, and UserPromptSubmit, which
	# starts a new turn and so cannot still be waiting on the last one's.
	case $event in
	PermissionRequest)
		# `true` and not `:`, which is what this line obviously wants
		# to be. `:` is a POSIX SPECIAL built-in, and a redirection
		# error on a special built-in makes the shell EXIT -- measured
		# here, status 1 under sh and 2 under dash, with the rest of
		# the script never reached. That would skip the `exit 0` at
		# the bottom and hand Claude Code a failing hook the first
		# time the state directory is not writable. `true` is an
		# ordinary built-in, so the same failure costs the marker and
		# nothing else.
		#
		# 2>/dev/null FIRST, for the reason spelled out at the slot
		# write below: redirections are applied left to right, so a
		# trailing one does not cover a failure to open the target.
		true 2>/dev/null > "$DIR/$sid.waiting"
		;;
	UserPromptSubmit | PostToolUse | Interrupt | Stop)
		rm -f "$DIR/$sid.waiting" 2>/dev/null
		;;
	esac

	# The name is read HERE and not beside the session id, because this is
	# the only branch that writes it. SessionEnd and the Subagent events were
	# paying two sed processes each to compute a value they then discarded,
	# on a path that runs many times a minute.
	name=$(printf '%s' "$input" | _projname)

	# Atomic write, same reason as the statusline shim: the daemon may read
	# at any moment and a half-written file parses as malformed. The
	# 2>/dev/null must come BEFORE the '>' target -- if opening the target
	# itself fails, the shell reports it using whatever stderr was in effect
	# when the '>' was processed, so a trailing redirect looks like it
	# suppresses the error and does not.
	#
	# The temp name carries this process's pid. Claude Code runs parallel
	# tool calls, so two of these can be writing the same session's slot at
	# once; with one shared temp name the second truncated the first's
	# half-written file and one of them renamed a torn one into place --
	# the exact malformed state the rename exists to prevent.
	#
	# The name is optional, so the fragment is built FIRST and the write
	# below stays a single path. Branching around two near-identical atomic
	# writes would leave the reasoning above attached to only one of them,
	# and the next person to fix the write would fix one copy.
	#
	# Omitted rather than written empty when nothing matched: an absent key
	# already reads as unknown on the other side, and an empty string would
	# be a second spelling of the same thing.
	#
	# Substituted rather than built with printf in a $(...): this runs on
	# every tool call and that was a fork per call for a two-field string.
	# Safe without quoting help because the class above already refused
	# every `"` and `\` that could end the JSON string early.
	nameval=''
	[ -n "$name" ] && nameval=",\"name\":\"$name\""

	# The PARENT pid -- the process Claude Code ran this hook from. It is here
	# because a session that dies without firing SessionEnd (a closed
	# terminal, a crash, kill -9) is invisible to the daemon otherwise: the
	# slot simply stops changing, and silence is the one thing a quiet session
	# and a dead one have in common. Whether that process still exists is a
	# fact the daemon can check instead of guessing from silence, which is the
	# guess this product removed on 2026-08-29 for crying wolf every way it
	# was tuned.
	#
	# $PPID and not `ps`, and not any other $(...): POSIX has the shell set
	# PPID at startup, so reading it is free, and this branch runs on every
	# tool call. The one fork already here (date) is one too many as it is.
	#
	# Omitted rather than written empty when it is not a plain positive
	# integer -- the same rule as the name, for a sharper reason. `"pid":`
	# with nothing after it is not JSON, so the daemon would fail to parse
	# the whole slot and the session would VANISH from the panel rather than
	# merely lose its liveness check. A shell that left PPID unset, or set it
	# to something surprising, must cost us this key and nothing else. 0 is
	# excluded because a pid of 0 names a process group rather than a
	# process, and it is not this hook's job to hand the reader that trap.
	pidval=''
	case "${PPID:-}" in
	'' | *[!0-9]*) ;;
	0*) ;;
	*) pidval=",\"pid\":$PPID" ;;
	esac
	printf '{"event":"%s","t":%s%s%s}' "$event" "$(date +%s)" "$pidval" \
		"$nameval" 2>/dev/null \
		> "$DIR/$sid.state.$$.tmp" &&
		mv -f "$DIR/$sid.state.$$.tmp" "$DIR/$sid.state" 2>/dev/null
	;;
esac

# Never fail a hook. A non-zero exit is a signal to Claude Code, and Blink
# having a bad day must not become the user's bad day.
exit 0
