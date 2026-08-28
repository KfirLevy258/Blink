#!/bin/sh
# Clauge execution-state hook.
#
# Claude Code runs this on lifecycle events and pipes the event JSON to stdin.
# We record which event fired, for which session, and when.
#
# WHAT IS CAPTURED, exactly: an event name, a session id, an agent id, and a
# clock reading. Nothing else is read from the payload -- not the prompt, not
# the tool arguments, not the transcript path, not the cwd, not the assistant's
# message. The two ids are opaque identifiers Claude Code generates; they are
# used as filenames so that concurrent sessions can be told apart, and for
# nothing else.
#
# This is a real widening from the first version, which captured an event name
# and a timestamp and nothing else. That made the metadata-only promise
# structural -- there was nothing there to leak. It is now a policy instead,
# which is weaker, and the reason for accepting that is that a single global
# slot silently misreports the moment a second session exists: two terminals
# overwrite each other and the panel confidently shows the wrong one.
#
# The event name arrives as $1, from the settings entry we wrote, rather than
# being parsed out of the payload. POSIX sh has no JSON parser and this runs on
# every tool call, so what parsing there is stays to one sed per invocation.
event=${1:-unknown}
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
# `$DIR/..` is ~/.clauge itself, where the SubagentStart/Stop branches below
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

# Private to the user. These files name the sessions someone has open, and
# the default umask would leave them readable by every account on the machine.
umask 077
DIR="$HOME/.clauge/state"
[ -d "$DIR" ] || mkdir -p "$DIR" 2>/dev/null

case $event in
SessionEnd)
	# The session is over: take its whole directory with it, agents and all.
	# rm -rf on a path built from the sanitised id above; a traversal cannot
	# reach here because the pattern would not have matched.
	rm -rf "${DIR:?}/$sid" 2>/dev/null
	rm -f "$DIR/$sid.state" 2>/dev/null
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
		: > "$DIR/$sid/$aid" 2>/dev/null
	else
		rm -f "$DIR/$sid/$aid" 2>/dev/null
	fi
	;;
*)
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
	printf '{"event":"%s","t":%s}' "$event" "$(date +%s)" 2>/dev/null \
		> "$DIR/$sid.state.$$.tmp" &&
		mv -f "$DIR/$sid.state.$$.tmp" "$DIR/$sid.state" 2>/dev/null
	;;
esac

# Never fail a hook. A non-zero exit is a signal to Claude Code, and Clauge
# having a bad day must not become the user's bad day.
exit 0
